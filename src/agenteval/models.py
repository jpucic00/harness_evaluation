"""Inspect ``Model`` instances for model profiles, with the profile's connection settings.

A profile with ``connection`` gets its own HTTP client instead of Inspect's default. It is
built from Inspect's own client defaults (timeouts, keepalive, connect retry), with:

- ``proxy: direct``: HTTP(S)_PROXY and the OS proxy settings are ignored
  (``trust_env=False``). The company gateway is reached directly over the VPN and drops
  proxied calls with "Server disconnected without sending a response".
- The trust Inspect's default client uses: ``SSL_CERT_FILE``, else ``SSL_CERT_DIR``, else
  the OS trust store (macOS Keychain, Windows store, Linux system store) via truststore.
  It is passed explicitly, because ``trust_env=False`` stops httpx from reading it.
- A client certificate (mTLS) from ``MTLS_CERT_FILE`` / ``MTLS_KEY_FILE`` /
  ``MTLS_KEY_PASSWORD``, or from a ``.p12``/``.pfx`` bundle in ``MTLS_PKCS12_FILE`` /
  ``MTLS_PKCS12_PASSWORD`` (needs the ``mtls`` extra: ``uv sync --extra mtls``).

These are the variable names the agentic-test-automation project uses for the same gateway.
"""

from __future__ import annotations

import os
import ssl
import tempfile

import httpx2
from inspect_ai._util.http_defaults_httpx2 import default_client_kwargs
from inspect_ai.model import Model, get_model
from openai import DefaultAsyncHttpxClient

from agenteval.config import Connection, ModelProfile

TLS_ENV = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "MTLS_CERT_FILE",
    "MTLS_KEY_FILE",
    "MTLS_KEY_PASSWORD",
    "MTLS_PKCS12_FILE",
    "MTLS_PKCS12_PASSWORD",
)

_models: dict[tuple[str, tuple[str | None, ...]], Model] = {}
_pkcs12_pem: dict[tuple[str, str | None], bytes] = {}


def build_model(profile: ModelProfile) -> Model:
    """``get_model`` for a profile: its ``model_args`` plus its connection settings."""
    if profile.connection is None:
        return get_model(profile.inspect_model, **profile.model_args)
    # One model (and HTTP client) per profile and TLS settings. Inspect's own cache key
    # drops non-JSON arguments such as the client, so it is bypassed (memoize=False).
    key = (profile.model_dump_json(), tuple(os.environ.get(v) for v in TLS_ENV))
    if key not in _models:
        model = get_model(
            profile.inspect_model,
            memoize=False,
            http_client=http_client(profile.connection),
            **profile.model_args,
        )
        # the provider kept its own copy; keep the logged model_args as before
        model.model_args.pop("http_client", None)
        _models[key] = model
    return _models[key]


def http_client(connection: Connection) -> DefaultAsyncHttpxClient:
    """The OpenAI SDK's async client (the httpx flavour Inspect's providers expect)."""
    return DefaultAsyncHttpxClient(
        **default_client_kwargs(trust_env=connection.proxy == "environment", verify=tls_context())
    )


def tls_context(*, client_certificate: bool = True) -> ssl.SSLContext:
    """Inspect's default trust, plus the ``MTLS_*`` client certificate if asked for."""
    ctx = httpx2.create_ssl_context(trust_env=True)  # SSL_CERT_FILE / _DIR / truststore
    if client_certificate:
        _load_client_cert(ctx)
    return ctx


def client_cert_source() -> str | None:
    """The configured client certificate file (``.p12``/``.pfx`` or PEM), if any."""
    return os.environ.get("MTLS_PKCS12_FILE") or os.environ.get("MTLS_CERT_FILE") or None


def _load_client_cert(ctx: ssl.SSLContext) -> None:
    p12 = os.environ.get("MTLS_PKCS12_FILE")
    if p12:
        pem = _pkcs12_to_pem(p12, os.environ.get("MTLS_PKCS12_PASSWORD") or None)
        # load_cert_chain only reads files: the decrypted key touches disk (0600) just
        # for this call
        fd, path = tempfile.mkstemp(prefix="agenteval-mtls-", suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            ctx.load_cert_chain(path)
        finally:
            os.remove(path)
        return
    cert = os.environ.get("MTLS_CERT_FILE")
    if cert:
        key = os.environ.get("MTLS_KEY_FILE") or None
        ctx.load_cert_chain(cert, key, os.environ.get("MTLS_KEY_PASSWORD") or None)


def _pkcs12_to_pem(path: str, password: str | None) -> bytes:
    """Certificate chain and unencrypted key of a PKCS#12 bundle as PEM (kept in memory)."""
    if (path, password) in _pkcs12_pem:
        return _pkcs12_pem[(path, password)]
    try:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            pkcs12,
        )
    except ImportError as e:
        raise RuntimeError(
            "MTLS_PKCS12_FILE needs the cryptography package: run `uv sync --extra mtls`"
        ) from e
    with open(path, "rb") as f:
        key, cert, extra = pkcs12.load_key_and_certificates(
            f.read(), password.encode() if password else None
        )
    if key is None or cert is None:
        raise RuntimeError(f"{path} holds no private key and certificate")
    pem: bytes = cert.public_bytes(Encoding.PEM)
    pem += b"".join(c.public_bytes(Encoding.PEM) for c in extra or [])
    pem += key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    _pkcs12_pem[(path, password)] = pem
    return pem


def _reset_cache() -> None:
    """For tests: forget built models and decoded bundles."""
    _models.clear()
    _pkcs12_pem.clear()
