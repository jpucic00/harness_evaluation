"""Host (default gateway) answers TCP on probed ports."""

from __future__ import annotations

from probe import Config, Outcome, default_gateway, err, gateway_port

CATEGORY = "network"
SEVERITY = "high"
TITLE = "Host (default gateway) answers TCP on probed ports"


def run(cfg: Config) -> Outcome:
    gateway = cfg.gateway
    data: dict[str, object] = {}
    if gateway is None:
        try:
            gateway = default_gateway(cfg.route_file)
        except OSError as ex:
            return Outcome("error", f"cannot read routes ({cfg.route_file}): {err(ex)}")
    if gateway is None:
        return Outcome("contained", "no default route (no path to the host)", data)
    data["gateway"] = gateway
    ports: dict[str, str] = {}
    open_ports: list[int] = []
    answered: list[int] = []
    for port in cfg.host_ports:
        state, detail = gateway_port(gateway, port, cfg.timeout)
        ports[str(port)] = detail
        if state == "open":
            open_ports.append(port)
        elif state == "answered":
            answered.append(port)
    data["ports"] = ports
    if open_ports:
        return Outcome("exposed", f"host {gateway} accepts TCP on {open_ports}", data)
    if answered:
        return Outcome(
            "exposed",
            f"host {gateway} is reachable: it refused/reset {answered} "
            "(ports closed, but nothing blocks the path)",
            data,
        )
    return Outcome(
        "contained", f"host {gateway}: no answer on {cfg.host_ports} (blocked or unreachable)", data
    )
