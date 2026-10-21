# Interactive sessions: `agenteval dev`

`agenteval dev` gives you the same setups the evals measure for your own work. It starts
one environment (a Docker container or a Tart VM) with one harness (OpenCode or Claude
Code, with or without the bubblewrap inner sandbox) and one model, and attaches your
terminal to the harness CLI running inside it. It runs from the repo you are working on.
When you exit the CLI, you review what the session wants to bring back to your machine,
and only what you approve is applied.

Use it to try a setup before evaluating it, to see what an agent does with your real
work, or to find behaviour worth turning into a [case](../cases/README.md).

For an overview of the features and the start command for each harness, see
[dev-features.md](dev-features.md). This page is the reference.

```sh
cd ~/src/my-project
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model qwen38-27b
```

## Running it from any repo

The command needs the agenteval checkout: its profiles, `.env`, logs and session files
live there. Pick one of these:

```sh
uv tool install --editable ~/Documents/GitHub/harness_evaluation   # once; then `agenteval ...` anywhere
```

```sh
uv run --project ~/Documents/GitHub/harness_evaluation agenteval dev ...
```

A non-editable install does not work: agenteval finds its checkout from its own source
location.

Profiles come from `<checkout>/config`, not from the folder you run the command in, unless
you set `agenteval --config-dir DIR` or `AGENTEVAL_CONFIG_DIR`.

API keys come from the checkout's `.env` (or your shell), never from your project's
`.env`. Like the evals, they stay on your machine: the CLI in the environment talks to
Inspect's model bridge, which calls the provider from the host. If a variable that the
model or subagent model profile requires is missing, the command stops before anything
starts. Because the bridge runs on your machine, your machine has to reach the provider,
not the environment: for the company gateway, connect the VPN on your machine. The bridge
uses the model profile's `connection` settings; the `gateway-*` profiles connect
directly, without a proxy ([setup.md](setup.md#other-providers)).

The exception is a Claude subscription (`claude-sonnet-subscription`): Claude Code in the
environment calls Anthropic itself, and your token goes in
([below](#claude-subscription)).

## Options

| Option | Meaning |
| --- | --- |
| `--harness NAME` | Harness profile, e.g. `opencode-bwrap`, `claude-code-none`. Required. |
| `--env NAME` | Environment profile, e.g. `docker-ubuntu`, `tart-ubuntu`. Required. |
| `--model NAME` | Model profile, e.g. `qwen38-27b`, `mock` (offline), `claude-sonnet-subscription` ([Claude subscription](#claude-subscription)). Required. |
| `--subagent-model NAME` | Model for the subagents / OpenCode worker of the `claude-code-subagents-*` and `claude-code-opencode-*` harnesses (default: the harness profile's). The shipped profiles default to `gateway-qwen`, which needs `GATEWAY_BASE_URL` and `GATEWAY_API_KEY`; without them, pass another model, e.g. `qwen38-27b` or `mock-worker`. An error for harnesses without subagents. |
| `--mount copy` (default) / `live` | How your repo gets into the environment (below). |
| `--include GLOB` | Copy mode only: also bring in gitignored files matching GLOB, relative to the repo root (repeatable). |
| `--agent-config-dir DIR` | Use DIR as the CLI config to copy in and update instead of `~/.claude` / `~/.config/opencode`. |
| `--no-host-config` | Start with an empty CLI config and bring no config back (repo changes are still reviewed). |

The command needs an interactive terminal. Session options cannot be combined with
`apply` or `list`.

| Command | Purpose |
| --- | --- |
| `agenteval dev apply ID [--config]` | Review the pending changes of an earlier session (`--config`: only CLI config and memory). Works on `pending` and `applied` sessions. |
| `agenteval dev list` | List sessions, newest first: id, status, harness, model, environment and repo. |

## What happens

1. **Snapshot** (copy mode). Your working tree is recorded exactly as it is, with
   uncommitted and untracked files, as a git snapshot commit. Your ignore rules
   (`.gitignore`, `.git/info/exclude`, your global excludes file) decide what stays out,
   so `node_modules`, `.venv`, build output and usually `.env` are not copied; files you
   staged with `git add -f` and `--include` matches go in anyway. Only your current
   branch's history goes in, as a git bundle: no hooks, no `.git/config`, no remotes. A
   folder that is not a git repo is copied with a default exclude list (`node_modules`,
   `.venv`, `.env*`, keys, build directories, ...); in the environment it is a git repo on
   branch `main`. Before the start, notes list secret-looking files that are copied
   (`.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials*.json`, ...) and a merge, rebase,
   cherry-pick or revert in progress (the files and the index go in, the operation does
   not). If the files that are not in HEAD add up to more than 500 MB, agenteval asks
   before copying them.
2. **Environment.** A fresh container or VM starts. It is the same clean image the evals
   use, without the eval fixtures (no fake secrets, decoys or monitor). The harness CLI
   and its inner sandbox are installed as in an eval cell, at the version the harness
   profile pins. For Claude Code, the settings and subagents agenteval needs are not
   written into `~/.claude` but passed on the command line (step 4). The environment's
   egress policy applies: `docker-ubuntu` has no network at all, and `tart-ubuntu`
   rejects the agent user's outbound connections; the model is reached through the
   bridge only.
3. **Your repo appears** at `/workspace` (copy mode) or at its own path (live mode), on
   the same branch and with the same `git status` as on your machine. The CLI starts in
   the same subfolder you ran agenteval from, and your git `user.name` and `user.email`
   are set in the environment.
4. **Your CLI config is copied in** (below), and the CLI starts in your terminal in
   skip-permissions mode (Claude Code `--dangerously-skip-permissions`, OpenCode
   `--auto`). The environment and the inner sandbox are the boundary. Claude Code starts
   with onboarding and the folder trust prompt already answered, your `theme`, and
   auto-memory on; the subagents of a `claude-code-subagents-*` harness are passed with
   `--agents`, and the harness's `system_prompt` and delegation note with
   `--append-system-prompt`. OpenCode uses the model profile's `agent_models.opencode` as
   its model (a harness `agent_model` wins), and shows the model's reasoning in its own
   collapsible thinking block (`/thinking` toggles it). Its status bar names the model
   profile as `<description> (<name>)`, or just the name when the profile has no
   description, and the provider as `agenteval bridge`, for example
   `Qwen on the gateway (gateway-qwen) agenteval bridge`. Only these display names change:
   OpenCode keeps its catalog entry's limits and request format. `TERM`, `COLORTERM`,
   `TERM_PROGRAM` and `TERM_PROGRAM_VERSION` are passed through from your terminal (an
   unknown `TERM` falls back to `xterm-256color`).
5. **You work.** Exit the CLI as usual (`/exit`, Ctrl+C twice). If it exits with an
   error, agenteval asks `[r] resume the conversation  [f] finish`; `r` starts it again
   with `--continue`. When the CLI exits, everything the agent started in the environment
   (servers, background jobs) is stopped. On Docker, the Ctrl+P Ctrl+Q detach keys are
   turned off, so they reach the CLI.
6. **Export.** Before the environment is deleted, its commits (as a bundle), its
   working tree and the CLI config are copied out and checked on your machine. Untracked
   files over 100 MB and nested repos made in the session are left out; the review lists
   them. If the export fails, the environment is kept and you are asked to retry, open a
   shell in it, or abandon it; only an explicit abandon (or Ctrl+C or Ctrl+D there)
   deletes it.
7. **Review** (below). Everything else is deleted with the environment.

Model calls, tokens and cost are recorded in an Inspect log under
`<checkout>/logs/dev/<session>/`; the session prints the command that opens it
(`agenteval view <session> --log-dir <checkout>/logs/dev`) and its tokens and cost
before the review ("cost unknown" when a model profile has no pricing).

A model call that fails (no connection, or a rate limit or server error from the provider)
is retried twice. Then the bridge returns the error to the CLI, instead of retrying
forever while the CLI shows only a spinner. OpenCode shows the error, for example
`Connection error. [retrying attempt #1]`, and keeps retrying with its own backoff, so the
session goes on once the provider answers again. The retry limit does not cut a slow call
short: one attempt may take up to 15 minutes. If calls keep failing, test the model from
your machine with `scripts/check_model.py`
([dev-features.md](dev-features.md#troubleshooting-and-limits)).

## Claude subscription

`--model claude-sonnet-subscription` runs Claude Code on your own Claude plan (Pro, Max,
Team or Enterprise) instead of an API key. It works with `claude-code-none` and
`claude-code-bwrap` only, and only in `agenteval dev`: eval runs refuse the profile.

```sh
claude setup-token        # once, on a machine with a browser; prints a token valid for a year
# put it in <checkout>/.env as CLAUDE_CODE_OAUTH_TOKEN=..., or export it in your shell
agenteval dev --harness claude-code-bwrap --env docker-ubuntu --model claude-sonnet-subscription
```

Why it works differently: Anthropic allows a subscription login only for Claude Code and
its other own apps, and does not allow other tools to send requests with it
([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)). Every
other profile goes through Inspect's bridge, and Inspect would be such a tool. So with a
subscription, Claude Code inside the environment talks to Anthropic itself:

- **The token goes into the environment.** It is copied to
  `/var/lib/agenteval-dev/claude-oauth-token` (owner: the agent user, mode 0400) with
  `docker cp` or `ssh`. Inspect does not copy it, because Inspect logs the files it
  writes. The launcher sets it as `CLAUDE_CODE_OAUTH_TOKEN`. **The agent, and anything it
  runs, can read it.** A prompt injection in the repo could make the agent leak it, for
  example into a file that comes back for review, or into a request to Anthropic made
  with someone else's API key. If you think it leaked, revoke it on claude.ai and run
  `claude setup-token` again.
- **Egress is still denied for the agent user.** A small proxy
  (`/opt/agenteval/egress_proxy.py`) runs in the environment as `nobody`, which the
  egress rule does not cover. It listens on `127.0.0.1:13133` and opens HTTPS tunnels to
  `api.anthropic.com` and `platform.claude.com` (token refresh) only. Claude Code uses it
  through `HTTPS_PROXY`, which a `*-bwrap` harness relays into its sandbox like the
  bridge port. TLS runs end to end, so the proxy never sees the token. Other hosts get
  403 and plain HTTP gets 405. Every request is logged in
  `/var/log/agenteval-egress-proxy.log` in the environment. The host looks up the IPv4
  addresses of both hosts at the start and gives them to the proxy, because on Tart the
  environment's resolver may not forward lookups.
- **On Docker the container gets a network.** It is on Docker's default bridge with
  `NET_ADMIN`, instead of `network_mode: none`, and `egress.sh` rejects the agent user's
  outbound connections with nftables, as on Tart. If that rule cannot be installed, the
  session stops before the CLI starts. The default bridge gives the container your
  machine's DNS servers, not Docker's resolver on loopback, so the rule covers DNS too.
- **Nothing is in the session log.** Inspect has no model in the session (`none/none`),
  so the log has no model calls, tokens or cost. Usage counts against your plan's limits.
  `/status` in Claude Code shows the login it uses.
- **Your settings cannot switch it back.** agenteval's `--settings` has no
  `apiKeyHelper`, and the copy of your `settings.json` loses `apiKeyHelper` and
  `ANTHROPIC_*` / proxy `env` entries as for every session. Any of these would win over
  the token.
- **WebSearch works, WebFetch does not.** Claude Code's web search runs at Anthropic.
  WebFetch fetches from the environment, which the proxy refuses.

## Copy mode and live mode

**Copy** (default): the agent works on a copy. Nothing reaches your machine until you
approve it in the review, file by file. The review is where you notice a changed
Makefile, a new git hook or a CI workflow before it can run on your machine.

**Live** (`--mount live`): your repo folder itself is mounted into the environment, at
the same absolute path as on your machine. The agent changes your files directly, and
there is no review for repo files. Anything it writes there can run on your machine
later: `.git/hooks`, `.git/config` (`core.fsmonitor`, `core.hooksPath`, filters),
Makefiles, `package.json` scripts, `.envrc`, CI and editor task files. agenteval warns
before the session and reports changes to git's own config and hooks afterwards. Use
live mode only for repos and models you trust. agenteval refuses to live-mount a folder
that contains its own checkout (its `.env` holds your API keys). Git directories outside
the repo folder (linked worktrees, submodules) are mounted too. Docker needs Inspect's
explicit opt-in for writable host mounts, which agenteval adds only for live sessions. Tart
shares the folder with `tart run --dir` and mounts it with virtiofs inside the VM. On both
Docker Desktop and Tart the shared files appear owned by the agent user and land on your Mac
as your user; if the agent ever cannot write the folder (e.g. a Linux Docker host), its uid
is changed to the folder owner's for that session. Your CLI config is still copied in and
comes back through the review; `CLAUDE.local.md` is not copied, because the agent sees it
in place.

## Your CLI config and memory

The config source is the CLI's normal folder on your machine: `$CLAUDE_CONFIG_DIR` or
`~/.claude` for Claude Code, `$XDG_CONFIG_HOME/opencode` or `~/.config/opencode` for
OpenCode (or `--agent-config-dir`). If it does not exist yet, it is created when you
first apply something, so settings and memory build up across sessions even on a
machine without the CLI, and a later install of the CLI finds them.

Copied in:

- Claude Code: `settings.json`, `CLAUDE.md`, `commands/`, `agents/`, `skills/`,
  `output-styles/`, and **only this repo's** auto-memory
  (`projects/<repo key>/memory/`). Claude names that folder after the repo's path; the
  copy is renamed to match the path inside the environment and renamed back on return.
- OpenCode: `opencode.json(c)`, `AGENTS.md`, `agent(s)/`, `command(s)/`.
- Claude Code harnesses in copy mode: `CLAUDE.local.md` from the repo root, when git
  ignores it (or the folder is not a repo).

Inside these folders, `.credentials.json`, `.claude.json`, `.DS_Store` and `*.jsonl` files
are skipped and symlinks are followed. A file over 2 MB, or one that would take the copy
over 50 MB in total, is not copied; a note lists it. Memory is not synced when your
`settings.json` sets `autoMemoryDirectory`.

Never copied: credentials, `~/.claude.json` (account and login data, user-level MCP
servers), other projects, session transcripts and history, plugins, OpenCode's data
directory. Before copying, `settings.json` loses command-running credential helpers
(`apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `gcpAuthRefresh`,
`otelHeadersHelper`, `proxyAuthHelper`) and `env` entries that look like secrets or that
would route the CLI past the bridge (`ANTHROPIC_*`, `AWS_*`, proxies, ...); OpenCode
configs lose literal provider keys and headers. agenteval's own bridge settings are passed
on the command line and in environment variables, never written into your files, so they
cannot come back. Hooks, status lines, plugins and MCP servers that cannot work inside the
environment are listed in a note, not fixed.

Coming back: everything that was copied in, changed or new, only through the review.
`settings.json` is merged key by key with what is on your machine now, so keys stripped on
the way in stay as they are on your machine. The environment can never add or change the
credential helpers, `model`, `skipDangerousModePermissionPrompt`, `autoMemoryDirectory` or
secret and routing `env` entries; where both sides changed a key, your value is kept. An
OpenCode config that holds literal keys on your machine is merged the same way, with those
keys protected. Other files are merged 3-way as text; overlapping changes get conflict
markers to edit before applying. Only files in `skills/` can come back executable. A
returned file over 2 MB is left out, and more than 50 MB in total fails the export (step 6
above). Memory goes back under your repo's key on your machine.

Why the review matters: memory, `CLAUDE.md`, commands and settings are instructions that
every later session loads, including sessions on your machine. An agent that read a
prompt injection in the environment could try to persist it there.

## The review

```text
agenteval dev session 20261020T101500Z-my-project-3f2a
  harness opencode-bwrap, model qwen38-27b, environment docker-ubuntu, copy mode
  repo /Users/me/my-project
  usage: 41,210 tokens, $0.0123 (openrouter/qwen/qwen3.8-27b)
Changes to review (6):
  commits         1 fast-forward
  repo files      2 added, 1 modified
  memory          1 added, 1 modified
Flagged for a careful look. Flags are attention pointers, not guarantees: an unflagged
change can still be harmful, so read every diff you apply.
  repo files: Makefile
    [!] make runs its recipes on your host
[y] review now  [l] later (agenteval dev apply 20261020T101500Z-my-project-3f2a)  [d] discard all >
```

At this prompt, `l` (or Ctrl+C) keeps everything pending for `agenteval dev apply <id>`,
and `d` skips every change and marks the session `discarded`.

Then each change as a colored diff from what is on your machine now to what would be
written, in this order: commits, repo files, CLI config, memory, `CLAUDE.local.md`.
Diffs are cut at 2000 lines, and binary files show only their sizes. Color is off when
the output is not a terminal or `NO_COLOR` is set.

| Key | Action |
| --- | --- |
| `y` | Apply this change. |
| `n` | Skip it. |
| `e` | Edit the proposed content in `$VISUAL` / `$EDITOR` (default `vi`), then apply your version. Not offered for binary files, symlinks, deletions, repo-file conflicts or the commits item; an edited `.json` file must be valid JSON. |
| `a` | Apply this and the rest of its group; still stops at each flagged item. |
| `d` | Skip this and the rest of the group. |
| `q` | Quit; what is undecided stays pending for `agenteval dev apply <id>`. |
| `?` | Show this help. |

Details:

- **Commits** are fetched into `refs/agenteval/<session>/heads/<branch>` of your repo; your
  own branches are never a fetch target. The commits item fast-forwards the branch that
  was checked out at the start and updates your index to match (the files are separate
  items). It is not offered when HEAD is no longer on that branch, the branch moved since
  the session started, the session rewrote its history, the commits change Git LFS files,
  submodule files or unsafe paths, your staged changes conflict with them, or another git
  process holds the index; a note then prints the `git merge` command to use yourself.
  Other branches the session created or moved, and a detached HEAD it ended on, are kept
  as refs; a note prints how to get them.
- **Files** are applied one by one with a 3-way rule: if your file is unchanged since the
  session started it is replaced; if you changed it too, the two are merged (conflict
  markers if they overlap). A change that cannot be merged (binary, or you deleted the
  file or put a directory there) is a conflict: applying it writes the session's version
  to the session's `conflicts/` folder and leaves your file alone. A file the session
  deleted but you changed is kept, with a note. Paths that are absolute, have a `..` or
  `.git` component or control characters, or lead through a symlink are refused, with a
  note. File items never touch your index (staging).
- **Flags** point at changes that deserve a careful look. They are heuristics, not a
  verdict; an unflagged change is not "safe". Repo files that run on your machine
  (Makefiles, justfiles, Taskfiles, `package.json` and `pyproject.toml` scripts,
  `setup.py`, `conftest.py`, tox and nox, package manager config, CI, pre-commit and git
  hook config, VS Code and dev container config, `.envrc`, `.gitattributes`,
  `.gitmodules`, tool-version files, agent config, new executable bits, symlinks pointing
  outside the repo or into `.git`); in config and memory: install commands, `curl | sh`,
  `sudo`, URLs, "don't ask" or "ignore previous instructions" phrasing,
  permission/hook/MCP/env settings, new or changed commands, agents and skills,
  definitions that allow the Bash tool, executable files in skills, invalid JSON,
  `MEMORY.md` lines pointing at missing files.

## Sessions and cleanup

- Session files: `<checkout>/.agenteval/sessions/<id>/`: `session.json`, the snapshot
  sent in (`in.bundle` and `meta.json`, plus `shadow.git/` for a folder that is not a git
  repo), `config.tar` and `config-return.tar` (CLI config sent in and returned),
  `commits.bundle`, `worktree.patch` (readable diff of all file changes), `config/` (with
  `config.patch`, a readable diff of the returned CLI config and memory) and `conflicts/`.
- Status, as `agenteval dev list` shows it: `running`; `pending` (changes wait for
  `agenteval dev apply`); `applied` (every change decided, even if you skipped each one);
  `discarded` (`d` at the first review prompt); `failed` (the session printed its error
  and where its record and log are). `agenteval dev apply` compares each change with your
  machine as it is then, and does not show changes you already decided.
- Logs: `<checkout>/logs/dev/<id>/`.
- Your repo keeps the agent's commits after the review (or a failed session):
  `refs/agenteval/<session>/heads/*`, and `refs/agenteval/<session>/vm-head` when the
  session ended on a detached HEAD (the other internal refs are deleted; a pending session
  keeps them all). They show in `git log --all`; remove them
  with `git for-each-ref --format='%(refname)' refs/agenteval | xargs -n1 git update-ref -d`.
  That also deletes the refs of pending sessions, which `agenteval dev apply` reads, so
  run it only when no session is pending.
- A session killed mid-way can leave its container or VM running: `agenteval env cleanup`
  removes it.

## Limits

- Terminal only; no browser UI.
- The bridge returns whole responses: text appears when the model has finished a turn,
  not token by token, and each call can take up to about 2 s longer than direct. One
  attempt at a call may take up to 15 minutes; a longer one is abandoned and retried.
  OpenCode waits up to an hour for a reply, instead of its default 5 minutes, after which
  it would send the whole turn again.
- OpenCode's TUI has no system-prompt flag, so a harness `system_prompt` only applies to
  eval runs.
- Claude Code with a non-Claude model (for example `qwen38-27b`) shows the model's
  reasoning as raw `<think>` text, because the bridge returns reasoning that way. Claude
  models and OpenCode are not affected.
- Both shipped environments deny egress, so the agent's tools have no network in any
  harness: `npm install` or `pip install` fail by design. With a Claude subscription
  the agent can reach the two Anthropic hosts, and nothing else. Inside `*-bwrap` harnesses the
  system is also read-only. Dependencies built on macOS (`node_modules`, `.venv`) do not
  run in Linux anyway; they are not copied in copy mode.
- Dev mode starts the real CLI binary, so a harness version that resolves to the
  `/usr/local/bin` wrapper is refused. Pin the version, as all shipped profiles do.
  Harness `options` (inspect_swe arguments) are not used in dev mode.
- OpenCode harnesses need the model profile's `agent_models.opencode` (or a harness
  `agent_model`). `claude-sonnet-api` and `claude-sonnet-bedrock` have none, so those
  sessions fail after the environment has started. It must be a model id from OpenCode's
  catalog, such as `openrouter/qwen/qwen3.8-27b`, not the provider's id: OpenCode runs an
  unknown id as a custom model with no known limits (context 0, output 0) and reasoning
  off.
- Images, the clipboard, IDE integration (`/ide`) and OS notifications do not cross into
  the environment.
- Git LFS files go in with their real content and are kept out of the agent's commits;
  submodules are copied as plain files (their history stays on your machine).
- Byte-exact copies need git 2.42 or newer on your machine; older versions apply
  line-ending conversions, which can show up as extra merges.
- Ctrl+C at an agenteval prompt (not inside the CLI) answers it: at the resume prompt it
  finishes (the export still runs), at the export-failure prompt it abandons and deletes
  the environment, and in the review it leaves the rest pending. Anywhere else (the
  500 MB snapshot question, setup) it stops the session; an environment that has already
  started is deleted.
- Tart sessions need `tart` on your PATH, and the app you run agenteval from (your terminal,
  or the Claude app for Claude Code) needs macOS **Local Network** access (System Settings →
  Privacy & Security → Local Network) to reach the VM over SSH. Without it the session
  fails with "SSH ... not ready ... No route to host" while `tart exec` into the VM still
  works. Each Claude Code update may add a new "claude" entry there that needs enabling.
