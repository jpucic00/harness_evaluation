"""Dev-session review UI: summary, diffs, the scripted review loop and the terminal IO."""

from __future__ import annotations

import io
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agenteval.dev.review import (
    MAX_DIFF_LINES,
    DevIO,
    Group,
    ItemKind,
    ReviewItem,
    render_diff,
    review,
    summarize,
    terminal_io,
)
from agenteval.dev.session import SESSION_FILE, DevSession, RepoState, save_session

ESC = "\x1b"
RLO = chr(0x202E)
SESSION_ID = "20261019T120000Z-demo-ab12"


class ScriptedIO:
    """``DevIO`` driven by a list of answers; records prompts, output and edits."""

    def __init__(
        self,
        answers: Sequence[str | BaseException] = (),
        *,
        edits: Sequence[bytes | None] = (),
        interactive: bool = True,
        color: bool = False,
    ) -> None:
        self.answers = list(answers)
        self.edits = list(edits)
        self.prompts: list[str] = []
        self.output: list[str] = []
        self.edit_calls: list[tuple[bytes, str]] = []
        self.io = DevIO(
            out=self.output.append,
            ask=self.ask,
            edit=self.edit,
            color=color,
            interactive=interactive,
        )

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def edit(self, content: bytes, suffix: str) -> bytes | None:
        self.edit_calls.append((content, suffix))
        return self.edits.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class Host:
    """Fake host: each item's ``apply`` records what it would write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes | None]] = []

    def item(
        self,
        item_id: str,
        *,
        group: Group = "repo",
        kind: ItemKind = "modified",
        current: bytes | None = b"old\n",
        proposed: bytes | None = b"new\n",
        flags: Sequence[str] = (),
        binary: bool = False,
        editable: bool = True,
        conflict: bool = False,
        host_changed: bool = False,
        note: str | None = None,
        fail: bool = False,
    ) -> ReviewItem:
        def apply(content: bytes | None) -> None:
            if fail:
                raise RuntimeError("disk full")
            self.writes.append((item_id, content))

        return ReviewItem(
            id=item_id,
            group=group,
            label=item_id.split(":", 1)[1],
            kind=kind,
            current=current,
            proposed=proposed,
            apply=apply,
            binary=binary,
            conflict=conflict,
            host_changed=host_changed,
            editable=editable,
            flags=list(flags),
            note=note,
        )


class Saves:
    """``save`` callback that snapshots the decisions on every call."""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, str]] = []

    def __call__(self, session: DevSession) -> None:
        self.snapshots.append(dict(session.decisions))


def make_session() -> DevSession:
    return DevSession(
        id=SESSION_ID,
        created=datetime(2026, 10, 19, 12, tzinfo=UTC),
        harness="claude-code-bwrap",
        model="mockllm/model",
        environment="docker-ubuntu",
        vm_root="/workspace",
        vm_cwd="/workspace",
        repo=RepoState(root="/home/me/demo", git=True),
    )


def headers(scripted: ScriptedIO) -> list[str]:
    """Item header lines (``[n/N] group: label (kind)``) in the order shown."""
    return [
        line
        for chunk in scripted.output
        for line in chunk.splitlines()
        if line.startswith("[") and "/" in line.split("]", 1)[0]
    ]


# --- review loop ---------------------------------------------------------------------


def test_yes_and_no_save_after_every_item() -> None:
    host, saves, session = Host(), Saves(), make_session()
    items = [host.item("repo:first.py"), host.item("repo:second.py")]
    scripted = ScriptedIO(["y", "y", "n"])
    assert review(session, items, scripted.io, save=saves) == "done"
    assert host.writes == [("repo:first.py", b"new\n")]
    assert session.decisions == {"repo:first.py": "applied", "repo:second.py": "skipped"}
    assert saves.snapshots == [
        {"repo:first.py": "applied"},
        {"repo:first.py": "applied", "repo:second.py": "skipped"},
    ]


def test_items_follow_group_order() -> None:
    host = Host()
    items = [
        host.item("memory:MEMORY.md", group="memory"),
        host.item("repo:first.py"),
        host.item("commits:main", group="commits", kind="ff", editable=False),
        host.item("config:settings.json", group="config"),
    ]
    scripted = ScriptedIO(["y", "n", "n", "n", "n"])
    assert review(make_session(), items, scripted.io, save=Saves()) == "done"
    assert [line.split("] ", 1)[1].split(":", 1)[0] for line in headers(scripted)] == [
        "commits",
        "repo files",
        "CLI config",
        "memory",
    ]
    assert host.writes == []


def test_edit_then_apply() -> None:
    host = Host()
    scripted = ScriptedIO(["y", "e"], edits=[b"mine\n"])
    result = review(make_session(), [host.item("repo:src/x.py")], scripted.io, save=Saves())
    assert result == "done"
    assert scripted.edit_calls == [(b"new\n", ".py")]
    assert host.writes == [("repo:src/x.py", b"mine\n")]


def test_edit_with_invalid_json_reprompts() -> None:
    host, session = Host(), make_session()
    item = host.item("config:claude/settings.json", group="config", proposed=b'{"a": 1}\n')
    scripted = ScriptedIO(["y", "E", "e"], edits=[b"{broken", b'{"a": 2}\n'])
    assert review(session, [item], scripted.io, save=Saves()) == "done"
    assert "Not valid JSON" in scripted.text
    # The second edit starts from the user's broken draft, not from the proposal.
    assert scripted.edit_calls == [(b'{"a": 1}\n', ".json"), (b"{broken", ".json")]
    assert host.writes == [("config:claude/settings.json", b'{"a": 2}\n')]
    assert session.decisions == {"config:claude/settings.json": "applied"}


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_edit_rejects_non_standard_json_constants(constant: bytes) -> None:
    host = Host()
    item = host.item("config:claude/settings.json", group="config", proposed=b"{}\n")
    scripted = ScriptedIO(["y", "e", "n"], edits=[b'{"a": ' + constant + b"}"])
    assert review(make_session(), [item], scripted.io, save=Saves()) == "done"
    assert "Not valid JSON" in scripted.text
    assert host.writes == []


def test_cancelled_edit_reprompts_and_applies_nothing() -> None:
    host, session = Host(), make_session()
    scripted = ScriptedIO(["y", "e", "n"], edits=[None])
    assert review(session, [host.item("repo:x.py")], scripted.io, save=Saves()) == "done"
    assert "Edit cancelled" in scripted.text
    assert host.writes == []
    assert session.decisions == {"repo:x.py": "skipped"}


@pytest.mark.parametrize(
    "item",
    [
        Host().item("repo:logo.png", binary=True, current=b"\x89PNG", proposed=b"\x89PNG2"),
        Host().item("repo:locked.txt", editable=False),
        Host().item("repo:gone.txt", kind="deleted", proposed=None),
        Host().item("commits:main", group="commits", kind="ff"),
    ],
    ids=["binary", "not-editable", "delete", "ff"],
)
def test_edit_refused_when_not_editable(item: ReviewItem) -> None:
    scripted = ScriptedIO(["y", "e", "n"])
    assert review(make_session(), [item], scripted.io, save=Saves()) == "done"
    assert scripted.edit_calls == []
    assert "cannot be edited" in scripted.text
    assert "[e]" not in scripted.prompts[1]


def test_apply_rest_of_group() -> None:
    host, session = Host(), make_session()
    items = [
        host.item("repo:first.py"),
        host.item("repo:second.py"),
        host.item("repo:third.py"),
        host.item("config:settings.json", group="config"),
    ]
    scripted = ScriptedIO(["y", "a", "n"])
    assert review(session, items, scripted.io, save=Saves()) == "done"
    assert [w[0] for w in host.writes] == ["repo:first.py", "repo:second.py", "repo:third.py"]
    assert session.decisions["config:settings.json"] == "skipped"
    assert len(scripted.prompts) == 3  # start, first.py, settings.json


def test_apply_rest_stops_at_flagged_item() -> None:
    host, session = Host(), make_session()
    items = [
        host.item("repo:first.py"),
        host.item("repo:Makefile", flags=["host-executed file: Makefile"]),
        host.item("repo:third.py"),
    ]
    scripted = ScriptedIO(["y", "a", "n"])
    assert review(session, items, scripted.io, save=Saves()) == "done"
    assert [w[0] for w in host.writes] == ["repo:first.py", "repo:third.py"]
    assert session.decisions["repo:Makefile"] == "skipped"
    assert "Flagged item: asking before applying it." in scripted.text
    assert "[!] host-executed file: Makefile" in scripted.text
    assert [h.split(" ", 1)[0] for h in headers(scripted)] == ["[1/3]", "[2/3]"]


def test_skip_rest_at_flagged_item_ends_apply_rest() -> None:
    host, session = Host(), make_session()
    items = [
        host.item("repo:first.py"),
        host.item("repo:.envrc", flags=["host-executed file"]),
        host.item("repo:third.py"),
    ]
    scripted = ScriptedIO(["y", "a", "d"])
    assert review(session, items, scripted.io, save=Saves()) == "done"
    assert host.writes == [("repo:first.py", b"new\n")]
    assert session.decisions == {
        "repo:first.py": "applied",
        "repo:.envrc": "skipped",
        "repo:third.py": "skipped",
    }


def test_interrupt_in_editor_keeps_item_pending() -> None:
    host, session = Host(), make_session()

    class InterruptedEdit(ScriptedIO):
        def edit(self, content: bytes, suffix: str) -> bytes | None:
            raise KeyboardInterrupt

    scripted = InterruptedEdit(["y", "e"])
    assert review(session, [host.item("repo:x.py")], scripted.io, save=Saves()) == "deferred"
    assert host.writes == []
    assert session.decisions == {}


def test_skip_rest_of_group() -> None:
    host, session = Host(), make_session()
    items = [
        host.item("repo:first.py"),
        host.item("repo:second.py"),
        host.item("config:settings.json", group="config"),
    ]
    scripted = ScriptedIO(["y", "d", "y"])
    assert review(session, items, scripted.io, save=Saves()) == "done"
    assert host.writes == [("config:settings.json", b"new\n")]
    assert session.decisions == {
        "repo:first.py": "skipped",
        "repo:second.py": "skipped",
        "config:settings.json": "applied",
    }


def test_quit_keeps_the_rest_pending() -> None:
    host, session = Host(), make_session()
    items = [host.item("repo:first.py"), host.item("repo:second.py"), host.item("repo:third.py")]
    scripted = ScriptedIO(["y", "y", "q"])
    assert review(session, items, scripted.io, save=Saves()) == "deferred"
    assert host.writes == [("repo:first.py", b"new\n")]
    assert session.decisions == {"repo:first.py": "applied"}
    assert "2 items still pending." in scripted.text


def test_later_applies_nothing() -> None:
    host, session, saves = Host(), make_session(), Saves()
    scripted = ScriptedIO(["l"])
    assert review(session, [host.item("repo:x.py")], scripted.io, save=saves) == "deferred"
    assert host.writes == []
    assert session.decisions == {}
    assert saves.snapshots == []
    assert f"agenteval dev apply {SESSION_ID}" in scripted.prompts[0]


def test_discard_all_skips_everything() -> None:
    host, session, saves = Host(), make_session(), Saves()
    items = [host.item("repo:x.py"), host.item("config:settings.json", group="config")]
    scripted = ScriptedIO(["D"])
    assert review(session, items, scripted.io, save=saves) == "discarded"
    assert host.writes == []
    assert session.decisions == {"repo:x.py": "skipped", "config:settings.json": "skipped"}
    assert saves.snapshots == [session.decisions]
    assert "Discarded: skipped 2 pending items." in scripted.text


def test_deferred_review_shows_only_pending_items(tmp_path: Path) -> None:
    def items(host: Host) -> list[ReviewItem]:
        return [host.item("repo:first.py"), host.item("repo:second.py"), host.item("repo:third.py")]

    def save(session: DevSession) -> None:
        save_session(session, tmp_path)

    first_host = Host()
    first = ScriptedIO(["y", "y", "q"])
    assert review(make_session(), items(first_host), first.io, save=save) == "deferred"
    assert first_host.writes == [("repo:first.py", b"new\n")]

    session = DevSession.model_validate_json((tmp_path / SESSION_FILE).read_text())
    assert session.decisions == {"repo:first.py": "applied"}
    second_host = Host()
    second = ScriptedIO(["y", "y", "y"])
    assert review(session, items(second_host), second.io, save=save) == "done"
    assert "first.py" not in second.text
    assert "Changes to review (2)" in second.text
    assert second_host.writes == [("repo:second.py", b"new\n"), ("repo:third.py", b"new\n")]
    saved = DevSession.model_validate_json((tmp_path / SESSION_FILE).read_text())
    assert set(saved.decisions) == {"repo:first.py", "repo:second.py", "repo:third.py"}


def test_apply_error_is_reported_and_review_continues() -> None:
    host, session = Host(), make_session()
    items = [host.item("repo:broken.py", fail=True), host.item("repo:fine.py")]
    scripted = ScriptedIO(["y", "y", "y"])
    assert review(session, items, scripted.io, save=Saves()) == "deferred"
    assert "Could not apply broken.py: disk full" in scripted.text
    assert host.writes == [("repo:fine.py", b"new\n")]
    assert session.decisions == {"repo:fine.py": "applied"}
    assert "1 item still pending" in scripted.text


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), EOFError()])
def test_interrupt_behaves_like_quit(interrupt: BaseException) -> None:
    host, session, saves = Host(), make_session(), Saves()
    items = [host.item("repo:first.py"), host.item("repo:second.py")]
    scripted = ScriptedIO(["y", "y", interrupt])
    assert review(session, items, scripted.io, save=saves) == "deferred"
    assert host.writes == [("repo:first.py", b"new\n")]
    assert session.decisions == {"repo:first.py": "applied"}
    assert saves.snapshots[-1] == {"repo:first.py": "applied"}


def test_interrupt_at_start_defers() -> None:
    host, session = Host(), make_session()
    scripted = ScriptedIO([KeyboardInterrupt()])
    assert review(session, [host.item("repo:x.py")], scripted.io, save=Saves()) == "deferred"
    assert host.writes == []
    assert session.decisions == {}


def test_non_interactive_defers_without_asking() -> None:
    host, session = Host(), make_session()
    scripted = ScriptedIO(interactive=False)
    assert review(session, [host.item("repo:x.py")], scripted.io, save=Saves()) == "deferred"
    assert scripted.prompts == []
    assert host.writes == []
    assert "Changes to review (1)" in scripted.text
    assert "No terminal to review on: nothing applied." in scripted.text


def test_nothing_pending_is_done_without_asking() -> None:
    host, session = Host(), make_session()
    session.decisions["repo:x.py"] = "applied"
    scripted = ScriptedIO()
    assert review(session, [host.item("repo:x.py")], scripted.io, save=Saves()) == "done"
    assert scripted.prompts == []
    assert "Nothing to review." in scripted.text


def test_answers_are_case_insensitive_and_unknown_ones_reprompt() -> None:
    host = Host()
    scripted = ScriptedIO([" Y ", "x", "", "?", "Y"])
    assert review(make_session(), [host.item("repo:x.py")], scripted.io, save=Saves()) == "done"
    assert host.writes == [("repo:x.py", b"new\n")]
    assert "Unknown answer 'x'" in scripted.text
    assert "apply this change to the host" in scripted.text
    assert len(scripted.prompts) == 5


def test_unknown_start_answer_reprompts() -> None:
    scripted = ScriptedIO(["maybe", "l"])
    result = review(make_session(), [Host().item("repo:x.py")], scripted.io, save=Saves())
    assert result == "deferred"
    assert "Answer y, l or d." in scripted.text


def test_item_header_shows_flags_conflict_and_host_change() -> None:
    item = Host().item(
        "repo:setup.py",
        flags=["host-executed file", "URL: https://example.invalid"],
        conflict=True,
        host_changed=True,
        note="kept the host file; the env version is in conflicts/setup.py",
    )
    scripted = ScriptedIO(["y", "n"])
    review(make_session(), [item], scripted.io, save=Saves())
    text = scripted.text
    assert "[1/1] repo files: setup.py (modified)" in text
    assert "  [!] host-executed file" in text
    assert "  [!] URL: https://example.invalid" in text
    assert "changed on host since the session started (3-way merged)" in text
    assert "  CONFLICT" in text
    assert "kept the host file; the env version is in conflicts/setup.py" in text


# --- render_diff ---------------------------------------------------------------------


def test_render_diff_plain() -> None:
    item = Host().item("repo:x.txt", current=b"a\nb\n", proposed=b"a\nc\n")
    out = render_diff(item, color=False)
    lines = out.splitlines()
    assert lines[:2] == ["--- x.txt (host now)", "+++ x.txt (proposed)"]
    assert lines[2].startswith("@@")
    assert "-b" in lines and "+c" in lines and " a" in lines
    assert ESC not in out


def test_render_diff_color() -> None:
    item = Host().item("repo:x.txt", current=b"a\nb\n", proposed=b"a\nc\n")
    out = render_diff(item, color=True)
    assert f"{ESC}[32m+c{ESC}[0m" in out
    assert f"{ESC}[31m-b{ESC}[0m" in out
    assert f"{ESC}[36m@@" in out
    assert f"{ESC}[32m+++" not in out
    assert out.startswith(f"{ESC}[1m--- x.txt (host now){ESC}[0m\n{ESC}[1m+++ x.txt (proposed)")


def test_render_diff_color_content_lines_that_look_like_headers() -> None:
    item = Host().item("repo:x.sql", current=b"-- old\nkeep\n", proposed=b"++ new\nkeep\n")
    out = render_diff(item, color=True)
    assert f"{ESC}[31m--- old{ESC}[0m" in out
    assert f"{ESC}[32m+++ new{ESC}[0m" in out


def test_render_diff_added_and_deleted() -> None:
    added = render_diff(Host().item("repo:new.txt", current=None, proposed=b"x\n"), False)
    assert added.splitlines()[:2] == ["--- /dev/null", "+++ new.txt (proposed)"]
    assert "+x" in added.splitlines()
    deleted = render_diff(Host().item("repo:old.txt", current=b"y\n", proposed=None), False)
    assert deleted.splitlines()[:2] == ["--- old.txt (host now)", "+++ /dev/null"]
    assert "-y" in deleted.splitlines()


def test_render_diff_binary() -> None:
    host = Host()
    assert (
        render_diff(host.item("repo:b", binary=True, current=b"abc", proposed=b"abcde"), False)
        == "binary file, 3 -> 5 bytes"
    )
    added = host.item("repo:b", binary=True, current=None, proposed=b"abcde")
    assert render_diff(added, False) == "binary file, added (5 bytes)"
    deleted = host.item("repo:b", binary=True, current=b"abc", proposed=None)
    assert render_diff(deleted, False) == "binary file, deleted (3 bytes)"


def test_render_diff_ff_shows_descriptions() -> None:
    item = Host().item(
        "commits:main",
        group="commits",
        kind="ff",
        current=b"main at 1111111 (host)",
        proposed=b"fast-forward main to 2222222\n3 commits",
    )
    out = render_diff(item, color=True)
    assert "now:      main at 1111111 (host)" in out
    assert "proposed: fast-forward main to 2222222\n    3 commits" in out


def test_render_diff_no_newline_and_unchanged() -> None:
    out = render_diff(Host().item("repo:x", current=b"a\n", proposed=b"a"), False)
    assert "\\ No newline at end of file" in out
    same = Host().item("repo:x", kind="mode", current=b"a\n", proposed=b"a\n")
    assert render_diff(same, False) == "(no content change)"
    empty_added = Host().item("repo:x", kind="added", current=None, proposed=b"")
    assert render_diff(empty_added, False) == "(empty file added)"
    empty_deleted = Host().item("repo:x", kind="deleted", current=b"", proposed=None)
    assert render_diff(empty_deleted, False) == "(empty file deleted)"


def test_render_diff_caps_long_diffs() -> None:
    proposed = "".join(f"line {n}\n" for n in range(2500)).encode()
    out = render_diff(Host().item("repo:big.txt", current=None, proposed=proposed), False)
    lines = out.splitlines()
    assert len(lines) == MAX_DIFF_LINES + 1
    assert lines[-1] == f"... {2500 + 3 - MAX_DIFF_LINES} more lines"


def test_render_diff_makes_control_characters_visible() -> None:
    alm = chr(0x061C)
    proposed = f"ok\x1b[2Jhidden\rfake {RLO}txt.exe{alm}\n".encode()
    item = Host().item(f"repo:evil{ESC}[8m.txt", current=None, proposed=proposed)
    out = render_diff(item, color=False)
    assert ESC not in out and "\r" not in out and RLO not in out and alm not in out
    assert "\\x1b[2Jhidden\\x0dfake \\u202etxt.exe\\u061c" in out
    assert "+++ evil\\x1b[8m.txt (proposed)" in out


# --- summarize -----------------------------------------------------------------------


def test_summarize_counts_flags_notes_and_usage() -> None:
    host = Host()
    items = [
        host.item("commits:main", group="commits", kind="ff"),
        host.item("repo:a.py", kind="added", current=None),
        host.item("repo:b.py", kind="added", current=None),
        host.item("repo:c.py", conflict=True),
        host.item("repo:d.py", kind="deleted", proposed=None, host_changed=True),
        host.item("config:settings.json", group="config", flags=["settings key: hooks"]),
    ]
    notes = ["autoMemoryDirectory is set: memory was not synced", "two\nlines"]
    out = summarize(make_session(), items, notes, "12,345 tokens, $0.04")
    assert f"agenteval dev session {SESSION_ID}" in out
    assert "harness claude-code-bwrap, model mockllm/model, environment docker-ubuntu" in out
    assert "copy mode" in out and "repo /home/me/demo" in out
    assert "Changes to review (6)" in out
    assert "1 fast-forward" in out
    assert (
        "2 added, 1 modified, 1 deleted, 1 conflict, 1 changed on host since the session started"
        in out
    )
    assert "CLI config" in out
    assert "  CLI config: settings.json\n    [!] settings key: hooks" in out
    assert "attention pointers, not guarantees" in out
    assert "  - autoMemoryDirectory is set: memory was not synced" in out
    assert "  - two\n    lines" in out
    assert "usage: 12,345 tokens, $0.04" in out


def test_summarize_without_usage_flags_or_notes() -> None:
    out = summarize(make_session(), [Host().item("repo:a.py")], (), None)
    assert "usage" not in out
    assert "Flagged" not in out
    assert "Notes" not in out
    assert "repo files" in out and "1 modified" in out
    # No flags is not a clean bill of health.
    assert "No item is flagged. Flags are attention pointers, not guarantees" in out
    assert "flagged" not in summarize(make_session(), [], (), None)


# --- terminal_io ---------------------------------------------------------------------


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_terminal_io_uses_the_real_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout, stdin = FakeTTY(), FakeTTY("yes\n")
    monkeypatch.setattr(sys, "__stdout__", stdout)
    monkeypatch.setattr(sys, "__stdin__", stdin)
    monkeypatch.delenv("NO_COLOR", raising=False)
    swapped_stdin = sys.stdin
    term = terminal_io()
    assert term.color and term.interactive
    term.out("hello")
    assert term.ask("answer> ") == "yes"
    assert stdout.getvalue() == "hello\nanswer> "
    assert sys.stdin is swapped_stdin
    with pytest.raises(EOFError):
        term.ask("again> ")

    monkeypatch.setenv("NO_COLOR", "1")
    assert not terminal_io().color
    monkeypatch.setattr(sys, "__stdin__", io.StringIO())
    assert not terminal_io().interactive


def test_terminal_io_edit_runs_the_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = tmp_path / "where"
    editor = tmp_path / "editor.sh"
    editor.write_text(f'#!/bin/sh\necho "$1" > "{where}"\nprintf "edited\\n" >> "$1"\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    assert terminal_io().edit(b"orig\n", ".json") == b"orig\nedited\n"
    temp = Path(where.read_text().strip())
    assert temp.suffix == ".json"
    assert not temp.exists()


def test_terminal_io_edit_editor_with_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "sh -c 'printf x > \"$0\"'")
    assert terminal_io().edit(b"orig\n", ".md") == b"x"


def test_terminal_io_edit_failure_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = FakeTTY()
    monkeypatch.setattr(sys, "__stdout__", stdout)
    monkeypatch.setenv("VISUAL", "false")
    assert terminal_io().edit(b"orig\n", "") is None
    monkeypatch.setenv("VISUAL", str(tmp_path / "missing-editor"))
    assert terminal_io().edit(b"orig\n", "") is None
    assert "Editing with" in stdout.getvalue()


def test_terminal_io_edit_bad_editor_settings_do_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = FakeTTY()
    monkeypatch.setattr(sys, "__stdout__", stdout)
    # Unbalanced quotes: reported, nothing run.
    monkeypatch.setenv("VISUAL", "vim 'unbalanced")
    assert terminal_io().edit(b"orig\n", ".txt") is None
    assert "Could not parse the editor command" in stdout.getvalue()
    # A blank $VISUAL falls through to $EDITOR (never runs the temp file itself).
    monkeypatch.setenv("VISUAL", "   ")
    monkeypatch.setenv("EDITOR", "sh -c 'printf y > \"$0\"'")
    assert terminal_io().edit(b"orig\n", ".txt") == b"y"
    # An editor that removes the file cancels the edit instead of raising.
    monkeypatch.delenv("VISUAL")
    monkeypatch.setenv("EDITOR", "rm")
    assert terminal_io().edit(b"orig\n", ".txt") is None


def test_terminal_io_out_survives_a_non_utf8_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "__stdout__", io.TextIOWrapper(raw, encoding="latin-1"))
    terminal_io().out("café � →")
    assert raw.getvalue() == "café ? ?\n".encode("latin-1")
