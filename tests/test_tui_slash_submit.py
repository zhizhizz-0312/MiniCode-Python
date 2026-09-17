"""Regression tests: slash commands typed in the TUI must submit on Enter.

Upstream bug (fixed 2026-09-15): ``_handle_normal_mode_return`` treated every
Enter press as "accept the highlighted autocomplete suggestion" whenever the
input started with "/" and matched any command, so fully-typed commands
(/sessions, /status, /help, ...) could never be submitted from the TUI.
Only /exit appeared to work, because tty_app exits as soon as the input
buffer equals "/exit" -- a separate check that bypasses the submit path.

These tests drive the real parser and event reducer (parse_input_chunk ->
_handle_event), mirroring the tty_app event loop, with a recording stand-in
for the submit callback (``_handle_input``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from minicode.cli_commands import SLASH_COMMANDS, find_matching_slash_commands
from minicode.tui.event_flow import _handle_event
from minicode.tui.input_parser import parse_input_chunk
from minicode.tui.navigation import _get_visible_commands
from minicode.tui.state import ScreenState

_USAGES = {command.usage for command in SLASH_COMMANDS}


class _SubmitRecorder:
    """Records submissions and emulates _handle_input's exit signal."""

    def __init__(self, exit_on: set[str] | None = None) -> None:
        self.submitted: list[str] = []
        self._exit_on = set(exit_on or ())

    def __call__(self, args, state, rerender, submitted_raw_input):
        self.submitted.append(submitted_raw_input)
        return submitted_raw_input.strip() in self._exit_on


def _drive(keystrokes: str, recorder, state=None):
    """Feed one character per event through the real TUI pipeline."""
    state = state if state is not None else ScreenState()
    args = SimpleNamespace()
    remainder = ""
    for chunk in keystrokes:
        parsed = parse_input_chunk(remainder + chunk, incoming_chunk=chunk)
        remainder = parsed.rest
        for event in parsed.events:
            _handle_event(args, state, event, lambda: None, None, None, recorder)
    return state


def test_exact_slash_command_submits_on_enter() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/sessions\r", recorder)

    assert recorder.submitted == ["/sessions"]
    assert state.input == ""


@pytest.mark.parametrize(
    "command",
    ["/session", "/memory", "/readiness", "/checkpoints", "/help", "/status", "/model"],
)
def test_commands_listed_in_help_all_submit(command: str) -> None:
    recorder = _SubmitRecorder()

    state = _drive(command + "\r", recorder)

    assert recorder.submitted == [command]
    assert state.input == ""


def test_exit_command_reaches_submit_path() -> None:
    recorder = _SubmitRecorder(exit_on={"/exit"})

    with pytest.raises(SystemExit):
        _drive("/exit\r", recorder)

    assert recorder.submitted == ["/exit"]


def test_partial_slash_command_completes_then_submits() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/sess\r", recorder)

    # First Enter accepts the highlighted completion instead of submitting.
    assert recorder.submitted == []
    completed = state.input.strip()
    assert completed in _USAGES

    # Second Enter submits the completed command.
    _drive("\r", recorder, state=state)
    assert recorder.submitted == [completed]


def test_tab_completion_then_enter_submits() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/sess\t", recorder)
    completed = state.input.strip()
    assert completed in _USAGES

    _drive("\r", recorder, state=state)
    assert [entry.strip() for entry in recorder.submitted] == [completed]
    assert state.input == ""


def test_command_with_arguments_submits() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/model gpt-4o\r", recorder)

    assert recorder.submitted == ["/model gpt-4o"]
    assert state.input == ""


def test_unknown_slash_command_submits_for_error_reporting() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/zzz\r", recorder)

    assert recorder.submitted == ["/zzz"]
    assert state.input == ""


def test_plain_text_still_submits() -> None:
    recorder = _SubmitRecorder()

    state = _drive("你好\r", recorder)

    assert recorder.submitted == ["你好"]
    assert state.input == ""


def test_find_matching_slash_commands_returns_empty_without_match() -> None:
    assert find_matching_slash_commands("/zzz") == []
    matches = find_matching_slash_commands("/sess")
    assert matches and all(c.startswith("/sess") for c in matches)


def test_visible_commands_empty_for_unmatched_input() -> None:
    assert _get_visible_commands("/zzz") == []


# ---------------------------------------------------------------------------
# Argument-bearing commands (/skills <subcommand>)
# ---------------------------------------------------------------------------


def test_argument_bearing_command_submits_on_enter() -> None:
    """Reported by the user: "/skills doctor" did nothing when Enter was pressed.

    Subcommands are not exact usages in the slash-command registry, but the
    dropdown lists them, so Enter used to rewrite the input to itself and never
    submit.
    """
    recorder = _SubmitRecorder()

    state = _drive("/skills doctor\r", recorder)

    assert recorder.submitted == ["/skills doctor"]
    assert state.input == ""


def test_partial_subcommand_accepts_the_suggestion_then_submits() -> None:
    recorder = _SubmitRecorder()

    state = _drive("/skills d\r", recorder)
    # Partial input: the first Enter completes it rather than running it.
    assert recorder.submitted == []
    assert state.input == "/skills doctor"

    state = _drive("\r", recorder, state)
    assert recorder.submitted == ["/skills doctor"]
    assert state.input == ""


def test_subcommand_dropdown_lists_options() -> None:
    visible = _get_visible_commands("/skills ")

    assert "/skills doctor" in visible
    assert "/skills install-examples" in visible


def test_argument_input_has_no_bogus_dropdown() -> None:
    """Previously "/rewind bogus" fuzzy-matched long usages as a subsequence."""
    assert _get_visible_commands("/rewind bogus") == []
    assert _get_visible_commands("/model gpt-4o") == []
