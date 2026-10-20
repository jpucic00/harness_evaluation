"""Call a model profile once, outside any harness or sandbox, and report what happened.

Separates "the provider is slow, unreachable or rejecting requests" from problems in
the harness, bridge or environment: if this answers quickly, the model side works.
When the profile has a *_BASE_URL variable, the network path to it is checked step by
step first (proxies, DNS, TCP, TLS), so a connection error says which step fails.

Usage, from the checkout root:

    uv run python scripts/check_model.py [PROFILE] [--timeout SECONDS] [--direct]

PROFILE is a model profile name from config/models/ (default: gateway-qwen). The
checkout's .env is loaded; shell variables win. Keys and proxy values are never printed.

The call uses the profile's `connection` settings, as agenteval does (see
src/agenteval/models.py). The gateway profiles have `connection: proxy: direct`:
proxies from the environment or the OS are ignored (the gateway drops proxied calls
with "Server disconnected without sending a response"), SSL_CERT_FILE is the CA bundle
and MTLS_* give a client certificate. --direct forces that for any profile. A .p12/.pfx
bundle needs the mtls extra: `uv sync --extra mtls`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import certifi
from dotenv import load_dotenv
from inspect_ai.model import GenerateConfig

from agenteval.config import ConfigError, Connection, load_profiles, missing_env
from agenteval.models import build_model, client_cert_source, tls_context

CHECKOUT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with the single word: ok"


def check_network(base_url: str, connection: Connection | None) -> None:
    """Print each step of a direct connection to base_url's host."""
    direct = connection is not None and connection.proxy == "direct"
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
        print(f"  proxies: {', '.join(kinds)} from {source}; ignored (direct connection)")
    else:
        used = not urllib.request.proxy_bypass(host)
        print(f"  proxies: {', '.join(kinds)} from {source}; this host goes through one: {used}")
        if used:
            print("  (the model call goes through the proxy; the direct checks below do not.")
            print("   If they pass but the call fails, try --direct)")
    if connection is None:
        print("  client certificate: not used (the profile has no `connection`)")
    else:
        print(f"  client certificate: {client_cert_source() or 'none'}")

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
    # The trust Inspect's clients use: SSL_CERT_FILE, else SSL_CERT_DIR, else the OS store.
    if os.environ.get("SSL_CERT_FILE"):
        trust = f"SSL_CERT_FILE {os.environ['SSL_CERT_FILE']}"
    elif os.environ.get("SSL_CERT_DIR"):
        trust = f"SSL_CERT_DIR {os.environ['SSL_CERT_DIR']}"
    else:
        trust = "the OS trust store"
    try:
        ctx = tls_context(client_certificate=connection is not None)
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            print(f"  TLS     ok ({tls.version()}, trusting {trust})")
    except ssl.SSLError as e:
        print(f"  TLS     FAILED trusting {trust}: {e}")
        if "HANDSHAKE_FAILURE" in str(e) or "CERTIFICATE_REQUIRED" in str(e):
            print("          the server refused the handshake: if it requires a client")
            print("          certificate, set MTLS_CERT_FILE/MTLS_KEY_FILE or MTLS_PKCS12_FILE")
        elif "CERTIFICATE_VERIFY_FAILED" in str(e):
            print("          the gateway's certificate is not trusted: append your company's CA")
            print(f"          certificate(s) to a copy of {certifi.where()}")
            print(
                "          and set SSL_CERT_FILE to that copy in .env (it replaces the trust store)"
            )
    finally:
        sock.close()


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
        help="connect directly (ignore proxies) even if the profile does not say so",
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

    if args.direct:
        profile = profile.model_copy(update={"connection": Connection(proxy="direct")})
    for var in profile.required_env:
        if var.endswith("_BASE_URL"):
            check_network(os.environ[var], profile.connection)

    model = build_model(profile)
    connection = profile.connection
    if connection is None:
        how = "with Inspect's default client"
    elif connection.proxy == "direct":
        how = "directly, ignoring proxies (connection: direct)"
    else:
        how = "with the profile's connection settings"
    print(f"calling the model {how} (gives up after about {args.timeout} s) ...")
    start = time.monotonic()
    try:
        config = GenerateConfig(max_retries=1, timeout=args.timeout)
        output = asyncio.run(model.generate(PROMPT, config=config))
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
