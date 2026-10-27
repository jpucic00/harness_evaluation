# Extending agenteval

Most extensions are a new YAML file. Some need code.

| You want to add | YAML only? | Code to touch |
| --- | --- | --- |
| A model (another OpenRouter model, an internal gateway, Bedrock, ...) | yes | none |
| Another version or inner-sandbox variant of OpenCode | yes | none |
| A new agent CLI (Codex CLI, ...) | no | `config.py`, `harnesses/`, `sandbox_exec.py`, `suites/monitors.py`, `dev/` |
| A Docker variant (other runtime, limits, `security_opt`, `bwrap_security_opt`) | yes | none, but the runtime must be installed in Docker |
| A new sandbox provider (another VM technology) | no | new provider module, `_registry.py`, `config.py`, `environments/__init__.py`, `runner.py`, `dev/task.py` |
| A capability task | data files | `tests/test_suites.py` (task count and list) |
| A new suite | no | `suites/`, `config.py` (`SuiteName`), optionally `report/` |
| A run file | yes | none |

All profiles live under `config/<kind>/<name>.yaml`. The profile name is the file name unless
the file sets `name:`. Unknown keys are rejected, so a typo fails loudly, and so are settings
that would have no effect (another provider's settings block, `inner_sandbox` options with
`type: none`, `subagents.opencode_version` with `runner: claude_code`) and keys that were
renamed (the error names the new key).

A profile can start from another profile of the same kind with `extends: <profile name>`
(see [the bwrap harnesses](#variants-with-extends)). The other profile's settings are merged
under the file's own: mappings key by key, lists and other values replaced. Chains are
allowed, cycles and unknown names are rejected, and `name` is never inherited. `extends`
itself is not part of the loaded profile, so it never shows up in task metadata or reports.
Run files have no `extends`.

Start a new profile or run file from the kind's `_template.yaml`: it lists every key with its
default and allowed values. Files whose name starts with `_` are never loaded or listed. The
first line of every file, `# yaml-language-server: $schema=../schema/<kind>.schema.json`, points
editors with the YAML language server (VS Code's YAML extension) at the JSON Schema in
`config/schema/`, for completion and validation as you type. After changing a model in
[config.py](../src/agenteval/config.py), regenerate the schemas with
`uv run python scripts/gen_config_schema.py` (a test fails while they are stale).

After any change:

```sh
uv run agenteval profiles                     # does it parse, and does it show up?
uv run agenteval run <run> --dry-run          # is the matrix what you expect?
uv run agenteval doctor --env <environment>   # tools, images, env vars
```

## Model profiles

`config/models/<name>.yaml`:

| Field | Default | Meaning |
| --- | --- | --- |
| `description` | `""` | Shown by `agenteval profiles`. |
| `access` | `bridge` | `bridge`: Inspect calls `inspect_model` for the agent. `subscription`: Claude Code calls Anthropic itself on the user's Claude plan (`agenteval dev` with a `claude_code` harness only); the profile then sets `agent_models.claude_code` and no `inspect_model`, `model_args`, `connection` or `pricing` ([dev.md](dev.md#claude-subscription)). |
| `inspect_model` | required (bridge) | Inspect model id: `provider/model`, e.g. `openrouter/qwen/qwen3.8-27b`, `mockllm/model`. Any [Inspect provider](https://inspect.aisi.org.uk/providers.html) works. |
| `model_args` | `{}` | Extra keyword arguments for Inspect's `get_model()`. They are provider-specific, e.g. OpenRouter's `provider:` routing preferences. |
| `connection` | none | How the host reaches the provider. OpenAI-compatible providers only (e.g. `openai-api/...`, `openrouter/...`, `openai/...`). One key, `proxy`: `environment` (the default) uses `HTTP(S)_PROXY`, `ALL_PROXY` and the OS proxy settings as usual; `direct` ignores them, for a gateway reached over a VPN that drops proxied calls. Setting `connection` gives the profile its own HTTP client ([models.py](../src/agenteval/models.py)), which trusts what Inspect's own client trusts (`SSL_CERT_FILE`, else `SSL_CERT_DIR`, else the OS trust store) and a client certificate from `MTLS_CERT_FILE` / `MTLS_KEY_FILE` / `MTLS_KEY_PASSWORD`, or from a `.p12`/`.pfx` in `MTLS_PKCS12_FILE` / `MTLS_PKCS12_PASSWORD` (needs `uv sync --extra mtls`). Without it, Inspect's default client is used. |
| `pricing` | none | USD per 1M tokens: `input`, `output`, optional `input_cache_read`, `input_cache_write`. Unset cache prices mean no cache discount: those tokens are charged at the `input` rate. Without `pricing`, cost is reported as `unknown`. |
| `required_env` | `[]` | Host env vars that must be set before a run starts. They are checked by name; values are never printed. |
| `agent_models` | `opencode: openrouter/qwen/qwen3.8-27b` (bridge profiles) | Model id handed to each agent CLI, keyed by harness `agent`. It only selects the CLI's request format and prompt template; OpenCode gets a placeholder key for its provider (`OPENROUTER_API_KEY=sk-none`). The model that answers is always `inspect_model` (see [architecture.md](architecture.md#model-calls-the-bridge)), except with `access: subscription`, where `claude_code` is the model Claude Code asks Anthropic for. The OpenCode id must be in OpenCode's bundled catalog (the harnesses turn off fetching models); an unknown id runs as a custom model with no known limits (context 0, output 0, reasoning off). A bridge profile without an `opencode` entry gets the default (`DEFAULT_AGENT_MODELS` in [config.py](../src/agenteval/config.py)): a catalog model with OpenRouter's OpenAI-style chat format and OpenCode's generic prompt. Set the entry only for a model OpenCode should treat differently, e.g. `anthropic/claude-sonnet-4-5` (Anthropic's format) in the Claude profiles, or the model's own catalog id. |

### Example: the internal OpenAI-compatible gateway instead of OpenRouter

Inspect's `openai-api` provider talks to any OpenAI-compatible endpoint. For
`openai-api/<service>/<model>` it reads `<SERVICE>_API_KEY` and `<SERVICE>_BASE_URL` from the
environment:

```yaml
# config/models/gateway-gpt-oss-120b.yaml
description: gpt-oss-120b on the internal OpenAI-compatible gateway
inspect_model: openai-api/gateway/gpt-oss-120b   # the part after "gateway/" is sent as the model name
required_env: [GATEWAY_API_KEY, GATEWAY_BASE_URL]
connection:
  proxy: direct   # reached over the VPN; the gateway drops calls sent through a proxy
pricing:   # internal rates in USD per 1M tokens; omit to report cost as "unknown"
  input: 0
  output: 0
```

Without `agent_models`, OpenCode runs this model with the default OpenAI-style id. To have
OpenCode treat it as gpt-oss (its limits and prompt), add `agent_models: {opencode:
openrouter/openai/gpt-oss-120b}`, after a one-sample run confirms that OpenCode's catalog has
that id. Nothing goes to OpenRouter either way: the bridge intercepts the call.

```sh
export GATEWAY_BASE_URL=https://<gateway-host>/v1
read -rs GATEWAY_API_KEY && export GATEWAY_API_KEY
uv run python scripts/check_model.py gateway-gpt-oss-120b   # one call from the host, no environment
uv run agenteval run opencode-qwen-tart --model gateway-gpt-oss-120b --limit 1
```

Use the model name exactly as the gateway expects it. `check_model.py` shows which network
step fails when the call does not get through (see
[setup.md](setup.md#8-doctor-and-first-runs)). With the gateway, prompts and code stay
on the internal network. The other downloads listed under
[data egress](setup.md#data-egress) still happen (agent binaries, base images).

The shipped `gateway-*` profiles follow this pattern. Claude uses Inspect's Anthropic
provider: `anthropic/<model>` with `ANTHROPIC_API_KEY` (`claude-sonnet-api`), or
`anthropic/bedrock/<inference-profile-id>` with AWS credentials in the host shell
(`claude-sonnet-bedrock`).

## Harness profiles

`config/harnesses/<name>.yaml`:

| Field | Default | Meaning |
| --- | --- | --- |
| `description` | `""` | |
| `agent` | required | Agent CLI: `opencode` or `claude_code` (see [below](#claude-code-harnesses-and-subagents)). |
| `version` | `stable` | Pinned release (e.g. `"1.19.3"`), `stable` or `latest`. Pin it for comparable results. |
| `agent_model` | none | Overrides the model profile's `agent_models` entry for this harness. |
| `inner_sandbox.type` | `none` | `none`: plain `exec` (the other `inner_sandbox` keys must then stay unset). `bwrap`: bubblewrap profile (below). |
| `inner_sandbox.hide_paths` | `[]` | Paths replaced by an empty tmpfs (directories) or `/dev/null` (files). `~` = the agent's home. Inspect's sandbox-tools dir and `/var/lib/agenteval` are always hidden too, and `/run` is always a private tmpfs. |
| `inner_sandbox.extra_writable_paths` | `[]` | Writable in addition to the workspace and the harness state dirs (`~/.local/share/opencode`, `~/.local/state/opencode`, `~/.cache/opencode`, `~/.config/opencode`, `~/.claude`). |
| `inner_sandbox.network` | `proxy-only` | `proxy-only`: own network namespace, only the model bridge is reachable (through a read-only socket dir). `host`: share the environment's network; the resolver file below `/run` is re-exposed read-only so DNS works. |
| `inner_sandbox.clear_env` | `true` | Start from an empty environment and pass only `PATH HOME USER LOGNAME LANG LC_ALL TERM TZ IS_SANDBOX`, `OPENCODE_*`, `ANTHROPIC_*`, `OPENAI_*`, `GOOGLE_*`, `OPENROUTER_*`, `CLAUDE_*`, `MCP_*`, and `env_passthrough`. |
| `inner_sandbox.env_passthrough` | `[]` | Extra variable names passed through `clear_env`. `agenteval dev` adds `COLORTERM`, `TERM_PROGRAM` and `TERM_PROGRAM_VERSION`. |
| `env` | `{}` | Environment variables for the agent CLI. |
| `system_prompt` | none | Prepended to any suite system prompt (e.g. CodeIPI's). In `agenteval dev`, Claude Code gets it with `--append-system-prompt`; OpenCode ignores it (its TUI has no flag for it). |
| `options` | `{}` | Extra keyword arguments for `inspect_swe.opencode()` or `inspect_swe.claude_code()`. `version`, `user`, `env` and `sandbox` are reserved; for Claude Code also `model`, `subagent_model` and `model_aliases`. `agenteval dev` does not use them. |

Examples:

```yaml
# config/harnesses/opencode-bwrap-hostnet.yaml: how much does the proxy-only network matter?
description: OpenCode in bubblewrap, but sharing the environment's network
agent: opencode
version: "1.19.3"
inner_sandbox:
  type: bwrap
  network: host
  clear_env: true
  hide_paths: [~/.ssh, ~/.aws, ~/.config/gcloud, ~/.netrc, /etc/agenteval/secrets, /root]
env:
  OPENCODE_DISABLE_AUTOUPDATE: "1"
  OPENCODE_DISABLE_MODELS_FETCH: "1"
  OPENCODE_DISABLE_DEFAULT_PLUGINS: "1"
  npm_config_offline: "true"
```

For another OpenCode release, copy a profile and change `version`. Keep the `OPENCODE_DISABLE_*`
variables and `npm_config_offline`: they stop OpenCode from updating itself, fetching model
lists, loading plugins or installing its plugin package from npm. Otherwise the harness's own
network traffic would be counted as an agent egress attempt.

### Variants with `extends`

Each shipped `*-bwrap` harness is its `*-none` twin plus the bubblewrap inner sandbox:

```yaml
# config/harnesses/opencode-bwrap.yaml
extends: opencode-none
description: OpenCode inside a bubblewrap sandbox (...)
inner_sandbox:
  type: bwrap
  network: proxy-only
  clear_env: true
  hide_paths: [~/.ssh, ~/.aws, ~/.config/gcloud, ~/.netrc, /etc/agenteval/secrets, /root]
```

`agent`, `version`, `env` and `subagents` come from `opencode-none`; the `inner_sandbox`
mapping is merged into the twin's `{type: none}`. A list is replaced, not appended to: to
hide one more path, repeat the whole `hide_paths` list. Change shared settings (a new
`version`, the subagents) in the `-none` file and both variants follow. A new variant, for
example `opencode-bwrap-hostnet`, can extend `opencode-bwrap` and set only
`inner_sandbox: {network: host}`.

The agent should run as an unprivileged user. Run as root, bubblewrap is a weaker boundary and
`sandbox-exec` prints a warning (see [Known limitations](security-tests.md#known-limitations)).

### Claude Code harnesses and subagents

`agent: claude_code` runs Claude Code through `inspect_swe.claude_code`
([harnesses/claude_code.py](../src/agenteval/harnesses/claude_code.py)). The pinned binary
(`version`) gets a wrapper at `/usr/local/bin/claude` that runs it through `sandbox-exec` and
sets `CLAUDE_CONFIG_DIR=~/.claude`, so all of Claude Code's state lives in one writable state
dir. The model axis is the **orchestrator**: every Claude Code call goes to the run's model.

`subagents` adds workers served by a second model profile:

| Field | Default | Meaning |
| --- | --- | --- |
| `runner` | required | `claude_code`: Claude Code's own subagents (Agent tool). `opencode`: OpenCode worker processes the orchestrator starts with `opencode-worker "<instructions>"`. |
| `model` | required | Model profile serving the workers, e.g. `gateway-qwen`. A run file's `subagent_model:` overrides it for every harness (the smoke run uses `mock-worker`); `agenteval dev --subagent-model` overrides it for one session. |
| `agents` | `{}` | `runner: claude_code` only. Custom subagents `name: {description, prompt, tools}`, written to `~/.claude/agents/<name>.md` before the run (`agenteval dev` passes them with `--agents` instead). Without any, Claude Code's built-in general-purpose subagent runs on the worker model. |
| `opencode_version` | `1.19.3` | `runner: opencode` only. Pinned OpenCode release for the workers. |
| `instructions` | a short delegation note | Appended to the orchestrator's system prompt. |

```yaml
# config/harnesses/claude-code-opencode-none.yaml (shortened)
agent: claude_code
version: "2.1.292"
inner_sandbox: {type: none}
subagents:
  runner: opencode
  model: gateway-qwen
```

```sh
uv run agenteval run smoke-claude-docker      # all six claude-code-* harnesses, mock models, offline
uv run agenteval run opencode-qwen-docker --model claude-sonnet-bedrock \
  --harness claude-code-opencode-bwrap --suite capability --limit 1
```

How the routing works is described in
[architecture.md](architecture.md#model-calls-the-bridge). The monitor ignores Claude Code's
runtime state (`~/.claude/projects`, `.claude.json`, backups, ...) but treats
`~/.claude/settings.json`, `agents/`, `commands/`, `skills/`, `hooks/` and `CLAUDE.md` as
persistence targets. `settings.json` is pre-seeded before the baseline with the content
inspect_swe writes at agent start, so only the agent's own changes count.

### Adding another agent type (e.g. `codex_cli`)

Mirror [harnesses/claude_code.py](../src/agenteval/harnesses/claude_code.py):

1. **Schema.** Add the name to `HarnessProfile.agent` in [config.py](../src/agenteval/config.py).
2. **Installer and runner.** `install_agent` installs the pinned binary with
   `ensure_agent_binary_installed(<source>, version, user, sandbox)` from
   `inspect_swe._util.agentbinary` (e.g. `codex_cli_binary_source()` from
   `inspect_swe._codex_cli.agentbinary`; private modules pinned by `uv.lock`, re-check on
   upgrade) and puts a root-owned `sandbox-exec` wrapper where the CLI's `which` lookup finds
   it first. `run_agent` calls the inspect_swe factory with `version="sandbox"`.
3. **Dispatch.** Return the solvers from `harness_solvers` in
   [harnesses/\_\_init\_\_.py](../src/agenteval/harnesses/__init__.py).
4. **Inner sandbox.** Add the CLI's state dirs to `STATE_DIRS` and its env prefixes to
   `ENV_PREFIXES` in [sandbox_exec.py](../src/agenteval/harnesses/sandbox_exec.py). A state
   *file* directly in the home directory needs a writable parent (Claude Code's is moved into
   `~/.claude` with `CLAUDE_CONFIG_DIR`).
5. **Monitor.** Add files the CLI writes on every start to `DEFAULT_IGNORE` in
   [suites/monitors.py](../src/agenteval/suites/monitors.py), and config it auto-loads to
   the `harness_config` watch group in [cases/monitor/harness_config.yaml](../cases/monitor/harness_config.yaml).
6. **Dev mode.** `agenteval dev` has its own per-agent code. Add the name to `AgentName` in
   [dev/session.py](../src/agenteval/dev/session.py). In
   [dev/task.py](../src/agenteval/dev/task.py), install the CLI in `install_cli` (it must
   return the real binary, not the `/usr/local/bin` wrapper) and add a launch function like
   `opencode_launch` (environment and arguments of the interactive CLI), called from
   `dev_session`. In [dev/userconfig.py](../src/agenteval/dev/userconfig.py), add the CLI's
   host config folder (`host_config_dir`) and the files copied in and back.
7. **Verify.** Unit tests (see `tests/test_harness_claude_code.py`), then a mock smoke run:
   containment must still work, and the capability samples must show no monitor violations.

## Environment profiles

`config/environments/<name>.yaml`:

| Field | Default | Meaning |
| --- | --- | --- |
| `description` | `""` | |
| `provider` | required | `tart`, `docker` or `sbx`. Only this provider's settings block may be set. |
| `agent_user` | `agent` | Unprivileged user the agent runs as (`provision.sh` refuses `root`). Passed to `provision.sh` when the image is built (Docker build argument; over SSH for Tart). Rebuild the image after changing it (`--force` for Tart). |
| `workspace` | `/workspace` | The agent's working directory, created by `provision.sh` like `agent_user`. The injection suite requires `/workspace`. |
| `egress` | `deny` | Outbound network for the agent user. Docker: `network_mode: none`. Tart: nftables rule on the agent's uid. `allow`: no restriction. |
| `ambient_secrets` | `true` | Plant fake credentials and canary env vars like a developer machine. `false` turns the secrets checks into `info`. |
| `max_concurrent` | `2` | Samples (VMs or containers) at once. A run uses the smallest value among its environments. |
| `probe.internet_target` | `1.1.1.1:443` | TCP target of `net.internet_tcp`. |
| `probe.dns_name` | `example.com` | Name resolved by `net.dns`. |
| `probe.host_ports` | `[22, 5000, 7000]` | Gateway ports tried by `net.host_gateway` (5000/7000: macOS AirPlay receiver). |
| `tart.base_image` | `ghcr.io/cirruslabs/ubuntu:24.04` | Public image the build starts from. |
| `tart.image` | `agenteval-ubuntu-24.04` | Local provisioned VM, cloned for every sample. |
| `tart.cpus`, `tart.memory` | `4`, `4g` | Per VM (`tart set --cpu`, and `--memory` in MiB). |
| `tart.network` | `nat` | `softnet` isolates the VM from the host but needs the privileged softnet helper (see [setup.md](setup.md#troubleshooting)). |
| `tart.ssh_user`, `tart.ssh_key` | `admin`, `~/.agenteval/ssh/id_ed25519` | Login used for setup and scoring (passwordless sudo). |
| `tart.bootstrap_password` | `admin` | The base image's public default password. Used once during the build; never logged. |
| `tart.boot_timeout` | `180` | Seconds to wait for an IP and SSH. |
| `tart.use_sudo` | `true` | Switch users with `sudo` (tests turn it off). |
| `docker.image` | `agenteval/ubuntu-24.04:latest` | Locally built image (never pulled). |
| `docker.runtime` | none | OCI runtime, e.g. `runsc` (gVisor) or `kata-runtime`. |
| `docker.security_opt` | `[]` (stock Docker) | `security_opt` for every container of this environment. |
| `docker.bwrap_security_opt` | `seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined` | Added only for cells whose harness uses the bwrap inner sandbox: the relaxations bubblewrap needs inside a container. They weaken it (trade-off explained in [setup.md](setup.md#troubleshooting)). |
| `docker.cpus`, `docker.memory` | none, `4g` | Container limits (`cpus` may be fractional, e.g. `1.5`); `null` = no limit. |
| `docker.pids_limit` | `4096` | Process limit per container (at least 64; `null` = none), so one sample cannot exhaust the Docker VM. |
| `sbx.image` | `agenteval/ubuntu-24.04:latest` | Docker image built from `provision/Dockerfile` (the one `docker-ubuntu` uses). |
| `sbx.template` | `agenteval/ubuntu-24.04-sbx:latest` | What sandboxes start from: `image` plus full root capabilities, loaded with `sbx template load`. |
| `sbx.cpus`, `sbx.memory` | none, `4g` | Per microVM (`null` cpus = sbx's default, every host CPU). |
| `sbx.create_timeout` | `300` | Seconds `sbx create` may take. |

Every provider sizes its sandbox with the same two keys in its own block: `cpus` (a whole
number; Docker also takes fractions) and `memory`, a size string with a binary unit (`4g`,
`512m`, `4096mb`) of at least `512m`. Images stay in the provider block too, because each
provider means something different by them (Tart: `base_image` and the built `image`; sbx:
`image` and the `template` made from it).


Build each new environment's image with `uv run agenteval env build <name>`. Environments that
share an image name share the build. Docker compose files are generated per cell under
`.agenteval/compose/<env>-<hash>.yaml`; the hash covers the file's content, so editing a
profile while a run is in progress never changes that run's containers.

`agenteval dev` ([dev.md](dev.md)) uses the same environment profiles, with these
differences: it plants no fake credentials whatever `ambient_secrets` says, runs one
environment per session, copies the repo to `workspace` (`--mount copy`) or mounts it at
its host path (`--mount live`), and turns on the bridge's web search only with
`egress: allow`.

### A different Docker runtime (gVisor, Kata)

```yaml
# config/environments/docker-gvisor.yaml
description: Docker container under gVisor (runsc)
provider: docker
egress: deny
max_concurrent: 4
docker:
  image: agenteval/ubuntu-24.04:latest
  runtime: runsc
  memory: 4g
```

- The runtime must be installed and registered with the Docker daemon. Check with
  `docker info --format '{{json .Runtimes}}'`. On Docker Desktop for Mac the daemon runs in
  Docker's own Linux VM, which makes adding runtimes awkward; a Linux Docker host is easier.
- For Kata, use the runtime name your install registers: current Kata ships only the
  containerd shim, `io.containerd.kata.v2`.
- bubblewrap may behave differently under another runtime. Run the containment suite first:
  `fs.workspace_write` and the column's errors tell you whether `opencode-bwrap` works there,
  and `info.kernel` shows which kernel the probe saw.
- bwrap cells still get `bwrap_security_opt`. gVisor runs the container on its own user-space
  kernel instead of the host's, which is why it is the recommended runtime for bwrap cells on a
  shared Docker host. `none` cells need no relaxation under any runtime.

### A new sandbox provider (another VM technology)

A provider is an Inspect `SandboxEnvironment`. Use the Tart provider (a VM reached over SSH,
[environments/tart/](../src/agenteval/environments/tart/), tests `tests/test_tart_*.py`) or the
Docker Sandboxes provider (a microVM reached through its CLI's exec,
[environments/sbx/](../src/agenteval/environments/sbx/), tests `tests/test_sbx.py`) as the
template; both test against fake CLIs. File transfer and timeouts through `exec` are shared in
[environments/guest.py](../src/agenteval/environments/guest.py).

1. Implement `@sandboxenv(name="myvm") class MyVMSandboxEnvironment(SandboxEnvironment)` with
   `task_init`, `sample_init`, `sample_cleanup`, `task_cleanup`, `cli_cleanup`, `exec`,
   `write_file`, `read_file`, `config_deserialize` and `default_concurrency`. Follow Inspect's
   [sandbox extension guide](https://inspect.aisi.org.uk/extensions.html#sandboxes) and the
   error semantics of its built-in Docker provider (timeouts, output limits, missing files).
2. Import it in [_registry.py](../src/agenteval/_registry.py), so the `inspect_ai` entry point
   registers it.
3. In [config.py](../src/agenteval/config.py), add the name to `EnvironmentProfile.provider`, a
   settings model (like `TartSettings`), and its default and the mismatch check in
   `_provider_settings`. Regenerate `config/schema/` (see above).
4. In [environments/\_\_init\_\_.py](../src/agenteval/environments/__init__.py), map the provider
   to a `SandboxEnvironmentSpec` in `sandbox_spec`. It also receives the cell's
   `inner_sandbox` type, for providers whose confinement must depend on it (as Docker's does),
   and `mounts`: host folders that `agenteval dev --mount live` shares at the same path (a
   bind mount for Docker, `tart run --dir` for Tart). Eval runs pass none.
5. In [runner.py](../src/agenteval/runner.py), extend `build_environment`, `doctor`,
   `cleanup_environments` and `_env_image`.
6. The guest must provide what
   [provision.sh](../src/agenteval/environments/provision/provision.sh) sets up: Python 3, bash,
   coreutils, sudo, bubblewrap and socat (for `bwrap` harnesses, with `/usr/bin/bwrap` owned by
   `root:agenteval-bwrap`, mode 0750), nftables (for `egress: deny`), git and pytest, the
   `agent` user, `/workspace`, `/opt/agenteval`, `/var/lib/agenteval` (root, 0700),
   `/etc/agenteval/secrets` and the collector name in `/etc/hosts`. Running `provision.sh`
   itself (with `AGENTEVAL_AGENT_USER` and `AGENTEVAL_WORKSPACE`) is the easiest way.
7. For `agenteval dev`, return a handle for the provider from `env_handle` in
   [dev/task.py](../src/agenteval/dev/task.py). It implements `EnvHandle`: attach the
   user's terminal, run a command, copy a file in and out, and print a shell command for
   recovery. If the guest must mount the live-mode shares itself, add that to
   `prepare_live` (Tart mounts its virtiofs shares there).

**The guest must be Linux.** The model bridge and inspect_swe's agent installers support only
Linux guests (x86_64 or arm64, glibc or musl): inspect_swe rejects other operating systems, and
Inspect's in-environment tools are Linux binaries. agenteval's own in-environment pieces are
Linux-specific too: bubblewrap, nftables, `/proc` and the Ubuntu provisioning. A macOS guest
(for example a Tart macOS VM) could therefore not run the agent suites as they are. Supporting
one would need a reworked containment suite (macOS equivalents of the inner sandbox and egress
rule) and upstream bridge support.

## Suites

A suite is a factory `def my_suite_task(cell: MatrixCell) -> Task`. `build_task` then applies
what every task shares: name, model, sandbox, limits, epochs and metadata.

1. Create `suites/my_suite.py`. Follow `capability_task` in
   [capability.py](../src/agenteval/suites/capability.py):

   ```python
   def my_suite_task(cell: MatrixCell) -> Task:
       options = MySuiteOptions.model_validate(cell.suite.options)  # pydantic, extra="forbid"
       return Task(
           dataset=my_dataset(options),
           setup=[
               prepare_sample(cell),
               my_setup(),
           ],  # root setup: canaries, collector, egress, baseline
           solver=harness_chain(cell),  # install + re-baseline + run the harness
           scorer=[  # monitor first, before any agent code runs
               boundary_monitor(timeout=monitor_timeout(cell.limits.time_limit)),
               my_scorer(),
           ],
       )
   ```

   Score effects from outside, through `sandbox()`. Do not trust files the agent could have
   edited (see how `tests_pass` restores pristine tests). Never run code the agent wrote as
   root: run it as the agent user, after `boundary_monitor`. Derive scorer timeouts from
   `time_limit`, because Inspect gives all scorers together half of it.
2. Register it in `SUITES` in [suites/\_\_init\_\_.py](../src/agenteval/suites/__init__.py), and
   add the name to `SuiteName` in [config.py](../src/agenteval/config.py). The CLI's `--suite`
   choices come from `SuiteName`.
3. Report. [report/scores.py](../src/agenteval/report/scores.py) recognises suites by scorer name
   (`containment_scorer`, `tests_pass`, `ipi_scorer`, `boundary_monitor`, `generated_vulns`). A new suite still
   appears in the Overview, Economics, Boundary monitor (when attached) and the per-sample
   drill-down. A dedicated section, or counting samples as resolved, needs a view in [report/views.py](../src/agenteval/report/views.py)
   and the template.

### Adding cases (capability, containment, monitor, injection)

New test cases are files under [cases/](../cases/), not code changes: copy the kind's
`_template` and follow [cases/README.md](../cases/README.md). The suites load every case
automatically, and `uv run pytest` validates it (for capability tasks: the reference cases
fail on the shipped repo and pass after `solution.patch` or `solution.sh`). Select cases in a run file with
the suite options `ids` and `limit`.

## Run files

`config/runs/<name>.yaml`:

| Field | Default | Meaning |
| --- | --- | --- |
| `description` | `""` | |
| `models`, `harnesses`, `environments` | required | Profile names; the run is their cross product. |
| `suites` | required | Suite names, or `{name, options}` mappings. |
| `epochs` | `1` | Repetitions of every sample. |
| `limits` | `time_limit: 900` | Per sample: `time_limit` and `working_limit` (seconds), `token_limit`, `message_limit`, `cost_limit` (USD). |
| `max_samples` | the environments' `max_concurrent` | Concurrent samples per task. |
| `max_tasks` | `1` | Tasks run in parallel. |
| `retry_attempts` | `1` | Retries of tasks that did not complete. Logs of retried attempts are kept; the report shows their spend as discarded-attempt usage. |
| `fail_on_error` | `0.5` | Fraction of errored samples at which a task fails (Inspect semantics). |
| `log_dir`, `report_dir` | `logs`, `reports` | Roots for `<run-id>/` directories. |
| `resource_interval` | `2` | Seconds between host CPU/memory readings for the report's Resources section; `0` turns sampling off. |

```yaml
# config/runs/gateway-vs-openrouter.yaml
description: Same harnesses on Tart, OpenRouter Qwen vs the internal gateway
models: [qwen38-27b, gateway-gpt-oss-120b]
harnesses: [opencode-none, opencode-bwrap]
environments: [tart-ubuntu]
suites:
  - containment
  - name: capability
    options:
      ids: [cap-01-pagination, cap-04-interval-merge, cap-06-expense-report]
  - name: injection
    options:
      vectors: [issue_text, code_comment]
      severities: [S1, S2]
      defense_prompt: true
epochs: 2
limits:
  time_limit: 900
  token_limit: 600000
  cost_limit: 1.0
```

Start with `--dry-run`, then `--limit 1`, and read the Economics section before the full run.

## Checking a change

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run agenteval run smoke-docker --run-id my-change-smoke   # offline end-to-end check
```
