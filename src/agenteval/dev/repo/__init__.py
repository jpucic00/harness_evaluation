"""Host-side git for ``agenteval dev``: snapshot, bundle transport and per-file apply.

Copy mode moves the user's repo into the env as git objects. ``snapshot`` commits the
working tree as it is (untracked files, ``--include``d ignored files and flattened
submodules included) as ``refs/agenteval/<id>/base`` and the index as ``.../index``, and
bundles them with the branch history into ``in.bundle``; ``guest/dev/dev_repo.py``
rebuilds the repo from it inside the env. At the end the env bundles its final working
tree and new commits; ``import_export`` verifies and fetches them into
``refs/agenteval/<id>/*`` (never into ``refs/heads``) and ``repo_items`` turns the
difference into review items. Each item writes one file with a 3-way rule; the branch
moves only through a guarded fast-forward, the only step that touches the user's index.

Blobs carry file bytes unconverted when git supports ``--attr-source`` (2.42+), so a host
file can be compared with its snapshot byte for byte. Every git call turns off hooks,
fsmonitor, the untracked cache, LFS filters and commit signing, and internal commits use a
fixed identity. A directory that is not a repo is tracked by a shadow git dir in the
session directory.

Live mode needs no transport: ``live_hashes`` and ``live_report`` point out what a session
changed that git or other host tools run.
"""

from __future__ import annotations

from agenteval.dev.repo.commits import cleanup_refs
from agenteval.dev.repo.common import (
    CONFLICTS_DIR,
    PATCH_NAME,
    SHADOW_DIR,
    RepoError,
    UnsafePathError,
    raw_attrs_supported,
)
from agenteval.dev.repo.discovery import RepoInfo, Snapshot, discover, snapshot
from agenteval.dev.repo.export_import import import_export, safe_branch_name
from agenteval.dev.repo.host_paths import repo_path_flags
from agenteval.dev.repo.live import live_hashes, live_report
from agenteval.dev.repo.review_items import repo_items, validate_path

__all__ = [
    "CONFLICTS_DIR",
    "PATCH_NAME",
    "RepoError",
    "RepoInfo",
    "SHADOW_DIR",
    "Snapshot",
    "UnsafePathError",
    "cleanup_refs",
    "discover",
    "import_export",
    "live_hashes",
    "live_report",
    "raw_attrs_supported",
    "repo_items",
    "repo_path_flags",
    "safe_branch_name",
    "snapshot",
    "validate_path",
]
