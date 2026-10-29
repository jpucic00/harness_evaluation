"""``agenteval profiles``: list the model, harness, environment and run profiles."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    ModelProfile,
    expand_matrix,
    load_profiles,
    load_run,
    yaml_files,
)
from agenteval.environments import PROVIDERS


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [list(headers), *rows]
    ]
    return "\n".join(lines)


def run_files(config_dir: Path) -> list[Path]:
    return yaml_files(config_dir / "runs")


def get_environment(config_dir: Path, name: str) -> EnvironmentProfile:
    environments = load_profiles(config_dir).environments
    if name not in environments:
        raise ConfigError(f"unknown environment '{name}'; available: {sorted(environments)}")
    return environments[name]


def _env_image(env: EnvironmentProfile) -> str:
    provider = PROVIDERS.get(env.provider)
    return provider.image_label(env) if provider is not None else "-"


def describe_profiles(config_dir: Path) -> str:
    profiles = load_profiles(config_dir)
    sections: list[str] = []

    def price(m: ModelProfile) -> str:
        return f"{m.pricing.input:g}/{m.pricing.output:g}" if m.pricing else "-"

    sections.append(
        "Models (pricing: USD per 1M input/output tokens)\n"
        + table(
            ["name", "inspect model", "pricing", "required env", "description"],
            [
                [
                    m.name,
                    m.inspect_model or f"- ({m.access})",
                    price(m),
                    ",".join(m.required_env) or "-",
                    m.description,
                ]
                for m in profiles.models.values()
            ],
        )
    )
    sections.append(
        "Harnesses\n"
        + table(
            ["name", "agent", "inner sandbox", "description"],
            [
                [
                    h.name,
                    f"{h.agent}@{h.version}",
                    h.inner_sandbox.type
                    + (f" ({h.inner_sandbox.network})" if h.inner_sandbox.type != "none" else ""),
                    h.description,
                ]
                for h in profiles.harnesses.values()
            ],
        )
    )
    sections.append(
        "Environments\n"
        + table(
            ["name", "provider", "image", "egress", "secrets", "concurrency", "description"],
            [
                [
                    e.name,
                    e.provider,
                    _env_image(e),
                    e.egress,
                    "planted" if e.ambient_secrets else "none",
                    str(e.max_concurrent),
                    e.description,
                ]
                for e in profiles.environments.values()
            ],
        )
    )
    run_rows: list[list[str]] = []
    for path in run_files(config_dir):
        try:
            run = load_run(path)
            cells = len(expand_matrix(run, profiles))
            matrix = (
                f"{len(run.models)}m x {len(run.harnesses)}h x {len(run.environments)}e x "
                f"{len(run.suites)}s = {cells} tasks"
            )
            run_rows.append([run.name, str(path), matrix, run.description])
        except ConfigError as ex:
            run_rows.append([path.stem, str(path), "INVALID", str(ex)])
    sections.append("Runs\n" + table(["name", "file", "matrix", "description"], run_rows))
    return "\n\n".join(sections)
