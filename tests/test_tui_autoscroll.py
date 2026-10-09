"""Regression tests for transcript auto-follow ("stick to the tail").

Reported behaviour: streaming output did not stay in view — the reader had to
scroll down to see what the model was emitting.

Root cause: ``ScreenState`` only tracked ``transcript_scroll_offset``, a
distance measured *backwards from the newest line*, and every streaming callback
hard-reset it to 0.  Appending a line therefore could not keep an existing
position: the offset has to be advanced by the number of new lines to stay on
the same content, so a reader parked in history was dragged backwards towards
older and older output, and the only alternative — the unconditional reset —
trampled the same reader instead.  What was missing was the predicate: nothing
in the state could answer "is the reader at the bottom?".

These tests pin the observable behaviour (what the rendered window contains or
does not contain) rather than the latch field, so a regression that reintroduces
either failure mode fails here.
"""

from __future__ import annotations

import threading
from re import search
from types import SimpleNamespace

import pytest

from minicode.permissions import PermissionManager
from minicode.tui import chrome
from minicode.tui import event_flow
from minicode.tui import navigation
from minicode.tui import transcript as transcript_module
from minicode.tui.chrome import strip_ansi
from minicode.tui.input_parser import KeyEvent, WheelEvent
from minicode.tui.state import ScreenState, TtyAppArgs
from minicode.tui.tool_lifecycle import (
    _append_to_transcript_entry,
    _collapse_tool_entry,
    _push_transcript_entry,
)
from minicode.tui.transcript import render_transcript

# A deliberately small terminal: the transcript viewport is the terminal height
# minus the surrounding chrome, so 80x24 is small enough that these tests really
# force the text to overflow (and therefore to actually scroll).
_COLUMNS = 80
_ROWS = 24


@pytest.fixture(autouse=True)
def _fixed_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the terminal size so viewport height is deterministic.

    ``_cached_terminal_size`` is imported *by value* into several modules, so
    both the chrome implementation and every module-level binding are replaced.
    """
    size = (_COLUMNS, _ROWS)
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: size)
    monkeypatch.setattr(transcript_module, "_cached_terminal_size", lambda: size)
    monkeypatch.setattr(transcript_module, "_transcript_panel_width", lambda: _COLUMNS)
    monkeypatch.setattr(navigation, "_cached_terminal_size", lambda: size)


def _make_args(tmp_path) -> TtyAppArgs:
    return TtyAppArgs(
        runtime={"model": "deepseek-v4-flash"},
        tools=SimpleNamespace(
            get_skills=lambda: [],
            get_mcp_servers=lambda: [],
            list=lambda: [],
        ),
        model=SimpleNamespace(),
        messages=[{"role": "system", "content": ""}],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
    )


def _visible_lines(args: TtyAppArgs, state: ScreenState) -> list[str]:
    """The plain text the user can actually see in the transcript panel.

    Styling, the one-column scrollbar and the scroll indicator are stripped so
    assertions describe content, not chrome.
    """
    rendered = render_transcript(
        state.transcript,
        state.transcript_scroll_offset,
        navigation._get_transcript_body_lines(args, state),
        state.transcript_revision,
    )
    lines = []
    for line in strip_ansi(rendered).splitlines():
        if "PgUp/PgDn" in line:  # the scroll indicator row
            continue
        text = line.rstrip("░▏▎▍▌▋▊▉█▲▼ ")
        if text.strip():
            lines.append(text)
    return lines


def _visible_text(args: TtyAppArgs, state: ScreenState) -> str:
    """Visible content with wrapping removed, for "is this token on screen?".

    Long text wraps mid-token, so a token can legitimately be on screen while
    ``"tok011"`` never appears in any single line.
    """
    return "".join("".join(_visible_lines(args, state)).split())


def _wheel(args: TtyAppArgs, state: ScreenState, direction: str) -> None:
    event_flow._handle_event(
        args,
        state,
        WheelEvent(direction=direction),
        lambda: None,
        threading.Event(),
        {},
        lambda *a, **k: False,
    )


def _key(args: TtyAppArgs, state: ScreenState, name: str, ctrl: bool = False) -> None:
    event_flow._handle_event(
        args,
        state,
        KeyEvent(name=name, ctrl=ctrl, meta=False),
        lambda: None,
        threading.Event(),
        {},
        lambda *a, **k: False,
    )


def _stream_many(
    args: TtyAppArgs, state: ScreenState, entry_id: int, tokens: range, label: str
) -> None:
    """Append tokens one at a time, exactly like the live stream callback."""
    for index in tokens:
        _append_to_transcript_entry(state, entry_id, f"{label}{index:03d} ", args)


def _state_with_overflowing_transcript(
    args: TtyAppArgs, tmp_path
) -> tuple[ScreenState, int]:
    """A state whose transcript is longer than the viewport, at the tail."""
    state = ScreenState()
    _push_transcript_entry(state, args, kind="user", body="hello")
    entry_id = _push_transcript_entry(state, args, kind="assistant", body="")
    _stream_many(args, state, entry_id, range(1, 401), "tok")
    assert navigation._get_max_transcript_scroll_offset(args, state) > 0, (
        "fixture must overflow the viewport or these tests would pass vacuously"
    )
    return state, entry_id


# ---------------------------------------------------------------------------
# 1. Streaming at the bottom keeps the newest content on screen
# ---------------------------------------------------------------------------


def test_streaming_while_at_bottom_keeps_newest_token_visible(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, entry_id = _state_with_overflowing_transcript(args, tmp_path)

    assert state.transcript_follow is True
    assert any("tok400" in line for line in _visible_lines(args, state))


def test_each_streamed_token_is_visible_without_user_scrolling(tmp_path) -> None:
    """Every single append must be visible — the reader never has to scroll."""
    args = _make_args(tmp_path)
    state = ScreenState()
    entry_id = _push_transcript_entry(state, args, kind="assistant", body="")

    missing: list[str] = []
    for index in range(1, 401):
        token = f"tok{index:03d}"
        _append_to_transcript_entry(state, entry_id, token + " ", args)
        if token not in _visible_text(args, state):
            missing.append(token)

    assert missing == [], f"tokens arrived below the fold: {missing[:5]}"


# ---------------------------------------------------------------------------
# 2. Scrolling up pauses following; the reader's window must not move
# ---------------------------------------------------------------------------


def test_wheel_up_then_streaming_leaves_the_visible_window_unchanged(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, entry_id = _state_with_overflowing_transcript(args, tmp_path)

    _wheel(args, state, "up")
    _wheel(args, state, "up")
    assert state.transcript_follow is False, "wheel up must pause following"

    frozen = _visible_lines(args, state)
    _stream_many(args, state, entry_id, range(401, 461), "tok")

    assert _visible_lines(args, state) == frozen, "history moved under the reader"
    assert not any("tok460" in line for line in _visible_lines(args, state)), (
        "a paused reader must not be yanked to the tail"
    )


def test_tool_entry_appended_while_paused_does_not_move_the_window(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")

    frozen = _visible_lines(args, state)
    _push_transcript_entry(
        state,
        args,
        kind="tool",
        toolName="read_file",
        status="running",
        body="action: read\n".join(f"line {i}" for i in range(40)),
    )

    assert _visible_lines(args, state) == frozen


def test_collapsing_a_tool_block_while_paused_does_not_re_arm_following(tmp_path) -> None:
    args = _make_args(tmp_path)
    state = ScreenState()
    _push_transcript_entry(state, args, kind="assistant", body="\n".join(
        f"keep{i:02d}" for i in range(10)
    ))
    entry_id = _push_transcript_entry(
        state,
        args,
        kind="tool",
        toolName="read_file",
        status="success",
        body="\n".join(f"big{i:03d}" for i in range(200)),
    )
    _wheel(args, state, "up")
    offset_before = state.transcript_scroll_offset
    assert state.transcript_follow is False

    _collapse_tool_entry(state, entry_id, "summary", args)

    assert state.transcript_follow is False, "a shrink must not silently re-follow"
    assert state.transcript_scroll_offset <= offset_before


# ---------------------------------------------------------------------------
# 3. Returning to the bottom resumes following
# ---------------------------------------------------------------------------


def test_end_key_resumes_following_at_the_tail(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, entry_id = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    _key(args, state, "end")

    assert state.transcript_follow is True
    assert state.transcript_scroll_offset == 0
    assert any("tok400" in line for line in _visible_lines(args, state))

    # ...and following really is live again.
    _append_to_transcript_entry(state, entry_id, "AFTEREND ", args)
    assert any("AFTEREND" in line for line in _visible_lines(args, state))


def test_pagedown_to_the_bottom_resumes_following(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    for _ in range(60):
        _key(args, state, "pagedown")

    assert state.transcript_scroll_offset == 0
    assert state.transcript_follow is True


def test_wheel_down_to_the_bottom_resumes_following(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    for _ in range(60):
        _wheel(args, state, "down")

    assert state.transcript_scroll_offset == 0
    assert state.transcript_follow is True


def test_ctrl_home_pauses_and_ctrl_end_resumes(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)

    _key(args, state, "home", ctrl=True)
    assert state.transcript_follow is False
    assert state.transcript_scroll_offset > 0

    _key(args, state, "end", ctrl=True)
    assert state.transcript_follow is True
    assert state.transcript_scroll_offset == 0


def test_plain_end_with_empty_prompt_returns_to_the_tail(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    _key(args, state, "end")

    assert state.transcript_scroll_offset == 0
    assert state.transcript_follow is True


def test_plain_end_with_text_keeps_editing_the_prompt(tmp_path) -> None:
    """End must stay a cursor move while the user is typing a prompt."""
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    state.input = "/help"
    state.cursor_offset = 0

    _key(args, state, "end")

    assert state.cursor_offset == len("/help")
    assert state.transcript_scroll_offset > 0, "the prompt key must not scroll"
    assert state.transcript_follow is False


# ---------------------------------------------------------------------------
# 4. Sending a new message resumes following
# ---------------------------------------------------------------------------


def test_submitting_a_prompt_resumes_following(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, entry_id = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    # What _handle_input does when the user presses Enter on a real prompt.
    navigation._resume_transcript_follow(state)

    assert state.transcript_follow is True
    assert state.transcript_scroll_offset == 0
    _append_to_transcript_entry(state, entry_id, "AFTERSEND ", args)
    assert any("AFTERSEND" in line for line in _visible_lines(args, state))


def test_new_user_turn_is_visible_immediately(tmp_path, monkeypatch) -> None:
    """A submitted prompt is pushed at the tail, not appended off-screen."""
    from minicode.tui import input_handler

    monkeypatch.setattr(input_handler, "save_history_entries", lambda history: None)
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    def _stop_before_model_call(*_a, **_k):
        # _handle_input reaches the agent turn by appending the user message and
        # resuming follow; fail loudly if a future refactor calls the model here.
        raise AssertionError("this test must not start an agent turn")

    monkeypatch.setattr(input_handler, "build_system_prompt_bundle", _stop_before_model_call)

    try:
        input_handler._handle_input(args, state, lambda: None, submitted_raw_input="a fresh question")
    except AssertionError:
        pass  # the turn itself is out of scope; the transcript state is what matters

    assert state.transcript_follow is True
    assert state.transcript_scroll_offset == 0
    assert any(
        "fresh question" in line
        for line in _visible_lines(args, state)
    )


def test_tool_shortcut_resumes_following(tmp_path) -> None:
    """/read and friends are typed requests: their output must be visible."""
    from minicode.tooling import ToolRegistry
    from minicode.tui import input_handler

    args = _make_args(tmp_path)
    args.tools = ToolRegistry([])  # the shortcut path only needs a registry object
    state, _ = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")
    assert state.transcript_follow is False

    input_handler._execute_tool_shortcut(
        args, state, "list_files", {}, lambda: None
    )

    assert state.transcript_follow is True
    assert any("list_files" in line for line in _visible_lines(args, state))


# ---------------------------------------------------------------------------
# 5. The latch itself: "at bottom" must be decidable in one place
# ---------------------------------------------------------------------------


def test_at_bottom_is_decided_by_one_predicate(tmp_path) -> None:
    args = _make_args(tmp_path)
    state, _ = _state_with_overflowing_transcript(args, tmp_path)

    assert navigation._is_transcript_at_bottom(state) is True

    # A single row of slack: offset 1 shows the same newest window (the content
    # row traded for the indicator), so it still counts as the bottom.
    state.transcript_scroll_offset = 1
    assert navigation._is_transcript_at_bottom(state) is True

    state.transcript_scroll_offset = 4
    assert navigation._is_transcript_at_bottom(state) is False


def test_scroll_indicator_reports_content_waiting_below(tmp_path) -> None:
    """The cheap half of "there is more to read": one line in the frame."""
    args = _make_args(tmp_path)
    state, entry_id = _state_with_overflowing_transcript(args, tmp_path)
    _wheel(args, state, "up")
    _wheel(args, state, "up")

    def _gap_count() -> int:
        rendered = strip_ansi(
            render_transcript(
                state.transcript,
                state.transcript_scroll_offset,
                navigation._get_transcript_body_lines(args, state),
                state.transcript_revision,
            )
        )
        match = search(r"(\d+) new lines? below", rendered)
        assert match is not None, "the indicator must say how much is waiting below"
        assert "End to follow" in rendered
        return int(match.group(1))

    paused_gap = _gap_count()
    assert paused_gap > 0

    _stream_many(args, state, entry_id, range(401, 421), "tok")

    # More lines arrived below a paused reader, and the counter says so.
    assert _gap_count() > paused_gap
