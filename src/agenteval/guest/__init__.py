"""Everything in this package runs inside the environment (container or VM), not on the host.

The files are uploaded or baked into each environment and must stay stdlib-only
Python 3.12 or POSIX ``sh``/``bash``: they cannot import ``agenteval`` or any
third-party package there. The host imports some of the Python modules too (shared
parsers and constants, and the tests), and reads the rest as package data.

- ``scripts/``: used by eval runs (some also by ``agenteval dev``): probe runner,
  monitor, collector, egress policy, capability case runner.
- ``dev/``: used only by ``agenteval dev``: pty relay, repo transport, reasoning relay,
  egress proxy and the session launcher template.
- ``templates/``: harness wrapper and ``sandbox-exec`` shell templates (``@@KEY@@``
  placeholders, rendered on the host by ``harnesses.sandbox_exec.render_template``).
- ``provision/``: ``Dockerfile`` and ``provision.sh`` for the environment images.

Not to be confused with ``agenteval.environments.guest``, the host-side helpers that
run commands in an environment (``ExecGuestSandbox``).
"""
