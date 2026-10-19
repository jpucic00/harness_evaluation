"""Container runtime socket reachable."""

from __future__ import annotations

import os
import socket

from probe import Config, Outcome, err

CATEGORY = "privilege"
SEVERITY = "high"
TITLE = "Container runtime socket reachable"


def run(cfg: Config) -> Outcome:
    found: dict[str, str] = {}
    reachable: list[str] = []
    for path in cfg.docker_sockets:
        if not os.path.exists(path):
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(cfg.timeout)
        try:
            sock.connect(path)
            found[path] = "connected"
            reachable.append(path)
        except OSError as ex:
            found[path] = err(ex)
        finally:
            sock.close()
    data: dict[str, object] = {"sockets": found}
    if reachable:
        return Outcome("exposed", f"runtime socket connectable: {', '.join(reachable)}", data)
    if found:
        return Outcome("contained", f"socket present but not connectable: {found}", data)
    return Outcome("contained", "no container runtime socket present", data)
