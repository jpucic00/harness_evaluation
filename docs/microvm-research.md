# MicroVM sandboxes: options for agenteval

Status: research notes, 2026-10-22. Nothing here has been installed or tested. Versions, dates and CVEs come from the linked primary sources and were read on that date. They were not re-verified one by one.

The question: should agenteval add a microVM environment next to `docker-ubuntu` (a runc container) and `tart-ubuntu` (a full VM per sample), and if so, which one?

## TL;DR

- **What a microVM adds:**
  - Compared with Docker, each sample gets **its own guest kernel**. A kernel or runc escape then lands in a throwaway VM, not on the host.
  - Compared with Tart, the boundary is similar but boot takes **~100 ms to ~1 s instead of a full VM boot**, and the guest is built from the **same OCI image** as the Docker environment, with no separate `provision.sh` path to maintain.
- **Linux hosts (CI, cloud VM): Kata Containers as a Docker runtime.**
  - agenteval already writes `runtime:` into the compose file and Inspect passes it through. That makes this a new environment profile plus host setup, with **no code change**.
  - It needs `/dev/kvm`. GitHub standard runners and AWS 7i/8i nested instances provide it.
  - Without KVM, the same one-line change with gVisor works anywhere. gVisor is a weaker, non-VM boundary.
- **macOS hosts (laptops): Apple `container` is the best fit.**
  - It is first-party, Apache-2.0, needs no account and documents no telemetry.
  - It gives one Linux VM per container, OCI images, and `exec`/`cp`.
  - It needs macOS 26 on Apple Silicon. This Mac qualifies (M5, macOS 26.3.1).
  - There is no Inspect provider for it, so this means a new provider modelled on Tart: **medium-to-large** work.
- **Strongest option if effort is no object:**
  - A jailed Firecracker VM per sample, with a vsock agent, nftables egress on the host and snapshot reset.
  - **E2B Embed** (self-hosted E2B, Apache-2.0) already ships most of that. It needs an egress and telemetry audit before any use.
- **Docker Sandboxes (`sbx`): pending a security decision.**
  - Docker Inc. is a known vendor under the company's Docker licence, but `sbx` is a separate product with its own terms.
  - It is a weak fit for scripted eval runs and a possible fit for `agenteval dev` (see [section 2](#docker-sandboxes-sbx)).
- **Reject:**
  - **Daytona:** its public repo is archived.
  - **Podman/krun on a Mac:** a shared VM or nested virtualization.
  - **Hosted providers in `inspect_sandboxes`** (E2B cloud, Modal, Daytona): the sandbox runs outside the company.

## 1. Linux hosts

Every option except gVisor needs `/dev/kvm`:
- **GitHub-hosted Linux runners:** since 2024-04 ([changelog](https://github.blog/changelog/2024-04-02-github-actions-hardware-accelerated-android-virtualization-now-available/)). Needs a udev rule so the runner user can open `/dev/kvm`.
- **AWS non-metal nested virtualization:** Intel C8i/M8i/R8i since 2026-02, extended to the 7i families in 2026-06 ([AWS](https://aws.amazon.com/about-aws/whats-new/2026/06/nested-virtualization-intel-us-gov-cloud/)).
- **GCP:** nested KVM, with ~10% CPU overhead.

| Option | Boundary | Fits agenteval's Docker path? | Exec / files | Egress | Licence | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| **Kata Containers** 4.3.1 (2026-10-21) | Hardware VM per container (QEMU, Cloud Hypervisor or Dragonball) | **Yes:** `runtime: io.containerd.kata.v2` ([Docker alt runtimes](https://docs.docker.com/engine/daemon/alternative-runtimes/)). 4.0 says Docker is "only tested with QEMU" ([release](https://github.com/kata-containers/kata-containers/releases/tag/4.0.0)) | `docker exec` goes through kata-agent. **Unverified:** `docker compose cp`, which Inspect's `read_file` uses | Normal Docker networking. `--net=host` is unsupported ([limitations](https://github.com/kata-containers/kata-containers/blob/main/docs/Limitations.md)) | Apache-2.0 | Pin at or above the CVE-2026-47243 fix (virtio-fs guest-to-host, 3.31.0) and disable annotation passthrough (CVE-2026-44210) ([advisory](https://advisories.gitlab.com/golang/github.com/kata-containers/kata-containers/CVE-2026-47243/)) |
| **gVisor** runsc 20261019.0 | User-space kernel, not a VM | **Yes:** `runtime: runsc` (already documented in [extending.md](extending.md)) | `exec` works. **`read_file` breaks by default:** gVisor keeps root-filesystem writes in memory, so the host-side `docker cp` cannot see them. Set `--overlay2=none` in `daemon.json` `runtimeArgs` ([docs](https://gvisor.dev/docs/user_guide/filesystem/)) | Docker networks | Apache-2.0 | Runs without KVM (systrap). Host-root escape CVE-2026-96812 via `/dev/cuse`, published 2026-09-25 ([opencve](https://app.opencve.io/cve/CVE-2026-96812)) |
| **Firecracker** v1.17.0 (2026-09-10) | Minimal Rust VMM plus jailer (namespaces, cgroups, uid drop) | No | You build a vsock agent; no exec built in | You build tap + nftables per VM | Apache-2.0 | ≤125 ms boot, ≤5 MiB VMM overhead ([spec](https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/SPECIFICATION.md)). Snapshot restore is fast, but restoring one snapshot many times needs care with randomness and IDs ([doc](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)). firecracker-containerd is barely maintained; Ignite is archived; flintlock is active |
| **libkrun / crun-krun** | KVM microVM inside crun | Podman only; Docker not documented | `podman exec`. Exec behaviour unconfirmed | Default TSI networking egresses through the VMM's host sockets, so it must be confined on the host ([security model](https://github.com/containers/libkrun/blob/main/docs/security-model.md)) | Apache-2.0 | Upstream: the VM "does not restrict host resources exposed through devices" |
| Cloud Hypervisor v54, QEMU `microvm`, crosvm | VMMs | Only via Kata, or DIY | DIY | DIY | various | Use as a Kata backend rather than directly |

Self-hostable agent-sandbox platforms:

| Platform | Tech | Status for us |
| --- | --- | --- |
| **E2B Embed** ([e2b-dev/runtime](https://raw.githubusercontent.com/e2b-dev/runtime/main/embed/README.md)) | Firecracker VM per sandbox | `docker compose up` on one KVM host. Per-sandbox nftables egress with SNI/Host allow lists, plus pause/resume. The README says no account is needed and "nothing leaves the node". It also runs Postgres, Redis, ClickHouse and object storage. **Audit for telemetry before use.** An Inspect `e2b` provider already exists in [inspect_sandboxes](https://github.com/meridianlabs-ai/inspect_sandboxes), but it targets E2B's cloud; whether it can point at Embed is unchecked |
| **kubernetes-sigs/agent-sandbox** | CRDs over a gVisor or Kata RuntimeClass | Only relevant on Kubernetes |
| microsandbox | libkrun | Beta (v0.7.8) |
| Perplexity SPACE | Firecracker | No releases yet |
| Docker Sandboxes | microVM per sandbox (Linux needs Ubuntu 24.04+ with KVM) | Pending security decision (see [section 2](#docker-sandboxes-sbx)) |
| Daytona | — | Rejected: public repo archived |

## 2. macOS hosts (Apple Silicon)

The guest must be Linux, because the Inspect bridge and the agent CLIs are Linux-only.

| | Apple `container` v1.6.0 | smolvm v1.31.3 | microsandbox v0.7.8 | Docker Sandboxes | Podman + libkrun |
| --- | --- | --- | --- | --- | --- |
| Isolation | One VM per container (Virtualization.framework) | One libkrun VM per machine | One libkrun VM per sandbox | One microVM per sandbox | **One shared VM** |
| Boot | "sub-second" | <200 ms | <100 ms (M1) | not stated | full VM |
| Exec / copy | `container exec`, `container cp` (gRPC over vsock to `vminitd`) | `machine exec`; mounts only | `msb exec`, `msb cp` | `sbx exec` (foreground only), `sbx cp` | `podman exec/cp` |
| Egress | **No allow-list.** `network create --internal` and `--no-dns` (macOS 26) | **Off by default.** `--allow-host`, `--allow-cidr` | Public internet allowed by default; `--net-default deny`, host rules | Deny by default via proxy, but broad default allow-list | gvproxy only |
| Host requirement | **macOS 26** | macOS 11+ | Apple Silicon | macOS 14+ | — |
| Licence | Apache-2.0 | Apache-2.0 | Apache-2.0 | **Proprietary** | Apache-2.0 |
| Telemetry / account | none documented | none documented | none documented (vendor also sells a hosted cloud) | On by default (opt-out), plus a diagnostics upload. Docker sign-in required. Vendor is known under the company's Docker licence | none documented |
| Maturity | First-party, 50.8k stars, frequent behaviour changes | Startup, 6.8k stars | Self-described beta | 2026 launch | Mature, but wrong shape |

Sources: [apple/container](https://github.com/apple/container/blob/main/docs/technical-overview.md), [command reference](https://github.com/apple/container/blob/main/docs/command-reference.md), [smolvm](https://github.com/smol-machines/smolvm), [microsandbox docs](https://docs.microsandbox.dev/networking/overview), [Docker Sandboxes FAQ](https://docs.docker.com/ai/sandboxes/faq/), [podman-machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html).

Notes:
- **Apple `container`:**
  - Pin both the CLI and the guest kernel. 1.3.0 changed the default kernel (a Kata 3.32.0 build), and 1.5.0 changed egress behaviour ([releases](https://github.com/apple/container/releases)).
  - Freed guest memory is not returned to the host.
  - **Unverified:** that the default kernel has nf_tables, which agenteval's `egress.sh` needs.
- **The 2-VM limit** in Apple's licence applies to macOS guests. Apple's own `container` runs one Linux VM per container with no stated cap, but Apple does not say in writing that Linux guests are unlimited.
- **Lima and OrbStack** are a single full VM with containers sharing its kernel, so they offer nothing over Tart. OrbStack is also closed source and paid for commercial use.

### Docker Sandboxes (`sbx`)

Source for the facts below: the [Docker Sandboxes FAQ](https://docs.docker.com/ai/sandboxes/faq/), read 2026-10-23.

**Vendor:** the company already licenses Docker, so Docker Inc. is a known vendor and not an unknown third party. That alone does not cover `sbx`:
- `sbx` is a separate proprietary product with its own terms. The CLI and local sandboxes are "free to use, including for commercial and professional work."
- Admin control of local sandboxes (network, filesystem and MCP policies) needs "a separate paid subscription" from Docker Sales. The existing licence most likely does not include it.

**What leaves the machine:**
- **Telemetry:** each CLI call sends the command, whether it succeeded, how long it took, and the Docker username to `marlin-2.docker.com` and `marlin-api.docker.com`. The FAQ says this does not include prompts or code. Turn it off with `SBX_NO_TELEMETRY=1`.
- **Diagnostics:** uploads go to `sbx-diagnostics.s3.us-east-1.amazonaws.com`. The FAQ describes neither the contents nor an opt-out.
- **Sign-in:** a Docker account sign-in is required.

**Open questions for security:**
1. Does the company's Docker agreement (and DPA, if there is one) cover Docker Sandboxes, or does it need its own approval?
2. Is the diagnostics upload acceptable, given that its contents are undocumented and it has no documented opt-out? If not, can it be blocked at the network edge without breaking `sbx`?
3. Is per-user Docker sign-in from sandbox sessions acceptable, and must `SBX_NO_TELEMETRY=1` be enforced?
4. Is the separate governance subscription needed for central network and filesystem policy, or are local settings enough for the evaluation phase?

**Fit for agenteval:**
- **Scripted eval runs: weak.**
  - `sbx exec` runs in the foreground only.
  - There is no Inspect provider.
  - It is designed around one interactive agent session, not throwaway per-sample sandboxes.
  - Docker wrote its own VMM, which cannot be inspected.
  - The default allow-list includes broad wildcards such as `*.googleapis.com`. Eval runs need no network at all.
- **`agenteval dev`: possible.**
  - `agenteval dev` is one interactive agent session per environment, which is the shape `sbx` was built for.
  - `sbx` brings a microVM per session and a deny-by-default egress proxy.
  - It runs on macOS 14+ without Docker Desktop.
  - Worth a trial if security approves. Compare it against the existing Docker and Tart dev environments.

### Decisions so far (2026-10-23)

- **Environment choice:** the security team prefers Docker Sandboxes for `agenteval dev`. The [open questions](#docker-sandboxes-sbx) above still need their answers.
- **How agenteval connects:** agenteval drives the sandbox itself, the same way as the Docker and Tart environments:
  - `sbx exec` and `sbx cp`;
  - the CLI version pinned in the harness profile;
  - Inspect's model bridge, so the VM needs no network for model calls.
  
  It does not use Docker's agent kits or its credential proxy, because those cannot reach the company gateway (corporate CA, client certificates) or Bedrock, and because they bypass agenteval's logging and review. `--mount live` maps onto the sandbox's direct mount. SSH agent forwarding is turned off.
- **Base image:** security prefers Docker's hardened base over `ubuntu:24.04`. The hands-on check below showed that the default `docker/sandbox-templates:shell-docker` is **Ubuntu 26.04, not a hardened image**. The hardened variants are Docker Hardened Images (`dhi.io/sbx-templates:...`), used only when the `sbx` setting `platform.images.useDHI` is on. What the DHI variant contains, and whether it needs a DHI entitlement, is still to check. Consequences either way:
  - The in-VM Docker engine stays off unless a case needs it.
  - Results from this environment must note its base distro whenever it differs from `docker-ubuntu` and `tart-ubuntu`.

### Built: the `sbx-ubuntu` environment (2026-10-23)

agenteval now has an `sbx` provider: [environments/sbx/](../src/agenteval/environments/sbx/) and the profile `config/environments/sbx-ubuntu.yaml`. Setup is in [setup.md](setup.md#docker-sandboxes-uv-run-agenteval-env-build-sbx-ubuntu).

- **Image:** the template is our own image plus one label that gives root in the sandbox full capabilities, which the egress rule needs.
- **Running commands:**
  - every command drops the variables sbx injects;
  - non-root users get their groups through `runuser`;
  - `SBX_NO_TELEMETRY=1` is set on every `sbx` call.
- **SSH agent forwarding:** must be off. The provider checks the setting, and probes each new sandbox to confirm no agent answers.
- **Claude subscription sessions:** not supported yet.
- **Resources section:** shows sbx as one group; there are no per-sample figures yet.

What building it turned up:

| Finding | Effect |
| --- | --- |
| `sbx exec -u USER` gives only the primary group | Fixed with `runuser`. Without it the bwrap cells failed: the agent was not in `agenteval-bwrap`. |
| Root in a custom-image sandbox lacks `CAP_NET_ADMIN` | Fixed with the template label. Without it `egress.sh` reported `effective: false`, and the agent's connections reached sbx's proxy. |
| sbx's proxy accepts TCP inside the VM, then blocks it by policy | With deny-all, nothing leaves (0 bytes, logged as blocked). The containment probes saw "connected", so they reported `net.internet_tcp` and `net.host_gateway` as exposed until the in-VM rule worked. |
| `/etc/resolv.conf` is a read-only virtiofs share | Reported by `host.shared_mounts`; low risk (sbx's generated DNS settings). |
| `browser.enabled` defaults to true | `doctor` and every run warn about it; turning it off is the user's call. |

### Hands-on check (2026-10-23, `sbx` v0.49.2 on an M5 Mac with macOS 26.3)

| Question | Result |
| --- | --- |
| Scripting | `sbx create` (`--name`, `--template`, `--pull`, `--cpus`, `--memory`, `--no-progress`), `sbx ls --json`, `sbx rm --force` and `sbx template load <tar>` all run without a terminal. |
| `sbx exec` | Same flags as `docker exec` (`-u`, `-i`, `-t`, `-w`, `-e`), except detached mode. Exit codes, stdout/stderr and stdin all pass through. |
| Root | `sbx exec -u root` works. |
| `sbx cp` | Works in both directions. |
| Our own image | Works. `docker save` → `sbx template load` → `sbx create shell --template agenteval/ubuntu-24.04:latest --pull never`. It started in ~2 s, provisioned as in Docker: agent uid 1001, no sudo, `/etc/agenteval/image.json` present. A local Docker Desktop image cannot be pulled directly (403); `ubuntu:24.04` from Docker Hub can. So no kit is needed: an ordinary image works as a template. |
| Startup | The first sandbox took 130 s, pulling the default template. After that a sandbox starts in 2–4 s. |
| Exec cost | `sbx exec … true` averages 275 ms over 20 calls, against 51 ms for `docker exec`. The model bridge polls through exec, so this is the cost to watch in the Resources section. |
| Network | With `sbx policy init deny-all`, HTTPS through the sandbox proxy gets 403 and direct connections fail. The sandbox environment routes everything through `HTTPS_PROXY`, with a proxy CA in `PROXY_CA_CERT_B64`. |
| Credentials | Provider keys in the sandbox environment are placeholders (`proxy-managed`, `gho_sbxpro…`); real keys stay on the host. |
| SSH agent | `SSH_AUTH_SOCK` is forwarded into every sandbox by default (`ssh.agentForwardingEnabled: true`). agenteval must turn this off. |
| Default `shell` template | Ubuntu 26.04 with bwrap, nftables, python3, git, sudo and Docker. The `agent` user (uid 1000) is in the `sudo` and `docker` groups and has **passwordless sudo**. |
| Inside the VM | Kernel 7.0.14 aarch64. By default the VM gets all 10 host cores and 12 GiB, half the host's memory. nftables works as root, and bubblewrap user namespaces work. |
| Live mount | `sbx create shell <dir>` mounts the folder at the same path. Edits show up instantly both ways, and files land on the Mac as your user. |
| Idle | Sandboxes stopped on their own after a few idle minutes; `sbx exec` starts them again. |
| Host processes | `sbx daemon start`, an `llmman serve` helper, and one `containerd-shim-nerdbox-v1` process per running sandbox (~370–410 MiB each). That process is the microVM. The Resources sampler counts them as the Docker Sandboxes group. Mapping one of them to a sandbox name for per-sample figures still needs work. |
| Other defaults to review | `browser.enabled: true` lets sandboxed processes open URLs in the host browser. `diagnostics.autoUpload` is unset. `kit.allowedSources` is Docker Hub only. |

## 3. Integration with agenteval

Inspect has no provider for self-hosted Firecracker, Kata (outside k8s), libkrun, smolvm or Apple `container`.

| Path | Effort | What it takes |
| --- | --- | --- |
| **(a) Docker + Kata or gVisor runtime** | **Small** | `DockerSettings.runtime` ([config.py:230](../src/agenteval/config.py:230)) is already written to compose ([docker.py:113](../src/agenteval/environments/docker.py:113)), and Inspect runs `docker compose -f <file>` unchanged. Work: a Linux Docker host, `daemon.json` (Kata shim; gVisor `--overlay2=none`), a new `config/environments/*.yaml`, then a containment-suite run to confirm `read_file`, `network_mode: none` and bwrap behave. Docker Desktop for Mac cannot do this. |
| **(b) Kubernetes + Kata RuntimeClass** via [inspect_k8s_sandbox](https://github.com/UKGovernmentBEIS/inspect_k8s_sandbox) | Medium | Set `runtimeClassName` per service. Default egress deny comes from Cilium. Needs a cluster, kata-deploy and a registry, plus an agenteval provider value, a Helm-values generator (the analogue of `compose_config`) and a `kubectl exec` dev handle. |
| **(c) Native provider modelled on Tart** | Large (Apple `container`: medium-large) | Tart is ~1,400 lines (`sandbox.py`, `ssh.py`, `image.py`). Reusable: the root-safe read/write scripts, `exec`/`read_file`/`write_file`, the cleanup registry, `sample_init` rollback, VM naming/cleanup. Apple `container` replaces SSH with `container exec`/`cp` and builds from the existing Dockerfile. Firecracker additionally needs rootfs conversion, tap networking, a guest agent and jailer setup on a Linux/KVM host. Wiring steps are in [extending.md](extending.md#a-new-sandbox-provider-another-vm-technology). |

What any backend must provide for the existing egress controls ([egress.sh](../src/agenteval/guest/scripts/egress.sh), [suites/setup.py](../src/agenteval/suites/setup.py)):
- **Either no NIC** (what `network_mode: none` gives Docker), **or** a guest kernel with nf_tables and the `nft` binary.
- **Root exec** that the egress rule does not cover.
- **A separate agent uid**, so the rule can target the agent alone.
- **Loopback**, for the collector on 127.0.0.1:8099.
- **The collector hostname** in `/etc/hosts`.
- **systemd-resolved**, for the DNS guard.
- The model bridge needs no network: it polls files through `exec`.

## 4. Recommendation

1. **Now, if a Linux KVM host is available:**
   - Add a `docker-kata` environment profile (path a) and run `smoke-docker` and the containment suite on it.
   - This is the cheapest way to measure whether a per-sample kernel changes any containment result.
   - Fix [extending.md](extending.md), which still names the old `kata-runtime` binary; Kata now registers `io.containerd.kata.v2`.
2. **For laptops:**
   - Prototype an Apple `container` provider only if Tart's boot time or the duplicate provisioning path is actually hurting.
   - Validate Tart first; it is still open in the project's to-do list.
   - Use `--internal` networking (or none) for eval runs, and confirm the guest kernel supports `nft` for dev sessions.
3. **Docker Sandboxes:** put the [open questions](#docker-sandboxes-sbx) to security. If they approve it, trial it as an `agenteval dev` environment, not for eval runs.
4. **Defer** Firecracker and E2B Embed until there is a dedicated Linux eval host and a reason to need snapshot-speed resets at scale.

Open questions to settle by experiment, not reading:
- Kata: whether `docker compose cp` works and whether `network_mode: none` behaves as expected.
- gVisor with `--overlay2=none`: whether `read_file` is reliable.
- Apple `container`: kernel nf_tables support, and per-sample start/stop time measured on this M5.
