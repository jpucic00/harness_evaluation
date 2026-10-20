# Capability cases

A capability case is one everyday developer task: fix a bug, answer questions about
documentation, do some git work, or review code. The agent works in a copy of `repo/`
(a git repository). Afterwards the scorer runs **reference cases** against the
workspace from outside the agent's reach and compares the results on the host, so the
agent cannot see or fake the expected values. The case is *resolved* when every
reference case passes and the visible tests were not weakened. Tokens, cost and time
are recorded for every case, so each kind of task also gets a cost estimate.

## Kinds of task

All kinds use the same files; what differs is what the reference cases look at.

| Kind | The agent... | Reference cases... | Examples |
| --- | --- | --- | --- |
| Code | fixes or changes Python code | import the workspace modules and call them | cap-01 to cap-06 |
| Docs Q&A | reads docs (and code) and fills in a shipped `answers.json` | read `answers.json` and normalise each answer | cap-07 to cap-09 |
| Git | commits, branches, merges, reverts, tags, pushes | run `git` in the workspace (and in its local `origin`) | cap-10 to cap-16 |
| Review | records findings in a shipped `findings.json` | match findings against planted defects; check decoys stay unreported | cap-17 to cap-19 |

## Add one

```bash
cp -R cases/capability/_template cases/capability/cap-20-short-name
```

Then edit the copy:

| File | What to put in it |
| --- | --- |
| `task.yaml` | `id` (must equal the folder name), `difficulty` (`easy` / `medium` / `hard`), `summary` (one line for the report), `prompt` (written like a user's request: for a bug, the symptom, not the fix). Optional `prompt_suffix`: appended to the prompt, by default "The project is in the current working directory. You can run its tests with `python3 -m pytest -q`." Replace it for tasks that are not about running tests; `""` drops it. |
| `repo/` | The project. Keep it small. Visible tests go in `repo/tests/` (required with the default `prompt_suffix`, which tells the agent to run them), plus the `pytest.ini` and `.gitignore` from the template. Python module names must be unique across all tasks. |
| `setup.sh` | Optional. Runs with bash (`-euo pipefail`) in the workspace as the agent user, after `repo/` is committed as "Initial commit" on `main`. Use it for git history, branches, tags, a local `origin`, or uncommitted changes. Never copied into the workspace. |
| `<name>_cases.py` | Reference cases (the file name must end in `_cases.py` and be unique across all tasks). `SETUP` is Python run before every case. `CASES` is a list of `Case(name, code, expected)`. At least 10. |
| `solution.patch` or `solution.sh` | A correct solution (exactly one of them). The patch is a `git diff` from the repo root; the script runs with bash in the workspace after `setup.sh`, for solutions a patch cannot express (commits, branches, tags). Never shown to the agent; the tests use it to prove the case is solvable. |

Making `solution.patch`:

```bash
tmp=$(mktemp -d) && cp -R cases/capability/cap-20-short-name/repo/. "$tmp" && cd "$tmp"
git init -q && git add -A && git -c user.name=x -c user.email=x@x commit -qm base
# fix the bug (or fill in answers.json / findings.json) in $tmp, then:
git diff > "$OLDPWD/cases/capability/cap-20-short-name/solution.patch"
```

Check it with `uv run pytest -q tests/test_suites.py`. Your case must **fail** on the
shipped repo (after `setup.sh`) and **pass** with the solution applied.

To watch an agent try the task first, start `agenteval dev` in such a scratch copy
before you fix it ([Explore with `agenteval dev`](../README.md#explore-with-agenteval-dev)).
`setup.sh` does not run there, so do its steps in the copy first if the task needs them.
In copy mode only the current branch's history goes in (no other branches, tags,
remotes or `.git/config`); `--mount live` mounts the folder itself, `.git` included.

## Writing good reference cases

- `code` is an expression, or statements that assign `result`. `expected` is a plain
  value (None, bool, int, float, str, Decimal, and lists/tuples/dicts of those) or
  `Raises("ValueError")`.
- `WORKSPACE` (the workspace path) is defined before `SETUP` runs, and the workspace is
  on `sys.path`, so `SETUP` can import its modules, read files in it or run
  `subprocess.run(["git", "-C", WORKSPACE, ...])`.
- Return the raw value, not a comparison, when the value comes from the agent's code:
  `f(x) == y` would let code that always returns `True` pass. Comparisons of git
  state (`git rev-parse a == git rev-parse b`) are fine.
- Return plain values, not objects of the task's own classes.
- Code tasks: cover the visible tests, then the actual bug, then edge cases (empty
  input, boundaries, error handling).
- Docs Q&A: ship `answers.json` with every key set to `null`, and say the exact answer
  format for each question in the repo (see cap-07's `QUESTIONS.md`). Normalise in
  `SETUP` (case, whitespace, numbers given as strings, aliases) so a correct answer in
  a slightly different form still passes. Include questions that need more than one
  lookup: a superseded decision, arithmetic on documented numbers, a place where the
  code and the doc disagree.
- Git: set a repo-local identity in `setup.sh` (`git config user.name/user.email`)
  so the agent never has to touch `~/.gitconfig` (the boundary monitor watches it).
  For pushes, create a bare `origin` inside `.git/` (`git init --bare
  .git/origin.git`): sandboxed harnesses can write only to the workspace. Check
  outcomes, not commands: commit count and parents, files per commit, branch and tag
  targets, upstream tracking, a clean `git status`, and the behaviour of the
  committed code (`git show REV:file`). Find setup commits by exact subject, not by
  hash. A tree hash (`git rev-parse BRANCH^{tree}`) depends only on file contents, so
  it can be an expected value.
- Review: define a fixed list of rule or category ids in the repo, plant each defect
  at a known place, and match a finding on file, id and a small line range. Add
  correct decoy code (an `alt=""` decorative image, a parameterised query) and check
  it is not reported, and cap the number of findings that match nothing.
- Difficulty: *easy* = one obvious step; *medium* = needs reading a couple of files or
  several git steps; *hard* = several interacting fixes, or answers that need the
  code and the docs together.

## Running only some cases

In a run file, the capability suite takes `ids` and `limit` (on the command line there
is only `--limit`):

```yaml
suites:
  - name: capability
    options:
      ids: [cap-20-short-name]
```
