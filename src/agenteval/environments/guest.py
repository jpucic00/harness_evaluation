"""Guest-side helpers for providers that reach a Linux guest only through ``exec``.

Tart (over SSH) and Docker Sandboxes (``sbx exec``) move files and enforce timeouts the
same way: constant ``/bin/sh`` scripts that take their arguments as ``$1..``, GNU
``timeout`` in the guest, and read limits checked before a byte is sent.
"""

from __future__ import annotations

import errno

from inspect_ai.util import ExecResult, OutputLimitExceededError, SandboxEnvironmentLimits

FILE_TIMEOUT = 600
"""Seconds allowed for one write_file/read_file transfer."""

KILL_AFTER = 5
"""Seconds the guest's ``timeout`` waits after SIGTERM before sending SIGKILL."""

# Internal scripts: /bin/sh by absolute path with a pinned PATH, args as $1.., so the
# image's PATH (which the agent may influence) never picks what root runs.
SH_PRELUDE = "PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH\nunset CDPATH\n"

WRITE_SCRIPT = """\
f=$1
if [ -d "$f" ]; then echo "agenteval: $f: Is a directory" >&2; exit 21; fi
mkdir -p -- "$(dirname -- "$f")" || exit 1
cat > "$f"
"""

# Only regular files (after symlinks): a FIFO or device (e.g. a link to /dev/zero) at
# an agent-controlled path would otherwise block or stream until FILE_TIMEOUT. head
# bounds the transfer even if the file changes after the checks.
READ_SCRIPT = """\
f=$1
limit=$2
if [ -d "$f" ]; then echo "agenteval: $f: Is a directory" >&2; exit 21; fi
if [ ! -e "$f" ]; then echo "agenteval: $f: No such file or directory" >&2; exit 2; fi
if [ ! -f "$f" ]; then echo "agenteval: $f: Not a regular file" >&2; exit 22; fi
if [ ! -r "$f" ]; then echo "agenteval: $f: Permission denied" >&2; exit 13; fi
size=$(stat -L -c %s -- "$f" 2>/dev/null) || size=$(wc -c < "$f") || exit 1
size=$(printf '%s' "$size" | tr -d ' \\t\\n')
if [ "$size" -gt "$limit" ]; then echo "agenteval: $f: file too large" >&2; exit 27; fi
exec head -c "$((limit + 1))" -- "$f"
"""


def sh(script: str, *args: str) -> list[str]:
    return ["/bin/sh", "-c", SH_PRELUDE + script, "sh", *args]


def timed_out(returncode: int, elapsed: float, timeout: int) -> bool:
    """GNU timeout: 124 after SIGTERM, 137 after the SIGKILL escalation; 143 = SIGTERM
    from elsewhere, so it only counts once the deadline has really passed."""
    return returncode in (124, 137) or (returncode == 143 and elapsed >= timeout)


def limit_error() -> OutputLimitExceededError:
    return OutputLimitExceededError(
        limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
    )


def write_error(result: ExecResult[bytes], file: str, where: str) -> Exception:
    """The exception for a failed ``WRITE_SCRIPT``, matching Inspect's Docker provider."""
    stderr = result.stderr.decode(errors="replace").strip()
    lowered = stderr.casefold()
    if result.returncode == 21 or "is a directory" in lowered:
        return IsADirectoryError(errno.EISDIR, "Is a directory.", file)
    if "permission denied" in lowered or "read-only file system" in lowered:
        return PermissionError(errno.EACCES, f"Permission denied. {stderr}", file)
    return RuntimeError(f"write_file({file!r}) failed in {where}: {stderr}")


def read_error(result: ExecResult[bytes], file: str, where: str) -> Exception:
    """The exception for a failed ``READ_SCRIPT``, matching Inspect's Docker provider."""
    stderr = result.stderr.decode(errors="replace").strip()
    if result.returncode == 2:
        return FileNotFoundError(errno.ENOENT, "No such file or directory.", file)
    if result.returncode == 21:
        return IsADirectoryError(errno.EISDIR, "Is a directory.", file)
    if result.returncode == 22:
        return OSError(errno.EINVAL, "Not a regular file.", file)
    if result.returncode == 27:
        return limit_error()
    if result.returncode == 13 or "permission denied" in stderr.casefold():
        return PermissionError(errno.EACCES, "Permission denied.", file)
    return RuntimeError(f"read_file({file!r}) failed in {where}: {stderr}")
