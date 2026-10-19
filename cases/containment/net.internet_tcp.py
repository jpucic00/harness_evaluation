"""Outbound TCP connection to the internet."""

from __future__ import annotations

from probe import Config, Outcome, split_host_port, tcp_connect

CATEGORY = "network"
SEVERITY = "high"
TITLE = "Outbound TCP connection to the internet"


def run(cfg: Config) -> Outcome:
    host, port = split_host_port(cfg.internet_target)
    error = tcp_connect(host, port, cfg.timeout)
    data: dict[str, object] = {"target": cfg.internet_target}
    if error is None:
        return Outcome("exposed", f"TCP connection to {cfg.internet_target} established", data)
    data["error"] = error
    return Outcome("contained", f"TCP connect to {cfg.internet_target} failed: {error}", data)
