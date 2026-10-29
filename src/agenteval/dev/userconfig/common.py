"""Limits, allow-lists and patterns the dev config modules share."""

from __future__ import annotations

import json
import re
from typing import Any, Final

MAX_FILE_BYTES: Final = 2 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 50 * 1024 * 1024
MAX_JSON_DEPTH: Final = 64
"""Deeper JSON from the env is treated as invalid (the merge code recurses)."""
LOCAL_FILE: Final = "CLAUDE.local.md"
SKIPPED_FILE: Final = "skipped.json"
"""``<session>/config/skipped.json``: returned files left out for size."""

CLAUDE_FILES: Final = ("settings.json", "CLAUDE.md")
CLAUDE_DIRS: Final = ("commands", "agents", "skills", "output-styles")
OPENCODE_FILES: Final = ("opencode.json", "opencode.jsonc", "AGENTS.md")
OPENCODE_DIRS: Final = ("agent", "agents", "command", "commands")
_SKIP_NAMES = frozenset({".credentials.json", ".claude.json", ".DS_Store"})
_AREA_HOME = {"claude": ".claude", "opencode": ".config/opencode"}
"""Logical area -> config dir in the env, relative to the agent's HOME."""

HELPER_KEYS: Final = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "gcpAuthRefresh",
    "otelHeadersHelper",
    "proxyAuthHelper",
)
PROTECTED_KEYS: Final = (
    *HELPER_KEYS,
    "model",
    "skipDangerousModePermissionPrompt",
    "autoMemoryDirectory",
)
"""settings.json keys the env can never change on the host."""
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
_ROUTING_ENV = re.compile(
    r"^(?:ANTHROPIC_|CLAUDE_CODE_USE_|AWS_|GOOGLE_|VERTEX_|CLOUD_ML_)"
    r"|^(?:HTTPS?_PROXY|ALL_PROXY|NO_PROXY)$",
    re.I,
)

CLAUDE_WATCHED: Final = (
    "permissions",
    "hooks",
    "env",
    "statusLine",
    "enabledMcpjsonServers",
    "enableAllProjectMcpServers",
    "mcpServers",
    "sandbox",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "outputStyle",
)
OPENCODE_WATCHED: Final = ("permission", "mcp", "plugin", "provider", "tools", "instructions")

_HOST_PATH = re.compile(r"/Users/|/opt/homebrew|/Applications\b|~/Library|/Volumes/")
_REFERENCE = re.compile(r"\{(?:env|file):[^}]*\}")
_FILE_REFERENCE = re.compile(r"\{file:\s*([~/][^}]*)\}")
_ANY_FILE_REFERENCE = re.compile(r"\{file:([^}]*)\}")
# Control characters, bidi controls and the code points HFS+ ignores in names.
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f؜​-‏‪-‮⁠-⁯﻿]")
_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_TRAILING_CLOSE = re.compile(r"\s*[}\]]")
_TOOLS_KEY = re.compile(r"^\s*(?:allowed[-_]tools|tools)\s*:(.*)$", re.I)

_TEXT_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "install command",
        re.compile(
            r"\b(?:npm|pnpm|yarn|pip3?|pipx|uv(?:\s+(?:pip|tool))?|brew|apt(?:-get)?|cargo|go)"
            r"\s+(?:install|add|i)\b",
            re.I,
        ),
    ),
    (
        "download piped to a shell",
        # Anchored on the first curl/wget (atomic) so long lines stay linear.
        re.compile(r"^(?>.*?\b(?:curl|wget)\b).*\|\s*(?:sudo\s+)?\w*sh\b", re.I),
    ),
    ("sudo", re.compile(r"\bsudo\b", re.I)),
    (
        "chmod +x",
        re.compile(
            r"\bchmod\s+(?:-\w+\s+)*(?:[ugoa]*\+[rws]*x|(?=[0-7]{3,4}\b)[0-7]*[1357])", re.I
        ),
    ),
    ("shell code block", re.compile(r"^\s*(?:```|~~~)\s*(?:bash|sh|zsh|shell|console)\b", re.I)),
    ("URL", re.compile(r"\bhttps?://", re.I)),
    ("@-import of an absolute or home path", re.compile(r"(?:^|\s)@(?:~|\.\.)?/")),
    (
        "tells the agent not to ask",
        re.compile(r"\b(?:don['’]?t|do\s+not|never)\s+(?:ask|confirm|prompt)", re.I),
    ),
    (
        "skips confirmation",
        re.compile(r"\bskip\w*\b.{0,40}?\b(?:confirmation|permission|approval)", re.I),
    ),
    (
        "ignore previous instructions",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules)", re.I
        ),
    ),
    ("without asking", re.compile(r"\bwithout\s+(?:asking|confirmation|permission)", re.I)),
    ("auto-approve", re.compile(r"\bauto[-\s]?approve", re.I)),
    ("always allow/approve/run", re.compile(r"\balways\s+(?:allow|approve|run)", re.I)),
)

_MISSING: Final = object()

JsonObject = dict[str, Any]


def _dump_json(data: object) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
