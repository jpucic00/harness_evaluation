"""Profile and run configuration.

A run is the cross product of four independent axes, each described by YAML
profiles under ``config/``:

- models/        which LLM answers the agent (Inspect model id + prices)
- harnesses/     which agent CLI runs, at which version, inside which inner sandbox
- environments/  where the agent runs (Tart VM, Docker container, ...)
- runs/          which models x harnesses x environments x suites to execute
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, get_args

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_DIR = Path("config")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


class _Named(_Strict):
    name: str


# --------------------------------------------------------------------------- models

DEFAULT_AGENT_MODELS: dict[str, str] = {"opencode": "openrouter/qwen/qwen3.8-27b"}
"""``agent_models`` entries a bridge profile gets when it sets none for that agent.

OpenCode's model id only selects its request format and prompt template, and it must be
in OpenCode's bundled catalog (the harnesses turn off fetching models): an unknown id
becomes a custom model with no known limits (context 0, output 0, reasoning off). This
one is in the catalog, uses OpenRouter's OpenAI-style chat format, and gets OpenCode's
generic prompt.
"""


class Pricing(_Strict):
    """USD per 1M tokens."""

    input: float
    output: float
    input_cache_read: float | None = None
    """Price of cached input tokens; ``None`` = same as ``input`` (no cache discount assumed)."""
    input_cache_write: float | None = None
    """Price of cache-write tokens; ``None`` = same as ``input``."""

    def cache_read_rate(self) -> float:
        return self.input if self.input_cache_read is None else self.input_cache_read

    def cache_write_rate(self) -> float:
        return self.input if self.input_cache_write is None else self.input_cache_write


class Connection(_Strict):
    """How the host reaches the provider (OpenAI-compatible providers only).

    Setting it also loads the CA bundle from ``SSL_CERT_FILE`` and a client certificate
    from ``MTLS_CERT_FILE`` / ``MTLS_KEY_FILE`` / ``MTLS_KEY_PASSWORD`` or
    ``MTLS_PKCS12_FILE`` / ``MTLS_PKCS12_PASSWORD`` (see ``agenteval.models``).
    """

    proxy: Literal["environment", "direct"] = "environment"
    """``direct`` ignores HTTP(S)_PROXY and the OS proxy settings: for a gateway reached
    over a VPN that drops proxied calls ("Server disconnected without sending a response")."""


class ModelProfile(_Named):
    description: str = ""
    access: Literal["bridge", "subscription"] = "bridge"
    """``bridge``: Inspect on the host calls ``inspect_model`` for the agent.

    ``subscription``: Claude Code in the environment calls Anthropic itself with the
    user's Claude subscription (``CLAUDE_CODE_OAUTH_TOKEN``). Anthropic allows
    subscription logins only for Claude Code itself, so the calls cannot go through
    Inspect: such a profile has no ``inspect_model``, no pricing, and works only in
    ``agenteval dev`` with a ``claude_code`` harness. ``agent_models.claude_code`` is
    the model Claude Code is started with.
    """
    inspect_model: str = ""
    """Inspect model id, e.g. ``openrouter/qwen/qwen3.8-27b`` or ``mockllm/model``."""
    model_args: dict[str, Any] = Field(default_factory=dict)
    connection: Connection | None = None
    """Custom HTTP client settings; ``None`` keeps Inspect's default client."""
    pricing: Pricing | None = None
    required_env: list[str] = Field(default_factory=list)
    """Host env vars that must be set before the run starts (e.g. OPENROUTER_API_KEY)."""
    agent_models: dict[str, str] = Field(default_factory=dict)
    """Model id each agent CLI is told to use, keyed by harness agent (e.g. ``opencode``).

    It selects the CLI's request format and prompt template; the calls themselves still
    go through the Inspect bridge to ``inspect_model``. A harness ``agent_model`` wins.
    Bridge profiles get ``DEFAULT_AGENT_MODELS`` for agents they leave out
    (``opencode: openrouter/qwen/qwen3.8-27b``).
    """

    @model_validator(mode="after")
    def _access_fields(self) -> ModelProfile:
        if self.access == "bridge":
            if not self.inspect_model:
                raise ValueError("inspect_model is required (unless access: subscription)")
            object.__setattr__(self, "agent_models", DEFAULT_AGENT_MODELS | self.agent_models)
            return self
        unused = [
            name
            for name, value in (
                ("inspect_model", self.inspect_model),
                ("model_args", self.model_args),
                ("connection", self.connection),
                ("pricing", self.pricing),
            )
            if value
        ]
        if unused:
            raise ValueError(f"access: subscription takes no {', '.join(unused)}")
        if not self.agent_models.get("claude_code"):
            raise ValueError("access: subscription needs agent_models.claude_code")
        return self


# --------------------------------------------------------------------------- harnesses


class InnerSandbox(_Strict):
    """Sandbox applied around the agent process *inside* the environment."""

    type: Literal["none", "bwrap"] = "none"
    hide_paths: list[str] = Field(default_factory=list)
    """Paths replaced by an empty tmpfs inside the sandbox. ``~`` = agent home."""
    extra_writable_paths: list[str] = Field(default_factory=list)
    """Writable in addition to the workspace and the harness state dirs."""
    network: Literal["proxy-only", "host"] = "proxy-only"
    """proxy-only: own network namespace, only the Inspect model proxy is reachable."""
    clear_env: bool = True
    """Start from an empty environment and pass through only allow-listed variables."""
    env_passthrough: list[str] = Field(default_factory=list)
    """Extra env var names passed through when ``clear_env`` is on."""

    @model_validator(mode="after")
    def _unused_without_sandbox(self) -> InnerSandbox:
        if self.type != "none":
            return self
        unused = [
            name
            for name in ("hide_paths", "extra_writable_paths", "env_passthrough")
            if getattr(self, name)
        ]
        unused += [
            name
            for name in ("network", "clear_env")
            if name in self.model_fields_set
            and getattr(self, name) != type(self).model_fields[name].default
        ]
        if unused:
            raise ValueError(
                f"inner_sandbox {', '.join(unused)} has no effect with type: none (use type: bwrap)"
            )
        return self


class SubagentDef(_Strict):
    """A Claude Code custom subagent (written to ``~/.claude/agents/<name>.md``)."""

    description: str
    """When the orchestrator should delegate to this subagent."""
    prompt: str
    tools: list[str] | None = None
    """Claude Code tool names the subagent may use; ``None`` inherits all tools."""


class Subagents(_Strict):
    """Workers the orchestrating agent delegates to, served by their own model.

    - ``runner: claude_code``: Claude Code's own subagents (``agents``) run on ``model``.
    - ``runner: opencode``: the orchestrator delegates by running ``opencode-worker``,
      an OpenCode process (pinned ``opencode_version``) that runs on ``model``.

    Both route through the orchestrator's Inspect model bridge, so the worker model's
    key stays on the host and its calls are logged and priced separately.
    """

    runner: Literal["claude_code", "opencode"]
    model: str
    """Model profile name (``config/models``) that serves the workers."""
    agents: dict[str, SubagentDef] = Field(default_factory=dict)
    opencode_version: str = "1.19.3"
    instructions: str | None = None
    """Appended to the orchestrator's system prompt (default: a short delegation note)."""

    @model_validator(mode="after")
    def _check(self) -> Subagents:
        for name in self.agents:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
                raise ValueError(f"subagent name {name!r}: use lowercase letters, digits, '-'")
        if self.runner == "opencode" and self.agents:
            raise ValueError("subagents.agents is only used with runner: claude_code")
        if self.runner == "claude_code" and "opencode_version" in self.model_fields_set:
            raise ValueError("subagents.opencode_version is only used with runner: opencode")
        return self


class HarnessProfile(_Named):
    description: str = ""
    agent: Literal["opencode", "claude_code"]
    version: str = "stable"
    """Pinned agent version (e.g. ``1.19.3``) or ``stable``/``latest``."""
    agent_model: str | None = None
    """Override for the model id handed to the CLI (see ``ModelProfile.agent_models``)."""
    inner_sandbox: InnerSandbox = Field(default_factory=InnerSandbox)
    env: dict[str, str] = Field(default_factory=dict)
    system_prompt: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    """Extra keyword arguments for the underlying inspect_swe agent factory."""
    subagents: Subagents | None = None
    """Workers the agent delegates to (``claude_code`` only)."""

    @model_validator(mode="after")
    def _subagents_need_orchestrator(self) -> HarnessProfile:
        if self.subagents is not None and self.agent != "claude_code":
            raise ValueError("subagents are only supported for agent: claude_code")
        return self


# --------------------------------------------------------------------------- environments


class ProbeSettings(_Strict):
    internet_target: str = "1.1.1.1:443"
    """host:port used by the outbound-internet containment check (TCP connect only)."""
    dns_name: str = "example.com"
    host_ports: list[int] = Field(default_factory=lambda: [22, 5000, 7000])
    """Ports tried on the default gateway (the VM host) by the host-reachability check.

    5000/7000 are macOS AirPlay receiver listeners, open by default on most Macs.
    """


_SIZE = re.compile(r"(\d+)([kmgt]?)b?", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def size_bytes(size: str) -> int:
    """Bytes in a size string such as ``4g``, ``512m`` or ``4096MB`` (binary units)."""
    match = _SIZE.fullmatch(size.strip())
    if match is None:
        raise ValueError(f"{size!r} is not a size: use a number with a unit, e.g. 4g or 512m")
    return int(match[1]) * _SIZE_UNITS[match[2].lower()]


def size_mb(size: str) -> int:
    """``size`` in whole MiB (what ``tart set --memory`` takes)."""
    return size_bytes(size) // 1024**2


def _memory_size(size: str) -> str:
    if size_bytes(size) < 512 * 1024**2:
        raise ValueError(f"memory {size!r} is below the 512m minimum")
    return size


MemorySize = Annotated[str, AfterValidator(_memory_size)]
"""A size string such as ``4g`` or ``512m`` (binary units, at least 512m)."""


class _ProviderSettings(_Strict):
    """Base of the provider blocks: rejects keys that were renamed, naming the new one."""

    _renamed: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _reject_renamed(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for old, new in cls._renamed.items():
                if old in data:
                    raise ValueError(f"'{old}' was renamed: use {new}")
        return data


class TartSettings(_ProviderSettings):
    _renamed: ClassVar[dict[str, str]] = {
        "cpu": "cpus",
        "memory_mb": "memory (a size such as 4g; 4096 MB = 4g)",
    }

    base_image: str = "ghcr.io/cirruslabs/ubuntu:24.04"
    image: str = "agenteval-ubuntu-24.04"
    """Local Tart VM built by ``agenteval env build``; cloned for every sample."""
    cpus: int = Field(default=4, ge=1)
    """vCPUs per VM."""
    memory: MemorySize = "4g"
    """Memory per VM (``tart set --memory`` in MiB)."""
    network: Literal["nat", "softnet"] = "nat"
    ssh_user: str = "admin"
    ssh_key: str = "~/.agenteval/ssh/id_ed25519"
    bootstrap_password: str = Field(default="admin", exclude=True, repr=False)
    """Default password of the public Cirrus Labs image; used once during image build.

    Excluded from dumps so it never reaches task metadata or eval logs.
    """
    boot_timeout: int = Field(default=180, ge=10)
    use_sudo: bool = True


class DockerSettings(_ProviderSettings):
    image: str = "agenteval/ubuntu-24.04:latest"
    runtime: str | None = None
    """Alternative OCI runtime, e.g. ``runsc`` (gVisor) or ``io.containerd.kata.v2`` (Kata)."""
    security_opt: list[str] = Field(default_factory=list)
    """security_opt for every container of this environment (stock Docker defaults if empty)."""
    bwrap_security_opt: list[str] = Field(
        default_factory=lambda: [
            "seccomp=unconfined",
            "apparmor=unconfined",
            "systempaths=unconfined",
        ]
    )
    """Extra relaxations added only for cells whose harness uses the bwrap inner sandbox.

    bwrap creates user namespaces (blocked by Docker's default seccomp profile) and mounts
    a fresh /proc (refused while Docker masks /proc paths). Cost: inside those containers,
    root can write global /proc/sys entries (e.g. kernel.core_pattern) and
    /proc/sysrq-trigger, and every user can create user namespaces. That is why scorers
    never run agent-written code as root, and why a gVisor (``runsc``) runtime is the
    better choice for bwrap cells on a shared Docker host.
    """
    memory: MemorySize | None = "4g"
    """Container memory limit (``mem_limit``); ``None`` = no limit."""
    cpus: float | None = Field(default=None, gt=0)
    """CPU limit in cores, fractions allowed (e.g. 1.5); ``None`` = no limit."""
    pids_limit: int | None = Field(default=4096, ge=64)
    """Per-container process limit, so one sample cannot exhaust the Docker VM."""


class SbxSettings(_ProviderSettings):
    """Docker Sandboxes (``sbx``): one microVM per sample, from a Docker image loaded as a
    sandbox template by ``agenteval env build``."""

    image: str = "agenteval/ubuntu-24.04:latest"
    """Docker image built from ``provision/Dockerfile`` (the one docker-ubuntu uses)."""
    template: str = "agenteval/ubuntu-24.04-sbx:latest"
    """What sandboxes start from: ``image`` plus the label that gives root in the sandbox
    its full capabilities (the egress rule needs CAP_NET_ADMIN), loaded with
    ``sbx template load``."""
    cpus: int | None = Field(default=None, ge=1)
    """vCPUs per sandbox (None: sbx's default, every host CPU)."""
    memory: MemorySize = "4g"
    """Memory per sandbox (``sbx create --memory``)."""
    create_timeout: int = Field(default=300, ge=10)


class EnvironmentProfile(_Named):
    description: str = ""
    provider: Literal["tart", "docker", "sbx"]
    agent_user: str = "agent"
    workspace: str = "/workspace"
    egress: Literal["allow", "deny"] = "deny"
    """Outbound network policy for the agent user (enforced by the environment, not the harness)."""
    ambient_secrets: bool = True
    """Plant fake credentials (canaries) the way a developer machine would have real ones."""
    max_concurrent: int = Field(default=2, ge=1)
    probe: ProbeSettings = Field(default_factory=ProbeSettings)
    tart: TartSettings | None = None
    docker: DockerSettings | None = None
    sbx: SbxSettings | None = None

    @model_validator(mode="after")
    def _provider_settings(self) -> EnvironmentProfile:
        for other in ("tart", "docker", "sbx"):
            if other != self.provider and getattr(self, other) is not None:
                raise ValueError(f"{other}: settings have no effect with provider: {self.provider}")
        if self.provider == "tart" and self.tart is None:
            object.__setattr__(self, "tart", TartSettings())
        if self.provider == "docker" and self.docker is None:
            object.__setattr__(self, "docker", DockerSettings())
        if self.provider == "sbx" and self.sbx is None:
            object.__setattr__(self, "sbx", SbxSettings())
        return self


# --------------------------------------------------------------------------- runs

SuiteName = Literal["containment", "capability", "injection"]


class SuiteSelection(_Strict):
    name: SuiteName
    options: dict[str, Any] = Field(default_factory=dict)


class Limits(_Strict):
    time_limit: int | None = 900
    working_limit: int | None = None
    token_limit: int | None = None
    message_limit: int | None = None
    cost_limit: float | None = None


class RunConfig(_Strict):
    name: str
    description: str = ""
    models: list[str]
    harnesses: list[str]
    environments: list[str]
    suites: list[SuiteSelection]
    epochs: int = Field(default=1, ge=1)
    max_samples: int | None = Field(default=None, ge=1)
    """Concurrent samples per task (defaults to the environment's max_concurrent)."""
    max_tasks: int = Field(default=1, ge=1)
    subagent_model: str | None = None
    """Model profile that serves the subagents of every harness that has them,
    replacing each harness's ``subagents.model`` (e.g. a mock model for smoke runs)."""
    limits: Limits = Field(default_factory=Limits)
    retry_attempts: int = Field(default=1, ge=0)
    fail_on_error: float = Field(default=0.5, ge=0)
    log_dir: str = "logs"
    report_dir: str = "reports"
    resource_interval: float = Field(default=2.0, ge=0)
    """Seconds between host CPU/memory readings for the report's Resources section; 0 = off."""

    @model_validator(mode="before")
    @classmethod
    def _suites_as_strings(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("suites"), list):
            data = dict(data)
            data["suites"] = [{"name": s} if isinstance(s, str) else s for s in data["suites"]]
        return data


# --------------------------------------------------------------------------- loading


class ProfileSet(BaseModel):
    models: dict[str, ModelProfile]
    harnesses: dict[str, HarnessProfile]
    environments: dict[str, EnvironmentProfile]


class MatrixCell(_Strict):
    """One task in the run: a suite executed for one model/harness/environment combo."""

    run: str
    suite: SuiteSelection
    model: ModelProfile
    harness: HarnessProfile
    environment: EnvironmentProfile
    limits: Limits
    epochs: int = 1
    subagent_model: ModelProfile | None = None
    """Resolved ``harness.subagents.model``."""

    @property
    def task_name(self) -> str:
        return f"{self.suite.name}--{self.harness.name}--{self.environment.name}--{self.model.name}"

    def metadata(self) -> dict[str, Any]:
        """Stored on the Inspect task so reports can regroup results by axis."""
        return {
            "agenteval": {
                "run": self.run,
                "suite": self.suite.model_dump(),
                "model": self.model.model_dump(),
                "harness": self.harness.model_dump(),
                "environment": self.environment.model_dump(),
                "subagent_model": (
                    self.subagent_model.model_dump() if self.subagent_model else None
                ),
            }
        }


class ConfigError(ValueError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    data.setdefault("name", path.stem)
    return data


def yaml_files(directory: Path) -> list[Path]:
    """The profile or run files in ``directory``; ``_``-prefixed ones (templates) are skipped."""
    if not directory.is_dir():
        return []
    paths = [*directory.glob("*.yaml"), *directory.glob("*.yml")]
    return sorted(p for p in paths if not p.name.startswith("_"))


EXTENDS = "extends"
"""Profile key naming another profile of the same kind that this one starts from."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` on top of ``base``: mappings merge key by key, anything else replaces."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_extends(raw: dict[str, tuple[Path, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Each profile's data with its ``extends`` chain merged in (``extends`` removed)."""
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str, chain: tuple[str, ...]) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        path, data = raw[name]
        parent = data.get(EXTENDS)
        own = {k: v for k, v in data.items() if k != EXTENDS}
        if parent is None:
            resolved[name] = own
            return own
        if not isinstance(parent, str) or not parent:
            raise ConfigError(f"{path}: extends must be a profile name")
        if parent in chain:
            cycle = " -> ".join([*chain[chain.index(parent) :], parent])
            raise ConfigError(f"{path}: extends cycle {cycle}")
        if parent not in raw:
            raise ConfigError(
                f"{path}: extends unknown profile '{parent}'; available: {sorted(raw)}"
            )
        base = {k: v for k, v in resolve(parent, (*chain, parent)).items() if k != "name"}
        resolved[name] = deep_merge(base, own)
        return resolved[name]

    for name in raw:
        resolve(name, (name,))
    return resolved


def _load_dir[T: _Named](directory: Path, model: type[T]) -> dict[str, T]:
    raw: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in yaml_files(directory):
        data = _read_yaml(path)
        name = data["name"]
        if not isinstance(name, str):
            raise ConfigError(f"{path}: name must be a string")
        if name in raw:
            raise ConfigError(f"{path}: duplicate profile name '{name}'")
        raw[name] = (path, data)
    resolved = _resolve_extends(raw)
    profiles: dict[str, T] = {}
    for name, (path, _) in raw.items():  # file order, not resolution order
        try:
            profiles[name] = model.model_validate(resolved[name])
        except Exception as ex:
            raise ConfigError(f"{path}: {ex}") from ex
    return profiles


def load_profiles(config_dir: Path = DEFAULT_CONFIG_DIR) -> ProfileSet:
    return ProfileSet(
        models=_load_dir(config_dir / "models", ModelProfile),
        harnesses=_load_dir(config_dir / "harnesses", HarnessProfile),
        environments=_load_dir(config_dir / "environments", EnvironmentProfile),
    )


def load_run(path: Path) -> RunConfig:
    try:
        return RunConfig.model_validate(_read_yaml(path))
    except ConfigError:
        raise
    except Exception as ex:
        raise ConfigError(f"{path}: {ex}") from ex


SCHEMA_KINDS: dict[str, type[BaseModel]] = {
    "model": ModelProfile,
    "harness": HarnessProfile,
    "environment": EnvironmentProfile,
    "run": RunConfig,
}
"""Profile kind -> its model; the schema of each is ``config/schema/<kind>.schema.json``."""


def config_schema(kind: str) -> dict[str, Any]:
    """JSON Schema of one profile kind, for editor completion in the YAML files.

    ``name`` is optional (it defaults to the file stem), profiles may set ``extends`` (and
    then leave required keys to the parent), and run ``suites`` may be bare names.
    """
    schema = SCHEMA_KINDS[kind].model_json_schema()
    schema["required"] = [r for r in schema.get("required", []) if r != "name"]
    if kind != "run":
        schema["properties"] = {
            EXTENDS: {
                "description": (
                    f"Name of another {kind} profile this one starts from: its settings are "
                    "merged under this file's (mappings key by key, lists and other values "
                    "replaced). Chains are allowed; cycles are rejected."
                ),
                "title": "Extends",
                "type": "string",
            },
            **schema["properties"],
        }
        # a profile that extends another may leave the required keys to its parent
        required = schema.pop("required")
        if required:
            schema["if"] = {"required": [EXTENDS]}
            schema["else"] = {"required": required}
    if kind == "run":
        names = {"enum": list(get_args(SuiteName)), "type": "string"}
        items = schema["properties"]["suites"]["items"]
        schema["properties"]["suites"]["items"] = {"anyOf": [names, items]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def write_config_schemas(schema_dir: Path) -> list[Path]:
    """Write every kind's schema to ``schema_dir`` (regenerate: scripts/gen_config_schema.py)."""
    schema_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind in SCHEMA_KINDS:
        path = schema_dir / f"{kind}.schema.json"
        path.write_text(json.dumps(config_schema(kind), indent=2) + "\n")
        paths.append(path)
    return paths


def expand_matrix(run: RunConfig, profiles: ProfileSet) -> list[MatrixCell]:
    def pick[T](kind: str, names: list[str], pool: dict[str, T]) -> list[T]:
        missing = [n for n in names if n not in pool]
        if missing:
            raise ConfigError(
                f"run '{run.name}': unknown {kind} {missing}; available: {sorted(pool)}"
            )
        return [pool[n] for n in names]

    models = pick("models", run.models, profiles.models)
    harnesses = pick("harnesses", run.harnesses, profiles.harnesses)
    environments = pick("environments", run.environments, profiles.environments)
    workers = {
        h.name: pick(
            f"subagent model of harness '{h.name}'",
            [run.subagent_model or h.subagents.model],
            profiles.models,
        )
        for h in harnesses
        if h.subagents is not None
    }
    subscription = sorted(
        {m.name for m in [*models, *(w[0] for w in workers.values())] if m.access != "bridge"}
    )
    if subscription:
        raise ConfigError(
            f"run '{run.name}': models {subscription} use a Claude subscription, which "
            "works only in `agenteval dev` (eval runs need calls through Inspect)"
        )
    return [
        MatrixCell(
            run=run.name,
            suite=suite,
            model=model,
            harness=harness,
            environment=environment,
            limits=run.limits,
            epochs=run.epochs,
            subagent_model=workers[harness.name][0] if harness.name in workers else None,
        )
        for suite in run.suites
        for model in models
        for harness in harnesses
        for environment in environments
    ]


def missing_env(models: list[ModelProfile]) -> list[str]:
    return sorted({v for m in models for v in m.required_env if not os.environ.get(v)})
