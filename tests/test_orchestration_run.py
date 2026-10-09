"""Tests for the scheduler, driven by a fake worker so they cost nothing and stay fast.

The interesting behaviour is scheduling and refusal, not model output: does a
dependency failure actually stop its dependents, does the run cap actually stop
dispatch, does an isolation failure really mean the subtask never ran.  A fake worker
makes those deterministic -- and it means the scheduler is verified *before* any real
spend happens, which is the order that matters.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from minicode.orchestration.budget import Budget
from minicode.orchestration.orchestrator import (
    Orchestrator,
    ready_subtask_ids,
    run_verification,
)
from minicode.orchestration.plan import plan_from_workflow
from minicode.orchestration.types import (
    OrchestrationPlan,
    OrchestrationStatus,
    SubtaskSpec,
    SubtaskStatus,
)
from minicode.orchestration.worker import (
    FORBIDDEN_TOOLS,
    AgentWorker,
    WorkerOutcome,
    allowed_tools,
    render_instruction,
)
from minicode.skill_system.composite import parse_workflow


# ---------------------------------------------------------------------------
# A worker that does exactly what a test tells it to
# ---------------------------------------------------------------------------


class FakeWorker:
    def __init__(
        self,
        outcomes: dict[str, SubtaskStatus] | None = None,
        *,
        delay: float = 0.0,
        writes: tuple[str, ...] = (),
        usage: tuple[int, float] = (0, 0.0),
    ) -> None:
        self.outcomes = outcomes or {}
        self.delay = delay
        self.writes = writes
        self.usage = usage
        self.calls: list[str] = []
        self.workdirs: dict[str, str] = {}
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append(spec.id)
            self.workdirs[spec.id] = workdir
        try:
            tracker.record_turn()
            tracker.record_tool_call()
            tracker.record_usage(tokens=self.usage[0], cost_usd=self.usage[1])
            if self.delay:
                time.sleep(self.delay)
            for name in self.writes:
                target = Path(workdir) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"{spec.id} was here\n", encoding="utf-8")
            status = self.outcomes.get(spec.id, SubtaskStatus.DONE)
            if status is SubtaskStatus.BUDGET_EXHAUSTED:
                return WorkerOutcome(
                    status=status,
                    summary=f"{spec.id} partial",
                    stop_reason="budget_exhausted",
                    turns=tracker.turns,
                )
            return WorkerOutcome(
                status=status,
                summary=f"{spec.id} summary",
                stop_reason="done" if status is SubtaskStatus.DONE else "failed",
                turns=tracker.turns,
                tool_calls=tracker.tool_calls,
                tokens=tracker.tokens,
            )
        finally:
            with self._lock:
                self._active -= 1


def _plan(payload: dict | None = None, **kwargs):  # noqa: ANN001, ANN202
    workflow, issues = parse_workflow(payload or {"steps": [{"id": "a", "skill": "s"}]}, name="t")
    assert workflow is not None, issues
    return plan_from_workflow(workflow, name="t", **kwargs)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=str(repo), capture_output=True, text=True, timeout=60)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, timeout=60)
    code = subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, timeout=60
    ).returncode
    if code != 0:
        pytest.skip("git is not usable here")
    return repo


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_ready_uses_dependencies_when_the_plan_has_no_workflow() -> None:
    plan = OrchestrationPlan(
        name="t",
        subtasks=(
            SubtaskSpec(id="a", skill="s"),
            SubtaskSpec(id="b", skill="s", depends_on=("a",)),
        ),
    )

    assert ready_subtask_ids(plan, set(), set(), not_yet_started={"a", "b"}) == ["a"]
    assert ready_subtask_ids(plan, {"a"}, set(), not_yet_started={"b"}) == ["b"]


def test_ready_follows_p1_condition_semantics() -> None:
    plan = _plan(
        {
            "steps": [
                {"id": "work", "skill": "s"},
                {"id": "rollback", "skill": "s", "when": "on_failure:work"},
                {"id": "report", "skill": "s", "when": "always"},
            ]
        }
    )

    after_success = ready_subtask_ids(plan, {"work"}, set(), not_yet_started={"rollback", "report"})
    after_failure = ready_subtask_ids(plan, set(), {"work"}, not_yet_started={"rollback", "report"})

    assert after_success == ["report"]
    assert after_failure == ["rollback", "report"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_a_dependency_chain_runs_in_order_and_completes(tmp_path: Path) -> None:
    plan = _plan(
        {
            "steps": [
                {"id": "a", "skill": "s"},
                {"id": "b", "skill": "s", "depends_on": ["a"]},
                {"id": "c", "skill": "s", "depends_on": ["b"]},
            ]
        }
    )
    worker = FakeWorker()

    result = Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    assert result.status is OrchestrationStatus.COMPLETE
    assert worker.calls == ["a", "b", "c"]
    assert [child.status for child in result.children] == [SubtaskStatus.DONE] * 3


def test_independent_subtasks_actually_overlap(tmp_path: Path) -> None:
    plan = _plan(
        {
            "steps": [
                {"id": "a", "skill": "s"},
                {"id": "b", "skill": "s"},
                {"id": "c", "skill": "s"},
            ]
        }
    )
    worker = FakeWorker(delay=0.05)

    Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    assert worker.max_active >= 2, "independent subtasks should run concurrently"


def test_concurrency_never_exceeds_the_plan_cap(tmp_path: Path) -> None:
    plan = _plan(
        {
            "steps": [
                {"id": name, "skill": "s"} for name in ("a", "b", "c", "d", "e")
            ]
        }
    )
    plan.max_concurrent = 2
    worker = FakeWorker(delay=0.05)

    Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    assert worker.max_active <= 2


# ---------------------------------------------------------------------------
# Failure, skipping and budgets
# ---------------------------------------------------------------------------


def test_a_failed_dependency_is_skipped_not_done(tmp_path: Path) -> None:
    plan = _plan(
        {
            "steps": [
                {"id": "a", "skill": "s"},
                {"id": "b", "skill": "s", "depends_on": ["a"]},
            ]
        }
    )
    worker = FakeWorker({"a": SubtaskStatus.FAILED})

    result = Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    by_id = {child.subtask_id: child for child in result.children}
    assert by_id["a"].status is SubtaskStatus.FAILED
    assert by_id["b"].status is SubtaskStatus.SKIPPED
    assert "b" not in worker.calls
    assert result.status is OrchestrationStatus.FAILED  # nothing succeeded
    assert any("skipped" in note for note in result.notes)


def test_a_partial_run_is_reported_as_partial(tmp_path: Path) -> None:
    plan = _plan(
        {"steps": [{"id": "a", "skill": "s"}, {"id": "b", "skill": "s"}]}
    )
    worker = FakeWorker({"b": SubtaskStatus.FAILED})

    result = Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    assert result.status is OrchestrationStatus.PARTIAL
    assert len(result.children) == 2  # no silent gap


def test_a_child_that_exhausts_its_budget_keeps_its_partial_work(tmp_path: Path) -> None:
    plan = _plan(
        {"steps": [{"id": "a", "skill": "s"}, {"id": "b", "skill": "s"}]}
    )
    plan.subtasks = tuple(
        SubtaskSpec(id=spec.id, skill=spec.skill, budget=Budget(max_turns=1))
        for spec in plan.subtasks
    )
    worker = FakeWorker({"a": SubtaskStatus.BUDGET_EXHAUSTED})

    result = Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    by_id = {child.subtask_id: child for child in result.children}
    assert by_id["a"].status is SubtaskStatus.BUDGET_EXHAUSTED
    assert by_id["a"].summary == "a partial"
    assert by_id["b"].status is SubtaskStatus.DONE  # the run continued


def test_the_run_cap_stops_dispatch_and_says_why(tmp_path: Path) -> None:
    plan = _plan(
        {"steps": [{"id": name, "skill": "s"} for name in ("a", "b", "c")]}
    )
    plan.max_children = 1
    worker = FakeWorker()

    result = Orchestrator(
        plan=plan, repo_root=tmp_path, worker=worker, orchestration_id="run1"
    ).run()

    assert len(worker.calls) == 1
    skipped = [child for child in result.children if child.status is SubtaskStatus.SKIPPED]
    assert len(skipped) == 2
    assert all("run budget exhausted" in child.summary for child in skipped)
    assert result.status is OrchestrationStatus.PARTIAL


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_writers_are_isolated_and_leave_a_patch_and_a_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    passes = f'"{sys.executable}" -c "pass"'
    plan = _plan(
        {"steps": [{"id": "write-a", "skill": "s"}]},
        overrides=_overrides({"write-a": {"writes": True, "verify": [passes]}}),
    )
    plan.max_concurrent = 3
    worker = FakeWorker(writes=("new.txt",))

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=worker,
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    child = result.children[0]
    assert child.status is SubtaskStatus.DONE
    assert child.branch.startswith("mc/orch-run1-")
    assert child.patch_path and Path(child.patch_path).exists()
    assert "new.txt" in child.changed_files
    # Nothing reached the main tree: applying the patch is a human decision.
    assert not (repo / "new.txt").exists()
    # The worktree is gone; the branch is kept as evidence.
    assert not Path(child.worktree).exists()
    assert any("branches were kept" in note for note in result.notes)


def test_isolation_failure_means_the_subtask_never_runs(tmp_path: Path) -> None:
    """The safety property: no silent fallback into the shared tree."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")  # makes the tree dirty
    plan = _plan(
        {"steps": [{"id": "write-a", "skill": "s"}]},
        overrides=_overrides({"write-a": {"writes": True}}),
    )
    plan.max_concurrent = 3
    worker = FakeWorker(writes=("leaked.txt",))

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=worker,
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    child = result.children[0]
    assert child.status is SubtaskStatus.FAILED
    assert "refusing to isolate" in child.error
    assert worker.calls == []  # never ran
    assert not (repo / "leaked.txt").exists()


def test_readers_are_not_copied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan({"steps": [{"id": "read-a", "skill": "s"}]}, overrides=_overrides({}))
    plan.max_concurrent = 3
    worker = FakeWorker()

    result = Orchestrator(
        plan=plan, repo_root=repo, worker=worker, orchestration_id="run1", base_dir=tmp_path / "wt"
    ).run()

    assert worker.workdirs["read-a"] == str(repo)
    assert result.children[0].branch == ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verification_runs_in_the_worktree_and_marks_the_result(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    passes = f'"{sys.executable}" -c "pass"'
    plan = _plan(
        {"steps": [{"id": "write-a", "skill": "s"}]},
        overrides=_overrides({"write-a": {"writes": True, "verify": [passes]}}),
    )
    plan.max_concurrent = 3
    worker = FakeWorker(writes=("new.txt",))

    result = Orchestrator(
        plan=plan, repo_root=repo, worker=worker, orchestration_id="run1", base_dir=tmp_path / "wt"
    ).run()

    assert result.children[0].verified is True
    assert result.unverified == ()


def test_a_failing_verification_turns_a_done_subtask_into_a_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fails = f'"{sys.executable}" -c "raise SystemExit(3)"'
    plan = _plan(
        {"steps": [{"id": "write-a", "skill": "s"}]},
        overrides=_overrides({"write-a": {"writes": True, "verify": [fails]}}),
    )
    plan.max_concurrent = 3
    worker = FakeWorker(writes=("new.txt",))

    result = Orchestrator(
        plan=plan, repo_root=repo, worker=worker, orchestration_id="run1", base_dir=tmp_path / "wt"
    ).run()

    child = result.children[0]
    assert child.status is SubtaskStatus.FAILED
    assert child.stop_reason == "verification_failed"
    assert "[exit 3]" in child.verify_output
    assert result.status is OrchestrationStatus.FAILED


def test_a_writer_without_verification_is_reported_unverified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan(
        {"steps": [{"id": "write-a", "skill": "s"}]},
        overrides=_overrides({"write-a": {"writes": True}}),
    )
    plan.max_concurrent = 3
    worker = FakeWorker(writes=("new.txt",))

    result = Orchestrator(
        plan=plan, repo_root=repo, worker=worker, orchestration_id="run1", base_dir=tmp_path / "wt"
    ).run()

    assert result.unverified == ("write-a",)


def test_run_verification_reports_the_transcript() -> None:
    passed, output = run_verification(["echo hi"], cwd=".")
    failed, failed_output = run_verification(["exit 1"], cwd=".")

    assert passed and "hi" in output
    assert not failed and "[exit 1]" in failed_output


# ---------------------------------------------------------------------------
# Worker policy
# ---------------------------------------------------------------------------


def test_a_child_cannot_be_given_a_spawning_tool() -> None:
    spec = SubtaskSpec(id="a", skill="s", tools_whitelist=("read_file", "spawn_agent"))

    with pytest.raises(ValueError) as error:
        allowed_tools(spec)

    assert "depth is fixed at one" in str(error.value)


def test_depth_limiting_covers_the_plausible_names() -> None:
    for name in sorted(FORBIDDEN_TOOLS):
        spec = SubtaskSpec(id="a", skill="s", tools_whitelist=(name,))
        with pytest.raises(ValueError):
            allowed_tools(spec)


def test_the_instruction_tells_the_child_it_cannot_delegate() -> None:
    spec = SubtaskSpec(id="a", skill="s", instruction="do the thing", writes=True)

    text = render_instruction(spec)

    assert "orchestration is not available to you" in text
    assert "not merged automatically" in text


def test_a_real_worker_reports_a_bad_context_instead_of_raising(tmp_path: Path) -> None:
    def _boom(_workdir: str):
        raise RuntimeError("no model configured")

    worker = AgentWorker(context_factory=_boom)
    from minicode.orchestration.budget import BudgetTracker

    outcome = worker.run(
        SubtaskSpec(id="a", skill="s"),
        workdir=str(tmp_path),
        tracker=BudgetTracker(Budget()),
        orchestration_id="run1",
    )

    assert outcome.status is SubtaskStatus.FAILED
    assert "no model configured" in outcome.error


def test_a_real_worker_stops_when_the_budget_is_already_spent(tmp_path: Path) -> None:
    from minicode.orchestration.budget import BudgetTracker
    from minicode.orchestration.worker import WorkerContext

    worker = AgentWorker(
        context_factory=lambda _w: WorkerContext(model=object(), tools=None),
        turn_runner=lambda **_kwargs: pytest.fail("the model must not be called"),
    )
    tracker = BudgetTracker(Budget(max_turns=0))

    outcome = worker.run(
        SubtaskSpec(id="a", skill="s"),
        workdir=str(tmp_path),
        tracker=tracker,
        orchestration_id="run1",
    )

    assert outcome.status is SubtaskStatus.BUDGET_EXHAUSTED
    assert outcome.stop_reason == "budget_exhausted"


def _overrides(subtasks: dict) -> object:
    from minicode.orchestration.plan import _Overrides

    return _Overrides(subtasks=subtasks)
