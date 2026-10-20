"""Call a model profile once, outside any harness or sandbox, and report what happened.

Separates "the provider is slow, unreachable or rejecting requests" from problems in
the harness, bridge or environment: if this answers quickly, the model side works.
When the profile has a *_BASE_URL variable, the network path to it is checked step by
step first (proxies, DNS, TCP, TLS), so a connection error says which step fails.

Usage, from the checkout root:

    uv run python scripts/check_model.py [PROFILE] [--timeout SECONDS] [--direct]

PROFILE is a model profile name from config/models/ (default: gateway-qwen). The
checkout's .env is loaded; shell variables win. Keys and proxy values are never printed.

--direct connects the way the agentic-test-automation project reaches the company
gateway: proxies from the environment or system settings are ignored (the gateway drops
proxied calls with "Server disconnected without sending a response"), SSL_CERT_FILE is
the CA bundle, and a client certificate comes from MTLS_CERT_FILE / MTLS_KEY_FILE /
MTLS_KEY_PASSWORD or MTLS_PKCS12_FILE / MTLS_PKCS12_PASSWORD. A .p12/.pfx bundle needs
the cryptography package: `uv run --with cryptography python scripts/check_model.py ...`.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import certifi
from dotenv import load_dotenv
from inspect_ai.model import GenerateConfig, get_model
from openai import DefaultAsyncHttpxClient

from agenteval.config import ConfigError, load_profiles, missing_env

CHECKOUT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with the single word: ok"


def client_cert() -> tuple[str, str | None, str | None] | None:
    """(certfile, keyfile, password) from the MTLS_* variables, or None."""
    p12 = os.environ.get("MTLS_PKCS12_FILE")
    if p12:
        return (_pkcs12_to_pem(p12, os.environ.get("MTLS_PKCS12_PASSWORD") or None), None, None)
    cert = os.environ.get("MTLS_CERT_FILE")
    if cert:
        key = os.environ.get("MTLS_KEY_FILE") or None
        return (cert, key, os.environ.get("MTLS_KEY_PASSWORD") or None)
    return None


def _pkcs12_to_pem(path: str, password: str | None) -> str:
    try:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            pkcs12,
        )
    except ImportError:
        sys.exit(
            "MTLS_PKCS12_FILE needs the cryptography package: run "
            "`uv run --with cryptography python scripts/check_model.py ...`"
        )
    key, cert, extra = pkcs12.load_key_and_certificates(
        Path(path).read_bytes(), password.encode() if password else None
    )
    if key is None or cert is None:
        sys.exit(f"{path} holds no private key and certificate")
    pem = cert.public_bytes(Encoding.PEM)
    pem += b"".join(c.public_bytes(Encoding.PEM) for c in extra or [])
    pem += key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    fd, out = tempfile.mkstemp(suffix=".pem")  # created 0600
    os.write(fd, pem)
    os.close(fd)
    atexit.register(os.remove, out)
    return out


def tls_context() -> ssl.SSLContext:
    """CA bundle (SSL_CERT_FILE, else certifi) plus the client certificate, if any."""
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or certifi.where())
    cert = client_cert()
    if cert is not None:
        ctx.load_cert_chain(cert[0], cert[1], cert[2])
    return ctx


def check_network(base_url: str, direct: bool) -> None:
    """Print each step of a direct connection to base_url's host."""
    url = urlparse(base_url)
    host = url.hostname or ""
    port = url.port or (443 if url.scheme == "https" else 80)
    print(f"network check for {url.scheme}://{host}:{port}")
    if not url.path.rstrip("/").endswith("/v1"):
        print("  note: the URL does not end in /v1; OpenAI-compatible gateways usually need it")

    # What Python's HTTP clients use: proxy variables, or else the OS proxy settings.
    proxies = urllib.request.getproxies()
    kinds = sorted(k for k in proxies if k != "no")
    source = "environment" if urllib.request.getproxies_environment() else "system settings"
    if not kinds:
        print("  proxies: none")
    elif direct:
        print(f"  proxies: {', '.join(kinds)} from {source}; ignored (--direct)")
    else:
        used = not urllib.request.proxy_bypass(host)
        print(f"  proxies: {', '.join(kinds)} from {source}; this host goes through one: {used}")
        if used:
            print("  (the model call goes through the proxy; the direct checks below do not.")
            print("   If they pass but the call fails, try --direct)")
    cert = client_cert()
    print(f"  client certificate: {'yes (' + cert[0] + ')' if cert else 'none'}")

    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as e:
        print(f"  DNS     FAILED: {e}")
        print("          the name does not resolve here: check VPN and the host name")
        return
    print(f"  DNS     ok: {', '.join(str(a) for a in addrs[:4])}")

    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        print(f"  TCP     FAILED: {e}")
        print("          no direct route: VPN, firewall, or a proxy is required")
        return
    print("  TCP     ok")

    if url.scheme != "https":
        sock.close()
        return
    # httpx (used by the provider SDKs) trusts certifi's bundle unless SSL_CERT_FILE is
    # set; a company TLS certificate is usually missing from certifi.
    cafile = os.environ.get("SSL_CERT_FILE") or certifi.where()
    try:
        with tls_context().wrap_socket(sock, server_hostname=host) as tls:
            print(f"  TLS     ok ({tls.version()}, CA bundle {cafile})")
        return
    except ssl.SSLError as e:
        print(f"  TLS     FAILED with the CA bundle Python uses ({cafile}): {e}")
        if "HANDSHAKE_FAILURE" in str(e) or "CERTIFICATE_REQUIRED" in str(e):
            print("          the server refused the handshake: if it requires a client")
            print("          certificate, set MTLS_CERT_FILE/MTLS_KEY_FILE or MTLS_PKCS12_FILE")
            return
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
                print("          but OpenSSL's default CA paths trust it: export your company's CA")
                print("          certificate(s) to a PEM file and set SSL_CERT_FILE to it in .env")
    except (OSError, ssl.SSLError) as e:
        print(f"          OpenSSL's default CA paths fail too: {e}")
        print("          (on macOS they do not include the Keychain)")


def error_chain(e: BaseException) -> list[str]:
    """The exception and everything behind it, innermost last (unwraps tenacity)."""
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = e
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        lines.append(f"{type(current).__name__}: {current}")
        last_attempt = getattr(current, "last_attempt", None)  # tenacity.RetryError
        inner = last_attempt.exception() if last_attempt is not None else None
        current = inner or current.__cause__ or current.__context__
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("profile", nargs="?", default="gateway-qwen", help="model profile name")
    parser.add_argument("--timeout", type=int, default=90, help="seconds per request (default: 90)")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="ignore proxies; use SSL_CERT_FILE and MTLS_* (see the module docstring)",
    )
    args = parser.parse_args()

    load_dotenv(CHECKOUT / ".env")
    try:
        models = load_profiles(CHECKOUT / "config").models
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    profile = models.get(args.profile)
    if profile is None:
        print(f"unknown model profile '{args.profile}'; known: {', '.join(sorted(models))}")
        return 2

    print(f"{'profile:':<18}{profile.name}")
    print(f"{'inspect_model:':<18}{profile.inspect_model}")
    for var in profile.required_env:
        value = os.environ.get(var)
        shown = value if value and var.endswith("_BASE_URL") else ("set" if value else "NOT SET")
        print(f"{var + ':':<18}{shown}")
    missing = missing_env([profile])
    if missing:
        print(f"missing: {', '.join(missing)} (add them to {CHECKOUT / '.env'})")
        return 1

    for var in profile.required_env:
        if var.endswith("_BASE_URL"):
            check_network(os.environ[var], args.direct)

    model_args: dict[str, Any] = dict(profile.model_args)
    if args.direct:
        # trust_env=False: no proxy from the environment or the OS, and no SSL_CERT_FILE
        # lookup by httpx, so the CA bundle and client certificate go in explicitly.
        model_args["http_client"] = DefaultAsyncHttpxClient(trust_env=False, verify=tls_context())
    model = get_model(
        profile.inspect_model,
        config=GenerateConfig(max_retries=1, timeout=args.timeout),
        **model_args,
    )
    how = "directly, ignoring proxies" if args.direct else "with the default client"
    print(f"calling the model {how} (gives up after about {args.timeout} s) ...")
    start = time.monotonic()
    try:
        output = asyncio.run(model.generate(PROMPT))
    except Exception as e:  # report any provider error as-is
        print(f"FAILED after {time.monotonic() - start:.1f} s:")
        for depth, line in enumerate(error_chain(e)):
            print(f"  {'  ' * depth}{line}"[:600])
        return 1
    elapsed = time.monotonic() - start
    print(f"OK, answered in {elapsed:.1f} s: {output.completion.strip()[:200]!r}")
    if output.usage is not None:
        print(f"tokens: {output.usage.input_tokens} in, {output.usage.output_tokens} out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
