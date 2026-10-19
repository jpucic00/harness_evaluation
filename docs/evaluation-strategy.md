# AI Agent Evaluation Strategy (draft v0)

Status: draft for discussion, 2026-10-11; revised 2026-10-12 (backbone is Inspect-only, plain Harbor demoted to cross-check, red teaming moved into Inspect). Scope: the evaluations track. It covers how to compare AI coding solutions on capability, security and economics before rolling them out.

## 1. What we are actually evaluating

The candidate solutions are not a flat list. They are combinations of three layers, and each layer fails in its own way:

| Layer | Candidates | Main question |
| --- | --- | --- |
| **Harness** (the agent program) | Claude Code, OpenCode, later others (Codex CLI, Aider, the in-house Pydantic AI agents) | What can it do on the machine, what does it send where, and how well does it drive the model? |
| **Model** (the LLM behind it) | Claude via Amazon Bedrock; internal gateway models: gpt-oss-120b, devstral-2512 / devstral-small, qwen, others | How good is the code, how reliable is tool calling, and what does it cost? |
| **Runtime** (where it runs) | Dev laptop, devcontainer, CI runner, VM; network egress policy; the gateway or proxy | Can it be contained, and what data leaves? |

The evaluation therefore runs a **matrix of harness × model inside a fixed, instrumented sandbox**. The runtime is held constant, and it is also a subject of the security tests.

Realistic matrix for phase 1:

| | Claude (Bedrock) | gpt-oss-120b | devstral-2512 | qwen |
| --- | --- | --- | --- | --- |
| Claude Code | primary | needs an Anthropic↔OpenAI translation proxy (unsupported, low priority) | same | same |
| OpenCode | yes (Bedrock provider) | yes (OpenAI-compatible) | yes | yes |

Notes:
- Claude Code talks to the Anthropic Messages API. Pointing it at the OpenAI-compatible internal gateway needs a translation layer (LiteLLM is the usual one). That is a big part of why LiteLLM came up, and it is also a reason to be careful (§4).
- The agentic-test-automation project found that the gateway (vLLM + xgrammar) silently drops tool schemas it cannot compile, and that llama-3.3-70b failed MCP tool calling. **Tool-calling fidelity through the gateway is a gating test**, not a detail.

## 2. Evaluation dimensions

### A. Coding capability (does it do the job?)

| What | How | Metrics |
| --- | --- | --- |
| **Internal task set (most important)** | 30–50 tasks replayed from real closed MRs/tickets on a low-sensitivity internal repo: check out the parent commit, give the ticket text, and run the MR's tests as the hidden grader (SWE-bench style). | resolve rate (pass@1), **pass^k** (succeeds on all k of k runs, which measures reliability), tests broken, diff size vs human diff |
| Public sanity benchmarks | SWE-bench Verified/Pro and Terminal-Bench 2.0, run via inspect_harbor (Harbor hub datasets as Inspect tasks) | resolve rate, for comparison with published numbers (catches setup bugs) |
| Tool-calling fidelity | Scripted probes through the gateway: schema compliance, parallel calls, long-context degradation (the ATA repo notes degradation past ~30K tokens) | % valid calls, failure modes |
| Human review quality | 2 senior devs blind-rate a sample of diffs (readability, idiomatic, would-merge?) | would-merge rate, review minutes |

Do not use LLM-as-judge as the primary coding grader. Tests are the grader, and a judge is only used for qualitative extras.

### B. Security (split into five separate questions)

These are **hard gates (pass/fail)**, not weighted scores. One exfiltration path blocks a candidate regardless of how good its code is.

**B1. Tool / supply-chain security of the harness itself**
- Review CVE history, release process, update mechanism, telemetry and dependency footprint (SBOM).
- Examples worth putting in front of the security team:
  - OpenCode had an unauthenticated local HTTP server that allowed RCE from any website (CVE-2026-22812).
  - It had an XSS in its web UI (CVE-2026-22813).
  - In Sept 2026 it had an RCE via `/global/upgrade` (fixed in 1.18.22, no CVE issued).
- Check: can it be pinned, installed from the internal Artifactory, and run with auto-update and telemetry off?

**B2. Containment / sandbox escape**

The sandbox is tested adversarially, not assumed:
- Plant **canary secrets** (fake AWS keys, `.env`, `~/.ssh`, `~/.aws`, a canary token URL) outside the workspace, and check whether the agent reads them or sends them anywhere.
- Egress probes: does a task that "needs" a non-allowlisted host get blocked? Log every DNS/HTTP attempt through an egress proxy.
- Persistence: writes outside the workspace, git hooks, shell rc files, cron entries.

**B3. Prompt-injection robustness (the realistic attack)**
- Put hostile instructions where agents read them: README, code comments, issue/ticket text, dependency docs, test fixtures, MCP/tool outputs, web pages.
- Goals of the injection: exfiltrate a canary secret, add a dependency, disable a test, push to a branch, curl an external URL.
- Metric: **attack success rate** per harness × model, with and without the sandbox (that pair shows how much the sandbox is worth).
- Tools: inspect_evals CodeIPI (`ipi_coding_agent`) run against the real CLIs via inspect_swe; our own canary/injection scenarios as Inspect tasks against our repos; inspect_evals `agentdojo` / `b3` / `cyberseceval_4` and garak as per-model baselines.
- Grade on side effects (canary reads, egress attempts, files written), not on what the agent says.
- promptfoo is optional here: its `coding-agent:*` plugins and adaptive attack strategies only generate tests via promptfoo's hosted service, so locally we would hand-author cases and use only its assertions.

**B4. Security of the produced code**

Run on every diff from track A, so it costs nothing extra:
- SAST delta (Semgrep/CodeQL findings introduced by the diff, not totals).
- Secrets in the diff (gitleaks).
- **New dependencies**: are they existing packages (slopsquatting check)? Do they have known vulns (OSV)? Are they pinned, and allowed by policy? A 2026 re-evaluation still finds ~5–6% package-hallucination rates on frontier models (arXiv 2605.17062).
- Optional: CyberSecEval-style insecure-code prompts per model.

**B5. Data exposure**
- What leaves the boundary, to whom, and under which contract:
  - Bedrock: region, cross-region inference profile geography, no-training terms, logging.
  - Internal gateway: logging and retention.
  - Harness telemetry.
- Verify by capturing egress during real runs, not by reading docs.
- Claude Code: managed settings can enforce the sandbox (`sandbox.failIfUnavailable`), set permissions and set an egress domain allowlist. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` cuts telemetry. Verify each setting against current docs.

### C. Economics

Measure **cost per successful task**, not cost per token. A cheap model that needs 3 attempts and 20 min of review is expensive.

| Input | Source |
| --- | --- |
| Tokens in/out/cached per run | Inspect eval logs (per-sample model usage, including bridged CLI agents via inspect_swe); cross-check against Bedrock / gateway logs |
| $ per token | Bedrock price list (incl. prompt-caching discount); internal gateway = GPU TCO ÷ realistic throughput (get from platform team) |
| Success rate, retries | track A |
| Human time | review minutes, fix-up minutes (track A human review) |
| Wall-clock latency | Inspect eval logs (total and working time per sample) |
| Licences / seats | procurement |

Outputs:
- $/resolved task.
- $/dev/month at projected usage.
- Break-even point between Bedrock and the internal gateway.
- Sensitivity to the success rate.

### D. Operability and governance

These are lighter checks, mostly a checklist:
- Central policy enforcement (managed settings / config lockdown).
- Audit logs.
- OpenTelemetry export.
- SSO and key management.
- Offline/air-gapped install.
- Vendor lock-in, and the ability to swap models.

## 3. Recommended tooling

| Need | Recommendation | Why / caveats |
| --- | --- | --- |
| Agentic coding and security evals in sandboxes | **Inspect AI** (UK AISI, MIT) + **inspect_swe** + **inspect_harbor** + **inspect_evals** | Per-sample Docker sandboxes, transcript and token logging, built-in scorers. inspect_swe runs the real CLIs (Claude Code, OpenCode, Codex) as the agent under test; inspect_harbor exposes the Harbor hub datasets (Terminal-Bench 2.0, SWE-bench Verified/Pro) without depending on the `harbor` package; inspect_evals supplies CodeIPI and the security baselines. Self-hosted, but set `INSPECT_HARBOR_NO_TELEMETRY=1` (it logs dataset downloads to its own Supabase by default). |
| Cross-checking benchmark numbers (optional) | plain **Harbor** (Terminal-Bench team, Apache-2.0) | Only to catch setup bugs or compare with published leaderboard numbers, or if we need its Kata/EC2 isolation backends. **Caveats:** PostHog telemetry on by default (`HARBOR_TELEMETRY=off`), hard litellm dependency, dataset registry on a third-party Supabase by default (use `-p <local dir>`), agents installed from the internet at runtime unless pre-baked into the image. |
| Prompt/model comparisons, CI regression tests, optional extra red teaming | **promptfoo** | YAML-driven and runs in CI. Not the primary red-team tool: its coding-agent plugins and adaptive attack strategies need promptfoo's hosted generator, and local generation quality is low per its own docs. **Caveats:** acquired by OpenAI (announced 2026-03-09). Red-team attack generation uses promptfoo's remote service by default, so disable remote generation and telemetry (`PROMPTFOO_DISABLE_REMOTE_GENERATION`, `PROMPTFOO_DISABLE_TELEMETRY`), confirm with an egress capture, pin the version and install from the internal mirror. |
| Unified model access, Anthropic↔OpenAI translation, budgets, cost logs | **LiteLLM proxy, only if the internal gateway can't do it** | Not an eval tool; it is a gateway. **Its PyPI package was compromised on 2026-03-24** (1.82.7/1.82.8, credential stealer via `.pth`, TeamPCP campaign, root cause a compromised Trivy in their CI). If used: pin exact version + hashes, internal mirror, run as an isolated container with no access to developer credentials. First ask whether the existing gateway already gives per-key budgets and usage logs. |
| Model-level vuln scanning | garak (NVIDIA), optionally PyRIT (Microsoft) | Per-model baselines for the gateway models; both are self-hostable. Repoint garak's default remote red-team model to the gateway. |
| Code-output security | Semgrep / CodeQL, gitleaks, OSV-scanner, a package-existence check against the internal registry | Run as scorers inside Inspect. Note that Trivy was itself compromised in the same 2026 campaign, so pin any scanner too. |
| Traces / observability | OpenTelemetry → self-hosted Langfuse or Arize Phoenix | Claude Code exports OTel natively. ATA already chose OTel over hosted Logfire. |

Bottom line: **Inspect AI (with inspect_swe, inspect_harbor and inspect_evals) is the backbone for agent, coding and security evals, and its logs feed the economics. The `harbor` package is not needed; plain Harbor is an optional cross-check. promptfoo is for quick model comparisons and optional extra red teaming. LiteLLM is optional infrastructure, not an evaluation tool, and it carries supply-chain baggage that this company will care about.**

## 4. Sandbox reference setup (used for evals, and the candidate for production)

```
dev / CI  ──▶  ephemeral container (devcontainer or Inspect sandbox)
                 ├─ workspace = repo checkout only, no host $HOME, no real creds
                 ├─ harness pinned, installed from internal Artifactory, auto-update off
                 ├─ Claude Code native sandbox ON + managed settings (2nd layer)
                 └─ all egress ──▶ egress proxy (allowlist + full logging)
                                     ├─ Bedrock via VPC endpoint / PrivateLink
                                     ├─ internal LLM gateway
                                     └─ internal package mirror (no public PyPI/npm)
```

Security tests B2/B3/B5 are run against exactly this setup. "Sandboxed" then becomes a measured claim.

## 5. Phase-1 plan (proposal)

1. **Agree on gates and metrics** with security and engineering leads, using this doc. Write down the hard security gates.
2. **Pick a low-sensitivity internal repo** and build 30 replayed-MR tasks plus ~15 injection scenarios plus ~10 containment probes.
3. **Stand up the harness**: Inspect + inspect_swe + inspect_harbor in Docker, the egress proxy, canary secrets, OTel collection.
4. **Run the matrix**: OpenCode × {Claude/Bedrock, gpt-oss-120b, devstral, qwen} and Claude Code × Claude/Bedrock. Use k=3 runs per task for pass^k.
5. **Produce a scorecard per cell**:
   - Security gates (pass/fail).
   - Resolve rate, pass^k and would-merge rate.
   - $/resolved task.
   - Latency.
   - Operability checklist.
6. **Recommend** which combinations are allowed for which data classifications, and the rollout guardrails.

The same harness also becomes the long-term eval for **agentic-test-automation**:
- Tasks = Xray test cases.
- Grader = whether the generated spec passes on staging, healer iterations and MR acceptance.
- Cost from its existing `core/usage.py` token tracking (which currently has no $).

## 6. Open questions for the company

- Data classification: which repos/data may go to Bedrock, and which only to the internal gateway?
- Bedrock: which region and inference profile? Is it reachable via PrivateLink from dev machines?
- Gateway: who owns it, what does it log and retain, and what does a token actually cost (TCO)?
- Which qwen model and version is on the gateway? It is not referenced in agentic-test-automation; that repo uses gpt-oss-120b and devstral.
- Is there an existing egress proxy or devcontainer standard to build on?

## Sources

- LiteLLM compromise: [Datadog Security Labs](https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-teampcp-supply-chain-campaign/), [Comet](https://www.comet.com/site/blog/litellm-supply-chain-attack/)
- Promptfoo acquisition: [OpenAI](https://openai.com/zh-Hans-CN/index/openai-to-acquire-promptfoo/), [TechCrunch](https://www.techcrunch.com/2026/03/09/openai-acquires-promptfoo-to-secure-its-ai-agents/)
- OpenCode vulns: [NVD CVE-2026-22812](https://nvd.nist.gov/vuln/detail/cve-2026-22812), [OSV CVE-2026-22813](https://osv.dev/vulnerability/CVE-2026-22813), [Datadog: opencode upgrade RCE](https://securitylabs.datadoghq.com/articles/opencode-upgrade-remote-code-execution/)
- Claude Code sandboxing: [official docs](https://code.claude.com/docs/en/sandboxing)
- Inspect AI: [tutorial](https://inspect.aisi.org.uk/tutorial.html), [inspect_evals / Harbor note](https://simple-repository.app.cern.ch/project/inspect-evals), [inspect_swe](https://github.com/meridianlabs-ai/inspect_swe), [inspect_harbor](https://github.com/meridianlabs-ai/inspect_harbor)
- Tool-by-tool findings (licences, telemetry, egress): [existing-eval-projects.md](existing-eval-projects.md)
- Package hallucination 2026: [arXiv 2605.17062](https://arxiv.org/pdf/2605.17062)
