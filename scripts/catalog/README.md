# Test catalog generator

Builds [docs/test-catalog.html](../../docs/test-catalog.html), a human-readable list of every
test with collapsible step-by-step explanations.

**Status: partly out of date.** `collect.py` reads the capability tasks and the local
injection cases through the loaders in `agenteval.cases` (plus CodeIPI's published
samples). `render.py` predates the `cases/` layout (2026-10-18) and has not been updated:
it has the containment checks and monitor signals written out by hand, instead of
reading `cases/containment/` and `cases/monitor/` (`load_containment_checks()`,
`load_monitor_groups()`).

Usage, from the repository root:

```bash
uv run python scripts/catalog/collect.py > /tmp/catalog-data.json
uv run python scripts/catalog/render.py /tmp/catalog-data.json docs/test-catalog.html --standalone
```

Without `--standalone`, the output is an artifact page body: no `<html>` skeleton. Both
forms load fonts from Google Fonts.
