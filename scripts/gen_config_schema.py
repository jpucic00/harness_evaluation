"""Regenerate the JSON Schemas of the config profiles in config/schema/.

Usage, from the checkout root:

    uv run python scripts/gen_config_schema.py

Run it after changing a profile model in src/agenteval/config.py; a test fails while the
committed schemas are stale. The YAML files point at them with a
`# yaml-language-server: $schema=...` first line, which editors use for completion.
"""

from __future__ import annotations

from pathlib import Path

from agenteval.config import write_config_schemas

if __name__ == "__main__":
    for path in write_config_schemas(Path(__file__).resolve().parent.parent / "config" / "schema"):
        print(path)
