# Dev mode: features and starting each harness

This guide explains what `agenteval dev` is, what it gives you, and how to start each
harness. It is written for developers and case authors who are new to evaluation tooling.
The full reference, with every rule about copying, config and the review, is
[dev.md](dev.md).

## What dev mode is

An evaluation (`agenteval run`) is automatic. It starts many fresh environments, gives the
agent fixed tasks, scores the results and writes a report. Nobody types into the agent.

Dev mode is the same setup with you at the keyboard. `agenteval dev` starts **one**
environment with **one** harness and **one** model, and attaches your terminal to the
agent CLI running inside it. You give it tasks on your own repo. When you exit the CLI,
you review what the session wants to bring back to your machine, and only what you
approve is applied.

Use it to:

- **Try a setup** before you evaluate it: see how a harness and model behave on real work.
- **Do day-to-day work** with a sandboxed agent: the agent runs in a container or VM, not
  on your machine.
- **Explore a behaviour** before you write a [case](../cases/README.md) for it: try it by
  hand, then turn it into a test.

The terms used below:

| Term | Meaning |
| --- | --- |
| Harness | The agent CLI (OpenCode or Claude Code) at a pinned version, plus the inner sandbox it runs in. Profiles in [config/harnesses/](../config/harnesses/). |
| Inner sandbox | An extra boundary inside the environment. `-none` profiles have none; `-bwrap` profiles run the CLI inside bubblewrap. |
| Environment | Where the CLI runs: a Docker container (`docker-ubuntu`) or a Tart Linux VM on macOS (`tart-ubuntu`). Profiles in [config/environments/](../config/environments/). |
| Model | The LLM that answers the CLI. Profiles in [config/models/](../config/models/). `mock` is offline and free. |
| Bridge | Inspect's model proxy. The CLI in the environment sends its model calls to the bridge, and the bridge calls the provider from your machine. |
| Session | One `agenteval dev` run, from start to review. Its id looks like `20261020T101500Z-my-project-3f2a` (time, repo name, random suffix). |

`uv run agenteval profiles`, run from the checkout, lists every profile with its
description.

## Features at a glance

| Feature | What it gives you | Details |
| --- | --- | --- |
| The real CLI in your terminal | The CLI's own interface runs in your terminal, attached with `docker exec -it` (Docker) or `ssh -t` (Tart). It starts in the same subfolder you started from. | [What happens](dev.md#what-happens) |
| Keys stay on your machine | The CLI only gets placeholder keys. Its model calls go through the bridge, which holds the real key on your machine. | [Running it from any repo](dev.md#running-it-from-any-repo) |
| The setups the evals measure | Same image, same CLI install, same inner sandbox and same network policy as an eval. The eval fixtures (fake secrets, decoys, the monitor) are left out. | [What happens](dev.md#what-happens) |
| Copy mode (default) | The agent works on a snapshot of your working tree, with uncommitted and untracked files. Nothing reaches your machine until you approve it. | [Copy mode and live mode](dev.md#copy-mode-and-live-mode) |
| Live mode | `--mount live` mounts your repo folder itself. The agent changes your files directly, and repo files are not reviewed. | [Copy mode and live mode](dev.md#copy-mode-and-live-mode) |
| Your CLI config and memory | Your settings, `CLAUDE.md` or `AGENTS.md`, commands, agents, skills and this repo's memory are copied in, without credentials. What the agent changes there comes back through the review. | [Your CLI config and memory](dev.md#your-cli-config-and-memory) |
| Resume | If the CLI exits with an error, you can resume the conversation instead of losing it. | [What happens](dev.md#what-happens) |
| The exit review | Each change is shown as a diff against your current files. You apply, skip or edit it. Changes that can run code on your machine later are flagged. | [The review](dev.md#the-review) |
| Review later | Answer `l` at the review and come back with `agenteval dev apply ID`. | [Sessions and cleanup](dev.md#sessions-and-cleanup) |
| Session list | `agenteval dev list` shows every session with its status. | [Sessions and cleanup](dev.md#sessions-and-cleanup) |
| Cost and transcript | Every model call goes into an Inspect log. The session prints its tokens and cost, and the command that opens the log. | [What happens](dev.md#what-happens) |
| Safe export | Before the environment is deleted, the changes are copied out and checked on your machine. If that fails, the environment is kept until you retry, open a shell in it, or abandon it. | [What happens](dev.md#what-happens) |
| Docker and Tart | The same command for both. Switch with `--env`. | [Limits](dev.md#limits) |

## Before you start

You need the agenteval checkout set up, a way to run it from your project, a built
environment image, and the API key for the model you pick. The examples assume the
checkout is at `~/Documents/GitHub/harness_evaluation`; replace that path with yours.

### 1. The checkout

Set up the checkout as described in [setup.md](setup.md) (uv, Python 3.12, `uv sync`). Then make
`agenteval` available in your project. Either install it once as an editable tool, which
puts `agenteval` on your PATH:

```sh
uv tool install --editable ~/Documents/GitHub/harness_evaluation
```

or start every command with `uv run --project`:

```sh
uv run --project ~/Documents/GitHub/harness_evaluation agenteval dev --help
```

A non-editable install does not work. agenteval finds its checkout from its own source
location, because the checkout holds the profiles, the `.env` file, the logs and the
session records. The examples below use the short form `agenteval dev ...`.

### 2. The environment image

Dev mode uses the same images as the evals, so an image you already built works. Run the
build from the checkout root.

| Environment | Prerequisites | Build once |
| --- | --- | --- |
| `docker-ubuntu` | Docker Desktop running, with Compose v2 ([setup.md](setup.md#4-docker-desktop)) | `uv run agenteval env build docker-ubuntu` |
| `tart-ubuntu` | A Mac with Apple silicon, Tart 2.41.1 on your PATH, installed by hand ([setup.md](setup.md#5-tart-manual-install)), and macOS Local Network access for the app you run agenteval from ([below](#troubleshooting-and-limits)) | `uv run agenteval env build tart-ubuntu` |

### 3. Doctor

Run doctor from the checkout root. It reads the profiles relative to the current folder,
so from another folder it finds none.

```sh
uv run agenteval doctor --env docker-ubuntu
```

With `--env NAME`, problems with that environment are failures (exit code 1). Doctor also
names the model profiles whose variables are missing, without showing values. It does not
check the editable install, the interactive terminal or macOS Local Network access.

### 4. API keys

Each model profile names the variables it needs. Put them in the checkout's `.env` file,
or export them in your shell; a variable set in the shell wins. Your project's own `.env`
is never read for keys. If a variable is missing, `agenteval dev` stops before anything
starts.

| Model profile | Variables | Notes |
| --- | --- | --- |
| `mock`, `mock-worker` | none | Offline and free. They reply with a fixed text, so the agent does nothing useful. |
| `qwen38-27b` | `OPENROUTER_API_KEY` | Qwen 3.8 27B through OpenRouter. |
| `claude-sonnet-api` | `ANTHROPIC_API_KEY` | An Anthropic Console API key. A Claude subscription login cannot be used. |
| `claude-sonnet-bedrock` | `AWS_REGION`, plus AWS credentials from the standard chain | Amazon Bedrock. |
| `gateway-gpt-oss-120b`, `gateway-devstral-2512`, `gateway-qwen` | `GATEWAY_BASE_URL` (including `/v1`), `GATEWAY_API_KEY` | The company gateway. `gateway-qwen` ships with a placeholder model id. No pricing, so cost shows as unknown. |

With a real model, your prompts, your repo's code and the agent's tool output go to the
model provider. Read [Data egress](setup.md#data-egress) before you use dev mode on
internal code. [setup.md](setup.md#other-providers) explains each provider.

## Starting each harness

Run `agenteval dev` from the project you want to work on, in an interactive terminal.
`--harness`, `--env` and `--model` are required. Start with the `mock` model: it is free
and offline, so you can check the whole path (start, attach, exit, review) before you
spend anything.

There are eight harness profiles: four kinds, each with and without bubblewrap.

| Profiles | Agent CLI | Second model |
| --- | --- | --- |
| `opencode-none`, `opencode-bwrap` | OpenCode 1.19.3 | no |
| `claude-code-none`, `claude-code-bwrap` | Claude Code 2.1.292 | no |
| `claude-code-subagents-none`, `claude-code-subagents-bwrap` | Claude Code 2.1.292 with two subagents | yes, for the subagents |
| `claude-code-opencode-none`, `claude-code-opencode-bwrap` | Claude Code 2.1.292 orchestrating OpenCode 1.19.3 workers | yes, for the workers |

Both CLIs start in skip-permissions mode: Claude Code with
`--dangerously-skip-permissions`, OpenCode with `--auto`. The agent does not ask before it
runs a command. The environment and the inner sandbox are the boundary.

Before the CLI appears, agenteval prints `note:` lines (for example, parts of your CLI
config that cannot work in the environment) and a line with the session id. The first
start of a CLI version downloads it, which can take a minute.

### `-none` or `-bwrap`

| | `-none` | `-bwrap` |
| --- | --- | --- |
| Boundary | The container or VM only. The CLI runs as the unprivileged `agent` user. | bubblewrap, inside the container or VM. |
| What the agent can write | Anything the `agent` user can write. | The workspace and the CLI's own state folders. The rest of the system is read-only, and `/tmp` is private. |
| Hidden from the agent | nothing extra | `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.netrc`, `/etc/agenteval/secrets`, `/root` |
| Network | Only the bridge: both shipped environments deny outbound traffic. | Only the bridge. |

In both variants, `npm install`, `pip install` and other downloads fail with the shipped
environments. Pick the variant you want to learn about: `-bwrap` is the tighter setup, and
`-none` shows what the agent does when only the environment stands in its way.
[security-tests.md](security-tests.md#reading-none-vs-bwrap-differences) explains the
difference as the containment checks measure it.

### OpenCode: `opencode-none`, `opencode-bwrap`

OpenCode's terminal interface, started with `--auto` and the model id from the model
profile's `agent_models.opencode`. `mock`, `mock-worker`, `qwen38-27b` and the `gateway-*`
profiles have one. `claude-sonnet-api` and `claude-sonnet-bedrock` do not, so OpenCode
cannot use them; that error only appears after the environment has started. A harness
`system_prompt` is not applied in dev mode, because OpenCode's interface has no flag for
it.

Offline check:

```sh
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model mock
```

With Qwen through OpenRouter (needs `OPENROUTER_API_KEY`):

```sh
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model qwen38-27b
```

For no inner sandbox, use `--harness opencode-none`.

### Claude Code: `claude-code-none`, `claude-code-bwrap`

Plain Claude Code. agenteval marks onboarding as done and the workspace as trusted, copies
your `theme`, and leaves auto memory on, because memory is part of what comes back for
review. Its own settings (the bridge address and a placeholder key) are passed with
`--settings` and environment variables, never written into your `settings.json`. These
harnesses take no `--subagent-model`.

Offline check:

```sh
agenteval dev --harness claude-code-bwrap --env docker-ubuntu --model mock
```

With Claude Sonnet on the Anthropic API (needs `ANTHROPIC_API_KEY`):

```sh
agenteval dev --harness claude-code-bwrap --env docker-ubuntu --model claude-sonnet-api
```

Use `--model claude-sonnet-bedrock` for Bedrock, and `--harness claude-code-none` for no
inner sandbox.

### Claude Code with subagents: `claude-code-subagents-none`, `claude-code-subagents-bwrap`

Claude Code with two subagents defined in the profile. `implementer` makes a specified
change and runs the tests. `reviewer` reviews the change; it is told not to edit files and
gets only the tools `Read`, `Grep`, `Glob` and `Bash`. They are passed to Claude Code with
`--agents`, and an appended system prompt tells it to delegate subtasks to them with the
Task tool. The main model answers Claude Code; the subagents' calls go to a second model,
the `--subagent-model`.

Pass `--subagent-model`. Without it, the profile's default `gateway-qwen` is used, which
needs `GATEWAY_BASE_URL` and `GATEWAY_API_KEY` and ships with a placeholder model id.

Offline check, with a second mock model so the subagents' calls show up separately in the
log (as `mockllm/worker`):

```sh
agenteval dev --harness claude-code-subagents-bwrap --env docker-ubuntu --model mock --subagent-model mock-worker
```

With Sonnet as the main model and Qwen for the subagents (needs `ANTHROPIC_API_KEY` and
`OPENROUTER_API_KEY`):

```sh
agenteval dev --harness claude-code-subagents-bwrap --env docker-ubuntu --model claude-sonnet-api --subagent-model qwen38-27b
```

### Claude Code with OpenCode workers: `claude-code-opencode-none`, `claude-code-opencode-bwrap`

Claude Code as an orchestrator. The profile's instructions, appended to its system prompt,
tell it not to edit files itself but to run `opencode-worker "<complete instructions>"`
with the Bash tool. Each worker is a separate OpenCode 1.19.3 process (`opencode run`)
that edits the files and prints its answer. Workers run inside the same inner sandbox as
Claude Code, and their model calls go to the `--subagent-model`. The same default as
above applies: pass `--subagent-model`, or set the gateway variables.

```sh
agenteval dev --harness claude-code-opencode-bwrap --env docker-ubuntu --model mock --subagent-model mock-worker
```

```sh
agenteval dev --harness claude-code-opencode-bwrap --env docker-ubuntu --model claude-sonnet-api --subagent-model qwen38-27b
```

These model pairs are the ones the run files `smoke-claude-docker` (mock with
`mock-worker`) and `claude-opencode-qwen-docker-full` (Sonnet with Qwen workers) use.

### Variations

These options work with every harness.

**Tart instead of Docker.** Replace `--env docker-ubuntu` with `--env tart-ubuntu`:

```sh
agenteval dev --harness opencode-bwrap --env tart-ubuntu --model mock
```

**Live mode.** `--mount live` mounts your repo folder at its own path instead of copying
it. The agent's changes land on your disk as it makes them, and repo files are not
reviewed. Anything it writes can run on your machine later: git hooks, `.git/config`,
Makefiles, `package.json` scripts, `.envrc`. agenteval warns before the session and,
after it, reports changes to git's own config and hooks. Use it only for repos and models
you trust. It is refused in a folder that contains the agenteval checkout, because the
checkout's `.env` holds your API keys.

```sh
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model mock --mount live
```

**Gitignored files (copy mode).** Copy mode leaves out what git ignores. `--include GLOB`
also copies ignored files that match GLOB, relative to the repo root. Quote the glob so
your shell does not expand it, and repeat the option for several globs. It cannot be
combined with `--mount live`. A glob that matches no ignored file gives a note.

```sh
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model mock --include 'data/**'
```

Files that look like secrets (`.env`, `*.pem`, `*.key`, `id_rsa*`, ...) and are not
ignored are copied too; a note lists them before the start. To keep them out, add them to
your ignore rules.

**Another CLI config folder.** By default the CLI config comes from `~/.claude` (or
`$CLAUDE_CONFIG_DIR`) for Claude Code, and from `~/.config/opencode` (or
`$XDG_CONFIG_HOME/opencode`) for OpenCode. `--agent-config-dir DIR` uses DIR instead, both
for what goes in and for where approved changes go. The folder does not have to exist: it
is created when you first apply a change to it. This keeps a separate config and memory
for dev sessions:

```sh
agenteval dev --harness claude-code-bwrap --env docker-ubuntu --model mock --agent-config-dir ~/claude-dev-config
```

**No CLI config.** `--no-host-config` starts the CLI with an empty config and brings no
config or memory back. Repo changes are still reviewed.

```sh
agenteval dev --harness claude-code-bwrap --env docker-ubuntu --model mock --no-host-config
```

**Other profiles.** Profiles come from the checkout's `config/`, whichever folder you
start from. To use another profile folder, put the global option before `dev`
(`agenteval --config-dir DIR dev ...`) or set `AGENTEVAL_CONFIG_DIR`. Pin the `version`
of a harness profile you use in dev mode, as the shipped profiles do; its `options` only
apply to eval runs. See [extending.md](extending.md#harness-profiles).

## While you work

Exit the CLI the way you normally do. When it exits, agenteval stops everything the agent
started in the environment (servers, background jobs) and copies the changes out.

If the CLI exits with an error code, agenteval asks:

```text
The CLI exited with code 1. [r] resume the conversation  [f] finish:
```

`r` starts the CLI again with `--continue`. Any other answer finishes the session.

If copying the changes out fails, the environment keeps running. agenteval prints the
error and a command to open a shell in the environment, then asks whether to retry (`r`),
open a shell in the environment (`s`) or abandon it (`a`). Only `a` (or Ctrl+C or Ctrl+D
at that prompt) deletes the environment and the changes in it.

## When you exit: the review

After the export, agenteval prints the command that opens the session log, then a
summary: the session, its tokens and cost, the number of changes per group (commits, repo
files, CLI config, memory, `CLAUDE.local.md`), the flagged changes with the reason for
each flag, and notes. [dev.md](dev.md#the-review) shows an example. Then it asks:

```text
[y] review now  [l] later (agenteval dev apply <id>)  [d] discard all >
```

`l` keeps everything pending, and `d` skips every change. With `y`, each change is shown
as a diff from your current file to what would be written:

| Key | Action |
| --- | --- |
| `y` | Apply this change. |
| `n` | Skip it. |
| `e` | Edit the proposed content in `$VISUAL` or `$EDITOR` (default `vi`), then apply your version. Not offered for binary files, symlinks, deletions, changes that cannot be merged or the commits item. |
| `a` | Apply this change and the rest of its group; still stops at each flagged change. |
| `d` | Skip this change and the rest of its group. |
| `q` | Quit; undecided changes stay pending. |
| `?` | Show the keys. |

What applying does, in short:

- **Commits** (copy mode). The agent's commits are fetched into
  `refs/agenteval/<id>/heads/<branch>` of your repo. The commits item fast-forwards your
  branch when that is safe, for example when your branch has not moved since the start.
  Otherwise a note prints the `git merge` command to run yourself.
- **Repo files** (copy mode). A file you have not changed since the start is replaced. If
  you changed it too, the two versions are merged. Applying a change that cannot be
  merged writes it to the session's `conflicts/` folder and leaves your file as it is.
- **CLI config and memory.** Merged with what is on your machine now. In Claude Code's
  `settings.json`, keys the environment must not set, such as credential helpers and
  `model`, keep your value.

**Flags** point at changes that can run code on your machine later or change what later
agent sessions do: Makefiles, package scripts, CI files, git hooks, `.envrc`, agent config
files, new executable files; in config and memory, install commands, URLs, permission and
hook settings, and "don't ask" phrasing. They are hints, not a verdict: read every diff you
apply.

In live mode the repo files are already on your disk, so the review covers the CLI config
and memory, plus notes about changes to git's config and hooks.

### Coming back later

List your sessions and their status:

```sh
agenteval dev list
```

Review a session's pending changes:

```sh
agenteval dev apply 20261020T101500Z-my-project-3f2a
```

`--config` reviews only the CLI config and memory; repo changes stay pending. `dev apply`
works for sessions with the status `pending` or `applied`. It compares each change with
your files as they are now, and does not show changes you already decided.

| Status | Meaning |
| --- | --- |
| `running` | The session has not finished. |
| `pending` | Changes wait for `agenteval dev apply`. |
| `applied` | Every change was decided, even if you skipped each one. |
| `discarded` | You answered `d` at the first review prompt. |
| `failed` | The session failed; it printed the error and where its record and log are. |

Everything about a session is kept in the checkout: the record in
`.agenteval/sessions/<id>/` (with `worktree.patch`, a readable diff of all file changes)
and the Inspect log in `logs/dev/<id>/`. In copy mode, the agent's commits stay in your
repo as `refs/agenteval/<id>/heads/*` after the review;
[dev.md](dev.md#sessions-and-cleanup) shows how to remove them.

## From a session to a test case

A dev session is not a test. Nothing is scored, and the eval fixtures that make security
results measurable (fake secrets, the decoy, the boundary monitor) are not there. To
measure a behaviour you found, write a case and run it as an eval.

1. **Look at what happened.** The session log holds every model call the CLI made. Open it
   with the command the session printed:

   ```sh
   agenteval view 20261020T101500Z-my-project-3f2a --log-dir ~/Documents/GitHub/harness_evaluation/logs/dev
   ```

   From the checkout root, `latest` opens the most recent session:

   ```sh
   uv run agenteval view latest --log-dir logs/dev
   ```

   `agenteval view` starts Inspect's log viewer (`inspect view`) on that log. In copy mode,
   the session's `worktree.patch` shows every file the agent changed.

2. **Pick the kind of case.**

   | What you saw | Kind of case | Guide |
   | --- | --- | --- |
   | The agent solved or failed an everyday task, or it cost a lot | Capability: a small task, graded by hidden reference cases | [capability/README.md](../cases/capability/README.md) |
   | The agent's process could reach something it should not: a file, a socket, the network | Containment: one probe of the boundary, run without a model | [containment/README.md](../cases/containment/README.md) |
   | The agent changed a file that makes code run later: a hook, a startup file, a cron job, CI | Boundary monitor: a watch group of such locations | [monitor/README.md](../cases/monitor/README.md) |
   | The agent followed an instruction it found in the repo or an issue | Prompt injection: a coding task with a planted instruction. You write the instruction. | [injection/README.md](../cases/injection/README.md) |

3. **Rebuild the situation as a small case.** Do not copy your project's code or real
   credentials into it: cases are benign, deterministic and self-contained. Copy the
   kind's `_template`, edit the copy and run `uv run pytest -q` from the checkout
   ([cases/README.md](../cases/README.md#workflow)).
4. **Run it as an eval** with the same harness, environment and model as your session.
   `agenteval run` takes `--harness`, `--env` and `--model` to override a run file's
   lists ([README.md](../README.md#command-reference)); each kind's guide shows how to try
   your case.

To go the other way, and try a case's repo by hand in dev mode, see
[cases/README.md](../cases/README.md#explore-with-agenteval-dev).

## Troubleshooting and limits

| Message or symptom | Cause and fix |
| --- | --- |
| `agenteval dev needs an interactive terminal` | stdin or stdout is not a terminal, for example in a pipe or a script. Run it in a terminal window. |
| `--harness, --env and --model are required` | Give all three. |
| ``session options go with `agenteval dev`, not its subcommands`` | `dev apply` and `dev list` take no session options. `dev apply` only takes `--config`. |
| `agenteval is not running from its checkout ...` | Install it with `uv tool install --editable <checkout>`, or use `uv run --project <checkout> agenteval ...`. |
| `missing required environment variables: ...` | Set them in the checkout's `.env` or your shell. With a `claude-code-subagents-*` or `claude-code-opencode-*` harness and no `--subagent-model`, the missing ones include the gateway variables: pass `--subagent-model`. |
| `harness '...' has no subagents; drop --subagent-model` | Only the `claude-code-subagents-*` and `claude-code-opencode-*` harnesses take `--subagent-model`. |
| `The session failed: ... has no agent_models.opencode entry ...` | OpenCode needs a model profile with `agent_models.opencode`, such as `mock` or `qwen38-27b`. |
| `harness '...' must pin a version: agenteval dev needs the real binary` | Set an exact `version` in the harness profile. |
| `--include only applies to --mount copy` | Drop `--include`, or use copy mode. |
| `--mount live would share ..., which contains the agenteval checkout ...` | Use copy mode, or work from a folder outside the checkout. |
| `the agent user cannot write ... in the environment; use --mount copy` | The live-mounted folder is not writable for the agent user. Use copy mode. |
| `The snapshot copies N MB ... Continue? [y/N]` | The files that are not in HEAD add up to more than 500 MB. Answer `y`, or ignore the large files in git and start again. |
| `SSH ... not ready ... No route to host` (Tart) | macOS Local Network access is missing for the app you run agenteval from. See [setup.md](setup.md#troubleshooting). |
| `npm install` or `pip install` fails | The shipped environments have no network besides the bridge, in both `-none` and `-bwrap`. |
| Your edit with `e` is lost | `$VISUAL` or `$EDITOR` starts an editor that returns at once, so agenteval can apply the change as proposed, without your edit. Use an editor command that waits until you close the file. |
| A container or VM is left after a killed session | `uv run agenteval env cleanup` from the checkout. It also removes environments still in use, including a running dev session, so run it only when nothing else runs. |

Limits worth knowing before you start (the full list is in [dev.md](dev.md#limits)):

- Terminal only; there is no browser UI.
- In live mode, repo files are not reviewed.
- OpenCode ignores a harness `system_prompt` in dev mode.
- Claude Code with a non-Claude model, such as `qwen38-27b`, shows the model's reasoning
  as raw `<think>` text. OpenCode shows it in its own thinking block.
- Byte-exact copies need git 2.42 or newer on your machine.
- At agenteval's resume, export and review prompts, Ctrl+C answers the prompt; elsewhere it
  stops the session. [dev.md](dev.md#limits) lists what it means at each one.
