"""Call a model profile once, outside any harness or sandbox, and report what happened.

Separates "the provider is slow, unreachable or rejecting requests" from problems in
the harness, bridge or environment: if this answers quickly, the model side works.

Usage, from the checkout root:

    uv run python scripts/check_model.py [PROFILE] [--timeout SECONDS]

PROFILE is a model profile name from config/models/ (default: gateway-qwen). The
checkout's .env is loaded; shell variables win. Keys are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai.model import GenerateConfig, get_model

from agenteval.config import ConfigError, load_profiles, missing_env

CHECKOUT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with the single word: ok"


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
        elapsed = time.monotonic() - start
        print(f"FAILED after {elapsed:.1f} s: {type(e).__name__}: {e}"[:1000])
        cause = e.__cause__ or e.__context__
        if cause is not None:
            print(f"  cause: {type(cause).__name__}: {cause}"[:600])
        return 1
    elapsed = time.monotonic() - start
    print(f"OK, answered in {elapsed:.1f} s: {output.completion.strip()[:200]!r}")
    if output.usage is not None:
        print(f"tokens: {output.usage.input_tokens} in, {output.usage.output_tokens} out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
