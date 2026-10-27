"""Host-side review of what a dev session brings back.

Producers (``agenteval.dev.repo.repo_items``, ``agenteval.dev.userconfig.config_items``)
turn the session's artifacts into ``ReviewItem`` objects, computing the host's current
content at call time and their own flags. ``review`` shows a summary, then each item
as a diff, and applies only what the user approves.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from agenteval.dev.session import DevSession

Group = Literal["commits", "repo", "config", "memory", "local"]
GROUP_ORDER: tuple[Group, ...] = ("commits", "repo", "config", "memory", "local")
ItemKind = Literal["added", "modified", "deleted", "mode", "ff"]


@dataclass
class ReviewItem:
    """One change the user can apply or skip."""

    id: str
    """Stable across ``agenteval dev apply`` runs, e.g. ``repo:src/x.py``."""
    group: Group
    label: str
    """Path or description shown to the user."""
    kind: ItemKind
    current: bytes | None
    """Host content now (None: absent). For ``ff`` items a text description."""
    proposed: bytes | None
    """What ``apply`` writes (None: delete). For ``ff`` items a text description."""
    apply: Callable[[bytes | None], None]
    """Write ``proposed`` (or the user's edited version) to the host."""
    binary: bool = False
    conflict: bool = False
    """``proposed`` carries conflict markers (or the host file is kept, see ``note``)."""
    host_changed: bool = False
    """The host file changed since the session started (3-way merged)."""
    editable: bool = True
    flags: list[str] = field(default_factory=list)
    """Attention pointers (not guarantees): why this change deserves a careful look."""
    note: str | None = None


@dataclass(frozen=True)
class DevIO:
    """Terminal access for prompts and review; tests pass scripted functions."""

    out: Callable[[str], None]
    ask: Callable[[str], str]
    edit: Callable[[bytes, str], bytes | None]
    """``(content, file suffix) -> edited content`` (None: editing cancelled)."""
    color: bool = False
    interactive: bool = True


ReviewOutcome = Literal["done", "deferred", "discarded"]

MAX_DIFF_LINES = 2000
GROUP_TITLES: dict[Group, str] = {
    "commits": "commits",
    "repo": "repo files",
    "config": "CLI config",
    "memory": "memory",
    "local": "CLAUDE.local.md",
}
_KIND_WORDS: dict[ItemKind, tuple[str, str]] = {
    "added": ("added", "added"),
    "modified": ("modified", "modified"),
    "deleted": ("deleted", "deleted"),
    "mode": ("mode change", "mode changes"),
    "ff": ("fast-forward", "fast-forwards"),
}
_GREEN, _RED, _CYAN, _YELLOW, _BOLD, _RESET = (
    "\x1b[32m",
    "\x1b[31m",
    "\x1b[36m",
    "\x1b[33m",
    "\x1b[1m",
    "\x1b[0m",
)
# Content comes from the env: terminal control sequences in it could hide or fake lines.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_SUFFIX = re.compile(r"\.[A-Za-z0-9_+-]{1,16}")
_ITEM_HELP = """\
  y  apply this change to the host
  n  skip it
  e  edit the proposed content in $VISUAL / $EDITOR, then apply your version
  a  apply this and the rest of this group (still stops at each flagged item)
  d  skip this and the rest of this group
  q  quit; undecided items stay pending for 'agenteval dev apply {id}'"""


def terminal_io() -> DevIO:
    """``DevIO`` on the real terminal; ``out`` prints one line, ``edit`` runs ``$VISUAL``.

    Uses ``sys.__stdout__``/``sys.__stdin__``: Inspect's display may have swapped
    ``sys.stdout``.
    """
    stdout = sys.__stdout__ or sys.stdout
    stdin = sys.__stdin__ or sys.stdin
    encoding = getattr(stdout, "encoding", None) or "utf-8"

    def out(text: str) -> None:
        # A non-UTF-8 terminal must not crash the review on characters it cannot show.
        stdout.write((text + "\n").encode(encoding, "replace").decode(encoding))
        stdout.flush()

    def ask(prompt: str) -> str:
        saved = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = stdin, stdout
        try:
            return input(prompt)
        finally:
            sys.stdin, sys.stdout = saved

    def edit(content: bytes, suffix: str) -> bytes | None:
        candidates = (os.environ.get("VISUAL"), os.environ.get("EDITOR"))
        editor = next((value for value in candidates if value and value.strip()), "vi")
        try:
            argv = shlex.split(editor)
        except ValueError as exc:
            out(f"Could not parse the editor command {editor!r}: {exc}")
            return None
        with tempfile.NamedTemporaryFile(
            prefix="agenteval-review-", suffix=suffix, delete=False
        ) as f:
            f.write(content)
        path = Path(f.name)
        try:
            try:
                done = subprocess.run([*argv, str(path)], check=False)
                return path.read_bytes() if done.returncode == 0 else None
            except OSError as exc:
                out(f"Editing with {editor!r} failed: {exc}")
                return None
        finally:
            path.unlink(missing_ok=True)

    return DevIO(
        out=out,
        ask=ask,
        edit=edit,
        color=stdout.isatty() and "NO_COLOR" not in os.environ,
        interactive=stdin.isatty() and stdout.isatty(),
    )


def _escape(match: re.Match[str]) -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def visible_text(text: str) -> str:
    """Make control characters visible (keeps tabs and newlines)."""
    return _UNSAFE.sub(_escape, text)


def _line(text: str) -> str:
    """One display line: control characters and newlines made visible."""
    return visible_text(text).replace("\n", "\\n")


def _indented(text: str, indent: str) -> str:
    """A possibly multi-line text, control characters made visible, every line indented."""
    return indent + visible_text(text.rstrip("\n")).replace("\n", "\n" + indent)


def _paint(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _text_lines(data: bytes) -> list[str]:
    parts = data.decode("utf-8", errors="replace").split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _describe(data: bytes | None) -> str:
    if data is None:
        return "(none)"
    return visible_text(data.decode("utf-8", errors="replace").rstrip("\n")).replace("\n", "\n    ")


def render_diff(item: ReviewItem, color: bool) -> str:
    """Unified diff from the host's current content to the proposed one."""
    if item.kind == "ff":
        return f"  now:      {_describe(item.current)}\n  proposed: {_describe(item.proposed)}"
    if item.binary:
        if item.current is None:
            return f"binary file, added ({len(item.proposed or b'')} bytes)"
        if item.proposed is None:
            return f"binary file, deleted ({len(item.current)} bytes)"
        return f"binary file, {len(item.current)} -> {len(item.proposed)} bytes"
    label = _line(item.label)
    diff = difflib.unified_diff(
        _text_lines(item.current or b""),
        _text_lines(item.proposed or b""),
        fromfile="/dev/null" if item.current is None else f"{label} (host now)",
        tofile="/dev/null" if item.proposed is None else f"{label} (proposed)",
    )
    lines: list[str] = []
    for raw in diff:
        lines.append(visible_text(raw.removesuffix("\n")))
        if not raw.endswith("\n"):
            lines.append("\\ No newline at end of file")
    if not lines:
        if item.current is None:
            return "(empty file added)"
        if item.proposed is None:
            return "(empty file deleted)"
        return "(no content change)"
    more = len(lines) - MAX_DIFF_LINES
    if more > 0:
        lines = lines[:MAX_DIFF_LINES] + [f"... {more} more lines"]
    if color:
        # The first two lines are the file headers; a content line may also start with
        # "---"/"+++" (e.g. a removed "-- comment") and must keep its red/green colour.
        lines = [_paint(line, _BOLD, True) for line in lines[:2]] + [
            _color_diff_line(line) for line in lines[2:]
        ]
    return "\n".join(lines)


def _color_diff_line(line: str) -> str:
    if line.startswith("+"):
        return _paint(line, _GREEN, True)
    if line.startswith("-"):
        return _paint(line, _RED, True)
    if line.startswith("@@"):
        return _paint(line, _CYAN, True)
    return line


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def summarize(
    session: DevSession, items: list[ReviewItem], notes: Sequence[str], usage: str | None
) -> str:
    """Session header, change counts per group, flagged items, notes and usage."""
    lines = [
        f"agenteval dev session {_line(session.id)}",
        f"  harness {_line(session.harness)}, model {_line(session.model)}, "
        f"environment {_line(session.environment)}, {session.mount} mode",
        f"  repo {_line(session.repo.root)}",
    ]
    if usage:
        lines.append(f"  usage: {_line(usage)}")
    if not items:
        lines.append("Nothing to review.")
    else:
        lines.append(f"Changes to review ({len(items)}):")
        for group in GROUP_ORDER:
            in_group = [item for item in items if item.group == group]
            if not in_group:
                continue
            kinds = Counter(item.kind for item in in_group)
            parts = [
                _plural(kinds[kind], *words) for kind, words in _KIND_WORDS.items() if kinds[kind]
            ]
            conflicts = sum(item.conflict for item in in_group)
            if conflicts:
                parts.append(_plural(conflicts, "conflict", "conflicts"))
            host_changed = sum(item.host_changed for item in in_group)
            if host_changed:
                parts.append(f"{host_changed} changed on host since the session started")
            lines.append(f"  {GROUP_TITLES[group]:<16}{', '.join(parts)}")
    flagged = [item for item in items if item.flags]
    if items and not flagged:
        lines.append(
            "No item is flagged. Flags are attention pointers, not guarantees: "
            "read every diff you apply."
        )
    if flagged:
        lines.append(
            "Flagged for a careful look. Flags are attention pointers, not guarantees: "
            "an unflagged change can still be harmful, so read every diff you apply."
        )
        for item in flagged:
            lines.append(f"  {GROUP_TITLES[item.group]}: {_line(item.label)}")
            lines.extend(f"    [!] {_line(flag)}" for flag in item.flags)
    if notes:
        lines.append("Notes:")
        lines.extend("  - " + _indented(note, "    ")[4:] for note in notes)
    return "\n".join(lines)


def _item_header(item: ReviewItem, position: int, total: int, color: bool) -> str:
    kind = _KIND_WORDS[item.kind][0]
    lines = [
        "",
        _paint(
            f"[{position}/{total}] {GROUP_TITLES[item.group]}: {_line(item.label)} ({kind})",
            _BOLD,
            color,
        ),
    ]
    lines.extend(_paint(f"  [!] {_line(flag)}", _YELLOW, color) for flag in item.flags)
    if item.host_changed:
        lines.append("  changed on host since the session started (3-way merged)")
    if item.conflict:
        lines.append(_paint("  CONFLICT", _RED, color))
    if item.note:
        lines.append(_indented(item.note, "  "))
    return "\n".join(lines)


def _editable(item: ReviewItem) -> bool:
    return item.editable and not item.binary and item.kind != "ff" and item.proposed is not None


def _suffix(label: str) -> str:
    suffix = PurePosixPath(label).suffix
    return suffix if _SUFFIX.fullmatch(suffix) else ""


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _json_error(label: str, content: bytes) -> str | None:
    if not label.lower().endswith(".json"):
        return None
    try:
        json.loads(content, parse_constant=_reject_constant)
    except ValueError as exc:
        return str(exc)
    return None


class _Review:
    """State of one ``review`` run: records and saves a decision per item."""

    def __init__(self, session: DevSession, io: DevIO, save: Callable[[DevSession], None]) -> None:
        self.session = session
        self.io = io
        self.save = save

    def record(self, item: ReviewItem, decision: Literal["applied", "skipped"]) -> None:
        self.session.decisions[item.id] = decision
        self.save(self.session)
        self.io.out(f"  {decision}  {_line(item.label)}")

    def apply(self, item: ReviewItem, content: bytes | None) -> None:
        try:
            item.apply(content)
        except Exception as exc:
            self.io.out(f"Could not apply {_line(item.label)}: {_line(str(exc))} (left pending)")
            return
        self.record(item, "applied")

    def ask_item(
        self, item: ReviewItem, position: int, total: int
    ) -> Literal["next", "apply_rest", "skip_rest", "quit"]:
        """Show one item and act on the user's answer."""
        io = self.io
        io.out(_item_header(item, position, total, io.color))
        io.out(render_diff(item, io.color))
        editable = _editable(item)
        edit = "[e] edit then apply  " if editable else ""
        prompt = (
            f"[y] apply  [n] skip  {edit}[a] apply the rest of this group  "
            "[d] skip the rest of this group  [q] quit (rest stays pending)  [?] help > "
        )
        draft = item.proposed or b""
        while True:
            answer = io.ask(prompt).strip().lower()
            if answer in ("y", "a"):
                self.apply(item, item.proposed)
                return "apply_rest" if answer == "a" else "next"
            if answer in ("n", "d"):
                self.record(item, "skipped")
                return "skip_rest" if answer == "d" else "next"
            if answer == "q":
                return "quit"
            if answer == "?":
                io.out(_ITEM_HELP.format(id=self.session.id))
            elif answer == "e" and not editable:
                io.out("This item cannot be edited.")
            elif answer == "e":
                edited = io.edit(draft, _suffix(item.label))
                if edited is None:
                    io.out("Edit cancelled; nothing applied.")
                    continue
                error = _json_error(item.label, edited)
                if error is not None:
                    draft = edited
                    io.out(f"Not valid JSON ({_line(error)}); nothing applied. [e] edits it again.")
                    continue
                self.apply(item, edited)
                return "next"
            else:
                io.out(f"Unknown answer {_line(answer)!r}; type ? for help.")


_START_ANSWERS: dict[str, Literal["review", "later", "discard"]] = {
    "y": "review",
    "l": "later",
    "d": "discard",
}


def _ask_start(io: DevIO, later: str) -> Literal["review", "later", "discard"]:
    prompt = f"[y] review now  [l] later ({later})  [d] discard all > "
    while True:
        choice = _START_ANSWERS.get(io.ask(prompt).strip().lower())
        if choice is not None:
            return choice
        io.out("Answer y, l or d.")


def review(
    session: DevSession,
    items: list[ReviewItem],
    io: DevIO,
    *,
    save: Callable[[DevSession], None],
    notes: Sequence[str] = (),
    usage: str | None = None,
) -> ReviewOutcome:
    """Summarize, then ask per pending item; ``save`` runs after every decision.

    Items with a decision in ``session.decisions`` are not shown again. Returns ``done``
    when every item is decided, ``deferred`` when some stay pending (``q``, ``l``,
    Ctrl-C, a failed apply, no terminal) and ``discarded`` when the user skipped all.
    The caller prints how to resume a deferred review.
    """
    pending = sorted(
        (item for item in items if item.id not in session.decisions),
        key=lambda item: GROUP_ORDER.index(item.group),
    )
    io.out(summarize(session, pending, notes, usage))
    if not pending:
        return "done"
    if not io.interactive:
        io.out("No terminal to review on: nothing applied.")
        return "deferred"
    try:
        start = _ask_start(io, f"agenteval dev apply {session.id}")
    except (KeyboardInterrupt, EOFError):
        start = "later"
    if start == "later":
        io.out("Nothing applied.")
        return "deferred"
    if start == "discard":
        for item in pending:
            session.decisions[item.id] = "skipped"
        save(session)
        io.out(f"Discarded: skipped {_plural(len(pending), 'pending item', 'pending items')}.")
        return "discarded"

    state = _Review(session, io, save)
    rest: dict[Group, Literal["apply_rest", "skip_rest"]] = {}
    try:
        for position, item in enumerate(pending, 1):
            mode = rest.get(item.group)
            if mode == "skip_rest":
                state.record(item, "skipped")
                continue
            if mode == "apply_rest" and not item.flags:
                state.apply(item, item.proposed)
                continue
            if mode == "apply_rest":
                io.out("\nFlagged item: asking before applying it.")
            step = state.ask_item(item, position, len(pending))
            if step == "quit":
                break
            if step != "next":
                rest[item.group] = step
    except (KeyboardInterrupt, EOFError):
        save(session)
    left = sum(item.id not in session.decisions for item in pending)
    if left:
        io.out(f"{_plural(left, 'item', 'items')} still pending.")
        return "deferred"
    return "done"
