"""Allow-listing HTTPS proxy for ``agenteval dev`` sessions on a Claude subscription.

The agent user's egress stays denied by the environment. Claude Code reaches Anthropic
through this proxy (``HTTPS_PROXY``), which runs as another user the egress rule does not
cover and opens tunnels only to the allowed hosts on port 443. It answers ``CONNECT``
only: TLS runs end to end between Claude Code and Anthropic, so the proxy never sees the
token or the traffic. Anything else gets 403 (other hosts or ports) or 405 (plain HTTP).
Each decision is logged as one line.

``--resolve HOST=IP[,IP...]`` pins the addresses of an allowed host, so the proxy needs no
DNS (on Tart the environment's resolver may not forward lookups off the box); hosts
without pins are looked up normally.

Usage: egress_proxy.py --listen PORT --allow HOST [--allow HOST ...]
       [--resolve HOST=IP[,IP...] ...] [--log FILE] [--daemon]
(stdlib only; run with ``python3 -I`` as a user other than the agent).
"""

from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
import threading
import time
from socketserver import BaseRequestHandler, ThreadingTCPServer
from typing import TextIO

PORT = 443
MAX_HEAD = 8192
CONNECT_TIMEOUT = 30.0
CHUNK = 65536


def parse_connect(head: bytes) -> tuple[str, str, int] | None:
    """``(method, host, port)`` of a request head; ``None`` when it is not HTTP."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    method, target = parts[0], parts[1]
    if method != "CONNECT":
        return method, "", 0
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or not host or "[" in host:
        return method, "", 0
    return method, host.lower().rstrip("."), int(port)


def decide(method: str, host: str, port: int, allowed: frozenset[str]) -> int:
    """HTTP status for a request: 200 opens the tunnel."""
    if method != "CONNECT":
        return 405
    if host in allowed and port == PORT:
        return 200
    return 403


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(CHUNK):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def parse_resolve(values: list[str]) -> dict[str, tuple[str, ...]]:
    pins: dict[str, tuple[str, ...]] = {}
    for value in values:
        host, sep, addresses = value.partition("=")
        ips = tuple(a for a in addresses.split(",") if a)
        if not sep or not host or not ips:
            raise ValueError(f"--resolve {value!r}: expected HOST=IP[,IP...]")
        pins[host.lower().rstrip(".")] = ips
    return pins


def connect(host: str, port: int, pins: dict[str, tuple[str, ...]]) -> socket.socket:
    """A connection to the first reachable pinned address of ``host`` (else ``host``)."""
    error: OSError | None = None
    for address in pins.get(host) or (host,):
        try:
            return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT)
        except OSError as ex:
            error = ex
    raise error or OSError(f"no address for {host}")


class Tunnel(BaseRequestHandler):
    allowed: frozenset[str] = frozenset()
    pins: dict[str, tuple[str, ...]] = {}
    log: TextIO = sys.stderr
    lock = threading.Lock()

    def _log(self, status: int, target: str, note: str = "") -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.lock:
            print(f"{stamp} {status} {target}{' ' + note if note else ''}", file=self.log)
            self.log.flush()

    def _reply(self, status: int, reason: str, body: str = "") -> None:
        text = f"agenteval egress proxy: {body}\n" if body else ""
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(text)}\r\n"
        self.request.sendall(f"{head}Connection: close\r\n\r\n{text}".encode())

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(CONNECT_TIMEOUT)
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < MAX_HEAD:
                data = client.recv(MAX_HEAD)
                if not data:
                    return
                head += data
        except OSError:
            return
        parsed = parse_connect(head)
        if parsed is None:
            self._log(400, "-", "not an HTTP request")
            return self._reply(400, "Bad Request")
        method, host, port = parsed
        target = f"{host}:{port}" if host else method
        status = decide(method, host, port, self.allowed)
        if status == 405:
            self._log(405, target, "only CONNECT is proxied")
            return self._reply(405, "Method Not Allowed", "only HTTPS (CONNECT) is proxied")
        if status == 403:
            self._log(403, target)
            return self._reply(403, "Forbidden", f"{target} is not on the allow list")
        try:
            upstream = connect(host, port, self.pins)
        except OSError as ex:
            self._log(502, target, str(ex))
            return self._reply(502, "Bad Gateway", f"cannot reach {target}")
        self._log(200, target)
        # bytes after the head (a client that did not wait for the 200) go upstream
        rest = head.split(b"\r\n\r\n", 1)[1]
        with upstream:
            client.settimeout(None)
            upstream.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
            back.start()
            _pipe(client, upstream)
            back.join()


class Server(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(
    listen: int,
    allowed: frozenset[str],
    log: TextIO,
    pins: dict[str, tuple[str, ...]] | None = None,
) -> Server:
    fields = {"allowed": allowed, "log": log, "pins": pins or {}}
    handler = type("BoundTunnel", (Tunnel,), fields)
    return Server(("127.0.0.1", listen), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Allow-listing HTTPS proxy for agenteval dev")
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--allow", action="append", required=True, metavar="HOST")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST=IP[,IP...]")
    parser.add_argument("--log", help="append one line per request here (default: stderr)")
    parser.add_argument("--daemon", action="store_true", help="detach once listening")
    args = parser.parse_args(argv)
    allowed = frozenset(h.lower().rstrip(".") for h in args.allow)
    try:
        pins = parse_resolve(args.resolve)
    except ValueError as ex:
        parser.error(str(ex))
    log: TextIO = open(args.log, "a", encoding="utf-8") if args.log else sys.stderr  # noqa: SIM115
    try:
        server = serve(args.listen, allowed, log, pins)
    except OSError as ex:
        if args.daemon and ex.errno == errno.EADDRINUSE:  # already running (resumed session)
            return 0
        print(f"egress_proxy: cannot listen on {args.listen}: {ex}", file=sys.stderr)
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
