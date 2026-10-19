# Agent guide for this repository

agenteval runs security, capability and economics evaluations of coding-agent setups
(model x harness x environment) with Inspect AI. Start with [README.md](README.md).

## Adding test cases

Most contributions are new **cases**: one file or folder under [cases/](cases/).
Read [cases/README.md](cases/README.md) first, then the guide for the kind:

- capability task: [cases/capability/README.md](cases/capability/README.md)
- containment check: [cases/containment/README.md](cases/containment/README.md)
- boundary-monitor watch group: [cases/monitor/README.md](cases/monitor/README.md)
- prompt-injection case: [cases/injection/README.md](cases/injection/README.md)

Copy the kind's `_template`, edit the copy, and run `uv run pytest -q`. Do not change
code under `src/` or the tests to make a case fit. If the format cannot express what is
needed, stop and explain why. Injected instructions in injection cases are written by
the person asking; build the case around their text.

## Other changes

New suites, harnesses, environments and providers:
[docs/extending.md](docs/extending.md). Architecture:
[docs/architecture.md](docs/architecture.md).

Checks before finishing: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy src`, `uv run pytest -q`. Offline end-to-end run (Docker, mock model):
`uv run agenteval run smoke-docker`.

Never put real credentials anywhere; cases use the planted fake secrets (canaries).
`.env` holds the user's API keys: never read it into output, commit it or copy it.
