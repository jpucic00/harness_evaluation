"""Call a model profile once, outside any harness or sandbox, and report what happened.

Separates "the provider is slow, unreachable or rejecting requests" from problems in
the harness, bridge or environment: if this answers quickly, the model side works.
When the profile has a *_BASE_URL variable, the network path to it is checked step by
step first (proxy variables, DNS, TCP, TLS), so a connection error says which step fails.

Usage, from the checkout root:

    uv run python scripts/check_model.py [PROFILE] [--timeout SECONDS]

PROFILE is a model profile name from config/models/ (default: gateway-qwen). The
checkout's .env is loaded; shell variables win. Keys and proxy values are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import certifi
from dotenv import load_dotenv
from inspect_ai.model import GenerateConfig, get_model

from agenteval.config import ConfigError, load_profiles, missing_env

CHECKOUT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with the single word: ok"
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")


def check_network(base_url: str) -> None:
    """Print each step of a direct connection to base_url's host."""
    url = urlparse(base_url)
    host = url.hostname or ""
    port = url.port or (443 if url.scheme == "https" else 80)
    print(f"network check for {url.scheme}://{host}:{port}")
    if not url.path.rstrip("/").endswith("/v1"):
        print("  note: the URL does not end in /v1; OpenAI-compatible gateways usually need it")

    proxies = [v for v in PROXY_VARS if os.environ.get(v) or os.environ.get(v.lower())]
    print(f"  proxy variables set: {', '.join(proxies) or 'none'}")
    if proxies:
        print("  (the model call goes through the proxy; the direct checks below do not)")

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
    # httpx (used by the provider SDKs) trusts certifi's bundle unless SSL_CERT_FILE or
    # SSL_CERT_DIR is set; a company TLS certificate is usually missing from certifi.
    cafile = os.environ.get("SSL_CERT_FILE") or certifi.where()
    try:
        with ssl.create_default_context(cafile=cafile).wrap_socket(
            sock, server_hostname=host
        ) as tls:
            print(f"  TLS     ok ({tls.version()}, CA bundle {cafile})")
        return
    except ssl.SSLError as e:
        print(f"  TLS     FAILED with the CA bundle Python uses ({cafile}): {e}")
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
            check_network(os.environ[var])

    model = get_model(
        profile.inspect_model,
        config=GenerateConfig(max_retries=1, timeout=args.timeout),
        **profile.model_args,
    )
    print(f"calling the model (gives up after about {args.timeout} s) ...")
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
