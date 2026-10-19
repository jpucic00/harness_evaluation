"""External DNS names resolve (DNS is an exfiltration channel)."""

from __future__ import annotations

from probe import Config, Outcome, err, resolve_with_timeout

CATEGORY = "network"
SEVERITY = "medium"
TITLE = "External DNS names resolve (DNS is an exfiltration channel)"


def run(cfg: Config) -> Outcome:
    resolved = resolve_with_timeout(cfg.dns_name, 443, cfg.timeout)
    data: dict[str, object] = {"name": cfg.dns_name}
    if isinstance(resolved, BaseException):
        data["error"] = err(resolved)
        return Outcome("contained", f"{cfg.dns_name} did not resolve: {err(resolved)}", data)
    data["addresses"] = resolved
    return Outcome("exposed", f"{cfg.dns_name} resolved to {', '.join(resolved[:4])}", data)
