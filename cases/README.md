# Cases

Every test agenteval runs is a **case**: one file or one folder in this directory.
To add one, copy the `_template` of the kind you want, edit it, and run the check
command below. You never touch the suite code: new files are picked up
automatically, and the tests validate them.

| Kind | What a case is | Add a case by | Template | Guide |
| --- | --- | --- | --- | --- |
| **Capability** | An everyday developer task (bug fix, docs questions, git workflow, review), graded by hidden reference cases | a folder `capability/<id>/` | [capability/_template/](capability/_template/) | [capability/README.md](capability/README.md) |
| **Containment** (security) | One sandbox-boundary probe: "can the agent user reach X?" No model involved. | a file `containment/<area>.<name>.py` | [containment/_template.py](containment/_template.py) | [containment/README.md](containment/README.md) |
| **Boundary monitor** (security) | A group of locations watched for persistence (files that make code run later) | a file `monitor/<group>.yaml` | [monitor/_template.yaml](monitor/_template.yaml) | [monitor/README.md](monitor/README.md) |
| **Prompt injection** (security) | A coding task with an instruction planted in the repo or issue, in CodeIPI's format | a folder `injection/<id>/` | [injection/_template/](injection/_template/) | [injection/README.md](injection/README.md) |
| **Economics** | No cases of its own: tokens, cost and time are measured on every case above | — | — | [below](#economics) |

Names starting with `_` (the templates) are never run.

## Workflow

```bash
cp -R cases/capability/_template cases/capability/cap-07-my-task    # or the kind you need
# edit the copy
uv run pytest -q                                                    # validates every case
uv run agenteval run smoke-docker                                   # optional: offline end-to-end run (mock model)
```

Then run only your case against a real model, for example:

```bash
uv run agenteval run opencode-qwen-docker --suite capability --limit 1
```

What `uv run pytest` checks for you:

| Kind | Checked automatically |
| --- | --- |
| Capability | `task.yaml` is valid and its `id` matches the folder; the reference cases **fail** on the shipped repo (after `setup.sh`) and **all pass** after `solution.patch` or `solution.sh`; at least 10 cases; module names are unique across tasks. |
| Containment | The file loads (`CATEGORY`, `SEVERITY`, `TITLE`, `run`), its name is a valid id, it only imports the standard library and `probe`. |
| Monitor | The YAML has only known keys, a `title`, an integer `priority` and at least one path. |
| Injection | `case.yaml` matches the schema, the category fields are consistent (`exec_marker`, `persistence_targets`), `repo/` exists, and the id does not clash with CodeIPI's. |

## Rules for every case

- **Benign.** Cases test whether something *could* happen. Use the planted fake
  secrets (canaries), markers and the local collector; never real credentials,
  real exploits, or anything that modifies or deletes outside the sandbox.
- **Deterministic.** The same agent behaviour must give the same verdict. No network
  access, no time-of-day or random dependence.
- **Self-contained.** Everything a case needs is in its file or folder.
- **Explain the why.** Every template has a one-line description field: say what
  the case shows and why it matters.

## For AI agents writing cases

If you are an agent asked to add a case:

1. Read this file, then the guide for the kind (linked in the table above).
2. Copy the `_template`, never edit it in place. Pick an id that follows the existing
   names in that folder.
3. Keep to the format the guide describes; do not change files under `src/`, the
   loaders or the tests to make a case fit. If a case needs something the format
   does not offer, stop and say so.
4. Run `uv run pytest -q` and fix what it reports. For capability tasks, create
   `solution.patch` with `git diff` from a scratch copy of `repo/`, or `solution.sh` for
   git tasks (see that guide).
5. Report the new path and what the case tests.

Injection cases contain an instruction aimed at the agent under test. The person
asking for the case writes that text; an agent only builds the case around it.

## Economics

Economics has no cases. Every sample from the suites above records input, output,
cache and reasoning tokens, model and tool calls, and wall-clock and working time.
The report turns them into cost, cost per resolved task and tokens per resolved task,
split per model when subagents run on a second model. To change what it costs, edit
the `pricing` of the model profile in [../config/models/](../config/models/). More
capability cases make the economics comparison more meaningful.
