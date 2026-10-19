"""Host folders shared into the environment (virtiofs/9p/fuse)."""

from __future__ import annotations

from probe import NOT_SHARED_FS_TYPES, SHARED_FS_TYPES, Config, Outcome, err

CATEGORY = "host"
SEVERITY = "high"
TITLE = "Host folders shared into the environment (virtiofs/9p/fuse)"


def run(cfg: Config) -> Outcome:
    try:
        with open(cfg.mounts_file) as f:
            lines = f.read().splitlines()
    except OSError as ex:
        return Outcome("error", f"cannot read {cfg.mounts_file}: {err(ex)}")
    shared: list[dict[str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        source, target, fstype = fields[0], fields[1], fields[2]
        if fstype in NOT_SHARED_FS_TYPES:
            continue
        if fstype in SHARED_FS_TYPES or fstype.startswith("fuse."):
            shared.append({"source": source, "target": target, "fstype": fstype})
    data: dict[str, object] = {"mounts": shared}
    if shared:
        desc = ", ".join(f"{m['target']} ({m['fstype']})" for m in shared[:5])
        return Outcome("exposed", f"host-shared mounts present: {desc}", data)
    return Outcome("contained", "no virtiofs/9p/fuse host mounts", data)
