#!/bin/sh
# agenteval dev launcher - generated per session by agenteval, do not edit.
# Starts the harness CLI interactively, wired to the session's model bridge and
# inside the harness's inner sandbox.
set -eu
@@ENV@@
: "${TERM:=xterm-256color}"
infocmp "$TERM" >/dev/null 2>&1 || TERM=xterm-256color
export TERM
cd -- @@CWD@@
exec @@COMMAND@@ "$@"
