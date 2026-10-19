# Existing open-source eval projects: what we can use out of the box

Status: research notes, 2026-10-11. Companion to [evaluation-strategy.md](evaluation-strategy.md).

The question: is there an existing open-source project that already runs harness × model coding-agent evals and that we could adopt instead of building our own? The harnesses are Claude Code and OpenCode. The models are Claude on Bedrock and the gateway models gpt-oss-120b, devstral and qwen.

## TL;DR

- **No single project covers the whole matrix.** Three of them together cover almost all of it, and all three are MIT-licensed and actively maintained:
  1. **inspect_swe**: runs the real Claude Code, OpenCode and Codex CLIs as Inspect agents inside a sandbox.
  2. **inspect_harbor**: exposes the ~170 Harbor hub datasets as Inspect tasks, including Terminal-Bench 2 and SWE-bench Verified/Pro.
  3. **inspect_evals**: ready-made security evals. **CodeIPI** (`ipi_coding_agent`) is very close to our planned B3 prompt-injection tests.
- **promptfoo now ships 13 `coding-agent:*` red-team plugins.** They map almost one-to-one onto our B2/B3 threat model (secret reads, sandbox escape, egress bypass, verifier sabotage, CI exfiltration). Its task generation is remote by default, so we would hand-author the test cases and use only the assertions.
- **For our own internal-MR task set:** use **RepoLaunch** and the **SWE-bench-Live** tooling to build environments and tasks, and the **SWE-bench** / **Multi-SWE-bench** harnesses to grade patches. Nothing hands us this without configuration.
- **Revised backbone:** Inspect + inspect_swe + inspect_harbor, with plain Harbor only for cross-checks. inspect_harbor does not depend on the `harbor` package, so it avoids Harbor's hard litellm dependency and its PostHog telemetry.

## 1. Agent runners (harness × model)

| Project | License / status | What it gives us | Security notes | Verdict |
| --- | --- | --- | --- | --- |
| **[inspect_swe](https://github.com/meridianlabs-ai/inspect_swe)** | MIT, 0.2.71 (2026-09-17), active | `claude_code()`, `opencode()`, `codex_cli()`, `gemini_cli()`, `mini_swe_agent()` as Inspect agents. The model can be any Inspect provider: `bedrock/…`, `openai-api/<gw>/…` (Chat Completions), or `vllm/…`. | Binaries are downloaded on the host, checksum-verified and copied into the sandbox; `version="sandbox"` uses a pre-baked binary instead. Model calls go through a loopback bridge, so the CLI never holds real credentials. Sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. **Best security fit of all the candidates.** | **Use as-is** |
| **[inspect_harbor](https://github.com/meridianlabs-ai/inspect_harbor)** | MIT, v1.0.0 (2026-09-23) | Harbor hub datasets as Inspect tasks, plus an `oracle` solver for sanity checks. Run with `inspect eval inspect_harbor/<dataset> --solver inspect_swe/claude_code`. | Does not depend on the `harbor` package, so no litellm and no PostHog. **However, it logs each dataset download to its own Supabase project by default**; set `INSPECT_HARBOR_NO_TELEMETRY=1`. | **Use with config** |
| **[Harbor](https://github.com/harbor-framework/harbor)** | Apache-2.0, v0.24.0 (2026-10-05) | About 45 built-in agents. `harbor run -d <dataset> -a opencode -m openai/<model> --ae OPENAI_BASE_URL=…` works today. Claude Code on Bedrock via `--ae CLAUDE_CODE_USE_BEDROCK=1`. | The known issues (PostHog telemetry, hard litellm dependency) still apply. **New findings:** the dataset registry defaults to a third-party Supabase project (override with `HARBOR_SUPABASE_URL`, or use `-p <local dir>` / `--registry-path`). Agents are installed inside the container from the internet (`curl … \| bash`, `npm i -g opencode-ai@latest`) unless they are pre-baked into the image. | **Use with config** (cross-check) |
| **[UiPath coder_eval](https://github.com/UiPath/coder_eval)** | Apache-2.0, v0.12.9 (2026-10-09) | Runs Claude Code, Codex, OpenCode, Pi and Gemini on our own YAML tasks. Supports Bedrock. Pins its dependencies against known CVEs. Can export to Harbor. | Not yet audited by us. | **Evaluate** as an alternative runner for private tasks |
| **[harness-evaluator](https://github.com/yorch/harness-evaluator)** | MIT, single author (1 star) | Design matches our matrix almost exactly: one container per cell, hidden tests, 5 repeats with bootstrap confidence intervals, and a proxy that records tokens, cost and latency to SQLite. | Unknown code quality. | **Reference** (copy the design) |
| [coding_agent_bench](https://github.com/hannahsmith-rh/coding_agent_bench) (Red Hat contributors) | No license | Validated vLLM recipes for OpenCode (`OPENCODE_CONFIG_CONTENT` with `@ai-sdk/openai-compatible`) and Claude Code (`ANTHROPIC_BASE_URL`). | — | **Reference** (copy the recipes) |
| [OpenBench](https://github.com/minghinmatthewlam/openbench) | MIT | Measures the effect of the harness with the model held fixed; built on Harbor. | — | Reference |
| Exgentic, opencode-bench, cline-bench, Roo-Code-Evals, OpenHands benchmarks, Aider polyglot | various | Each either lacks OpenCode, grades with an LLM judge only (opencode-bench, through a third-party API), is stale or unlicensed, or runs only its own agent. | OpenHands' remote runtime is SaaS by default. | Skip |

## 2. Security suites

| Project | Tests the CLI agent? | Notes | Verdict |
| --- | --- | --- | --- |
| **inspect_evals `ipi_coding_agent` (CodeIPI)** | Yes, by swapping its default `react()` solver for an inspect_swe agent | 45 samples. Injections arrive via issue text, code comments, README and config files. Uses canary environment variables and `/root/.secret_canary`. Payload types are exfiltration, code execution and persistence (requirements.txt, git hooks, `.bashrc`). Runs with `network_mode: none`. **Still to verify:** that the scorer sees the tool calls the bridged CLI makes. | **Use as-is**; it maps to B3 |
| **promptfoo `coding-agent:*` plugins** ([docs](https://www.promptfoo.dev/docs/red-team/plugins/coding-agent/)) | Yes, via `anthropic:claude-agent-sdk` (supports Bedrock) and `opencode:sdk` | Plugins: repo-prompt-injection, terminal-output-injection, secret-env-read, secret-file-read, procfs-credential-read, sandbox-read-escape, sandbox-write-escape, network-egress-bypass, delayed-ci-exfil, automation-poisoning, steganographic-exfil, verifier-sabotage, generated-vulnerability. Grading runs deterministic canary checks first and falls back to an LLM rubric only at the end. **Task generation uses promptfoo's remote generator by default.** For a fully local run, hand-author the test cases and use the assertions directly, or set `PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION=true` with `redteam.provider` pointed at the gateway. Also set `PROMPTFOO_DISABLE_TELEMETRY=1` and `PROMPTFOO_DISABLE_UPDATE=1`. The docs say `copy_working_dir` "isn't a sandbox". | **Use with config**; it maps to B2/B3 |
| **[SusVibes](https://github.com/LeiLiLab/susvibes)** (MIT, ICML 2026) | Yes; it is agent-agnostic and grades submitted patches | About 200 real-repo Python feature tasks, each with both functional and security tests. Needs x86_64 and about 300 GB of disk. | **Use with config**; it maps to B4 |
| inspect_evals: `cyberseceval_4`, `agentdojo`, `b3`, `agent_threat_bench` | Mostly model-level | Useful as baselines per gateway model. | Use as-is (baseline) |
| [garak](https://github.com/NVIDIA/garak) (Apache-2.0, v0.17.0) | No; it tests the model endpoint | Probes include `latentinjection`, `packagehallucination` and `malwaregen`. The default red-team model for `agent_breaker` is a remote NVIDIA NIM; repoint it to the gateway. Some probes download data from Hugging Face. | Use with config (model baseline) |
| CWEval, BaxBench | No | Model-level secure-code benchmarks. CWEval depends on litellm. BaxBench needs a patch to accept a custom base URL. | Optional baseline |
| [IssueTrojanBench](https://arxiv.org/html/2607.20759v1) | Tested real Claude Code, Cursor and Codex | No license is stated. Its attacks include fake PyPI installs, edits to CLAUDE.md, and hidden hooks. | **Payload source** for our own fixtures |
| PyRIT, AgentDojo standalone, ASB, InjecAgent, WASP, RedCode, MCP-SafetyBench | No | These test the model or the benchmark's own toy agent. | Skip / reference |

Package hallucination: we found no harness that tests it at the agent level. Assert "no new dependencies and no fetches from unknown registries" through egress capture and promptfoo's network checks instead.

## 3. Building our own internal-MR task set (track A)

| Project | License / status | Role | Verdict |
| --- | --- | --- | --- |
| **[RepoLaunch](https://github.com/microsoft/RepoLaunch)** | MIT, active (NeurIPS 2026) | Takes a repo and produces a Docker image, a test command, and a parser that reports per-test status. Supports 7+ languages. Uses text actions, which suits small models. Depends on litellm. Private repos are not documented, so use local clones. | **Use with config** |
| **[SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live)** | MIT, active | Turns a PR into a task instance, then evaluates patches. Its crawler targets GitHub, so it needs adapting for GitLab MRs. | **Use with config** |
| **[SWE-bench harness](https://github.com/SWE-bench/SWE-bench)** | MIT, active | Grades patches using fail-to-pass and pass-to-pass tests. Point it at local JSONL and locally built images instead of Hugging Face and Docker Hub. | **Use with config** (Python) |
| **[Multi-SWE-bench](https://github.com/multi-swe-bench/multi-swe-bench)** | Apache-2.0, slowing (2025-12) | Same approach for Java, TS/JS, Go, Rust, C and C++. | Use with config (other languages) |
| [SWE-smith](https://github.com/SWE-bench/SWE-smith) | MIT, active | Generates synthetic bugs to get more tasks per repo. Mostly Python. Synthetic bugs are easier than real tickets. | Optional |
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | MIT, active | A minimal bash agent that serves as a "bare harness" control for the gateway models. Hard dependency on litellm, with the compromised versions excluded. | Optional baseline |
| SWE-Factory | AGPL for non-commercial use only | — | **Skip** (licence) |

## 4. Recurring supply-chain and egress pitfalls

Every candidate needs the same hardening, so plan it once:

- **litellm** shows up in Harbor, RepoLaunch, mini-swe-agent and CWEval. Pin an exact version with hashes and mirror it internally. Versions 1.82.7 and 1.82.8 are the compromised ones.
- **Telemetry** is on by default in four tools. Turn each off:

  | Tool | Setting |
  | --- | --- |
  | Harbor | `HARBOR_TELEMETRY=off` |
  | inspect_harbor | `INSPECT_HARBOR_NO_TELEMETRY=1` |
  | promptfoo | `PROMPTFOO_DISABLE_TELEMETRY=1` |
  | DeepEval | `DEEPEVAL_TELEMETRY_OPT_OUT=1` |

- **Run-time downloads.** Agents are installed via npm or curl, task images come from Docker Hub or GHCR, and datasets come from Hugging Face. Pre-bake the agents into the images, mirror the images, and use local datasets.
- **Confirm all of the above with an egress capture.** Reading docs is not enough.

## 5. Open checks (do these first in the sandbox)

1. Does OpenCode fetch `@ai-sdk/openai-compatible` from npm, or call models.dev, at startup? Test it in an egress-blocked container.
2. Does Harbor's `opencode -m openai/<model>` route call `/responses` (which a vLLM gateway may not support) instead of `/chat/completions`?
3. Does the Inspect sandbox agent bridge work with Docker `network_mode: none`?
4. Does the CodeIPI scorer see the tool calls of a bridged CLI agent (inspect_swe), or only those of `react()`?
5. Do promptfoo's `coding-agent:*` assertions work against the Claude Agent SDK and OpenCode SDK targets? The docs only name Codex, HTTP and `file://` targets.

Note: these findings come from web and GitHub research done on 2026-10-11, partly by reading source in shallow clones. Version numbers, defaults and telemetry endpoints should be re-checked when each tool is pinned.
