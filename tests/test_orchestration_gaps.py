"""Regression tests for the defects a real run against a dirty tree exposed.

Both of these were invisible to the offline suite, so they are pinned here with the
exact shape that produced them: a writer whose isolation is refused, with dependents
queued behind it, and a plan whose remaining nodes can never become ready.

The first one breaks a promise this stage makes explicitly -- "no silent gaps, every
subtask has a terminal status" -- and it broke it by dropping nodes from the result
entirely rather than by mislabelling them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minicode.orchestration.audit import OrchestrationAudit, load_entries, replay
from minicode.orchestration.orchestrator import Orchestrator
from minicode.orchestration.plan import _Overrides, plan_from_workflow
from minicode.orchestration.report import format_result
from minicode.orchestration.types import OrchestrationStatus, SubtaskStatus
from minicode.skill_system.composite import parse_workflow

#: The shape from the real run: one writer, then a chain behind it, then a branch that
#: depends on a node in the middle of that chain.
CHAINED = {
    "steps": [
        {"id": "diff", "skill": "s", "description": "collect"},
        {"id": "review", "skill": "s", "depends_on": ["diff"]},
        {"id": "secrets", "skill": "s", "depends_on": ["review"]},
        {"id": "report", "skill": "s", "depends_on": ["secrets"]},
        {"id": "merge-ready", "skill": "s", "depends_on": ["report"]},
    ]
}


class NeverCalledWorker:
    """Fails the test if it runs: the whole point is that nothing should start."""

    def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
        raise AssertionError(f"worker must not run for {spec.id}")


def _repo(tmp_path: Path, *, dirty: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=str(repo), capture_output=True, timeout=60)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, timeout=60)
    code = subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, timeout=60
    ).returncode
    if code != 0:
        pytest.skip("git is not usable here")
    if dirty:
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    return repo


def _plan():
    workflow, issues = parse_workflow(CHAINED, name="code-review")
    assert workflow is not None, issues
    overrides = _Overrides(
        subtasks={name: {"writes": True} for name in ("diff", "review", "secrets", "report", "merge-ready")}
    )
    plan = plan_from_workflow(workflow, name="code-review", overrides=overrides)
    plan.max_concurrent = 3
    return plan


def test_every_subtask_gets_a_terminal_status_even_when_the_chain_is_blocked(
    tmp_path: Path,
) -> None:
    """The silent gap: nodes beyond the first blocked ring used to vanish entirely."""
    repo = _repo(tmp_path, dirty=True)
    plan = _plan()

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    assert len(result.children) == len(plan.subtasks), "a subtask was dropped from the result"
    statuses = {child.subtask_id: child.status for child in result.children}
    assert statuses["diff"] is SubtaskStatus.FAILED
    for name in ("review", "secrets", "report", "merge-ready"):
        assert statuses[name] is SubtaskStatus.SKIPPED, f"{name} should be skipped, not missing"
    assert result.status is OrchestrationStatus.FAILED


def test_a_skipped_subtask_names_the_dependency_that_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, dirty=True)

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    by_id = {child.subtask_id: child for child in result.children}
    # `review` names `diff`; the transitive ones name the node in their own chain.
    assert "diff" in by_id["review"].summary
    assert "review" in by_id["secrets"].summary
    assert "condition not satisfied" not in by_id["review"].summary


def test_the_trail_covers_every_subtask_so_replay_stays_consistent(tmp_path: Path) -> None:
    """Refused and skipped subtasks used to write nothing, so replay called it incomplete."""
    repo = _repo(tmp_path, dirty=True)
    audit = OrchestrationAudit("run1", home=tmp_path)
    plan = _plan()

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
        audit=audit,
    ).run()

    entries = load_entries(audit.path)
    report = replay(
        entries,
        expected_cost_usd=result.cost_usd,
        expected_children=[child.subtask_id for child in result.children],
    )

    assert report.consistent, report.to_dict()
    assert set(report.per_child) >= {"diff", "review", "secrets", "report", "merge-ready"}
    # The parent's own terminal state is recognised now.
    assert report.per_child["main"]["terminal"] == "failed"


def test_the_rendering_shows_why_isolation_was_refused(tmp_path: Path) -> None:
    """`isolation refused` alone does not tell the reader which file to deal with."""
    repo = _repo(tmp_path, dirty=True)

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    rendered = format_result(result)

    assert "Failures:" in rendered
    assert "refusing to isolate" in rendered
    assert "a.txt" in rendered  # the dirty path is named
    assert "Skipped (not run -- not a success):" in rendered


def test_a_clean_tree_still_runs_the_chain(tmp_path: Path) -> None:
    """The guard must not become a blanket refusal: the same plan works when clean."""
    from minicode.orchestration.types import SubtaskStatus as S
    from minicode.orchestration.worker import WorkerOutcome

    class OkWorker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
            self.calls.append(spec.id)
            tracker.record_turn()
            return WorkerOutcome(status=S.DONE, summary=f"{spec.id} ok", stop_reason="done")

    repo = _repo(tmp_path, dirty=False)
    worker = OkWorker()

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=worker,
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    assert worker.calls == ["diff", "review", "secrets", "report", "merge-ready"]
    assert result.status is OrchestrationStatus.COMPLETE
    # Every subtask here declares `writes` with no verification command -- exactly the
    # situation the plan preview warns about -- so the run reports them all as
    # unverified rather than implying that finishing proved anything.
    assert set(result.unverified) == {"diff", "review", "secrets", "report", "merge-ready"}
