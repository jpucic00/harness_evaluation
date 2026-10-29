# Containment cases (security)

A containment case is one **probe** of the sandbox boundary: can a process running
as the agent user, inside the harness's inner sandbox, do X? No model is involved.
Every probe runs in every configuration, and the report shows the configurations
side by side (`exposed` / `contained`). This is where the difference between "no
sandbox" and "bubblewrap" shows up.

Each file is one check. The file name is the check id: `<area>.<name>.py`, lowercase,
e.g. `net.dns.py`, `fs.secret_home.py`.

## Add one

```bash
cp cases/containment/_template.py cases/containment/fs.browser_cookies.py
```

Set the header constants and write `run(cfg)`:

| Name | Required | Meaning |
| --- | --- | --- |
| `CATEGORY` | yes | `secrets`, `filesystem`, `network`, `privilege`, `process`, `host`, `info`, or a new one (shown as its own group) |
| `SEVERITY` | yes | `high`, `medium`, `low` or `info` |
| `TITLE` | yes | The risk in one line, e.g. "Browser cookies are readable" |
| `SCORED` | no (`True`) | `False` = informational, never counted as exposed |
| `REQUIRES_CANARIES` | no (`False`) | Only scored when the environment plants fake secrets (`ambient_secrets: true`) |
| `VERIFY_WRITES` | no (`False`) | The check writes marker files with `write_outcome`: the host confirms from outside, as root, that they reached the real filesystem, and downgrades to `contained` if they did not |
| `run(cfg) -> Outcome` | yes | The probe itself |

`run` returns `Outcome(status, evidence, data)`:

| Status | Use when |
| --- | --- |
| `exposed` | The action succeeded (the boundary did not hold) |
| `contained` | It was blocked, or the thing does not exist |
| `error` | The check could not tell (it is shown, but never counted either way) |
| `info` | Informational result |

`evidence` is one human-readable line (it appears in the report on hover). `data` is
an optional dict for details. An exception in `run` becomes `error` with the message,
so one broken check never hides the others.

## Rules

- **Standard library only, Python 3.12.** The file runs inside the environment.
  Import shared code from `probe` only.
- **Benign.** Read, connect with a short timeout (`cfg.timeout`), or write a marker
  with `write_outcome`. Never exploit, modify or delete anything.
- **Fast.** A probe should finish within a few seconds.

## What `probe` gives you

| Name | Use |
| --- | --- |
| `Config` (`cfg`) | `cfg.home`, `cfg.workspace`, `cfg.timeout`, `cfg.marker`, `cfg.canaries_planted`, `cfg.expand("~/x")`, and `cfg.raw.get("key", default)` for your own settings. Existing settings: `secret_home_paths`, `secret_system_paths`, `write_home_dir`, `write_tmp_dir`, `write_system_dirs`, `collector_host`, `collector_port`, `internet_target`, `dns_name`, `host_ports`, `docker_sockets`, `shadow_path`, `proc_dir`, `mounts_file`. |
| `Outcome(status, evidence, data)` | The result |
| `read_secret_files(cfg, paths)` | `exposed` if any file contains a canary token |
| `write_outcome(cfg, dirs)` | Writes a marker into each dir; `exposed` if any write succeeded (pair it with `VERIFY_WRITES = True`) |
| `tcp_connect(host, port, timeout)` | `None` on success, else the error text |
| `resolve_with_timeout(name, port, timeout)` | Addresses, or the exception |
| `split_host_port("host:port")`, `default_gateway(route_file)`, `gateway_port(host, port, timeout)` | Network helpers |
| `read_small(path)`, `err(exception)` | A small file's text or `None`; an exception as one line |
| `CANARY_PREFIX`, `COLLECTOR_HOST`, `COLLECTOR_PORT`, `SECRETS_DIR`, `OPT_DIR` | Shared constants |

A setting that differs per environment (a hostname, a port list) belongs in the
environment profile's `probe:` settings. That needs a code change in
`src/agenteval/config.py` (`ProbeSettings`) and `suites/containment.py`
(`probe_config`). Until then, give your check a sensible default via `cfg.raw.get`.

## Try it

```bash
uv run pytest -q tests/test_containment_scripts.py         # loads and validates every check
uv run python src/agenteval/guest/scripts/probe.py --checks cases/containment \
  --config '{"only": ["fs.browser_cookies"]}'              # runs it on this machine (as you)
uv run agenteval run smoke-docker --suite containment      # in Docker, both sandbox configs
```
