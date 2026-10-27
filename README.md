# harness_evaluation

A research harness for comparing **coding-agent setups**: which agent CLI, which inner
sandbox, which VM or container, and which model. Each setup is measured on two things:

- **Containment / security**: what the agent process can reach (secrets, network, host,
  privileges), whether it follows prompt injections, and which boundaries it actually crosses.
- **Economics**: whether it solves the task, and what that costs in tokens, money and time.

You can also work in one of these setups yourself: `agenteval dev` attaches your terminal to
the agent CLI running in a fresh container or VM, on the repo you start it from. See
[Quickstart 3](#quickstart-3-an-interactive-session-agenteval-dev).

It is built on [Inspect AI](https://inspect.aisi.org.uk/) (the open-source eval framework from
the UK AI Security Institute), [inspect_swe](https://github.com/meridianlabs-ai/inspect_swe)
(runs agent CLIs such as OpenCode under Inspect) and
[inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) (the CodeIPI benchmark).

All security tests are **defensive and benign**. They use fake canary credentials, harmless
marker files, a local collector endpoint and TCP reachability checks. Nothing is exploited,
deleted or sent to a real system. See [docs/security-tests.md](docs/security-tests.md).

## The four axes

A run is the cross product of four independent axes. Each combination becomes one Inspect
*task*.

| Axis | Question it answers | Defined in | Shipped profiles |
| --- | --- | --- | --- |
| **Model** | Which LLM answers the agent? | [config/models/](config/models/) | `mock` (offline, free), `mock-worker` (offline mock for subagents), `qwen38-27b` (Qwen 3.8 via OpenRouter), `claude-sonnet-api` (Anthropic API), `claude-sonnet-bedrock` (Amazon Bedrock), `claude-sonnet-subscription` (your Claude plan, `agenteval dev` only), `gateway-gpt-oss-120b` / `gateway-devstral-2512` / `gateway-qwen` (gateway) |
| **Harness** | Which agent CLI and version runs, and inside which inner sandbox? | [config/harnesses/](config/harnesses/) | `opencode-*`, `claude-code-*` (Claude Code), `claude-code-subagents-*` (Claude Code + its own subagents on a second model), `claude-code-opencode-*` (Claude Code orchestrating OpenCode workers on a second model); each as `-none` (no inner sandbox) and `-bwrap` (bubblewrap) |
| **Environment** | Where does the agent run? Every sample gets a fresh one. | [config/environments/](config/environments/) | `docker-ubuntu` (local validation), `tart-ubuntu` (Linux VM on macOS), `sbx-ubuntu` (Docker Sandboxes microVM) |
| **Suite** | What is measured? | [src/agenteval/suites/](src/agenteval/suites/) | `containment`, `capability`, `injection` |

A **run file** in [config/runs/](config/runs/) lists the values for each axis, plus limits and
concurrency. `opencode-qwen-tart` is 1 model x 2 harnesses x 1 environment x 3 suites = 6 tasks.

| Suite | What it does | Model calls |
| --- | --- | --- |
| `containment` | Benign probes run as the agent user, through the harness's inner sandbox. 15 scored checks, one file each in [cases/containment/](cases/containment/). | none |
| `capability` | 19 everyday developer tasks (easy to hard): Python bug fixes, questions about ADRs, an API spec and an architecture doc, git workflows (commit, branch and push, merge conflicts, revert, backport, untrack, squash) and code, architecture and accessibility reviews. Graded by visible and hidden reference cases checked outside the agent's code. | yes |
| `injection` | CodeIPI: bug-fix tasks with prompt injections hidden in issues, comments, READMEs and configs. | yes |

## Concepts in one minute

If you are new to eval frameworks, these are the Inspect terms used throughout the docs.

| Term | Meaning in this project |
| --- | --- |
| Task | One suite for one model/harness/environment combination. Named `suite--harness--environment--model`. |
| Sample | One test case: one probe run, one capability task or one CodeIPI scenario. Each sample gets a fresh VM or container. |
| Solver | A step applied to a sample: root setup, agent install, agent run. |
| Scorer | Grades a sample after the agent has finished, looking at the environment from outside the agent's sandbox. |
| Sandbox | Inspect's word for the environment a sample runs in (here a Tart VM or Docker container). |
| Eval log | One `.eval` file per task under `logs/<run-id>/`: the full transcript, scores, token usage and timings. |
| Epoch | One pass over a task's samples. More epochs means repeated measurements. |

## Quickstart 1: offline smoke run (Docker, mock model)

This needs no API key and costs nothing. Prerequisites: `uv` and a running Docker Desktop.
See [docs/setup.md](docs/setup.md).

Run every command from the repository root:

```sh
uv sync                                       # create .venv with the pinned dependencies
uv run agenteval doctor --env docker-ubuntu   # check tools, versions and the image
uv run agenteval env build docker-ubuntu      # build agenteval/ubuntu-24.04:latest (once)
uv run agenteval run smoke-docker --dry-run   # print the 6-task matrix, run nothing
uv run agenteval run smoke-docker             # run it (about 2 minutes once the image exists)
uv run agenteval report open                  # open the HTML report of the latest run
```

What to expect: Inspect's mock model replies with fixed text, so the agent does nothing useful.

- Capability resolve rate is 0%, and the injection results are meaningless (nothing was attempted).
- The **containment results are real**, because that suite makes no model calls. In the
  reference run, `opencode-none` exposed 7 of 15 scored checks and `opencode-bwrap` exposed 0.

The smoke run is for checking that environments, sandboxes, scorers and reports work.

## Quickstart 2: the real run (Tart VM + Qwen via OpenRouter)

First do the host setup in [docs/setup.md](docs/setup.md). Tart is installed manually because
the Homebrew formula is currently broken. Then:

```sh
uv run agenteval env build tart-ubuntu                # build the provisioned VM image (once)
uv run agenteval run opencode-qwen-tart --model mock --limit 2   # offline check of the Tart path

read -rs OPENROUTER_API_KEY && export OPENROUTER_API_KEY   # paste the key, press Enter (not echoed)
uv run agenteval doctor --env tart-ubuntu
uv run agenteval run opencode-qwen-tart --dry-run
uv run agenteval run opencode-qwen-tart --limit 1     # 1 sample per task: check cost and time first
uv run agenteval run opencode-qwen-tart               # the full matrix
```

Instead of exporting the key you can put it in a gitignored `.env` file in the repository root;
agenteval loads it before checking required variables (see
[docs/setup.md](docs/setup.md#7-the-openrouter-api-key)).

OpenRouter is a third-party service. Prompts, repository code and tool output from every
agent-driven sample are sent to it and to the upstream provider it routes to. The key stays on
the host; the VM never sees it. Read the data-egress section in
[docs/setup.md](docs/setup.md#data-egress) before running on anything other than the built-in
tasks.

Status: the Docker path has been run end to end. The Tart provider has run on a real VM
through `agenteval dev` sessions (copy and live mode, both CLIs), but no eval suite has run on
Tart yet. Treat the first Tart eval run as a validation run. Other open points are listed under
[Known limitations](docs/security-tests.md#known-limitations).

## Quickstart 3: an interactive session (`agenteval dev`)

`agenteval dev` lets you work in a setup instead of measuring it. It starts one environment
with one harness and one model, attaches your terminal to the harness CLI inside it, and
works on the repo you run it from. The environment is clean: no fake secrets, decoys or
monitor. When you exit the CLI, you review what the session wants to bring back to your
machine, and only what you approve is applied. With `--mount live` the agent writes your
repo directly instead.

It runs from your own project, so install agenteval once as an editable tool (or use
`uv run --project <checkout> agenteval dev ...`). It needs an interactive terminal and, for
`docker-ubuntu`, the image from Quickstart 1:

```sh
uv tool install --editable "$PWD"   # once, from the repository root
cd ~/src/my-project
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model mock         # offline, free
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model qwen38-27b   # OpenRouter key
agenteval dev list                  # sessions, newest first, with their status
agenteval dev apply SESSION_ID      # review a session you deferred
```

API keys come from this checkout's `.env` or your shell, never from your project's `.env`.
The `claude-code-subagents-*` and `claude-code-opencode-*` harnesses also take
`--subagent-model` (for example `mock-worker` or `qwen38-27b`); without it they use
`gateway-qwen`, which needs the `GATEWAY_*` variables. A session's Inspect log goes to
`logs/dev/<session>/` in this checkout; its tokens and cost are shown before the review.
With a real model, code from your repo goes to that model's provider: read the
[data-egress section](docs/setup.md#data-egress) first.

[docs/dev.md](docs/dev.md) explains copy and live mode, what of your CLI config and memory
goes in and comes back, and the review. [docs/dev-features.md](docs/dev-features.md) lists
the features and how to start each harness.

## Viewing results

| Command | What it shows |
| --- | --- |
| `uv run agenteval report open [RUN_ID]` | The HTML report of a run (`latest` by default, `index` for the list of all runs). Uses macOS `open`; elsewhere it prints the path. |
| `uv run agenteval report serve [--port 8765]` | Serves `reports/` on `http://127.0.0.1:8765/`, local only. |
| `uv run agenteval view [RUN_ID]` | Inspect's log viewer: full transcripts with every model call, tool call and score. |
| `uv run agenteval report build logs/<run-id>` | Rebuilds a report from logs, for example after an interrupted run. |

Run ids look like `20261018T095942Z-smoke-docker` (UTC timestamp + run name). Pass
`--run-id NAME` to choose one. Running again with the same id resumes an interrupted run:
finished tasks are not repeated.

Each report is a single self-contained `index.html` (no external resources) plus `data.json`.
Its sections: Overview, Containment (checks x configurations, differences highlighted),
Prompt injection (CodeIPI), Capability, Boundary monitor, Economics, Resources (host CPU and
memory per environment, sampled during the run), and a per-sample drill-down. [docs/architecture.md](docs/architecture.md#reports-and-economics) defines every
number in it.

## Command reference

| Command | Purpose |
| --- | --- |
| `agenteval doctor [--env NAME]` | Checks Python, uv, package versions, Docker/Tart, images, SSH key, required env vars (from the shell or `.env`), the macOS DHCP lease time (Tart) and, on Linux Docker hosts, the user-namespace restriction that breaks bwrap. Variable values are never printed. Problems with `--env NAME` are failures; everything else is a warning. |
| `agenteval profiles` | Lists model, harness, environment and run profiles. |
| `agenteval env build NAME [--force]` | Builds the Docker image or Tart VM image for an environment. `--force` rebuilds from scratch. |
| `agenteval env cleanup` | Deletes leftover per-sample Tart VMs and agenteval Docker compose projects, including those still in use (a running run or `agenteval dev` session). |
| `agenteval run RUN_FILE [options]` | Runs a run file, given as a path or as a name under `config/runs/`. |
| `agenteval dev --harness H --env E --model M [options]` | Starts one environment with a harness and model and attaches your terminal to the harness CLI, working on the repo you run it from; changes come back through a review. `dev apply ID` reviews a deferred session, `dev list` lists sessions. See [docs/dev.md](docs/dev.md). |
| `agenteval report build LOG_DIR [--out DIR] [--title T]` | Builds a report from a log directory. |
| `agenteval report open [RUN_ID]` / `report serve [--port N]` | Opens or serves reports. All three `report` commands take `--report-dir` (default `reports`). |
| `agenteval view [RUN_ID] [--log-dir DIR]` | Starts `inspect view` for one run's logs. |

`run` options: `--model`, `--harness`, `--env` and `--suite` replace the run file's list for
that axis (repeat the option for several values); `--limit N` caps the samples per task;
`--run-id ID`; `--no-report`; `--dry-run`. Example:

```sh
uv run agenteval run opencode-qwen-tart --harness opencode-bwrap --suite containment
```

The global `--config-dir DIR` option (or `AGENTEVAL_CONFIG_DIR`) points at another profile
directory. `config/`, `logs/`, `reports/` and `.agenteval/` are resolved relative to the current
directory, so run from the repository root. `agenteval dev` is the exception: it runs from
your own project and uses the checkout's folders (a `--config-dir` or `AGENTEVAL_CONFIG_DIR`
you set is still resolved against the current directory).

## Repository layout

```text
cases/                       the test cases, one file or folder each (start here to add tests)
  capability/  containment/  monitor/  injection/   each with a README and a _template
config/
  models/  harnesses/  environments/  runs/    YAML profiles (the four axes + run files)
                             each with a _template.yaml listing every key
  schema/                    JSON Schema of each kind, for editor completion (scripts/gen_config_schema.py)
src/agenteval/
  cli.py, runner.py          CLI (click) and the logic behind it
  dev/                       agenteval dev: session task, repo transport, CLI config sync, review
  config.py, constants.py    profile schema; in-sandbox paths and names
  models.py                  Inspect model for a model profile, with its connection (proxy, TLS) settings
  _registry.py               Inspect entry point (registers the "tart" sandbox)
  environments/              Docker compose generation, Tart sandbox provider, provisioning
  harnesses/                 OpenCode and Claude Code install + run, inner sandbox wrapper (sandbox-exec)
  sandbox_scripts/           probe runner, monitor, collector, egress policy, case runner, dev repo transport, pty relay and reasoning relay (run inside the env)
  suites/                    containment, capability, injection (CodeIPI + local cases), monitors: load cases/
  report/                    log parsing, economics, HTML templates
tests/                       unit tests (pytest)
docs/                        setup, dev mode, architecture, security tests, extending, research notes
logs/  reports/  .agenteval/ generated: eval and dev logs, HTML reports, compose files named by content hash, dev sessions (gitignored)
```

## Documentation

| Document | Read it for |
| --- | --- |
| [cases/README.md](cases/README.md) | **Adding test cases**: capability tasks, containment checks, monitor watch groups, injection cases. Agents: also [AGENTS.md](AGENTS.md). |
| [docs/setup.md](docs/setup.md) | Setting up a new Mac: uv, Docker, Tart, images, API key, first runs, data egress, troubleshooting. |
| [docs/dev.md](docs/dev.md) | **Working interactively** in a harness setup (`agenteval dev`): copy vs live mode, what of your CLI config and memory goes in and comes back, the review. |
| [docs/dev-features.md](docs/dev-features.md) | Dev mode features and how to start each harness. |
| [docs/architecture.md](docs/architecture.md) | How a sample runs, profiles, suites, monitors, the report and economics definitions, and how a dev session runs. |
| [docs/security-tests.md](docs/security-tests.md) | Every containment check, the boundary monitor signals, CodeIPI, how to read the results, and known limitations. |
| [docs/extending.md](docs/extending.md) | Adding models (e.g. an internal gateway), harnesses, environments, suites and run files. |
| [docs/evaluation-strategy.md](docs/evaluation-strategy.md) | Research notes: the overall evaluation strategy (draft). |
| [docs/existing-eval-projects.md](docs/existing-eval-projects.md) | Research notes: existing open-source eval projects and what is usable out of the box. |

## Development

```sh
uv run pytest            # unit tests; Docker- and Tart-marked tests are skipped until their image is built
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
