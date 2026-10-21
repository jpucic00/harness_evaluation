"""Model profiles' connection settings (agenteval.models): proxy policy, trust, mTLS."""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import certifi
import openai
import pytest
import truststore
from inspect_ai._util.http_defaults_httpx2 import default_timeout
from inspect_ai.model import GenerateConfig, get_model

from agenteval import models
from agenteval.config import Connection, ModelProfile, load_profiles

REPO = Path(__file__).resolve().parents[1]
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (*models.TLS_ENV, *PROXY_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    models._reset_cache()
    yield
    models._reset_cache()


def gateway_profile(**update: Any) -> ModelProfile:
    profile = ModelProfile(
        name="gw",
        inspect_model="openai-api/gateway/qwen/qwen3.8-27b",
        model_args={"stream": False},
    )
    return profile.model_copy(update=update)


def fake_get_model(calls: list[dict[str, Any]]) -> Any:
    def get(name: str, **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(model_args=dict(kwargs))

    return get


def caused_by(exc: BaseException, kind: type[BaseException]) -> bool:
    """Whether ``kind`` is anywhere in the chain (Inspect wraps provider errors)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, kind):
            return True
        seen.add(id(current))
        last = getattr(current, "last_attempt", None)  # tenacity.RetryError
        current = (last.exception() if last else None) or current.__cause__ or current.__context__
    return False


# ------------------------------------------------------------------ build_model


def test_build_model_without_connection_keeps_inspects_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(models, "get_model", fake_get_model(calls))
    models.build_model(gateway_profile())
    assert calls == [{"stream": False}]


@pytest.mark.parametrize(("proxy", "trust_env"), [("direct", False), ("environment", True)])
def test_build_model_with_connection_passes_http_client(
    monkeypatch: pytest.MonkeyPatch, proxy: str, trust_env: bool
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(models, "get_model", fake_get_model(calls))
    model = models.build_model(gateway_profile(connection=Connection(proxy=proxy)))
    [kwargs] = calls
    assert kwargs["stream"] is False  # model_args kept
    assert kwargs["memoize"] is False  # Inspect's cache key ignores the client
    client = kwargs["http_client"]
    assert client.trust_env is trust_env
    assert client.timeout == default_timeout()  # Inspect's client defaults kept
    assert "http_client" not in model.model_args  # logs keep the plain model_args


def test_build_model_reuses_one_model_per_profile_and_tls_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(models, "get_model", fake_get_model(calls))
    profile = gateway_profile(connection=Connection(proxy="direct"))
    first = models.build_model(profile)
    assert models.build_model(profile) is first and len(calls) == 1
    bundle = tmp_path / "corp.pem"
    shutil.copy(certifi.where(), bundle)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    assert models.build_model(profile) is not first  # new trust: new client


def test_connection_rejects_unknown_proxy_mode() -> None:
    with pytest.raises(ValueError):
        Connection(proxy="sometimes")
    with pytest.raises(ValueError):
        Connection.model_validate({"proxy": "direct", "verify": False})  # extra keys forbidden


def test_gateway_profiles_connect_directly() -> None:
    profiles = load_profiles(REPO / "config").models
    gateways = [p for p in profiles.values() if p.inspect_model.startswith("openai-api/gateway/")]
    assert gateways
    for profile in gateways:
        assert profile.connection == Connection(proxy="direct"), profile.name
    assert profiles["qwen38-27b"].connection is None  # OpenRouter keeps Inspect's client


# ------------------------------------------------------------------ TLS settings


def test_tls_context_defaults_to_the_os_trust_store() -> None:
    ctx = models.tls_context()
    assert isinstance(ctx, truststore.SSLContext)  # like Inspect's default client
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert models.client_cert_source() is None


def test_tls_context_uses_ssl_cert_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = tmp_path / "corp.pem"
    shutil.copy(certifi.where(), bundle)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    ctx = models.tls_context()
    assert not isinstance(ctx, truststore.SSLContext) and ctx.get_ca_certs()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(FileNotFoundError):
        models.tls_context()


def test_client_cert_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MTLS_CERT_FILE", "/c.pem")
    assert models.client_cert_source() == "/c.pem"
    monkeypatch.setenv("MTLS_PKCS12_FILE", "/c.pfx")
    assert models.client_cert_source() == "/c.pfx"  # the bundle wins, as when loading


def test_pkcs12_without_cryptography_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.serialization", None)
    monkeypatch.setenv("MTLS_PKCS12_FILE", "/client.pfx")
    with pytest.raises(RuntimeError, match="uv sync --extra mtls"):
        models.tls_context()


# ------------------------------------------------------------------ servers


def _completion(text: str) -> bytes:
    return json.dumps(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen/qwen3.8-27b",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    ).encode()


class _Gateway(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = _completion("ok")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(server: ThreadingHTTPServer) -> Iterator[int]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gateway() -> Iterator[int]:
    yield from _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Gateway))


@pytest.fixture
def dropping_proxy() -> Iterator[int]:
    """A proxy that reads the request and closes without answering (the company proxy)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.2)
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                try:
                    conn.recv(65536)
                except OSError:
                    pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield sock.getsockname()[1]
    stop.set()
    thread.join(timeout=10)
    sock.close()


async def test_direct_connection_skips_a_dropping_proxy(
    monkeypatch: pytest.MonkeyPatch, gateway: int, dropping_proxy: int
) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", f"http://127.0.0.1:{gateway}/v1")
    monkeypatch.setenv("GATEWAY_API_KEY", "dummy")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dropping_proxy}")
    config = GenerateConfig(max_retries=0, timeout=10)

    # Inspect's default client follows HTTP_PROXY (not memoized: no stale cached model)
    proxied = get_model("openai-api/gateway/qwen/qwen3.8-27b", memoize=False, stream=False)
    with pytest.raises(Exception) as failed:  # noqa: B017 - checked below
        await proxied.generate("hi", config=config)
    assert caused_by(failed.value, openai.APIConnectionError)

    direct = models.build_model(gateway_profile(connection=Connection(proxy="direct")))
    output = await direct.generate("hi", config=config)
    assert output.completion == "ok"


# ------------------------------------------------------------------ mTLS


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    """A test CA, a server certificate for localhost and a client certificate (+ .pfx)."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl CLI")
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
             "-out", "ca.pem", "-days", "1", "-subj", "/CN=Test CA", cwd=tmp_path)  # fmt: skip
    (tmp_path / "san.ext").write_text("subjectAltName=DNS:localhost\n")
    for name, subj in (("srv", "/CN=localhost"), ("cli", "/CN=client")):
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key",
                 "-out", f"{name}.csr", "-subj", subj, cwd=tmp_path)  # fmt: skip
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-out", f"{name}.pem", "-days", "1",
                 "-extfile", "san.ext", cwd=tmp_path)  # fmt: skip
    _openssl("pkcs12", "-export", "-in", "cli.pem", "-inkey", "cli.key", "-out", "cli.pfx",
             "-passout", "pass:secret", cwd=tmp_path)  # fmt: skip
    return tmp_path


@pytest.fixture
def mtls_gateway(pki: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki / "srv.pem", pki / "srv.key")
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(pki / "ca.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    for port in _serve(server):
        monkeypatch.setenv("GATEWAY_BASE_URL", f"https://localhost:{port}/v1")
        monkeypatch.setenv("GATEWAY_API_KEY", "dummy")
        monkeypatch.setenv("SSL_CERT_FILE", str(pki / "ca.pem"))
        yield


async def test_client_certificate_reaches_an_mtls_gateway(
    monkeypatch: pytest.MonkeyPatch, pki: Path, mtls_gateway: None
) -> None:
    config = GenerateConfig(max_retries=0, timeout=10)
    profile = gateway_profile(connection=Connection(proxy="direct"))

    with pytest.raises(Exception) as refused:  # noqa: B017 - checked below
        await models.build_model(profile).generate("hi", config=config)
    assert caused_by(refused.value, openai.APIConnectionError)  # no client certificate

    monkeypatch.setenv("MTLS_CERT_FILE", str(pki / "cli.pem"))
    monkeypatch.setenv("MTLS_KEY_FILE", str(pki / "cli.key"))
    output = await models.build_model(profile).generate("hi", config=config)
    assert output.completion == "ok"


async def test_pkcs12_bundle_reaches_an_mtls_gateway_without_a_key_on_disk(
    monkeypatch: pytest.MonkeyPatch, pki: Path, mtls_gateway: None
) -> None:
    pytest.importorskip("cryptography")
    monkeypatch.setenv("MTLS_PKCS12_FILE", str(pki / "cli.pfx"))
    monkeypatch.setenv("MTLS_PKCS12_PASSWORD", "secret")
    profile = gateway_profile(connection=Connection(proxy="direct"))
    output = await models.build_model(profile).generate(
        "hi", config=GenerateConfig(max_retries=0, timeout=10)
    )
    assert output.completion == "ok"
    leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("agenteval-mtls-")]
    assert leftovers == []  # the decrypted key was on disk only while it was loaded


# --------------------------------------------------------------------------- subscription


def test_subscription_profile_fields() -> None:
    profile = load_profiles(REPO / "config").models["claude-sonnet-subscription"]
    assert profile.access == "subscription" and not profile.inspect_model
    assert profile.agent_models["claude_code"] == "claude-sonnet-5-5"
    assert profile.required_env == ["CLAUDE_CODE_OAUTH_TOKEN"]
    with pytest.raises(ValueError, match="inspect_model is required"):
        ModelProfile(name="m")
    with pytest.raises(ValueError, match="takes no inspect_model, pricing"):
        ModelProfile(
            name="m",
            access="subscription",
            inspect_model="anthropic/claude-sonnet-5-5",
            pricing={"input": 1, "output": 1},
            agent_models={"claude_code": "sonnet"},
        )
    with pytest.raises(ValueError, match="agent_models.claude_code"):
        ModelProfile(name="m", access="subscription")


@pytest.mark.parametrize("field", ["models", "subagent_model"])
def test_eval_runs_refuse_subscription(field: str) -> None:
    from agenteval.config import ConfigError, RunConfig, expand_matrix

    profiles = load_profiles(REPO / "config")
    run = RunConfig(
        name="r",
        models=["claude-sonnet-subscription"] if field == "models" else ["mock"],
        harnesses=["claude-code-subagents-none"],
        environments=["docker-ubuntu"],
        suites=["capability"],
        subagent_model="claude-sonnet-subscription" if field == "subagent_model" else None,
    )
    with pytest.raises(ConfigError, match="only in `agenteval dev`"):
        expand_matrix(run, profiles)
