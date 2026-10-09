"""Tests for the audit trail, its replay and the renderings.

The claim this stage makes is "one trusted source, and it can be replayed".  A claim
like that is worth exactly as much as its cross-checks, so most of these tests are
about the trail *disagreeing* with itself: a child that started and never reported, a
total that does not match the sum of its parts.  The happy path is here too -- but the
checks are the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.orchestration.audit import (
    AuditEntry,
    OrchestrationAudit,
    audit_path,
    format_replay,
    format_timeline,
    format_tree,
    load_entries,
    orchestration_dir,
    replay,
)
from minicode.orchestration.orchestrator import Orchestrator
from minicode.orchestration.plan import plan_from_workflow
from minicode.orchestration.report import (
    format_plan_preview,
    format_result,
    format_status_line,
)
from minicode.orchestration.types import (
    ChildResult,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskStatus,
)
from minicode.skill_system.composite import parse_workflow


class FakeWorker:
    def __init__(self, statuses: dict[str, SubtaskStatus] | None = None, cost: float = 0.0) -> None:
        self.statuses = statuses or {}
        self.cost = cost

    def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
        from minicode.orchestration.worker import WorkerOutcome

        tracker.record_turn()
        tracker.record_usage(tokens=100, cost_usd=self.cost)
        status = self.statuses.get(spec.id, SubtaskStatus.DONE)
        return WorkerOutcome(
            status=status,
            summary=f"{spec.id} done",
            stop_reason="done" if status is SubtaskStatus.DONE else "failed",
            cost_usd=self.cost,
            tokens=100,
        )


def _plan(ids: tuple[str, ...] = ("a", "b")):
    workflow, issues = parse_workflow(
        {"steps": [{"id": name, "skill": "s"} for name in ids]}, name="t"
    )
    assert workflow is not None, issues
    return plan_from_workflow(workflow, name="t")


def _audit(tmp_path: Path, *, enabled: bool = True) -> OrchestrationAudit:
    return OrchestrationAudit("run1", home=tmp_path, enabled=enabled)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_entries_are_attributed_and_sequenced(tmp_path: Path) -> None:
    audit = _audit(tmp_path)

    first = audit.record("run_start")
    second = audit.record("child_start", subtask_id="a", child_id="a", tool="read_file")

    assert (first.seq, second.seq) == (1, 2)
    assert second.orchestration_id == "run1"
    assert second.subtask_id == "a"
    assert second.tool == "read_file"
    assert audit.seq == 2


def test_the_trail_is_append_only_jsonl_on_disk(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    audit.record("run_start")
    audit.record("child_start", subtask_id="a")

    path = audit_path("run1", home=tmp_path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert [line["event"] for line in lines] == ["run_start", "child_start"]
    assert lines[1]["subtaskId"] == "a"


def test_paths_follow_the_orchestration_id(tmp_path: Path) -> None:
    assert orchestration_dir("run1", home=tmp_path).name == "run1"
    assert audit_path("run1", home=tmp_path).name == "audit.jsonl"


def test_the_home_can_be_overridden_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINI_CODE_ORCHESTRATION_HOME", str(tmp_path / "elsewhere"))

    assert orchestration_dir("run1").parent == tmp_path / "elsewhere"


def test_a_disabled_trail_still_buffers_but_writes_nothing(tmp_path: Path) -> None:
    audit = _audit(tmp_path, enabled=False)

    audit.record("run_start")

    assert not audit_path("run1", home=tmp_path).exists()
    assert [entry.event for entry in audit.entries()] == ["run_start"]


def test_an_unreadable_trail_does_not_lose_the_run(tmp_path: Path) -> None:
    """A directory where the file should be: the entry stays in the buffer."""
    audit = _audit(tmp_path)
    audit.path.parent.mkdir(parents=True, exist_ok=True)
    audit.path.mkdir()

    audit.record("run_start")

    assert [entry.event for entry in audit.entries()] == ["run_start"]


def test_unreadable_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"seq": 1, "event": "run_start"}\nnot json at all\n{"seq": 2, "event": "run_end"}\n',
        encoding="utf-8",
    )

    entries = load_entries(path)

    assert [entry.seq for entry in entries] == [1, 2]


def test_terminal_events_map_from_the_child_status(tmp_path: Path) -> None:
    audit = _audit(tmp_path)

    audit.record_result(ChildResult(subtask_id="a", status=SubtaskStatus.DONE, cost_usd=0.01))
    audit.record_result(ChildResult(subtask_id="b", status=SubtaskStatus.FAILED))
    audit.record_result(ChildResult(subtask_id="c", status=SubtaskStatus.SKIPPED))

    events = [(entry.subtask_id, entry.event) for entry in audit.entries()]
    assert events == [("a", "child_end"), ("b", "child_failed"), ("c", "child_skipped")]
    assert audit.entries()[0].cost_usd == pytest.approx(0.01)


def test_a_security_decision_keeps_its_rule_id_and_reaches_p2s_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirrored: list[dict] = []
    import minicode.security_chain.audit as security_audit

    monkeypatch.setattr(
        security_audit, "record", lambda *a, **k: mirrored.append({"args": a, "kwargs": k})
    )
    from minicode.security_chain.types import Decision, RiskLevel, SecurityDecision, Stage

    decision = SecurityDecision(
        decision=Decision.DENY,
        risk=RiskLevel.HIGH,
        stage=Stage.L1_RULES,
        reason="exfiltration",
        rule_id="L1-EXFIL-ENDPOINT",
    )
    audit = _audit(tmp_path)

    entry = audit.record_security(decision, child_id="a", subtask_id="a", tool="run_command")

    assert entry.rule_id == "L1-EXFIL-ENDPOINT"
    assert entry.decision == "deny"
    assert mirrored and mirrored[0]["kwargs"]["kind"] == "orchestration"
    assert mirrored[0]["kwargs"]["session_id"] == "run1"


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _entries(*specs: tuple[str, str, str, float]) -> list[AuditEntry]:
    return [
        AuditEntry(seq=index + 1, event=event, orchestration_id="run1", subtask_id=who, cost_usd=cost)
        for index, (event, who, _decision, cost) in enumerate(specs)
    ]


def test_replay_totals_per_child_and_detects_a_gap() -> None:
    entries = _entries(
        ("child_start", "a", "", 0.0),
        ("child_end", "a", "done", 0.02),
        ("child_start", "b", "", 0.0),  # started, never reported
    )

    report = replay(entries, expected_children=["a", "b"])

    assert report.per_child["a"]["terminal"] == "done"
    assert report.per_child["a"]["costUsd"] == pytest.approx(0.02)
    assert report.gaps == ("b",)
    assert not report.consistent


def test_replay_flags_a_total_that_does_not_match() -> None:
    entries = _entries(("child_end", "a", "done", 0.02))

    report = replay(entries, expected_cost_usd=0.05)

    assert not report.consistent
    assert "does not match the run total" in report.mismatches[0]


def test_replay_is_consistent_when_the_numbers_agree() -> None:
    entries = _entries(
        ("child_start", "a", "", 0.0),
        ("child_end", "a", "done", 0.02),
        ("child_start", "b", "", 0.0),
        ("child_end", "b", "done", 0.03),
    )

    report = replay(entries, expected_cost_usd=0.05, expected_children=["a", "b"])

    assert report.consistent
    assert report.total_cost_usd == pytest.approx(0.05)
    assert report.to_dict()["consistent"] is True


def test_replay_reports_a_child_with_no_events_at_all() -> None:
    report = replay(_entries(("child_end", "a", "done", 0.0)), expected_children=["a", "zz"])

    assert any("no audit events for zz" in item for item in report.mismatches)


def test_the_renderings_say_what_matters() -> None:
    report = replay(
        _entries(("child_start", "a", "", 0.0), ("child_end", "a", "done", 0.02)),
        expected_cost_usd=0.9,
    )

    timeline = format_timeline(report)
    tree = format_tree(report)
    full = format_replay(report)

    assert "#1" in timeline and "child_start" in timeline
    assert "a " in tree and "done" in tree
    assert "MISMATCH" in full


# ---------------------------------------------------------------------------
# The integration that matters: parent and children, one trail
# ---------------------------------------------------------------------------


def test_a_run_writes_one_attributed_trail_that_replays_consistently(tmp_path: Path) -> None:
    plan = _plan(("a", "b", "c"))
    audit = OrchestrationAudit("run1", home=tmp_path)
    result = Orchestrator(
        plan=plan,
        repo_root=tmp_path,
        worker=FakeWorker(),
        orchestration_id="run1",
        audit=audit,
    ).run()

    entries = load_entries(audit.path)
    events = [entry.event for entry in entries]
    report = replay(entries, expected_cost_usd=result.cost_usd, expected_children=["a", "b", "c"])

    assert events[0] == "run_start"
    assert events[-1] == "run_end"
    assert events.count("child_start") == 3
    assert events.count("child_end") == 3
    # Every entry belongs to the same run, so attribution is a lookup.
    assert {entry.orchestration_id for entry in entries} == {"run1"}
    assert report.consistent, report.to_dict()


def test_a_failed_child_is_visible_in_the_trail(tmp_path: Path) -> None:
    plan = _plan(("a",))
    audit = OrchestrationAudit("run1", home=tmp_path)

    Orchestrator(
        plan=plan,
        repo_root=tmp_path,
        worker=FakeWorker({"a": SubtaskStatus.FAILED}),
        orchestration_id="run1",
        audit=audit,
    ).run()

    assert [entry.event for entry in load_entries(audit.path)] == [
        "run_start",
        "child_start",
        "child_failed",
        "run_end",
    ]


# ---------------------------------------------------------------------------
# Rendering an orchestration result
# ---------------------------------------------------------------------------


def _result(**kwargs) -> OrchestrationResult:  # noqa: ANN003
    defaults = dict(
        orchestration_id="run1",
        status=OrchestrationStatus.PARTIAL,
        started_at=0.0,
        finished_at=2.0,
        children=[
            ChildResult(subtask_id="a", status=SubtaskStatus.DONE, verified=True, cost_usd=0.01, tool_calls=3),
            ChildResult(subtask_id="b", status=SubtaskStatus.DONE, verified=False, cost_usd=0.02),
            ChildResult(subtask_id="c", status=SubtaskStatus.SKIPPED, summary="dependency failed"),
        ],
    )
    defaults.update(kwargs)
    return OrchestrationResult(**defaults)


def test_the_result_rendering_separates_done_from_proven(tmp_path: Path) -> None:
    rendered = format_result(_result())

    assert "Status : partial" in rendered
    assert "Unverified (finished, but nothing proved them):" in rendered
    assert "  - b" in rendered
    assert "Skipped (not run -- not a success):" in rendered
    assert "  - c: dependency failed" in rendered


def test_the_result_rendering_names_patches_as_awaiting_a_decision() -> None:
    result = _result(
        children=[
            ChildResult(
                subtask_id="a",
                status=SubtaskStatus.DONE,
                branch="mc/orch-run1-a",
                patch_path="/tmp/a.patch",
            )
        ]
    )

    rendered = format_result(result)

    assert "Awaiting your decision (nothing was merged):" in rendered
    assert "branch mc/orch-run1-a" in rendered
    assert "/tmp/a.patch" in rendered


def test_a_complete_run_with_unverified_work_says_so() -> None:
    result = _result(
        status=OrchestrationStatus.COMPLETE,
        children=[ChildResult(subtask_id="a", status=SubtaskStatus.DONE, verified=False)],
    )

    assert "some are unverified" in format_result(result)


def test_the_status_line_is_one_line(tmp_path: Path) -> None:
    line = format_status_line(_result())

    assert line.startswith("run1: partial (2/3 done")
    assert "\n" not in line


def test_the_plan_preview_shows_isolation_and_caps() -> None:
    plan = _plan(("a", "b"))
    plan.subtasks = tuple(
        type(plan.subtasks[0])(
            id=spec.id,
            skill=spec.skill,
            writes=True,
            budget=__import__("minicode.orchestration.budget", fromlist=["Budget"]).Budget(
                max_turns=5, max_cost_usd=0.05
            ),
        )
        for spec in plan.subtasks
    )

    rendered = format_plan_preview(plan)

    assert "concurrency" in rendered
    assert "cost cap" in rendered
    assert "writes" in rendered
    assert "worktree" in rendered  # writers with concurrency > 1 are isolated
