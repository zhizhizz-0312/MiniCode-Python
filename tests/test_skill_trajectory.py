"""Tests for trajectory collection (``minicode/skill_system/collector.py``).

The collector runs inside the agent loop's hot path, so the tests care as much
about its failure behaviour as its happy path: it must never raise, must never
write when disabled, and must label a turn honestly (a turn with tool errors is
not a "success" just because it finished).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from minicode.skill_system import collector as collector_module
from minicode.skill_system.collector import (
    DISABLE_ENV,
    MAX_CONTENT_CHARS,
    MAX_PROMPT_CHARS,
    TrajectoryCollector,
    TrajectorySink,
    format_summary,
    iter_trajectories,
    load_trajectories,
    trajectory_path,
)


def _event(kind: str, **kwargs) -> SimpleNamespace:
    """Stand-in for ``minicode.turn_events.TurnEvent`` (duck-typed on purpose)."""
    payload = {"kind": kind, "step": kwargs.pop("step", 1)}
    payload.update(kwargs)
    return SimpleNamespace(**payload)


def _collector(tmp_path: Path, session_id: str = "s1") -> TrajectoryCollector:
    return TrajectoryCollector(session_id, path=tmp_path, enabled=True)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_tool_events_are_recorded_with_outcome_flags(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("do the thing")

    collector.record_event(_event("tool_start", tool_name="read_file", step=2))
    collector.record_event(_event("tool_result", tool_name="read_file", output="ok", is_error=False, step=2))
    collector.record_event(_event("tool_start", tool_name="run_command", step=3))
    collector.record_event(_event("tool_result", tool_name="run_command", output="boom", is_error=True, step=3))

    turn = collector.current
    assert turn is not None
    assert turn.tool_calls == ["read_file", "run_command"]
    assert turn.tool_errors == 1
    assert [step.index for step in turn.steps] == [0, 1, 2, 3]


def test_tool_error_without_a_start_event_is_still_counted(tmp_path: Path) -> None:
    """``tool_calls`` counts starts, but an orphan result must still flag an error."""
    collector = _collector(tmp_path)
    collector.begin_turn("a")

    collector.record_event(_event("tool_result", tool_name="read_file", is_error=True))

    turn = collector.current
    assert turn is not None
    assert turn.tool_calls == []
    assert turn.tool_errors == 1


def test_stream_and_thinking_chunks_are_ignored(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("hi")

    assert collector.record_event(_event("assistant_stream", content="tok")) is None
    assert collector.record_event(_event("thinking", content="hmm")) is None
    assert collector.record_event(_event("totally_unknown")) is None

    turn = collector.current
    assert turn is not None
    assert turn.steps == []


def test_assistant_and_runtime_events_carry_content(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("hi")

    collector.record_event(_event("assistant", content="here is the answer"))
    runtime = SimpleNamespace(phase="verify", message="verification passed", stop_reason="")
    collector.record_event(_event("runtime", runtime_event=runtime))

    turn = collector.current
    assert turn is not None
    assert turn.steps[0].content == "here is the answer"
    assert turn.steps[1].content == "verification passed"
    assert turn.steps[1].phase == "verify"


def test_recording_without_begin_turn_opens_one_implicitly(tmp_path: Path) -> None:
    collector = _collector(tmp_path)

    collector.record_event(_event("tool_start", tool_name="read_file"))

    turn = collector.current
    assert turn is not None
    assert turn.turn == 1
    assert turn.tool_calls == ["read_file"]


def test_prompt_and_content_are_truncated(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("p" * 900)
    collector.record_event(_event("assistant", content="c" * 900))

    turn = collector.current
    assert turn is not None
    assert len(turn.prompt) == MAX_PROMPT_CHARS + 1  # clipped plus the ellipsis
    assert len(turn.steps[0].content) == MAX_CONTENT_CHARS + 1


# ---------------------------------------------------------------------------
# Finalising and outcome labelling
# ---------------------------------------------------------------------------


def test_explicit_success_and_failure_win_over_inference(tmp_path: Path) -> None:
    collector = _collector(tmp_path)

    collector.begin_turn("a")
    collector.record_event(_event("tool_result", tool_name="x", is_error=True))
    assert collector.finalize(success=True).outcome == "success"  # type: ignore[union-attr]

    collector.begin_turn("b")
    assert collector.finalize(success=False).outcome == "failure"  # type: ignore[union-attr]


def test_clean_turn_labels_success(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")
    collector.record_event(_event("assistant", content="done"))
    collector.record_event(_event("done", stop_reason="done"))

    turn = collector.finalize()

    assert turn is not None
    assert turn.outcome == "success"
    assert turn.stop_reason == "done"


def test_turn_with_tool_errors_is_not_a_success(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")
    collector.record_event(_event("tool_result", tool_name="x", is_error=True))
    collector.record_event(_event("done", stop_reason="done"))

    assert collector.finalize().outcome == "failure"  # type: ignore[union-attr]


def test_non_done_stop_reason_labels_failure(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")
    collector.record_event(_event("done", stop_reason="blocked"))

    turn = collector.finalize()

    assert turn is not None
    assert turn.outcome == "failure"
    assert turn.stop_reason == "blocked"


def test_turn_without_signals_stays_unknown(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")

    assert collector.finalize().outcome == "unknown"  # type: ignore[union-attr]


def test_finalize_without_a_turn_returns_none(tmp_path: Path) -> None:
    assert _collector(tmp_path).finalize() is None


def test_matched_skills_and_task_state_are_recorded(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("release the package")

    turn = collector.finalize(
        success=True, matched_skills=["release-flow"], task_state="completed"
    )

    assert turn is not None
    assert turn.matched_skills == ["release-flow"]
    assert turn.task_state == "completed"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_flush_appends_one_line_per_turn(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    for prompt in ("first", "second"):
        collector.begin_turn(prompt)
        collector.finalize(success=True)

    path = trajectory_path("s1", tmp_path)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(lines) == 2
    assert [json.loads(line)["prompt"] for line in lines] == ["first", "second"]


def test_turn_numbering_continues_across_restarts(tmp_path: Path) -> None:
    first = _collector(tmp_path)
    first.begin_turn("a")
    first.finalize(success=True)

    second = _collector(tmp_path)  # simulates a new process on the same session
    turn = second.begin_turn("b")

    assert turn.turn == 2


def test_round_trip_preserves_the_trajectory(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("hello trajectory")
    collector.record_event(_event("tool_start", tool_name="read_file", step=1))
    collector.record_event(_event("tool_result", tool_name="read_file", output="content", is_error=False, step=1))
    collector.record_event(_event("done", stop_reason="done"))
    original = collector.finalize(success=True, matched_skills=["code-review"])

    restored = load_trajectories("s1", path=tmp_path)
    assert len(restored) == 1
    assert original is not None
    assert restored[0].to_dict() == original.to_dict()


def test_disabled_collector_writes_nothing(tmp_path: Path) -> None:
    collector = TrajectoryCollector("s1", path=tmp_path, enabled=False)
    collector.begin_turn("a")
    collector.record_event(_event("tool_start", tool_name="read_file"))

    assert collector.current is not None
    assert collector.current.steps == []
    assert collector.finalize(success=True) is not None
    assert not trajectory_path("s1", tmp_path).exists()


def test_disable_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DISABLE_ENV, "1")

    collector = TrajectoryCollector("s1", path=tmp_path)
    collector.begin_turn("a")
    collector.finalize(success=True)

    assert collector.enabled is False
    assert not trajectory_path("s1", tmp_path).exists()


def test_unwritable_target_never_raises(tmp_path: Path) -> None:
    target = tmp_path / "blocked"
    target.mkdir()
    collector = TrajectoryCollector("s1", path=target / "file.txt")
    (target / "file.txt").mkdir()  # a directory where the JSONL file should be

    collector.begin_turn("a")
    collector.record_event(_event("tool_start", tool_name="read_file"))
    collector.finalize(success=True)  # must not raise


def test_corrupt_lines_are_skipped(tmp_path: Path) -> None:
    path = trajectory_path("s1", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schemaVersion": 1, "sessionId": "s1", "turn": 1}\nnot json\n\n', encoding="utf-8")

    turns = list(iter_trajectories("s1", path=tmp_path))

    assert len(turns) == 1
    assert turns[0].turn == 1


def test_reading_an_absent_store_is_empty(tmp_path: Path) -> None:
    assert load_trajectories("nobody", path=tmp_path) == []
    assert list(iter_trajectories(path=tmp_path)) == []


def test_load_across_sessions_finds_every_file(tmp_path: Path) -> None:
    for session_id in ("s1", "s2"):
        collector = TrajectoryCollector(session_id, path=tmp_path, enabled=True)
        collector.begin_turn(session_id)
        collector.finalize(success=True)

    turns = load_trajectories(path=tmp_path)

    assert sorted(turn.session_id for turn in turns) == ["s1", "s2"]


def test_limit_keeps_the_most_recent_turns(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    for index in range(3):
        collector.begin_turn(f"prompt {index}")
        collector.finalize(success=True)

    turns = load_trajectories("s1", path=tmp_path, limit=2)

    assert [turn.prompt for turn in turns] == ["prompt 1", "prompt 2"]


# ---------------------------------------------------------------------------
# Sink adapter and reporting
# ---------------------------------------------------------------------------


def test_sink_records_and_forwards_to_the_inner_sink(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")
    received: list[str] = []
    inner = SimpleNamespace(on_event=lambda event: received.append(event.kind))

    TrajectorySink(collector, inner).on_event(_event("tool_start", tool_name="read_file"))
    TrajectorySink(collector, None).on_event(_event("done", stop_reason="done"))

    assert received == ["tool_start"]
    turn = collector.current
    assert turn is not None
    assert [step.kind for step in turn.steps] == ["tool_start", "done"]


def test_sink_never_raises_when_the_inner_sink_breaks(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("a")

    def _boom(_event) -> None:
        raise RuntimeError("inner sink exploded")

    sink = TrajectorySink(collector, SimpleNamespace(on_event=_boom))

    with pytest.raises(RuntimeError):
        # The inner sink's own failure is the inner sink's problem: the collector
        # adapter must not hide it, but it must also not corrupt the trajectory.
        sink.on_event(_event("tool_start", tool_name="read_file"))

    turn = collector.current
    assert turn is not None
    assert turn.tool_calls == ["read_file"]


def test_format_summary_on_empty_store(tmp_path: Path) -> None:
    rendered = format_summary(path=tmp_path)

    assert "No trajectories recorded yet." in rendered
    assert DISABLE_ENV in rendered


def test_format_summary_reports_outcomes_and_tools(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.begin_turn("first")
    collector.record_event(_event("tool_start", tool_name="read_file"))
    collector.record_event(_event("done", stop_reason="done"))
    collector.finalize(matched_skills=["code-review"])
    collector.begin_turn("second")
    collector.record_event(_event("tool_result", tool_name="run_command", is_error=True))
    collector.finalize()

    rendered = format_summary("s1", path=tmp_path)

    assert "Turns recorded: 2" in rendered
    assert "1 success, 1 failure, 0 unknown" in rendered
    assert "Most used tools: read_file x1" in rendered
    assert "Skills seen in trajectories: code-review" in rendered
    assert "turn 1: [ok]" in rendered
    assert "turn 2: [FAILED]" in rendered


def test_module_exports_the_documented_names() -> None:
    for name in ("TrajectoryCollector", "TurnTrajectory", "load_trajectories", "format_summary"):
        assert hasattr(collector_module, name)


# ---------------------------------------------------------------------------
# Live wiring: the collector must actually run inside a TUI turn
# ---------------------------------------------------------------------------


def test_tui_agent_turn_records_a_trajectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive a real ``_handle_input`` turn and prove a trajectory reaches disk.

    The TUI publishes agent events into a ``TurnEventQueue`` that ``tty_app``'s
    frame loop drains, so this test emulates that drain loop -- without it the
    collector would look wired-up while never actually flushing anything.
    """
    import time

    from minicode.permissions import PermissionManager
    from minicode.tui import input_handler
    from minicode.tui.state import ScreenState, TtyAppArgs
    from minicode.turn_events import TurnEvent

    def _fake_bundle(*_args, **_kwargs):
        return SimpleNamespace(prompt="system prompt", product_snapshot={})

    def _fake_turn(**kwargs):
        queue = kwargs["callbacks"]
        queue.on_event(
            TurnEvent.tool_started(step=1, tool_name="read_file", tool_input={"path": "x.py"})
        )
        queue.on_event(
            TurnEvent.tool_finished(step=1, tool_name="read_file", output="contents", is_error=False)
        )
        queue.on_event(TurnEvent.completed(step=2, messages=[], stop_reason="done"))
        return []

    monkeypatch.setattr(input_handler, "build_system_prompt_bundle", _fake_bundle)
    monkeypatch.setattr(input_handler, "run_agent_turn", _fake_turn)

    args = TtyAppArgs(
        runtime={"model": "test-model"},
        tools=SimpleNamespace(get_skills=lambda: [], get_mcp_servers=lambda: [], list=lambda: []),
        model=SimpleNamespace(),
        messages=[{"role": "system", "content": ""}],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
    )
    state = ScreenState()
    state.session = SimpleNamespace(session_id="tui-session")  # type: ignore[assignment]
    monkeypatch.setattr(
        input_handler,
        "_active_trajectory",
        TrajectoryCollector("tui-session", path=tmp_path, enabled=True),
    )

    input_handler._handle_input(args, state, lambda: None, submitted_raw_input="read a file")

    deadline = time.time() + 5
    while time.time() < deadline:
        state.agent_event_queue.drain(state.agent_event_handler)
        if state.agent_result and state.agent_result.get("done"):
            break
        time.sleep(0.01)

    turns = load_trajectories("tui-session", path=tmp_path)
    assert len(turns) == 1
    assert turns[0].prompt == "read a file"
    assert turns[0].tool_calls == ["read_file"]
    assert turns[0].stop_reason == "done"
    assert turns[0].outcome == "success"
