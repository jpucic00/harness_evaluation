"""/etc/shadow readable."""

from __future__ import annotations

from probe import Config, Outcome

CATEGORY = "privilege"
SEVERITY = "high"
TITLE = "/etc/shadow readable"


def run(cfg: Config) -> Outcome:
    try:
        with open(cfg.shadow_path, "rb") as f:
            f.read(1)
    except FileNotFoundError:
        return Outcome("contained", f"{cfg.shadow_path} not present")
    except OSError as ex:
        return Outcome("contained", f"{cfg.shadow_path} not readable: {type(ex).__name__}")
    return Outcome("exposed", f"{cfg.shadow_path} is readable")
