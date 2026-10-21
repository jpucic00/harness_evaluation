"""The allow-listing HTTPS proxy for Claude subscription dev sessions (egress_proxy.py)."""

from __future__ import annotations

import io
import socket
import threading
from collections.abc import Iterator

import pytest

from agenteval.sandbox_scripts import egress_proxy


@pytest.mark.parametrize(
    ("head", "parsed"),
    [
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n", ("CONNECT", "api.anthropic.com", 443)),
        (b"CONNECT API.Anthropic.com.:443 HTTP/1.1\r\n\r\n", ("CONNECT", "api.anthropic.com", 443)),
        (b"CONNECT api.anthropic.com HTTP/1.1\r\n\r\n", ("CONNECT", "", 0)),
        (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", ("CONNECT", "", 0)),
        (b"GET http://example.com/ HTTP/1.1\r\n\r\n", ("GET", "", 0)),
        (b"\x16\x03\x01 tls hello", None),
        (b"\xff\xfe", None),
    ],
)
def test_parse_connect(head: bytes, parsed: tuple[str, str, int] | None) -> None:
    assert egress_proxy.parse_connect(head) == parsed


def test_decide() -> None:
    allowed = frozenset({"api.anthropic.com"})
    assert egress_proxy.decide("CONNECT", "api.anthropic.com", 443, allowed) == 200
    assert egress_proxy.decide("CONNECT", "api.anthropic.com", 80, allowed) == 403
    assert egress_proxy.decide("CONNECT", "evil.example", 443, allowed) == 403
    assert egress_proxy.decide("CONNECT", "api.anthropic.com.evil.example", 443, allowed) == 403
    assert egress_proxy.decide("GET", "", 0, allowed) == 405


def test_parse_resolve() -> None:
    pins = egress_proxy.parse_resolve(["API.anthropic.com=192.0.2.1,192.0.2.2"])
    assert pins == {"api.anthropic.com": ("192.0.2.1", "192.0.2.2")}
    for bad in ("api.anthropic.com", "=192.0.2.1", "api.anthropic.com="):
        with pytest.raises(ValueError):
            egress_proxy.parse_resolve([bad])


def test_connect_tries_pins_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[tuple[str, int]] = []

    def fake(address: tuple[str, int], timeout: float) -> socket.socket:
        tried.append(address)
        if address[0] != "192.0.2.2":
            raise OSError("unreachable")
        return socket.socket()

    monkeypatch.setattr(socket, "create_connection", fake)
    pins = {"h": ("192.0.2.1", "192.0.2.2", "192.0.2.3")}
    egress_proxy.connect("h", 443, pins).close()
    assert tried == [("192.0.2.1", 443), ("192.0.2.2", 443)]
    with pytest.raises(OSError, match="unreachable"):
        egress_proxy.connect("other", 443, pins)
    assert tried[-1] == ("other", 443)


@pytest.fixture
def echo_port() -> Iterator[int]:
    server = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch, echo_port: int) -> Iterator[tuple[int, io.StringIO]]:
    monkeypatch.setattr(egress_proxy, "PORT", echo_port)  # the "443" of this test
    log = io.StringIO()
    # "pinned.test" has no DNS entry: only its pin reaches the echo server
    pins = {"pinned.test": ("127.0.0.1",)}
    server = egress_proxy.serve(0, frozenset({"127.0.0.1", "pinned.test"}), log, pins)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], log
    server.shutdown()
    server.server_close()


def request(port: int, head: bytes, then: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head)
        reply = b""
        while b"\r\n\r\n" not in reply:
            data = sock.recv(4096)
            if not data:
                break
            reply += data
        if then and reply.startswith(b"HTTP/1.1 200"):
            sock.sendall(then)
            sock.shutdown(socket.SHUT_WR)
            while data := sock.recv(4096):
                reply += data
        return reply


def test_tunnels_to_allowed_host(proxy: tuple[int, io.StringIO], echo_port: int) -> None:
    port, log = proxy
    reply = request(port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode(), b"ping")
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\nping"
    assert f" 200 127.0.0.1:{echo_port}" in log.getvalue()


def test_pinned_addresses(proxy: tuple[int, io.StringIO], echo_port: int) -> None:
    port, _ = proxy
    reply = request(port, f"CONNECT pinned.test:{echo_port} HTTP/1.1\r\n\r\n".encode(), b"pin")
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\npin"


def test_bytes_sent_with_the_head_go_upstream(
    proxy: tuple[int, io.StringIO], echo_port: int
) -> None:
    port, _ = proxy
    head = f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\nearly".encode()
    reply = request(port, head, b"-late")
    assert reply.endswith(b"early-late")


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"CONNECT localhost:{port} HTTP/1.1\r\n\r\n", b"403"),
        (b"CONNECT 127.0.0.1:22 HTTP/1.1\r\n\r\n", b"403"),
        (b"GET http://127.0.0.1:{port}/ HTTP/1.1\r\n\r\n", b"405"),
        (b"hello\r\n\r\n", b"400"),
    ],
)
def test_refuses_everything_else(
    proxy: tuple[int, io.StringIO], echo_port: int, head: bytes, status: bytes
) -> None:
    port, log = proxy
    reply = request(port, head.replace(b"{port}", str(echo_port).encode()), b"ping")
    assert reply.startswith(b"HTTP/1.1 " + status)
    assert b"ping" not in reply
    assert f" {status.decode()} " in log.getvalue()
