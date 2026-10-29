"""Host CLI config and Claude memory for dev sessions.

Copy-in (``stage``): an allow-listed, sanitized copy of the user's Claude Code
(``~/.claude``) or OpenCode (``~/.config/opencode``) config, plus this repo's Claude
memory, goes into the env as ``<session>/config.tar``:

- ``home/<path>``: relative to the agent's HOME (``.claude/settings.json``,
  ``.claude/projects/<vm key>/memory/...``, ``.config/opencode/opencode.json``);
- ``workspace/CLAUDE.local.md``: relative to the repo root in the env.

Members are regular files owned by 0:0 (mode 0644/0755); the env extracts them with
``--no-same-owner`` as the agent. Each file also lands in
``<session>/config/base/<logical name>`` (``claude/settings.json``, ``memory/x.md``,
``opencode/AGENTS.md``, ``local/CLAUDE.local.md``), the merge base for the return.
Credentials, ``~/.claude.json``, other projects, transcripts, history, plugins and
the OpenCode data dir are never read into the session.

Return: the env archives the ``export_paths`` that exist in the same layout,
``extract_returned`` unpacks the allow-listed members into
``<session>/config/vm/<logical name>`` and ``config_items`` turns each difference
into a ``ReviewItem``, merged 3-way against the host file at review time. Nothing
is written to the host before the user approves an item; memory goes back under the
host's project key and the host config dir is created on the first write.
"""

from __future__ import annotations

from agenteval.dev.userconfig.common import HELPER_KEYS, MAX_FILE_BYTES
from agenteval.dev.userconfig.discovery import (
    ConfigPlan,
    canonical_root,
    claude_project_key,
    host_config_dir,
    plan,
)
from agenteval.dev.userconfig.flags import (
    definition_flags,
    memory_flags,
    settings_flags,
    text_flags,
)
from agenteval.dev.userconfig.merge import merge_json, settings_protected
from agenteval.dev.userconfig.reports import claude_global_seed, host_theme
from agenteval.dev.userconfig.returned import export_paths, extract_returned
from agenteval.dev.userconfig.review_items import config_items, config_patch
from agenteval.dev.userconfig.sanitizing import sanitize_claude_settings, sanitize_opencode
from agenteval.dev.userconfig.staging import StageResult, stage

__all__ = [
    "ConfigPlan",
    "HELPER_KEYS",
    "MAX_FILE_BYTES",
    "StageResult",
    "canonical_root",
    "claude_global_seed",
    "claude_project_key",
    "config_items",
    "config_patch",
    "definition_flags",
    "export_paths",
    "extract_returned",
    "host_config_dir",
    "host_theme",
    "memory_flags",
    "merge_json",
    "plan",
    "sanitize_claude_settings",
    "sanitize_opencode",
    "settings_flags",
    "settings_protected",
    "stage",
    "text_flags",
]
