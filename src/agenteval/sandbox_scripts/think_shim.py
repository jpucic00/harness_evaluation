"""Reasoning relay between OpenCode and Inspect's model bridge (``agenteval dev`` only).

The bridge returns a model's reasoning as ``<think signature="...">...</think>`` text in
chat-completions content, and OpenCode shows that markup verbatim. OpenCode's OpenRouter
client renders reasoning as its collapsible "Thinking" block only when it arrives in the
``reasoning`` / ``reasoning_details`` fields, as from OpenRouter itself.

This relay listens on 127.0.0.1 and forwards everything to the bridge. For
``POST .../chat/completions`` it asks the bridge for a complete (non-streamed) response,
moves the ``<think>`` block into ``reasoning`` and ``reasoning_details`` (the bridge's
signature carries OpenRouter's original details), and streams the result back the way
the bridge would. OpenCode then sends ``reasoning_details`` back with its history, which
the bridge already understands, so the model sees the same history as before. Responses
without a ``<think>`` block, other paths and errors pass through unchanged.

Usage: think_shim.py --listen PORT --upstream PORT [--daemon]
(stdlib only; run with ``python3 -I`` as the agent user, outside the inner sandbox).
"""

from __future__ import annotations

import argparse
import errno
import html
import http.client
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

UPSTREAM_TIMEOUT = 3600.0
"""Model calls can take minutes; the bridge itself has no shorter limit."""
DETAILS_PREFIX = "reasoning-details://"
"""Inspect's signature prefix for OpenRouter reasoning details (``_openrouter_reasoning``)."""
CHUNK = 48
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}
_TAG = re.compile(r"<think([^>]*)>|</think>")


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'{name}="([^"]*)"', attrs)
    return html.unescape(match.group(1)) if match else None


def _first_block(content: str) -> tuple[int, int, str, str] | None:
    """``(start, end, attrs, body)`` of the first ``<think>`` block (nesting-aware)."""
    start: int | None = None
    attrs = ""
    body_start = 0
    depth = 0
    for match in _TAG.finditer(content):
        if match.group(0).startswith("<think"):
            if start is None:
                start, attrs, body_start = match.start(), match.group(1), match.end()
            depth += 1
        elif start is not None:
            depth -= 1
            if depth == 0:
                return start, match.end(), attrs, content[body_start : match.start()]
    return None


def split_reasoning(content: str) -> tuple[str, str | None, list[Any] | None]:
    """``(content without the block, reasoning text, reasoning_details)``.

    ``reasoning_details`` come from the bridge's signature when it carries OpenRouter's
    originals, else a single ``reasoning.text`` entry is made from the text.
    """
    block = _first_block(content)
    if block is None:
        return content, None, None
    start, end, attrs, body = block
    text = body.strip("\n")
    summary = re.match(r"\s*<summary>(.*?)</summary>\n?", text, re.DOTALL)
    if summary:
        text = text[summary.end() :]
    details: list[Any] | None = None
    signature = _attr(attrs, "signature")
    if signature and signature.startswith(DETAILS_PREFIX):
        try:
            parsed = json.loads(signature[len(DETAILS_PREFIX) :])
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and parsed:
            details = parsed
    if details is None:
        if _attr(attrs, "redacted") == "true":
            # opaque provider payload: nothing readable to show, keep it as it was
            return content, None, None
        details = [{"type": "reasoning.text", "text": text, "format": "unknown", "index": 0}]
    rest = (content[:start] + content[end:]).strip()
    return rest, text, details


def transform_completion(completion: dict[str, Any]) -> bool:
    """Move ``<think>`` blocks of every choice into reasoning fields; True if any moved."""
    changed = False
    for choice in completion.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            continue
        rest, text, details = split_reasoning(message["content"])
        if details is None:
            continue
        message["content"] = rest
        message["reasoning"] = text
        message["reasoning_details"] = details
        changed = True
    return changed


def sse_events(completion: dict[str, Any], include_usage: bool) -> list[dict[str, Any] | str]:
    """A complete completion as ``chat.completion.chunk`` events (reasoning first)."""
    base = {
        "id": completion.get("id", "chatcmpl-agenteval"),
        "object": "chat.completion.chunk",
        "created": completion.get("created", int(time.time())),
        "model": completion.get("model", ""),
    }
    events: list[dict[str, Any] | str] = []

    def emit(index: int, delta: dict[str, Any], finish: str | None = None) -> None:
        choice = {"index": index, "delta": delta, "finish_reason": finish}
        events.append({**base, "choices": [choice]})

    for choice in completion.get("choices") or []:
        index = choice.get("index", 0)
        message = choice.get("message") or {}
        first: dict[str, Any] = {"role": "assistant"}
        if message.get("reasoning_details"):
            first["reasoning"] = message.get("reasoning") or ""
            first["reasoning_details"] = message["reasoning_details"]
        emit(index, first)
        content = message.get("content") or ""
        for i in range(0, len(content), CHUNK):
            emit(index, {"content": content[i : i + CHUNK]})
        for position, call in enumerate(message.get("tool_calls") or []):
            emit(index, {"tool_calls": [{"index": position, **call}]})
        emit(index, {}, choice.get("finish_reason") or "stop")
    if include_usage and completion.get("usage") is not None:
        events.append({**base, "choices": [], "usage": completion["usage"]})
    events.append("[DONE]")
    return events


class Relay(BaseHTTPRequestHandler):
    upstream_port = 0
    protocol_version = "HTTP/1.0"  # one response per connection; no chunked encoding

    def log_message(self, format: str, *args: Any) -> None:  # quiet: the TUI owns the terminal
        return

    def _forward(self, body: bytes | None) -> tuple[int, list[tuple[str, str]], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=UPSTREAM_TIMEOUT)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            conn.close()

    def _reply(self, status: int, headers: list[tuple[str, str]], data: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            if name.lower() not in HOP_HEADERS:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _chat(self, raw: bytes) -> None:
        try:
            request = json.loads(raw)
        except ValueError:
            request = None
        if not isinstance(request, dict):
            self._reply(*self._forward(raw))
            return
        stream = bool(request.get("stream"))
        options = request.pop("stream_options", None)
        include_usage = isinstance(options, dict) and bool(options.get("include_usage"))
        request["stream"] = False
        status, headers, data = self._forward(json.dumps(request).encode())
        try:
            completion = json.loads(data) if status == 200 else None
        except ValueError:
            completion = None
        if not isinstance(completion, dict):
            self._reply(status, headers, data)
            return
        transform_completion(completion)
        if not stream:
            data = json.dumps(completion).encode()
            self._reply(200, [("Content-Type", "application/json")], data)
            return
        lines = []
        for event in sse_events(completion, include_usage):
            payload = event if isinstance(event, str) else json.dumps(event)
            lines.append(f"data: {payload}\n\n")
        self._reply(200, [("Content-Type", "text/event-stream")], "".join(lines).encode())

    def do_POST(self) -> None:
        body = self._body()
        if self.path.split("?", 1)[0].endswith("/chat/completions") and body:
            self._chat(body)
        else:
            self._reply(*self._forward(body))

    def do_GET(self) -> None:
        self._reply(*self._forward(None))


def serve(listen: int, upstream: int) -> ThreadingHTTPServer:
    handler = type("BoundRelay", (Relay,), {"upstream_port": upstream})
    server = ThreadingHTTPServer(("127.0.0.1", listen), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reasoning relay for agenteval dev")
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--upstream", type=int, required=True)
    parser.add_argument("--daemon", action="store_true", help="detach once listening")
    args = parser.parse_args(argv)
    try:
        server = serve(args.listen, args.upstream)
    except OSError as ex:
        if args.daemon and ex.errno == errno.EADDRINUSE:  # already running (resumed session)
            return 0
        print(f"think_shim: cannot listen on {args.listen}: {ex}", file=sys.stderr)
        return 1
    if args.daemon:
        if os.fork():
            return 0  # the port is bound already: callers can connect at once
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
