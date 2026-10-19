# Test catalog generator

Builds [docs/test-catalog.html](../../docs/test-catalog.html), a human-readable list of every
test with collapsible step-by-step explanations.

**Status: out of date.** These scripts predate the `cases/` layout (2026-10-18) and have
not been updated:

- `collect.py` reads capability tasks from `src/agenteval/suites/capability_tasks/` (now
  `cases/capability/`) and only CodeIPI's published samples (not `cases/injection/`).
- `render.py` has the containment checks and monitor signals written out by hand,
  instead of reading `cases/containment/` and `cases/monitor/`.

Usage once updated, run from the repository root:

```bash
uv run python scripts/catalog/collect.py > /tmp/catalog-data.json
uv run python scripts/catalog/render.py /tmp/catalog-data.json docs/test-catalog.html --standalone
```

Without `--standalone`, the output is an artifact page body: no `<html>` skeleton, and
fonts are loaded from Google Fonts.
