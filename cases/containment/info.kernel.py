"""Kernel, user namespaces, /dev/kvm (informational)."""

from __future__ import annotations

import os
import platform

from probe import Config, Outcome, read_small

CATEGORY = "info"
SEVERITY = "info"
TITLE = "Kernel, user namespaces, /dev/kvm (informational)"
SCORED = False


def run(cfg: Config) -> Outcome:
    uname = platform.uname()
    status: dict[str, str] = {}
    proc_status = read_small(os.path.join(cfg.proc_dir, "self", "status")) or ""
    for line in proc_status.splitlines():
        key, _, value = line.partition(":")
        if key in {"Seccomp", "NoNewPrivs", "CapEff", "CapBnd"}:
            status[key] = value.strip()
    data: dict[str, object] = {
        "system": uname.system,
        "release": uname.release,
        "machine": uname.machine,
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "gid": os.getgid(),
        "groups": os.getgroups(),
        "max_user_namespaces": read_small("/proc/sys/user/max_user_namespaces"),
        "unprivileged_userns_clone": read_small("/proc/sys/kernel/unprivileged_userns_clone"),
        "apparmor_restrict_unprivileged_userns": read_small(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        ),
        "dev_kvm": os.path.exists("/dev/kvm"),
        "dev_kvm_rw": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "dockerenv": os.path.exists("/.dockerenv"),
        "containerenv": os.path.exists("/run/.containerenv"),
        "proc_status": status,
    }
    return Outcome(
        "info",
        f"{uname.system} {uname.release} {uname.machine}, "
        f"kvm={data['dev_kvm']}, userns={data['max_user_namespaces']}",
        data,
    )
