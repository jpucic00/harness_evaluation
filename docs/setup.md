# Setting up a host machine

This guide sets up a new Mac from scratch. Run every `agenteval` command from the repository
root, except `agenteval dev`, which runs from the project you work on (see [dev.md](dev.md)).

1. [Requirements](#1-requirements)
2. [uv and Python 3.12](#2-uv-and-python-312)
3. [The project](#3-the-project)
4. [Docker Desktop](#4-docker-desktop)
5. [Tart (manual install)](#5-tart-manual-install)
6. [Building the environment images](#6-building-the-environment-images)
7. [The OpenRouter API key](#7-the-openrouter-api-key)
8. [Doctor and first runs](#8-doctor-and-first-runs)
9. [Data egress](#data-egress)
10. [Troubleshooting](#troubleshooting)

## 1. Requirements

| Item | Needed for | Notes |
| --- | --- | --- |
| Mac with Apple silicon (M1 or later) | Tart | Tart runs Linux VMs through Apple's Virtualization.framework. Docker-only use also works on other hosts. |
| Recent macOS | everything | Developed on macOS 26. Check Tart's README for its minimum version. |
| RAM | parallel samples | `tart-ubuntu` boots 2 VMs at once with 4 GB each, and `docker-ubuntu` runs 4 containers. 16 GB or more is comfortable. |
| Disk | images | Allow tens of GB for the Tart base image, the Docker image and logs. Tart clones are copy-on-write, so per-sample VMs are cheap. |
| Command line tools | `git`, `ssh`, `ssh-keygen`, `shasum`, `curl`, `tar` | All ship with macOS. Install the Xcode Command Line Tools if `git` asks for them. `agenteval dev` copies your repo byte for byte only with git 2.42 or newer. |
| uv, Python 3.12 | the project | See step 2. |
| Docker Desktop | `docker-ubuntu` environment | See step 4. |
| Tart 2.41.1 | `tart-ubuntu` environment | See step 5. |
| OpenRouter API key | `qwen38-27b` model | Not needed for the `mock` model. See step 7. |

## 2. uv and Python 3.12

[uv](https://docs.astral.sh/uv/) manages the virtual environment and the locked dependencies
(`uv.lock`). Install it with its standalone installer or with Homebrew (`brew install uv`); see
the [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/). Then
install Python:

```sh
uv --version
uv python install 3.12
```

## 3. The project

```sh
git clone <repository-url> harness_evaluation
cd harness_evaluation
uv sync --python 3.12    # creates .venv with the pinned packages, including dev tools
uv run agenteval --help
uv run pytest -q         # optional: about 870 unit tests, no network needed; Docker- and Tart-marked ones skip until their image is built, and the dev-mode ones then download the agent CLIs once
```

`uv sync` installs the exact versions in `uv.lock`: `inspect-ai` 0.3.281, `inspect-swe` 0.2.71,
`inspect-evals` 0.25.0 and `openai` 3.29.0, which Inspect's OpenRouter provider needs. It also
registers agenteval's `tart` sandbox with Inspect through a package entry point.

To run `agenteval dev` from your other projects, install the checkout once as an editable
tool:

```sh
uv tool install --editable .    # then `agenteval dev` works from any folder
```

Without it, use `uv run --project <checkout> agenteval dev ...`. A non-editable install does
not work: `agenteval dev` finds the checkout (profiles, `.env`, logs, session files) from its
own source location.

## 4. Docker Desktop

The Docker environment is the local validation path. It needs no VM tooling and no API key.

1. Install [Docker Desktop for Mac (Apple silicon)](https://docs.docker.com/desktop/setup/install/mac-install/)
   and start it.
2. In Settings -> Resources, give it enough memory. `docker-ubuntu` runs up to 4 containers with
   a 4 GB limit each. Give Docker at least 8 GB, or lower `max_concurrent` in
   [config/environments/docker-ubuntu.yaml](../config/environments/docker-ubuntu.yaml).
3. Check that the daemon and Compose v2 work (Inspect drives containers through `docker compose`):

   ```sh
   docker info --format '{{.ServerVersion}}'
   docker compose version
   ```

## 5. Tart (manual install)

[Tart](https://github.com/openai/tart) runs the per-sample Linux VMs. The old
`github.com/cirruslabs/tart` repository redirects there. The Homebrew formula is currently
broken, so install the signed release tarball by hand. When Tart is missing, `agenteval doctor`
points to the [releases page](https://github.com/openai/tart/releases); use these steps.

```sh
TART_VERSION=2.41.1
TART_SHA256=e680a734febccfce7d4e910c1f7f13fc3b00f19a181c945e2d746de3214c32c2

mkdir -p ~/Downloads/tart-$TART_VERSION && cd ~/Downloads/tart-$TART_VERSION
curl -fL -o tart.tar.gz \
  "https://github.com/openai/tart/releases/download/$TART_VERSION/tart.tar.gz"
echo "$TART_SHA256  tart.tar.gz" | shasum -a 256 -c -     # must print "tart.tar.gz: OK"

mkdir -p ~/.local/opt/tart-$TART_VERSION ~/.local/bin
tar -xzf tart.tar.gz -C ~/.local/opt/tart-$TART_VERSION   # contains tart.app/ and LICENSE
ln -sfn ~/.local/opt/tart-$TART_VERSION/tart.app/Contents/MacOS/tart ~/.local/bin/tart

command -v tart      # must print ~/.local/bin/tart; if not, add ~/.local/bin to PATH in ~/.zshrc
tart --version       # 2.41.1
cd -
```

The checksum is the SHA-256 digest GitHub lists for the `tart.tar.gz` asset of release
[2.41.1](https://github.com/openai/tart/releases/tag/2.41.1). Do not continue if `shasum`
reports a mismatch. Release tags have no `v` prefix.

If `tart` misbehaves when started through the symlink (for example, it cannot find its bundle
resources), replace the symlink with a small wrapper script. The Homebrew formula does the same:

```sh
rm ~/.local/bin/tart
printf '#!/bin/sh\nexec "%s" "$@"\n' "$HOME/.local/opt/tart-2.41.1/tart.app/Contents/MacOS/tart" > ~/.local/bin/tart
chmod 755 ~/.local/bin/tart
```

agenteval finds `tart` through the `PATH` of the shell or app you run it from, so any install
folder works. If you keep Tart elsewhere (for example an extracted `tart.app` in another
folder), link or wrap its `tart.app/Contents/MacOS/tart` into a folder on `PATH` as above, and
check with `command -v tart`. agenteval reaches the VMs over SSH, so that app also needs macOS
Local Network access (see [Troubleshooting](#troubleshooting)).

**Licence.** Tart's repository `LICENSE` is the Functional Source License 1.1 with an Apache 2.0
future licence (`FSL-1.1-ALv2`), copyright OpenAI. The FSL restricts some uses. **Have legal
confirm** that this use is covered before relying on Tart internally.

**Recommended: shorter DHCP leases.** On the default `nat` network, every sample VM gets a new
MAC address and so a new DHCP lease from macOS (`bootpd`). The default lease lasts 24 hours
(86400 s), and the VM subnet has room for about 250 leases, so a busy day of runs can exhaust
it. `agenteval doctor` and the start of every Tart task warn when the lease is longer than one
hour. Tart's own install notes recommend 10 minutes. This changes a macOS system setting, so run
it yourself:

```sh
sudo defaults write /Library/Preferences/SystemConfiguration/com.apple.InternetSharing.default.plist bootpd -dict DHCPLeaseTimeSecs -int 600
```

The alternative is `tart.network: softnet` in the environment profile (see
[Troubleshooting](#troubleshooting)). See [Tart's FAQ](https://tart.run/faq/) for details.

## 6. Building the environment images

Each environment needs an image, built once. Every sample then starts from a fresh copy.
`agenteval dev` sessions start from the same images and need no extra build.

### Docker: `uv run agenteval env build docker-ubuntu`

- Runs `docker build -t agenteval/ubuntu-24.04:latest` on
  [src/agenteval/environments/provision/](../src/agenteval/environments/provision/), with
  `FROM ubuntu:24.04` plus [provision.sh](../src/agenteval/environments/provision/provision.sh).
  The environment's `agent_user` and `workspace` are passed as build arguments.
- `provision.sh` installs packages (bubblewrap, socat, python3, pytest, git, nftables, ...),
  creates the unprivileged user `agent` (no sudo, locked password), `/workspace`,
  `/opt/agenteval`, `/var/lib/agenteval` (root only) and `/etc/agenteval/secrets`, and sets a
  system git identity. It makes `/usr/bin/bwrap` executable only by root and the group
  `agenteval-bwrap` (`root:agenteval-bwrap`, mode 0750); the agent user joins that group per
  sample, and only in cells whose harness uses bubblewrap. It writes
  `/etc/agenteval/image.json` (agent user, workspace, sha256 of `provision.sh`), so a built image
  can be matched to the profile and script that produced it.
- `--force` rebuilds without the layer cache and pulls the base image again.
- Samples never pull this image from a registry. If it is missing, the run fails at once.
- Each run records the image ID and the hash of the current `provision.sh` in the eval log
  metadata.

### Docker Sandboxes: `uv run agenteval env build sbx-ubuntu`

`sbx-ubuntu` runs every sample in its own Docker Sandboxes microVM, built from the same image
as `docker-ubuntu`.

1. Install Docker Sandboxes (on macOS it is the Homebrew cask `sbx`; see
   https://docs.docker.com/ai/sandboxes/install/) and sign in with `sbx login`. Use the company
   Docker account: Docker Sandboxes is a separate product under Docker's own terms, and it
   requires a sign-in even for local sandboxes.
2. Set the global network policy to deny-all: `sbx policy init deny-all`. agenteval's samples
   need no network; the agent user's egress is also denied inside the VM by `egress.sh`.
3. Turn SSH agent forwarding off: `sbx settings set ssh.agentForwardingEnabled false`, then
   `sbx daemon restart`. With it on, every sandbox can use the keys in your SSH agent, so
   agenteval refuses to start a sample (and checks inside each new sandbox that no agent
   answers). `doctor` also warns while `browser.enabled` is true (sandboxed processes may open
   URLs in your browser); consider `sbx settings set browser.enabled false`.
4. `uv run agenteval env build sbx-ubuntu` builds the Docker image (as for `docker-ubuntu`),
   then derives the template `agenteval/ubuntu-24.04-sbx` from it. The template adds one label,
   `com.docker.sandboxes.start-docker="true"`, which gives root in the sandbox its full
   capability set as in Docker's own templates. Without it root lacks `CAP_NET_ADMIN`, and
   `egress.sh` cannot install the rule that blocks the agent user. The agent never runs as
   root, and the microVM is the boundary, as in Tart. The build copies the template into sbx's
   own image store with `docker save` and `sbx template load`, because sbx cannot see Docker
   Desktop's images, and checks that the loaded template has the Docker image's ID. Run it
   again after rebuilding the Docker image.

Commands for a non-root user run as root and switch with `runuser`. `sbx exec -u USER`
would start them with the user's primary group only, so the per-sample `agenteval-bwrap`
membership would be lost.

How sbx networking reads in the containment results: every TCP connection from the VM goes to
sbx's host-side proxy, which accepts it inside the VM and then applies its policy. Without the
in-VM rule, `net.internet_tcp` and `net.host_gateway` would report connections as established
even though no byte left the VM (`sbx policy log` lists them as blocked). With the rule, the
agent's connections are rejected in the VM, as in Tart. `host.shared_mounts` reports
`/etc/resolv.conf`, a read-only virtiofs file that sbx shares to configure DNS.

agenteval runs every `sbx` command with `SBX_NO_TELEMETRY=1`. sbx adds about forty variables to
every command in a sandbox (proxy settings, placeholder API keys, a git config override, its CA
bundle); agenteval removes all that the image does not define, so commands see the same
environment as in Docker and Tart. Sandboxes are named `agenteval-sbx-<8 hex>`;
`uv run agenteval env cleanup` removes leftovers. Not supported yet: Claude subscription
sessions in `agenteval dev`, and per-sample figures in the report's Resources section (sbx is
shown as one group).

### Tart: `uv run agenteval env build tart-ubuntu`

1. Creates the SSH key `~/.agenteval/ssh/id_ed25519` (ed25519, no passphrase, mode 0600) if it
   does not exist yet.
2. Clones `ghcr.io/cirruslabs/ubuntu:24.04` to a temporary VM named `agenteval-build-<hex>`. The
   first build downloads the base image (several GB) into Tart's cache.
3. Boots it headless, with no host folders shared.
4. Installs the public key for the image's default user `admin`. This uses the image's public
   default password (`admin`) **once**. The password is passed to `ssh` through a private
   `SSH_ASKPASS` helper and an environment variable, never on a command line or on disk.
5. Uploads and runs `provision.sh` with sudo (the same script as Docker), passing the
   environment's `agent_user` and `workspace`, so custom values work on Tart too. In the VM it
   first masks the periodic package jobs (`apt-daily`, `apt-daily-upgrade`,
   `unattended-upgrades`, `motd-news`, the snap repair timer) and holds snap refreshes, so no
   sample clone downloads or installs packages in the background. apt commands are retried
   while another process holds the package lock. On Ubuntu 24.04 it also installs an AppArmor
   profile that lets bubblewrap create user namespaces.
6. Hardens SSH: password and keyboard-interactive logins and root login are disabled, and
   `admin`'s password is locked. It then checks that key login still works and that a password
   login is refused.
7. Shuts the VM down and renames it to `agenteval-ubuntu-24.04`.

If the image already exists, the command does nothing; use `--force` to rebuild. If the image
exists but the SSH key is missing, the build refuses: the image only accepts the key it was
built with, so rebuild with `--force` (which creates a new key). A failed build deletes its
temporary VM, so you never get a half-provisioned image. `admin` keeps passwordless sudo,
because agenteval runs root-side setup and scoring over SSH as `admin` + `sudo`. The agent runs
as `agent`, which has no sudo.

After changing `agent_user` or `workspace` in the profile, rebuild with `--force`. A sample
whose user or workspace does not exist in the image fails at start with a rebuild hint.

## 7. The OpenRouter API key

The `qwen38-27b` model profile calls Qwen 3.8 through [OpenRouter](https://openrouter.ai/) and
needs `OPENROUTER_API_KEY`.

- **Recommended: export it in the shell that runs agenteval.** It then never touches the disk.

  ```sh
  read -rs OPENROUTER_API_KEY && export OPENROUTER_API_KEY   # paste, press Enter; not echoed or kept in history
  ```

  Optionally, keep it in the macOS Keychain instead of retyping it:

  ```sh
  security add-generic-password -a "$USER" -s agenteval-openrouter -w          # prompts for the key
  export OPENROUTER_API_KEY="$(security find-generic-password -a "$USER" -s agenteval-openrouter -w)"
  ```

- **Alternative: a `.env` file** in the repository root with the line
  `OPENROUTER_API_KEY=...`. `.env` is gitignored. agenteval loads it (python-dotenv, searched
  upward from the current directory, like Inspect does) before it checks required variables;
  a variable already set in the shell wins. `agenteval dev`, which runs from your own
  project, reads the checkout's `.env`, not your project's. Never commit it, and never put
  the key in your shell profile.
- **It stays on the host.** Inspect makes the model calls from the host process. Inside the
  VM, OpenCode only gets a placeholder key (`OPENROUTER_API_KEY=sk-none`) and talks to a local
  bridge (see [architecture.md](architecture.md#model-calls-the-bridge)). The VM never sees the
  real key, and it does not appear in eval logs.
- `agenteval doctor`, `agenteval run` and `agenteval dev` report a missing key by name only,
  never its value. `doctor` also says which `.env` file it loaded.
- Set a credit limit on the key in the OpenRouter dashboard. Per-sample `token_limit` (and
  optionally `cost_limit`) in the run file add a second cap.

### Other providers

The same rules apply to every provider: the key is read on the host (shell or `.env`), and
the agent inside the environment only ever sees a placeholder.

| Model profiles | Variables | Notes |
| --- | --- | --- |
| `gateway-gpt-oss-120b`, `gateway-devstral-2512`, `gateway-qwen` | `GATEWAY_BASE_URL` (including `/v1`), `GATEWAY_API_KEY` | The company's OpenAI-compatible gateway, via Inspect's `openai-api/gateway/<model>` provider. A gateway model id goes after that prefix, so the gateway's `qwen/qwen3.8-27b` is `openai-api/gateway/qwen/qwen3.8-27b`; without the prefix Inspect reports `Model API qwen ... not recognized`. **Direct connection:** the profiles set `connection: {proxy: direct}`, so the host calls the gateway directly over the VPN and ignores `HTTP(S)_PROXY`, `ALL_PROXY` and the macOS proxy settings (the company proxy drops gateway calls). Optional, in `.env` or the shell: `SSL_CERT_FILE`, a PEM CA bundle that trusts the gateway's certificate (default: the OS trust store, including the macOS Keychain and the Windows certificate store, as for every provider; `SSL_CERT_FILE` replaces it for agenteval's other HTTPS calls too, so keep the public CAs in it), and a client certificate if the gateway asks for one: `MTLS_CERT_FILE` (plus `MTLS_KEY_FILE` for a separate key and `MTLS_KEY_PASSWORD` for an encrypted one), or a `.p12`/`.pfx` bundle in `MTLS_PKCS12_FILE` (plus `MTLS_PKCS12_PASSWORD`). These are the names the agentic-test-automation project uses. A `.p12`/`.pfx` needs `uv sync --extra mtls` (a later plain `uv sync` removes it again; the `agenteval dev` tool install needs `uv tool install --editable . --with cryptography`). No pricing: cost shows as unknown unless you add an internal rate. |
| `claude-sonnet-api` | `ANTHROPIC_API_KEY` | An Anthropic **Console API key**. Eval runs cannot use a Claude subscription: Claude Code's calls go through Inspect, and Anthropic allows subscription logins only for Claude Code itself. |
| `claude-sonnet-subscription` | `CLAUDE_CODE_OAUTH_TOKEN` | Your Claude plan, from `claude setup-token`. **`agenteval dev` only**, with `claude-code-none` / `claude-code-bwrap`. Unlike every other profile, the token goes into the environment and Claude Code calls Anthropic directly through an allow-listing proxy; see [dev.md](dev.md#claude-subscription). |
| `claude-sonnet-bedrock` | `AWS_REGION`, plus AWS credentials from the standard chain (`AWS_PROFILE` for SSO or a named profile, or env keys) | Amazon Bedrock via Inspect's `anthropic/bedrock/<model>`. The profile uses the **global** inference profile; switch to the `eu.`/`us.` one if data must stay in one geography. Run `aws sso login` first when using SSO. |

`.env` has empty entries for all of these except `CLAUDE_CODE_OAUTH_TOKEN`; fill in what you use, and add `CLAUDE_CODE_OAUTH_TOKEN=` if you use a subscription.

## 8. Doctor and first runs

```sh
uv run agenteval doctor                  # everything; problems are warnings
uv run agenteval doctor --env tart-ubuntu   # problems with this environment are failures (exit 1)
```

It checks Python, uv, package versions, the Inspect entry point, that every profile and run
file parses, the Docker daemon, Tart, built images, the SSH key and its permissions, the macOS
DHCP lease time (Tart `nat` network), and whether each model's required variables are set (from
the shell or `.env`). On a Linux Docker host it also warns when
`kernel.apparmor_restrict_unprivileged_userns=1`, which makes bwrap cells fail (see
[Troubleshooting](#troubleshooting)). Suggested order for the first runs:

| Step | Command | Cost |
| --- | --- | --- |
| 1. Docker smoke run | `uv run agenteval run smoke-docker` | free, offline |
| 2. Tart with the mock model | `uv run agenteval run opencode-qwen-tart --model mock --limit 2` | free; only package downloads |
| 3. Tart containment only | `uv run agenteval run opencode-qwen-tart --suite containment` | free (no model calls), but needs the key set |
| 4. One sample per task | `uv run agenteval run opencode-qwen-tart --limit 1` | small; check the Economics section |
| 5. Full run | `uv run agenteval run opencode-qwen-tart` | 6 tasks: 2 x (1 + 6 + 12) samples |

Open each result with `uv run agenteval report open` and `uv run agenteval view`.

To test a model profile on its own, before a run or when its calls fail, call it once from
the host, outside any harness or environment:

```sh
uv run python scripts/check_model.py gateway-qwen   # a profile name; also --timeout SECONDS, --direct
```

If the profile has a `*_BASE_URL` variable, the script first prints the proxies Python
would use (from the environment or the macOS settings) and checks DNS, TCP and TLS to that
host one step at a time. It then makes one call with the profile's `connection` settings and
prints the answer, or the whole error chain. `--direct` ignores proxies for any profile.
Keys and proxy values are never printed.

### Interactive sessions: `agenteval dev`

`agenteval dev` attaches your terminal to a harness CLI running in one environment, working on
the project you start it from. See [dev.md](dev.md), and [dev-features.md](dev-features.md)
for the command that starts each harness. It uses the same images, and this checkout's
profiles and `.env`, so it needs nothing beyond the steps above, plus:

- the editable tool install from step 3, or `uv run --project <checkout> agenteval dev ...`;
- an interactive terminal: it refuses to start when stdin or stdout is not a terminal;
- for `tart-ubuntu`, `tart` on `PATH` and Local Network access for the app you run it from
  (step 5);
- for the review's edit key (`e`), `$VISUAL` or `$EDITOR` set to an editor command that waits
  until you close the file. The default is `vi`.

A free first session (the mock model answers with a fixed text), from a project of yours:

```sh
cd ~/src/my-project
agenteval dev --harness opencode-bwrap --env docker-ubuntu --model mock
```

## Data egress

What leaves the machine, where it goes and when:

| Data | Destination | When | Notes |
| --- | --- | --- | --- |
| Prompts, repository code, tool output, model replies | OpenRouter and the upstream provider it routes to | Every model call in `capability` and `injection` samples, including CodeIPI's grader (model role `grader`, bound to the evaluated model), and in `agenteval dev` sessions | **Third party.** The profile sets OpenRouter provider routing `data_collection: deny`, which only routes to providers that do not collect request data. They still receive it. The built-in tasks are synthetic or public; an `agenteval dev` session sends code from your own repo. Get approval before pointing it at internal code. |
| Prompts, code, tool output (Claude profiles) | Anthropic API, or Amazon Bedrock in your AWS account | Every model call when a `claude-sonnet-*` profile is the task or subagent model | Third party (Anthropic) or your cloud provider (Bedrock). With the global Bedrock inference profile, requests may be served in any region. |
| Prompts, code, tool output (`claude-sonnet-subscription`) | Anthropic (`api.anthropic.com`; `platform.claude.com` for token refresh) | Every model call of an `agenteval dev` session with that profile | Third party, under your Claude plan's terms. Sent **from the environment** through its egress proxy, not from the host. Not in the session log. |
| Prompts, code, tool output (gateway profiles) | The company gateway | Every model call when a `gateway-*` profile is used | Internal. Sent from the host directly over the VPN, not through a proxy (the profiles' `connection: {proxy: direct}`). |
| Claude Code release manifest and binary | `claude.ai/install.sh` (to find the release bucket), `downloads.claude.ai` | First use of a Claude Code version | Downloaded **on the host** by inspect_swe, checked against the SHA-256 in the release manifest, cached, then copied into the environment. Inside the environment Claude Code runs with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and only reaches the bridge (or, on a subscription, the two Anthropic hosts). |
| OpenCode release metadata and binary | `api.github.com`, GitHub release assets (`anomalyco/opencode`) | First use of a version | Downloaded **on the host** by inspect_swe, checked against the SHA-256 digest GitHub publishes, cached in `~/Library/Caches/inspect_swe`, then copied into the environment over exec. The environment never downloads anything. |
| Node.js, ripgrep | `nodejs.org`, GitHub (`BurntSushi/ripgrep`) | First use | Runtime dependencies of OpenCode, downloaded on the host and cached like the above. inspect_swe 0.2.71 does not verify their checksums. |
| Inspect sandbox tools | `inspect-sandbox-tools.s3.us-east-2.amazonaws.com` | First use | Inspect's in-environment helper used by the model bridge. SHA-256 verified, cached in `~/Library/Caches/inspect_ai`. |
| Base image `ubuntu:24.04` | Docker Hub | `env build docker-ubuntu` | |
| Base VM `ghcr.io/cirruslabs/ubuntu:24.04` | GitHub Container Registry (Cirrus Labs) | First `env build tart-ubuntu` | |
| Docker account sign-in, sbx telemetry and diagnostics | Docker (`login.docker.com`; telemetry `marlin-*.docker.com`; diagnostics S3 bucket) | `sbx login`; any `sbx` use | Docker Sandboxes requires the sign-in. agenteval sets `SBX_NO_TELEMETRY=1` on every `sbx` call; set it in your shell too when you run `sbx` yourself. What the diagnostics upload contains is not documented. |
| Ubuntu packages | Ubuntu mirrors (arm64: `ports.ubuntu.com`) | Image builds only | Samples do not install packages. |
| Tart | GitHub releases (`openai/tart`) | Manual install | SHA-256 checked by you (step 5). |
| Python packages | PyPI | `uv sync` | Pinned by `uv.lock`. |

Everything else stays local. The harness profiles set `npm_config_offline: "true"`, so OpenCode
does not fetch its plugin package from npm inside the environment (that traffic would also be
counted as an agent egress attempt). Eval logs in `logs/` hold full transcripts, including
code, tool output and the fake canary tokens; reports in `reports/` are built from them. Both
directories are gitignored. `agenteval dev` writes its logs to `logs/dev/` and keeps each
session's repo snapshot, copied CLI config and returned changes in `.agenteval/sessions/`,
which is gitignored too. The mock model makes no network calls at all.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `inner sandbox 'bwrap' failed to start as user 'agent'` / `bubblewrap could not set up the sandbox ... the command was NOT run` / `/usr/bin/bwrap is not executable for uid ...` | bubblewrap cannot start. The wrapper fails closed: it never runs the agent unsandboxed. **Not executable:** only root and the group `agenteval-bwrap` may run bwrap; agenteval adds the run user to that group in bwrap cells only. **Tart:** Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor. `provision.sh` installs `/etc/apparmor.d/agenteval-bwrap` for this. Rebuild the image with `uv run agenteval env build tart-ubuntu --force`, and check inside a debug VM (below) with `sudo aa-status \| grep agenteval-bwrap`. **Docker:** bwrap cells need `bwrap_security_opt` (next row); do not empty it for them. On a Linux Docker host with `kernel.apparmor_restrict_unprivileged_userns=1` (`doctor` warns), the host's AppArmor policy applies inside the container too: the host needs an AppArmor profile granting `userns` to `/usr/bin/bwrap`, the sysctl set to 0, or the environment on `runtime: runsc`. |
| Docker `security_opt` and its trade-off | Cells without an inner sandbox (`opencode-none`) run with stock Docker confinement (`security_opt: []` by default). Cells whose harness uses bubblewrap (`opencode-bwrap`) also get `bwrap_security_opt`: `seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined`. bubblewrap needs them: Docker's default seccomp profile blocks user namespaces, and mounting a fresh `/proc` is refused while Docker masks `/proc` paths. The cost is real: inside those containers root can write global `/proc/sys` entries (for example `kernel.core_pattern`, which affects the whole Docker host kernel; on a Mac that is Docker Desktop's Linux VM) and `/proc/sysrq-trigger`, and every user can create user namespaces. The agent runs as the unprivileged `agent`, and agenteval's own scorers never run agent-written code as root (CodeIPI's scorer is the exception, see [Known limitations](security-tests.md#known-limitations)). Fine for local validation, but do not read these results as those of a hardened container. On a shared Docker host, run bwrap cells under gVisor (`runtime: runsc`, see [extending.md](extending.md#a-different-docker-runtime-gvisor-kata)). |
| `SSH on admin@192.168.64.x not ready ... No route to host` (Tart), while `tart exec <vm> true` works | macOS Local Network privacy blocks the app you run agenteval from (terminal, IDE, or the Claude app) from reaching the VM. Allow it in System Settings → Privacy & Security → Local Network and restart that app. Check with `ping <VM IP>` from that app: a blocked app gets `sendto: No route to host` at once. |
| `Tart VM ... got no IP within 180s`, many runs on the same day | DHCP lease exhaustion. Each sample VM uses a new MAC address and holds a lease for 24 hours by default; `doctor` and each Tart task warn about it. Shorten the lease time (step 5) or use `tart.network: softnet`. Stale leases expire on their own, or can be removed from `/var/db/dhcpd_leases` (root-owned; do it while no Tart VM runs). See [Tart's FAQ](https://tart.run/faq/). |
| `Tart image ... exists but its SSH key ... is missing` | The image only accepts the key it was built with. Rebuild with `uv run agenteval env build tart-ubuntu --force` (creates a new key). |
| `Working directory ... does not exist in Tart image` / `User ... does not exist in Tart image` | The image was built for another `workspace` or `agent_user`. Rebuild with `--force`. |
| `sandbox-exec: warning: running as root (uid 0); root runs are a weaker boundary` | The agent runs as root inside bubblewrap (e.g. injection with `agent_user: root`). Root keeps uid 0, so root-owned IPC endpoints still visible inside the sandbox can act outside it. Use an unprivileged user for meaningful bwrap results. |
| `network: softnet` hangs or fails | Softnet isolates VMs from the host and from each other, but needs the separate `softnet` helper ([openai/softnet](https://github.com/openai/softnet)) on `PATH`, made SUID root or allowed through passwordless sudo. Tart asks for the sudo password interactively on first use, which cannot happen while agenteval runs `tart run` in the background. Set it up once in a terminal, or keep `network: nat` (the default). Softnet has not been tested with agenteval yet. |
| Leftover VMs or containers after Ctrl-C or a crash | `uv run agenteval env cleanup` deletes every per-sample VM (`agenteval-<8 hex>`) and every Docker compose project created from `.agenteval/compose/`, including those still in use, such as a running `agenteval dev` session: run it only when nothing else is running. It never touches the image `agenteval-ubuntu-24.04`. Build leftovers (`agenteval-build-<hex>`, only after a hard kill) are not matched: find them with `tart list` and remove them with `tart delete NAME`. |
| `Tart image 'agenteval-ubuntu-24.04' not found` / `SSH key ... not found` | Run `uv run agenteval env build tart-ubuntu`. |
| `missing required environment variables: OPENROUTER_API_KEY` | Export it in this shell (step 7). The mock model needs no key: `--model mock`. |
| `missing required environment variables: GATEWAY_API_KEY, GATEWAY_BASE_URL` from `agenteval dev` with a `claude-code-subagents-*` or `claude-code-opencode-*` harness | Their default subagent model is `gateway-qwen`, which needs the gateway variables (step 7). Pass `--subagent-model`, for example `--subagent-model mock-worker` with `--model mock`, or `--subagent-model qwen38-27b`. |
| `Server disconnected without sending a response` / `APIConnectionError: Connection error.` with a `gateway-*` profile (in an `agenteval dev` OpenCode session: `Connection error.`, then OpenCode retries) | Run `uv run python scripts/check_model.py <profile>` first (step 8): it shows which step fails. Inspect calls the gateway from the host, so the **host** needs the VPN; the environment does not. The company proxy drops gateway calls with this error, so the profile needs `connection: {proxy: direct}`. The shipped `gateway-*` profiles have it; copy it into your own. `--direct` tries a direct call with any profile. DNS or TCP failing: check the VPN and the host name in `GATEWAY_BASE_URL`. TLS failing: next row. |
| `CERTIFICATE_VERIFY_FAILED` or a refused TLS handshake (`HANDSHAKE_FAILURE`, `CERTIFICATE_REQUIRED`) with the gateway | `check_model.py` shows which of the two it is. **Certificate not trusted:** the OS trust store (or the `SSL_CERT_FILE` you set) lacks the company CA. Install the CA in the OS store, or append the company CA certificate(s) (PEM) to a copy of certifi's bundle (`uv run python -m certifi` prints its path) and set `SSL_CERT_FILE` to that file in `.env`; agenteval's other HTTPS calls use it too. **Handshake refused:** the gateway wants a client certificate: set `MTLS_CERT_FILE` (and `MTLS_KEY_FILE`, `MTLS_KEY_PASSWORD`) or `MTLS_PKCS12_FILE` (and `MTLS_PKCS12_PASSWORD`; needs `uv sync --extra mtls`). The client certificate is used only for profiles with `connection`, as the gateway profiles have. See [Other providers](#other-providers). |
| `The session failed: ... has no agent_models.opencode entry` (`agenteval dev` with an `opencode-*` harness) | The OpenCode TUI needs the model profile's `agent_models.opencode`. `claude-sonnet-api` and `claude-sonnet-bedrock` have none; use a model that has one, such as `mock` or `qwen38-27b`. The check runs after the environment has started. |
| `run 'X' not found` / `no runs found in logs` | Run from the repository root: `logs/` and `reports/` are relative to the current directory. |
| A workspace check or capability test errors under `opencode-bwrap` | Look at the `fs.workspace_write` containment check and at the sample's error in `uv run agenteval view`. |

**Debugging inside a Tart VM.** Per-sample VMs are deleted when the sample ends. To look around
in the provisioned image, clone it under a name that cleanup does not match:

```sh
tart clone agenteval-ubuntu-24.04 agenteval-debug
tart run agenteval-debug --no-graphics &
ssh -i ~/.agenteval/ssh/id_ed25519 -o IdentitiesOnly=yes admin@"$(tart ip agenteval-debug --wait 120)"
# ... when done:
tart stop agenteval-debug && tart delete agenteval-debug
```

To work in a provisioned VM with a harness already set up (and your repo copied in), use
`agenteval dev --env tart-ubuntu --harness ... --model ...` instead; see [dev.md](dev.md).
Its `--mount live` mode shares your repo folder with `tart run --dir` and mounts it inside
the VM with virtiofs; if the agent user cannot write the share, its uid is changed to the
folder's owner for that session.
