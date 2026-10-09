from __future__ import annotations

from typing import Any

from minicode.cli_commands import SLASH_COMMANDS, find_matching_slash_commands
from minicode.tui.chrome import _cached_terminal_size
from minicode.tui.state import ScreenState, TtyAppArgs
from minicode.tui.chrome import get_permission_prompt_max_scroll_offset
from minicode.tui.transcript import (
    _build_transcript_layout,
    get_transcript_max_scroll_offset,
)


_HEADER_LINES_ESTIMATE = 11
_PROMPT_LINES_ESTIMATE = 7
_FOOTER_LINES = 1
_GAPS = 3
_TRANSCRIPT_FRAME_LINES = 4


def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    _, rows = _cached_terminal_size()
    rows = max(24, rows)
    chrome_overhead = (
        _HEADER_LINES_ESTIMATE
        + _PROMPT_LINES_ESTIMATE
        + _FOOTER_LINES
        + _GAPS
        + _TRANSCRIPT_FRAME_LINES
    )
    return max(6, rows - chrome_overhead)


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(
        state.transcript,
        _get_transcript_body_lines(args, state),
        state.transcript_revision,
    )


# ---------------------------------------------------------------------------
# Tail following
#
# The transcript offset is a *distance from the tail*, so "keep the reader
# where they are" is not something the offset can express on its own: appending
# lines leaves the number untouched while every already-visible line slides one
# row nearer the top.  Before this latch existed the only way to stay on the
# newest output was to force the offset back to 0 on every append — which is
# exactly why scrolling up to read history was impossible while the model was
# streaming: the next token yanked the viewport back to the bottom.
#
# The latch is the missing predicate.  All four behaviours the TUI needs
# (follow the stream, pause on manual scroll-up, resume at the bottom, resume
# on send) are derived from ``ScreenState.transcript_follow`` plus the
# before/after line measurements below, so no call site has to guess whether
# the reader is "at the bottom".
# ---------------------------------------------------------------------------


def _transcript_total_lines(state: ScreenState) -> int:
    """Total width-wrapped lines of the transcript as it would render now."""
    return _build_transcript_layout(state.transcript, state.transcript_revision).total_lines


def _is_transcript_at_bottom(state: ScreenState) -> bool:
    """True when the viewport shows the newest line of the transcript.

    The offset counts *backwards from the tail*, so the bottom is offset 0 —
    comparing against ``max_offset`` here would be exactly inverted.  One line
    of slack is allowed because render_transcript() shows the same newest window
    at both offset 0 (full content window) and offset 1 (one content row traded
    for the scroll indicator), so a reader who is visually sitting on the tail
    must not be reported as "paused in history" just for nudging the wheel once.
    """
    return state.transcript_scroll_offset <= 1


def _sync_transcript_follow(state: ScreenState) -> None:
    """Refresh the follow latch from the (already updated) scroll offset.

    Called after every user scrolling gesture so the latch always describes
    what the user just did, instead of the scroll position being re-derived by
    whoever appends the next line.
    """
    state.transcript_follow = _is_transcript_at_bottom(state)


def _resume_transcript_follow(state: ScreenState) -> None:
    """Jump to the tail and follow it again.

    Used by the entry points where the user has clearly asked to see what comes
    next — sending a prompt, clearing the view — even if they were reading
    history a moment ago.
    """
    state.transcript_scroll_offset = 0
    state.transcript_follow = True


def _apply_transcript_scroll_after_change(state: ScreenState, total_before: int) -> None:
    """Keep the viewport on the same content after the transcript changed size.

    ``scroll_offset`` counts backwards from the newest line, so a mutation that
    changes the total line count silently moves whatever the reader is looking
    at.  Compensating by the measured delta is what makes "scrolled up" a
    stable position rather than a slowly drifting one:

    * while following, the offset is pinned to 0 so the newest content is
      always what is on screen;
    * while not following, the offset is advanced by exactly the number of
      lines that were appended, leaving the visible rows unchanged;
    * shrinking content (a tool block collapsing under the reader) is not
      compensated, because the history above the window did not change — the
      renderer clamps an out-of-range offset on its own.
    """
    total_after = _transcript_total_lines(state)
    if total_after == total_before:
        return

    total_delta = total_after - total_before
    if total_delta <= 0:
        # Removed lines shift everything below the reader; the reader's own
        # position is unaffected, so the offset stays as it is.  It can exceed
        # the new maximum, which render_transcript() clamps harmlessly.
        return

    if state.transcript_follow:
        state.transcript_scroll_offset = 0
    else:
        state.transcript_scroll_offset += total_delta


def _scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    max_offset = _get_max_transcript_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        # The offset did not move, but the gesture still happened: reaching the
        # bottom by scrolling down must resume following even when the clamp
        # swallowed the delta (already at 0), otherwise the latch could stay
        # stuck on "paused" at the tail.
        _sync_transcript_follow(state)
        return False
    state.transcript_scroll_offset = next_offset
    _sync_transcript_follow(state)
    return True


def _jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    next_offset = _get_max_transcript_scroll_offset(args, state) if target == "top" else 0
    moved = next_offset != state.transcript_scroll_offset
    state.transcript_scroll_offset = next_offset
    _sync_transcript_follow(state)
    return moved


def _scroll_pending_approval_by(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or not pending.details_expanded:
        return False
    max_offset = get_permission_prompt_max_scroll_offset(pending.request, expanded=True)
    next_offset = max(0, min(max_offset, pending.details_scroll_offset + delta))
    if next_offset == pending.details_scroll_offset:
        return False
    pending.details_scroll_offset = next_offset
    return True


def _toggle_pending_approval_expand(state: ScreenState) -> bool:
    pending = state.pending_approval
    if not pending or pending.request.get("kind") != "edit":
        return False
    pending.details_expanded = not pending.details_expanded
    pending.details_scroll_offset = 0
    return True


def _move_pending_approval_selection(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or pending.feedback_mode:
        return False
    total = len(pending.request.get("choices", []))
    if total <= 0:
        return False
    pending.selected_choice_index = (pending.selected_choice_index + delta + total) % total
    return True


def _history_up(state: ScreenState) -> bool:
    if not state.history or state.history_index <= 0:
        return False
    if state.history_index == len(state.history):
        state.history_draft = state.input
    state.history_index -= 1
    state.input = state.history[state.history_index] if state.history_index < len(state.history) else ""
    state.cursor_offset = len(state.input)
    return True


def _history_down(state: ScreenState) -> bool:
    if state.history_index >= len(state.history):
        return False
    state.history_index += 1
    state.input = (
        state.history_draft
        if state.history_index == len(state.history)
        else (state.history[state.history_index] if state.history_index < len(state.history) else "")
    )
    state.cursor_offset = len(state.input)
    return True


def _get_visible_commands(input_text: str) -> list[Any]:
    if not input_text.startswith("/"):
        return []
    if input_text == "/":
        return SLASH_COMMANDS
    matches = find_matching_slash_commands(input_text)
    if not matches:
        return []
    # Registry usages keep their SlashCommand object (so the menu can show a
    # description); completions that are not registry entries -- the
    # "/skills doctor" family of subcommands -- pass through as plain text.
    # Filtering everything through SLASH_COMMANDS used to drop them silently,
    # which is why the subcommand dropdown appeared empty.
    by_usage = {getattr(cmd, "usage", str(cmd)): cmd for cmd in SLASH_COMMANDS}
    return [by_usage.get(usage, usage) for usage in matches]
