"""Processes of other users are visible."""

from __future__ import annotations

import os

from probe import Config, Outcome, err

CATEGORY = "process"
SEVERITY = "low"
TITLE = "Processes of other users are visible"


def run(cfg: Config) -> Outcome:
    me = os.getuid()
    try:
        entries = [e for e in os.listdir(cfg.proc_dir) if e.isdigit()]
    except OSError as ex:
        return Outcome("error", f"cannot list {cfg.proc_dir}: {err(ex)}")
    others: dict[int, int] = {}
    for entry in entries:
        try:
            uid = os.stat(os.path.join(cfg.proc_dir, entry)).st_uid
        except OSError:
            continue
        if uid != me:
            others[uid] = others.get(uid, 0) + 1
    data: dict[str, object] = {
        "processes": len(entries),
        "other_uids": {str(k): v for k, v in sorted(others.items())},
    }
    if others:
        total = sum(others.values())
        return Outcome(
            "exposed",
            f"{total} process(es) of {len(others)} other uid(s) visible "
            f"(uids {sorted(others)[:5]})",
            data,
        )
    return Outcome("contained", f"only own processes visible ({len(entries)})", data)
