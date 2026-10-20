"""The reasoning relay for OpenCode in agenteval dev (sandbox_scripts/think_shim.py)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from inspect_ai.model import ChatMessageAssistant, ContentReasoning, ContentText
from inspect_ai.model._model_output import ChatCompletionChoice
from inspect_ai.model._openai import messages_from_openai, openai_chat_choices
from inspect_ai.model._openrouter_reasoning import openrouter_reasoning_details_to_reasoning

from agenteval.sandbox_scripts import think_shim

DETAILS = [
    {
        "type": "reasoning.text",
        "text": 'The user asked <x> & "y"\n',
        "format": "unknown",
        "index": 0,
    }
]


def bridge_completion(reasoning: ContentReasoning | None, text: str) -> dict[str, Any]:
    """What the bridge returns for chat completions (Inspect's own serializer)."""
    content: list[ContentReasoning | ContentText] = [ContentText(text=text)]
    if reasoning is not None:
        content.insert(0, reasoning)
    choice = ChatCompletionChoice(message=ChatMessageAssistant(content=content), stop_reason="stop")
    choices = openai_chat_choices([choice])
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 1,
        "model": "openrouter/qwen/qwen3.8-27b",
        "choices": [c.model_dump(exclude_none=True) for c in choices],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


def test_openrouter_reasoning_round_trips() -> None:
    reasoning = openrouter_reasoning_details_to_reasoning(DETAILS)
    completion = bridge_completion(reasoning, "`/workspace` holds a repo.")
    assert "<think" in completion["choices"][0]["message"]["content"]  # the problem

    assert think_shim.transform_completion(completion)
    message = completion["choices"][0]["message"]
    assert message["content"] == "`/workspace` holds a repo."
    assert message["reasoning_details"] == DETAILS
    assert message["reasoning"] == DETAILS[0]["text"].strip("\n")

    # OpenCode sends the details back with its history; the bridge rebuilds the same
    # reasoning, so the model sees what it saw before
    history: Any = [{"role": "assistant", "content": message["content"],
                     "reasoning_details": message["reasoning_details"]}]  # fmt: skip
    import anyio

    parsed = anyio.run(messages_from_openai, history)
    restored = [c for c in parsed[0].content if isinstance(c, ContentReasoning)]
    assert len(restored) == 1 and restored[0].signature == reasoning.signature


def test_plain_think_tag_gets_text_details() -> None:
    completion = bridge_completion(ContentReasoning(reasoning="step 1\nstep 2"), "done")
    assert think_shim.transform_completion(completion)
    message = completion["choices"][0]["message"]
    assert message["content"] == "done"
    assert message["reasoning"] == "step 1\nstep 2"
    assert message["reasoning_details"][0]["type"] == "reasoning.text"


def test_without_reasoning_nothing_changes() -> None:
    completion = bridge_completion(None, "just text")
    before = json.dumps(completion)
    assert not think_shim.transform_completion(completion)
    assert json.dumps(completion) == before


@pytest.mark.parametrize(
    ("content", "rest", "text"),
    [
        ("<think>\na <think>b</think> c\n</think>\nanswer", "answer", "a <think>b</think> c"),
        ("<think>\n<summary>short</summary>\nlong\n</think>\nok", "ok", "long"),
        ("no tag at all", "no tag at all", None),
        ("<think>never closed", "<think>never closed", None),
    ],
)
def test_split_reasoning(content: str, rest: str, text: str | None) -> None:
    got_rest, got_text, _ = think_shim.split_reasoning(content)
    assert (got_rest, got_text) == (rest, text)


def test_redacted_without_details_stays_as_is() -> None:
    content = '<think redacted="true" signature="opaque">\nxx\n</think>\nok'
    assert think_shim.split_reasoning(content) == (content, None, None)


def test_sse_events_put_reasoning_first() -> None:
    completion = bridge_completion(openrouter_reasoning_details_to_reasoning(DETAILS), "x" * 100)
    completion["choices"][0]["message"]["tool_calls"] = [
        {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    ]
    think_shim.transform_completion(completion)
    events = think_shim.sse_events(completion, include_usage=True)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert deltas[0]["role"] == "assistant" and deltas[0]["reasoning_details"] == DETAILS
    text = "".join(d.get("content", "") for d in deltas)
    assert text == "x" * 100 and all("reasoning" not in d for d in deltas[1:])
    assert deltas[-2]["tool_calls"][0]["index"] == 0
    assert chunks[-2]["choices"][0]["finish_reason"] in ("stop", "tool_calls")
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 7
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


# --------------------------------------------------------------------- live relay


class FakeBridge(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    completion: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append({"path": self.path, "body": body})
        if self.path == "/v1/fail/chat/completions":
            self._send(500, b'{"error": "upstream broke"}')
        else:
            self._send(200, json.dumps(self.completion).encode())

    def do_GET(self) -> None:
        self._send(200, b'{"ok": true}')

    def _send(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def relay() -> Iterator[int]:
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
    FakeBridge.seen = []
    FakeBridge.completion = bridge_completion(
        openrouter_reasoning_details_to_reasoning(DETAILS), "the answer"
    )
    shim = think_shim.serve(0, bridge.server_address[1])
    for server in (bridge, shim):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield shim.server_address[1]
    shim.shutdown()
    bridge.shutdown()


def request(port: int, method: str, path: str, body: Any = None) -> tuple[int, str, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    result = response.status, response.getheader("Content-Type") or "", response.read().decode()
    conn.close()
    return result


def test_relay_streams_reasoning_to_the_client(relay: int) -> None:
    body = {"model": "qwen", "messages": [], "stream": True, "stream_options": {"include_usage": 1}}
    status, kind, text = request(relay, "POST", "/v1/chat/completions", body)
    assert status == 200 and kind == "text/event-stream"
    sent = FakeBridge.seen[-1]["body"]
    assert sent["stream"] is False and "stream_options" not in sent  # asks for a whole reply
    events = [line[6:] for line in text.split("\n\n") if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    first = json.loads(events[0])["choices"][0]["delta"]
    assert first["reasoning_details"] == DETAILS
    content = "".join(
        json.loads(e)["choices"][0]["delta"].get("content", "")
        for e in events[:-1]
        if json.loads(e)["choices"]
    )
    assert content == "the answer" and "<think" not in text


def test_relay_non_stream_and_passthrough(relay: int) -> None:
    status, _, text = request(relay, "POST", "/v1/chat/completions", {"messages": []})
    message = json.loads(text)["choices"][0]["message"]
    assert status == 200 and message["content"] == "the answer" and message["reasoning_details"]
    assert request(relay, "GET", "/v1/models") == (200, "application/json", '{"ok": true}')
    status, _, text = request(relay, "POST", "/v1/fail/chat/completions", {"stream": True})
    assert status == 500 and "upstream broke" in text


def test_daemon_exits_quietly_when_already_running(relay: int) -> None:
    assert think_shim.main(["--listen", str(relay), "--upstream", "1", "--daemon"]) == 0
