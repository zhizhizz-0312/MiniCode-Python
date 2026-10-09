from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from minicode.tui.navigation import (
    _apply_transcript_scroll_after_change,
    _transcript_total_lines,
)
from minicode.tui.state import ScreenState
from minicode.tui.tool_helpers import _summarize_collapsed_tool_body
from minicode.tui.types import TranscriptEntry

if TYPE_CHECKING:
    from minicode.tui.state import TtyAppArgs


def _bump_transcript_revision(state: ScreenState) -> None:
    state.transcript_revision += 1


# ---------------------------------------------------------------------------
# Scroll preservation across transcript mutations
#
# Every helper below changes how many rendered lines the transcript has.  The
# scroll offset is measured from the tail, so without compensation the reader's
# position silently shifts backwards by exactly the number of lines that were
# added — that is the drift a reader sees as "the view will not stay where I put
# it".  These wrappers measure the rendered line count before and after a
# mutation and let navigation.py decide what that means for the viewport, so the
# decision lives in one place instead of being re-guessed by each call site.
#
# ``args`` is accepted and forwarded for signature compatibility with the call
# sites that have a TtyAppArgs; the scroll correction itself is pure state.
# ---------------------------------------------------------------------------


class _TranscriptLineBaseline:
    """Context manager that settles the scroll offset after a mutation.

    Measuring *before* the mutation matters: ``_transcript_total_lines`` reads
    the layout cache, and the mutation bumps ``transcript_revision``, which
    invalidates that cache.  Taking the baseline first is therefore a cache hit,
    while the "after" measurement is one the following render needs anyway.
    """

    __slots__ = ("_state", "_before")

    def __init__(self, state: ScreenState, args: TtyAppArgs | None = None) -> None:
        self._state = state
        self._before = 0

    def __enter__(self) -> "_TranscriptLineBaseline":
        self._before = _transcript_total_lines(self._state)
        return self

    def __exit__(self, *_exc: Any) -> None:
        _apply_transcript_scroll_after_change(self._state, self._before)


def _push_transcript_entry(
    state: ScreenState, args: TtyAppArgs | None = None, **kwargs: Any
) -> int:
    with _TranscriptLineBaseline(state, args):
        entry_id = state.next_entry_id
        state.next_entry_id += 1
        state.transcript.append(TranscriptEntry(id=entry_id, **kwargs))
        _bump_transcript_revision(state)
        return entry_id


def _update_transcript_entry(
    state: ScreenState, entry_id: int, args: TtyAppArgs | None = None, **kwargs: Any
) -> bool:
    with _TranscriptLineBaseline(state, args):
        for entry in state.transcript:
            if entry.id == entry_id:
                changed = False
                for key, value in kwargs.items():
                    if hasattr(entry, key) and getattr(entry, key) != value:
                        setattr(entry, key, value)
                        changed = True
                if changed:
                    _bump_transcript_revision(state)
                return changed
        return False


def _find_transcript_entry(
    state: ScreenState,
    entry_id: int,
    *,
    prefer_tail: bool = False,
) -> TranscriptEntry | None:
    entries = reversed(state.transcript) if prefer_tail else state.transcript
    for entry in entries:
        if entry.id == entry_id:
            return entry
    return None


def _append_to_transcript_entry(
    state: ScreenState,
    entry_id: int,
    extra_body: str,
    args: TtyAppArgs | None = None,
) -> bool:
    with _TranscriptLineBaseline(state, args):
        if not extra_body:
            return False
        entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
        if entry is None:
            return False
        entry.body += extra_body
        _bump_transcript_revision(state)
        return True


def _mark_running_tools_as_error(
    state: ScreenState, message: str, args: TtyAppArgs | None = None
) -> None:
    with _TranscriptLineBaseline(state, args):
        changed = False
        for entry in state.transcript:
            if entry.kind == "tool" and entry.status == "running":
                entry.status = "error"
                entry.body = message
                entry.collapsed = False
                entry.collapsedSummary = None
                entry.collapsePhase = None
                state.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
                changed = True
        if any(e.kind == "tool" and e.status == "error" for e in state.transcript):
            state.active_tool = None
        if changed:
            _bump_transcript_revision(state)


def _update_tool_entry(
    state: ScreenState,
    entry_id: int,
    status: str,
    body: str,
    args: TtyAppArgs | None = None,
) -> bool:
    with _TranscriptLineBaseline(state, args):
        entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
        if entry is None or entry.kind != "tool":
            return False

        changed = False
        updates = {
            "status": status,
            "body": body,
            "collapsed": False,
            "collapsedSummary": None,
            "collapsePhase": None,
        }
        for key, value in updates.items():
            if getattr(entry, key) != value:
                setattr(entry, key, value)
                changed = True
        if changed:
            _bump_transcript_revision(state)
        return changed


def _set_tool_entry_collapse_phase(
    state: ScreenState,
    entry_id: int,
    phase: int,
    args: TtyAppArgs | None = None,
) -> bool:
    with _TranscriptLineBaseline(state, args):
        entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
        if entry is None or entry.kind != "tool" or entry.status == "running":
            return False
        if entry.collapsePhase == phase:
            return False
        entry.collapsePhase = phase
        _bump_transcript_revision(state)
        return True


def _collapse_tool_entry(
    state: ScreenState,
    entry_id: int,
    summary: str,
    args: TtyAppArgs | None = None,
) -> bool:
    with _TranscriptLineBaseline(state, args):
        entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
        if entry is None or entry.kind != "tool" or entry.status == "running":
            return False

        changed = False
        updates = {
            "collapsePhase": None,
            "collapsed": True,
            "collapsedSummary": summary,
        }
        for key, value in updates.items():
            if getattr(entry, key) != value:
                setattr(entry, key, value)
                changed = True
        if changed:
            _bump_transcript_revision(state)
        return changed


def _get_running_tool_entries(state: ScreenState) -> list[TranscriptEntry]:
    return [e for e in state.transcript if e.kind == "tool" and e.status == "running"]


def _finalize_dangling_running_tools(
    state: ScreenState, args: TtyAppArgs | None = None
) -> None:
    running = _get_running_tool_entries(state)
    if running:
        error_message = (
            f"{running[0].body}\n\n"
            "ERROR: Tool did not report a final result before the turn ended. "
            "This usually means the command kept running in the background "
            "or the tool lifecycle got out of sync."
        )
        _mark_running_tools_as_error(state, error_message, args)
        state.status = f"Previous turn ended with {len(running)} unfinished tool call(s)."


def _schedule_tool_auto_collapse(
    state: ScreenState,
    entry_id: int,
    output: str,
    rerender: Callable[[], None],
    args: TtyAppArgs | None = None,
) -> None:
    summary = _summarize_collapsed_tool_body(output)

    def _do_collapse() -> None:
        time.sleep(0.25)
        # The auto-collapse runs on a worker thread and edits the transcript
        # after the turn event that scheduled it.  Routing it through the same
        # wrapper keeps the reader's position out of the blast radius if the
        # fold happens while they are reading history.
        _collapse_tool_entry(state, entry_id, summary, args)
        rerender()

    t = threading.Thread(target=_do_collapse, daemon=True)
    t.start()
