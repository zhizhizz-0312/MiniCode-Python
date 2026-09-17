"""Regression tests for the slash-command surface.

Upstream shipped ``LocalCommandSurface.SLASH_COMMANDS`` entries -- and help text
promising them -- for commands that had **no dispatch site anywhere**: ``/cost``,
``/tasks``, ``/history``, ``/clear``, ``/retry``, ``/transcript-save`` and
``/debug`` all answered "Unknown command".  ``/transcript-save`` was implemented
for headless mode only, and ``tty_app._format_history`` /
``tty_app._save_transcript`` were written (and unit-tested) but never wired to a
command.

These tests pin the two halves of the fix:

1. ``test_every_registered_slash_command_has_a_dispatch_site`` statically proves
   the registry can never again promise a command nothing handles.
2. The behaviour tests drive the real ``_handle_input`` entry point (and the
   shared ``try_handle_local_command`` dispatch) for each newly implemented
   command.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from minicode.cli_commands import (
    SLASH_COMMANDS,
    format_prompt_history,
    format_task_report,
    try_handle_local_command,
)
from minicode.permissions import PermissionManager
from minicode.state import add_cost, create_app_store, update_context_usage
from minicode.tui import input_handler
from minicode.tui.state import ScreenState, TtyAppArgs
from minicode.tui.types import TranscriptEntry

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules that are allowed to dispatch a slash command.  Adding a command to
# the registry means wiring it in one of these files.
_DISPATCH_SOURCES = (
    "minicode/cli_commands.py",
    "minicode/local_tool_shortcuts.py",
    "minicode/tui/input_handler.py",
    "minicode/main.py",
    "minicode/tty_app.py",
)


# ---------------------------------------------------------------------------
# 1. Registry coverage (static, no side effects)
# ---------------------------------------------------------------------------


def _is_input_target(node: ast.AST) -> bool:
    """True for the "user just typed this" side of a comparison."""
    if isinstance(node, ast.Name):
        return "input" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return node.attr.lower() in {"input", "input_text"}
    return False


def _collect(node: ast.AST, found: set[str]) -> None:
    """Record slash tokens from a string constant (or a container of them)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        for token in node.value.split():
            if token.startswith("/"):
                found.add(token)
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        for element in node.elts:
            _collect(element, found)


def _dispatched_admin_command_names() -> set[str]:
    """Statically collect the command literals compared against user input.

    Only ``==``-style comparisons and ``.startswith(...)`` calls on an
    input-like target are inspected, so the help-text listing inside
    ``format_slash_commands`` cannot satisfy this scan -- that listing is
    exactly what used to lie about the available commands.
    """
    found: set[str] = set()
    for relative in _DISPATCH_SOURCES:
        tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(_is_input_target(operand) for operand in operands):
                    for operand in operands:
                        _collect(operand, found)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"startswith", "endswith"}
                and _is_input_target(node.func.value)
            ):
                for argument in node.args:
                    _collect(argument, found)
    return {token for token in found if token.startswith("/")}


def test_every_registered_slash_command_has_a_dispatch_site() -> None:
    registered = {command.usage.split()[0] for command in SLASH_COMMANDS}
    dispatched = _dispatched_admin_command_names()
    undispatched = sorted(registered - dispatched)

    assert not undispatched, (
        "these slash commands are advertised in SLASH_COMMANDS/help but nothing "
        f"dispatches them: {undispatched}"
    )


def test_registry_and_help_agree_on_the_same_command_set() -> None:
    """The help listing and the autocomplete registry must not drift apart.

    Matching is token-exact so ``/config`` is not considered "listed" merely
    because the help text mentions ``/config-paths``.
    """
    import re

    from minicode.cli_commands import format_slash_commands

    help_text = format_slash_commands()
    missing_from_help = sorted(
        command.name
        for command in SLASH_COMMANDS
        if not re.search(
            rf"(?<![\w/.-]){re.escape(command.name)}(?![\w./-])", help_text
        )
    )

    assert not missing_from_help, f"registered but absent from /help: {missing_from_help}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_args(cwd: Path) -> TtyAppArgs:
    return TtyAppArgs(
        runtime={"model": "deepseek-v4-flash"},
        tools=SimpleNamespace(
            get_skills=lambda: [],
            get_mcp_servers=lambda: [],
            list=lambda: [],
        ),
        model=SimpleNamespace(),
        messages=[{"role": "system", "content": ""}],
        cwd=str(cwd),
        permissions=PermissionManager(str(cwd)),
    )


def _state_with(*entries: TranscriptEntry) -> ScreenState:
    state = ScreenState()
    state.transcript.extend(entries)
    return state


def _submit(args: TtyAppArgs, state: ScreenState, raw: str) -> None:
    input_handler._handle_input(args, state, lambda: None, submitted_raw_input=raw)


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------


def test_clear_empties_the_transcript_view(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = _state_with(
        TranscriptEntry(id=1, kind="user", body="hello"),
        TranscriptEntry(id=2, kind="assistant", body="hi there"),
    )
    state.transcript_scroll_offset = 12
    revision_before = state.transcript_revision

    _submit(args, state, "/clear")

    # Only the confirmation notice is left in the view.
    assert len(state.transcript) == 1
    assert state.transcript[0].kind == "assistant"
    assert "Cleared 2 transcript entries" in state.transcript[0].body
    assert state.transcript_scroll_offset == 0
    assert state.transcript_revision > revision_before


def test_clear_says_conversation_context_is_untouched(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = _state_with(TranscriptEntry(id=1, kind="user", body="hello"))

    _submit(args, state, "/clear")

    assert "Conversation context is unchanged" in state.transcript[-1].body


def test_clear_with_empty_transcript_is_still_a_no_op_notice(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = ScreenState()

    _submit(args, state, "/clear")

    assert "Cleared 0 transcript entries" in state.transcript[-1].body


# ---------------------------------------------------------------------------
# /retry
# ---------------------------------------------------------------------------


def test_retry_without_history_explains_itself(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = ScreenState()

    _submit(args, state, "/retry")

    assert "No previous prompt to retry" in state.transcript[-1].body


def test_retry_resubmits_the_last_natural_language_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached_turn = threading.Event()
    captured: dict = {}

    def _fake_bundle(*_args, **_kwargs):
        return SimpleNamespace(prompt="system prompt", product_snapshot={})

    def _fake_turn(**kwargs):
        captured.update(kwargs)
        reached_turn.set()
        return []

    monkeypatch.setattr(input_handler, "build_system_prompt_bundle", _fake_bundle)
    monkeypatch.setattr(input_handler, "run_agent_turn", _fake_turn)

    args = _make_args(tmp_path)
    state = _state_with(
        TranscriptEntry(id=1, kind="user", body="explain the parser"),
        TranscriptEntry(id=2, kind="assistant", body="sure"),
    )

    _submit(args, state, "/retry")

    # The turn runs on a worker thread; wait so the stub is still installed.
    assert reached_turn.wait(5), "retry never reached the agent turn"
    assert captured["messages"][-1] == {"role": "user", "content": "explain the parser"}
    assert any(
        entry.kind == "user" and entry.body == "explain the parser"
        for entry in state.transcript
    )


def test_retry_skips_slash_commands_and_picks_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached_turn = threading.Event()
    captured: dict = {}

    def _fake_bundle(*_args, **_kwargs):
        return SimpleNamespace(prompt="system prompt", product_snapshot={})

    def _fake_turn(**kwargs):
        captured.update(kwargs)
        reached_turn.set()
        return []

    monkeypatch.setattr(input_handler, "build_system_prompt_bundle", _fake_bundle)
    monkeypatch.setattr(input_handler, "run_agent_turn", _fake_turn)

    args = _make_args(tmp_path)
    state = _state_with(
        TranscriptEntry(id=1, kind="user", body="first real prompt"),
        TranscriptEntry(id=2, kind="assistant", body="ok"),
        TranscriptEntry(id=3, kind="user", body="/help"),
    )

    _submit(args, state, "/retry")

    assert reached_turn.wait(5)
    assert captured["messages"][-1] == {"role": "user", "content": "first real prompt"}


def test_last_natural_language_prompt_ignores_tools_and_assistant_entries() -> None:
    state = _state_with(
        TranscriptEntry(id=1, kind="user", body="the real prompt"),
        TranscriptEntry(id=2, kind="assistant", body="response"),
        TranscriptEntry(id=3, kind="tool", body="tool output", toolName="read_file"),
        TranscriptEntry(id=4, kind="user", body="/status"),
    )

    assert input_handler._last_natural_language_prompt(state) == "the real prompt"


# ---------------------------------------------------------------------------
# /debug
# ---------------------------------------------------------------------------


def test_debug_reports_scroll_and_terminal_diagnostics(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = _state_with(TranscriptEntry(id=1, kind="user", body="hello"))
    state.transcript_scroll_offset = 7
    state.history = ["one", "two"]

    _submit(args, state, "/debug")

    body = state.transcript[-1].body
    assert "TUI diagnostics" in body
    assert "Scroll offset: 7" in body
    assert "cols x" in body
    # "/debug" itself is pushed onto the history buffer before dispatch.
    assert "History entries: 3" in body
    assert "deepseek-v4-flash" in body


# ---------------------------------------------------------------------------
# /transcript-save
# ---------------------------------------------------------------------------


def test_transcript_save_requires_a_path(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = ScreenState()

    _submit(args, state, "/transcript-save")

    assert "Usage: /transcript-save <path>" in state.transcript[-1].body


def test_transcript_save_passes_the_path_to_the_shared_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict = {}

    def _fake_save(state_obj, cwd, permissions, output_path):
        recorded["path"] = output_path
        recorded["cwd"] = cwd
        return str(tmp_path / "saved.txt")

    monkeypatch.setattr(input_handler, "_save_transcript", _fake_save)

    args = _make_args(tmp_path)
    state = ScreenState()

    _submit(args, state, "/transcript-save saved.txt")

    assert recorded["path"] == "saved.txt"
    assert recorded["cwd"] == str(tmp_path)
    assert "Saved transcript to" in state.transcript[-1].body


def test_transcript_save_surfaces_helper_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args, **_kwargs):
        raise PermissionError("write denied by policy")

    monkeypatch.setattr(input_handler, "_save_transcript", _boom)

    args = _make_args(tmp_path)
    state = ScreenState()

    _submit(args, state, "/transcript-save saved.txt")

    assert "Failed to save transcript" in state.transcript[-1].body
    assert "write denied by policy" in state.transcript[-1].body


def test_transcript_save_writes_the_real_transcript(tmp_path: Path) -> None:
    args = _make_args(tmp_path)
    state = _state_with(
        TranscriptEntry(id=1, kind="user", body="a distinctive prompt"),
        TranscriptEntry(id=2, kind="assistant", body="a distinctive answer"),
    )

    _submit(args, state, "/transcript-save transcript.txt")

    saved = tmp_path / "transcript.txt"
    assert saved.exists()
    rendered = saved.read_text(encoding="utf-8")
    assert "a distinctive prompt" in rendered
    assert "a distinctive answer" in rendered
    assert str(saved) in state.transcript[-1].body


# ---------------------------------------------------------------------------
# /cost (shared dispatch, both surfaces)
# ---------------------------------------------------------------------------


def _store_with_usage(model: str = "deepseek-v4-flash"):
    store = create_app_store({"session_id": "abcd1234-session", "model": model})
    store.set_state(add_cost(0.0123))
    store.set_state(update_context_usage(2048))
    return store


def test_cost_reports_live_store_counters() -> None:
    store = _store_with_usage()

    report = try_handle_local_command("/cost", app_state=store)

    assert report is not None
    assert "Cost & Usage Report" in report
    assert "$0.0123" in report
    assert "API calls: 1" in report
    assert "2,048 / 128,000" in report
    assert "abcd1234" in report


def test_cost_uses_the_registered_price_for_the_active_model() -> None:
    report = try_handle_local_command("/cost", app_state=_store_with_usage())

    assert "deepseek-v4-flash: $0.03 in / $0.12 out" in report


def test_cost_warns_when_the_model_has_no_price_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "minicode.cli_commands.load_runtime_config",
        lambda: {"model": "mystery-model-9000"},
    )

    report = try_handle_local_command(
        "/cost", app_state=_store_with_usage("mystery-model-9000")
    )

    assert "no explicit entry in MODEL_PRICING" in report
    assert "mystery-model-9000" in report


def test_cost_names_the_session_model_when_it_differs_from_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "minicode.cli_commands.load_runtime_config",
        lambda: {"model": "deepseek-v4-flash"},
    )

    report = try_handle_local_command(
        "/cost", app_state=_store_with_usage("deepseek-v4-pro")
    )

    assert "Model: deepseek-v4-flash" in report
    assert "Session started with: deepseek-v4-pro" in report


def test_cost_detailed_lists_every_registered_price() -> None:
    report = try_handle_local_command(
        "/cost --detailed", app_state=_store_with_usage()
    )

    assert "Registered model prices:" in report
    assert "deepseek-v4-pro" in report
    assert "default" in report


def test_cost_rejects_an_unknown_flag() -> None:
    report = try_handle_local_command("/cost --bogus", app_state=_store_with_usage())

    assert report == "Usage: /cost [--detailed]"


def test_cost_falls_back_to_the_global_store_when_none_is_passed() -> None:
    from minicode.state import set_global_store

    set_global_store(_store_with_usage())

    report = try_handle_local_command("/cost")

    assert report is not None
    assert "$0.0123" in report


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------


def test_history_numbers_recent_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "minicode.cli_commands.load_history_entries",
        lambda: ["first", "second", "third"],
    )

    report = try_handle_local_command("/history")

    assert "1. first" in report
    assert "3. third" in report
    assert "last 3 of 3" in report


def test_history_limits_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "minicode.cli_commands.load_history_entries",
        lambda: [f"prompt {index}" for index in range(30)],
    )

    report = try_handle_local_command("/history")

    assert "prompt 29" in report
    assert "prompt 9" not in report
    assert "last 20 of 30" in report


def test_format_prompt_history_handles_an_empty_store() -> None:
    assert "No prompt history yet" in format_prompt_history([])


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------


def _isolate_task_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("minicode.background_tasks.list_background_tasks", lambda: [])
    monkeypatch.setattr(
        "minicode.background_tasks.get_slot_stats",
        lambda: {"used_slots": 0, "max_slots": 5, "available_slots": 5, "total_tracked": 0},
    )
    monkeypatch.setattr("minicode.task_graph.list_task_graphs", lambda: [])


def test_tasks_reports_an_empty_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_task_surfaces(monkeypatch)

    report = try_handle_local_command("/tasks")

    assert report is not None
    assert "Task List" in report
    assert "No tasks are tracked right now" in report
    assert "Task graphs: 0 saved" in report
    assert "slots 0/5 used" in report


def test_tasks_lists_background_shell_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_task_surfaces(monkeypatch)
    monkeypatch.setattr(
        "minicode.background_tasks.list_background_tasks",
        lambda: [
            {"taskId": "task-1", "command": "pytest -q", "status": "running"},
        ],
    )
    monkeypatch.setattr(
        "minicode.background_tasks.get_slot_stats",
        lambda: {"used_slots": 1, "max_slots": 5, "available_slots": 4, "total_tracked": 1},
    )

    report = try_handle_local_command("/tasks")

    assert "[task-1] pytest -q [running]" in report
    assert "slots 1/5 used" in report


def test_task_format_reports_delegated_tasks_and_graphs() -> None:
    report = format_task_report(
        delegated_tasks=[{"label": "audit module", "status": "running"}],
        delegation_status={"running_tasks": 1, "available_slots": 2, "max_slots": 3},
        task_graphs=[
            {"id": "graph-a", "progress": 50.0, "complete": False, "tasks": 4, "completed": 2}
        ],
    )

    assert "Delegated agents: 1 tracked" in report
    assert "- audit module [running]" in report
    assert "/delegation reports capacity in detail" not in report
    assert "graph-a [in progress] 50% (2/4 tasks)" in report


def test_task_format_marks_completed_graphs() -> None:
    report = format_task_report(
        task_graphs=[
            {"id": "graph-b", "progress": 100.0, "complete": True, "tasks": 2, "completed": 2}
        ]
    )

    assert "graph-b [complete] 100% (2/2 tasks)" in report
