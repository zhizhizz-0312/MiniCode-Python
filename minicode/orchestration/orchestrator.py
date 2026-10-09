"""The scheduler: readiness, dispatch, isolation, verification and aggregation.

Four decisions shape it.

**Readiness is P1's.**  A subtask becomes runnable per ``composite.ready_steps``, which
already implements the condition DSL (``deps``, ``always``, ``on_failure:<step>``,
``on_success:<step>``).  A second implementation here would be a second place for the
semantics to drift, and a workflow whose rollback branch fires on success is the kind
of bug nobody notices until it matters.

**Isolation is decided before dispatch, and a failure to isolate stops the subtask.**
A writer that cannot get a worktree is *not* quietly run in the main tree; it is
reported as failed with git's reason.  Falling back would be the one failure mode that
makes this whole stage unsafe.

**Concurrency is bounded by the plan, and the run budget is consulted before every
start** -- including the cost cap, because ten children each inside their own budget
still add up.

**"Skipped" and "unverified" are first-class.**  A dependency failing does not make its
dependents succeed, and a subtask whose verification never ran is reported as
unverified rather than counted as proven.
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from minicode.orchestration.audit import START_EVENT, OrchestrationAudit
from minicode.orchestration.budget import BudgetTracker, OrchestrationBudget
from minicode.orchestration.types import (
    ChildResult,
    Isolation,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskSpec,
    SubtaskStatus,
)
from minicode.orchestration.worker import AgentWorker, Worker, WorkerOutcome
from minicode.orchestration.worktree import WorktreeError, WorktreeManager

__all__ = ["Orchestrator", "ready_subtask_ids", "run_verification"]

DEFAULT_VERIFY_TIMEOUT = 300


def ready_subtask_ids(
    plan: OrchestrationPlan,
    completed: set[str],
    failed: set[str],
    *,
    not_yet_started: set[str],
) -> list[str]:
    """Subtasks whose turn has come, in plan order.

    Uses P1's ``ready_steps`` when the plan carries its workflow.  The fallback for
    hand-built plans honours ``depends_on`` only and deliberately ignores the
    conditional branches -- it is honest about being a subset rather than guessing at
    ``on_failure`` semantics.
    """
    workflow = getattr(plan, "workflow", None)
    if workflow is not None:
        try:
            from minicode.skill_system.composite import ready_steps

            return [
                step.id
                for step in ready_steps(workflow, completed, failed)
                if step.id in not_yet_started
            ]
        except Exception:  # noqa: BLE001 - fall through to the minimal rule
            pass
    ready: list[str] = []
    for spec in plan.subtasks:
        if spec.id not in not_yet_started:
            continue
        if all(dependency in completed for dependency in spec.depends_on):
            ready.append(spec.id)
    return ready


def run_verification(
    commands: Sequence[str],
    *,
    cwd: str,
    timeout: int = DEFAULT_VERIFY_TIMEOUT,
) -> tuple[bool, str]:
    """Run a subtask's verification commands; ``(passed, output)``.

    Run where the work happened (the worktree for an isolated subtask), so "verified"
    means "verified in the tree that made the change" rather than in some other copy.
    """
    transcript: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            transcript.append(f"$ {command}\n[could not run: {error}]")
            return False, "\n".join(transcript)
        tail = (result.stdout or "")[-2000:] + (result.stderr or "")[-2000:]
        transcript.append(f"$ {command}\n[exit {result.returncode}]\n{tail.strip()}")
        if result.returncode != 0:
            return False, "\n".join(transcript)
    return True, "\n".join(transcript)


@dataclass
class _Child:
    """Bookkeeping for one dispatched subtask."""

    spec: SubtaskSpec
    tracker: BudgetTracker
    workdir: str
    session: Any = None
    future: Future | None = None
    started_at: float = 0.0


class Orchestrator:
    """Runs one plan to completion (or to an honest partial result)."""

    def __init__(
        self,
        *,
        plan: OrchestrationPlan,
        repo_root: str | Path,
        worker: Worker | None = None,
        orchestration_id: str = "",
        base_dir: str | Path | None = None,
        budget: OrchestrationBudget | None = None,
        audit: OrchestrationAudit | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self.repo_root = Path(repo_root).resolve()
        self.orchestration_id = orchestration_id or f"orch-{int(time.time())}"
        self.worker = worker or AgentWorker()
        self.max_concurrent = max(1, min(plan.max_concurrent, max(1, plan.max_children)))
        self.budget = budget or OrchestrationBudget(
            max_children=plan.max_children,
            max_concurrent=self.max_concurrent,
            max_cost_usd=plan.max_cost_usd,
        )
        # One trail for the parent and every child: attribution has to be a lookup,
        # not a reconstruction from several logs that might disagree.
        self.audit = audit if audit is not None else OrchestrationAudit(self.orchestration_id)
        self._base_dir = base_dir
        self._clock = clock
        self._worktrees: WorktreeManager | None = None
        self.notes: list[str] = []

    # -- isolation ---------------------------------------------------------

    def _worktree_manager(self) -> WorktreeManager:
        if self._worktrees is None:
            self._worktrees = WorktreeManager(
                self.repo_root,
                orchestration_id=self.orchestration_id,
                base_dir=self._base_dir
                or (Path.home() / ".mini-code" / "worktrees" / self.orchestration_id),
            )
        return self._worktrees

    def _prepare_workdir(self, spec: SubtaskSpec) -> tuple[str, Any, str]:
        isolation = _resolve(spec, self.max_concurrent)
        if isolation is not Isolation.WORKTREE:
            return str(self.repo_root), None, ""
        try:
            session = self._worktree_manager().prepare(spec.id)
        except WorktreeError as error:
            # Never fall back to the shared tree: that would be the unsafe failure.
            return "", None, str(error)
        return str(session.path), session, ""

    # -- one child ---------------------------------------------------------

    def _run_child(self, child: _Child) -> ChildResult:
        spec = child.spec
        outcome = self.worker.run(
            spec,
            workdir=child.workdir,
            tracker=child.tracker,
            orchestration_id=self.orchestration_id,
        )
        result = ChildResult(
            subtask_id=spec.id,
            status=outcome.status,
            summary=outcome.summary,
            evidence=tuple(outcome.evidence),
            stop_reason=outcome.stop_reason,
            turns=outcome.turns,
            tool_calls=outcome.tool_calls,
            tokens=outcome.tokens,
            cost_usd=outcome.cost_usd,
            error=outcome.error,
            started_at=child.started_at,
            finished_at=self._clock(),
        )
        if child.session is not None:
            result.worktree = str(child.session.path)
            result.branch = child.session.branch
        self._collect_changes(child, result)
        self._verify(spec, child, result)
        return result

    def _collect_changes(self, child: _Child, result: ChildResult) -> None:
        """Commit and export a writer's work; never apply it."""
        if child.session is None:
            return
        manager = self._worktree_manager()
        try:
            changed = manager.changes(child.session)
            if not changed:
                return
            manager.commit(child.session, f"orchestration {self.orchestration_id}: {child.spec.id}")
            patch = manager.export_patch(
                child.session,
                Path(self._base_dir or (Path.home() / ".mini-code" / "worktrees" / self.orchestration_id))
                / f"{child.spec.id}.patch",
            )
            result.changed_files = changed
            result.patch_path = str(patch)
        except WorktreeError as error:
            result.error = (result.error + f"; patch export failed: {error}").strip("; ")

    def _verify(self, spec: SubtaskSpec, child: _Child, result: ChildResult) -> None:
        if not spec.verify or result.status is not SubtaskStatus.DONE:
            return
        passed, output = run_verification(spec.verify, cwd=child.workdir)
        result.verified = passed
        result.verify_output = output
        if not passed and result.status is SubtaskStatus.DONE:
            result.status = SubtaskStatus.FAILED
            result.stop_reason = "verification_failed"

    # -- the run -----------------------------------------------------------

    def run(self) -> OrchestrationResult:
        started = self._clock()
        self.audit.record("run_start", detail=f"plan={self.plan.name} subtasks={len(self.plan.subtasks)}")
        results: dict[str, ChildResult] = {}
        completed: set[str] = set()
        failed: set[str] = set()
        not_started = {spec.id for spec in self.plan.subtasks}
        in_flight: dict[Future, _Child] = {}
        skip_reasons: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            while not_started or in_flight:
                self._dispatch(pool, in_flight, not_started, completed, failed, results, skip_reasons)
                if not in_flight:
                    break
                done, _pending = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    child = in_flight.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:  # noqa: BLE001 - a child must not kill the run
                        result = ChildResult(
                            subtask_id=child.spec.id,
                            status=SubtaskStatus.FAILED,
                            error=f"{type(error).__name__}: {error}",
                        )
                    results[child.spec.id] = result
                    self.budget.absorb(child.tracker)
                    # The trail carries the child's cost, so replay can add the run up
                    # from its parts and disagree loudly if it does not match.
                    self.audit.record_result(result)
                    if result.status is SubtaskStatus.DONE:
                        completed.add(child.spec.id)
                    else:
                        failed.add(child.spec.id)

        self._cleanup()
        result = self._aggregate(results, skip_reasons, started)
        self.audit.record(
            "run_end",
            decision=result.status.value,
            detail=f"{len(result.children)} subtask(s)",
            tokens=result.tokens,
            cost_usd=result.cost_usd,
        )
        return result

    def _dispatch(
        self,
        pool: ThreadPoolExecutor,
        in_flight: dict[Future, _Child],
        not_started: set[str],
        completed: set[str],
        failed: set[str],
        results: dict[str, ChildResult],
        skip_reasons: dict[str, str],
    ) -> None:
        while len(in_flight) < self.max_concurrent:
            ready = ready_subtask_ids(
                self.plan, completed, failed, not_yet_started=not_started
            )
            # A subtask whose dependencies are all terminal but which never became
            # ready will never run: say so instead of leaving a silent gap.
            blocked = self._blocked_forever(completed, failed, not_started)
            for spec_id in blocked:
                not_started.discard(spec_id)
                skip_reasons[spec_id] = "its condition was never satisfied"
                results[spec_id] = ChildResult(
                    subtask_id=spec_id, status=SubtaskStatus.SKIPPED,
                    summary="condition not satisfied",
                )
                failed.add(spec_id)
            if not ready:
                return

            for spec_id in ready:
                if len(in_flight) >= self.max_concurrent:
                    return
                spec = self.plan.get(spec_id)
                if spec is None:  # pragma: no cover - plan integrity
                    continue
                reason = self.budget.can_start_child()
                if reason is not None:
                    for pending_id in [spec_id, *sorted(not_started - {spec_id})]:
                        if pending_id in not_started:
                            not_started.discard(pending_id)
                            skip_reasons[pending_id] = f"run budget exhausted ({reason})"
                            results[pending_id] = ChildResult(
                                subtask_id=pending_id, status=SubtaskStatus.SKIPPED,
                                summary=f"run budget exhausted ({reason})",
                            )
                            failed.add(pending_id)
                    return

                workdir, session, isolation_error = self._prepare_workdir(spec)
                if isolation_error:
                    not_started.discard(spec_id)
                    failed.add(spec_id)
                    results[spec_id] = ChildResult(
                        subtask_id=spec_id,
                        status=SubtaskStatus.FAILED,
                        error=isolation_error,
                        summary="isolation refused",
                    )
                    continue

                tracker = self.budget.start_child()
                tracker.budget = spec.budget or self.budget.child_defaults
                self.audit.record(
                    START_EVENT,
                    subtask_id=spec.id,
                    child_id=spec.id,
                    tool=",".join(spec.tools_whitelist or ()),
                    detail=f"skill={spec.skill} isolation={'worktree' if session else 'shared'}",
                )
                child = _Child(
                    spec=spec,
                    tracker=tracker,
                    workdir=workdir,
                    session=session,
                    started_at=self._clock(),
                )
                not_started.discard(spec_id)
                in_flight[pool.submit(self._run_child, child)] = child

    def _blocked_forever(
        self, completed: set[str], failed: set[str], not_started: set[str]
    ) -> list[str]:
        """Subtasks that cannot run because a dependency already failed.

        Reported as skipped rather than left pending: "never ran" and "ran and failed"
        are different facts, and an orchestration that ends with a pending node is
        hiding one of them.
        """
        blocked: list[str] = []
        for spec_id in list(not_started):
            spec = self.plan.get(spec_id)
            if spec is None:
                continue
            if any(dependency in failed for dependency in spec.depends_on):
                blocked.append(spec_id)
        return blocked

    def _cleanup(self) -> None:
        if self._worktrees is None:
            return
        for subtask_id, gone, detail in self._worktrees.cleanup():
            note = f"worktree for {subtask_id} {'removed' if gone else 'NOT removed'}"
            if detail:
                note += f" ({detail})"
            self.notes.append(note)
        self.notes.append(
            "branches were kept: they are the evidence of what each agent changed"
        )

    def _aggregate(
        self,
        results: dict[str, ChildResult],
        skip_reasons: dict[str, str],
        started: float,
    ) -> OrchestrationResult:
        children = [results[spec.id] for spec in self.plan.subtasks if spec.id in results]
        done = [child for child in children if child.status is SubtaskStatus.DONE]
        if len(done) == len(self.plan.subtasks):
            status = OrchestrationStatus.COMPLETE
        elif done:
            status = OrchestrationStatus.PARTIAL
        else:
            status = OrchestrationStatus.FAILED

        notes = list(self.notes)
        for spec_id, reason in sorted(skip_reasons.items()):
            notes.append(f"{spec_id} skipped: {reason}")
        for child in children:
            if child.branch:
                notes.append(f"{child.subtask_id}: branch {child.branch} (not merged)")
            if child.patch_path:
                notes.append(f"{child.subtask_id}: patch at {child.patch_path}")

        result = OrchestrationResult(
            orchestration_id=self.orchestration_id,
            plan=self.plan,
            children=children,
            status=status,
            started_at=started,
            finished_at=self._clock(),
            notes=tuple(notes),
        )
        return result


def _resolve(spec: SubtaskSpec, max_concurrent: int) -> Isolation:
    from minicode.orchestration.plan import resolve_isolation

    return resolve_isolation(spec, max_concurrent=max_concurrent)
