"""Rendering an orchestration result.

Kept separate from the scheduler so the CLI, the slash command and the web panel all
show the same thing.  Two descriptions of one run is how a system starts disagreeing
with itself about what happened.

What the rendering refuses to smooth over: ``skipped`` is not success (it is listed),
``unverified`` is not proven (it is listed), and a patch is not a merge (it is named as
something awaiting a decision).
"""

from __future__ import annotations

from typing import Sequence

from minicode.orchestration.pricing import format_cny
from minicode.orchestration.types import (
    ChildResult,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskStatus,
    one_line,
)

__all__ = ["format_plan_preview", "format_result", "format_status_line"]

_STATUS_LABEL = {
    SubtaskStatus.PENDING: "pending",
    SubtaskStatus.RUNNING: "running",
    SubtaskStatus.DONE: "done",
    SubtaskStatus.FAILED: "failed",
    SubtaskStatus.SKIPPED: "SKIPPED",
    SubtaskStatus.BUDGET_EXHAUSTED: "budget-exhausted",
}


def format_status_line(result: OrchestrationResult) -> str:
    done = sum(1 for child in result.children if child.status is SubtaskStatus.DONE)
    return (
        f"{result.orchestration_id}: {result.status.value} "
        f"({done}/{len(result.children)} done, {format_cny(result.cost_usd)}, "
        f"{result.duration_seconds:.1f}s)"
    )


def format_result(result: OrchestrationResult, *, show_notes: bool = True) -> str:
    lines = [
        f"Orchestration {result.orchestration_id}",
        "=" * 72,
        f"Status : {result.status.value}",
        f"Cost   : {format_cny(result.cost_usd)} (estimated, CNY)   "
        f"Tokens (est.): {result.tokens}   "
        f"Duration: {result.duration_seconds:.1f}s",
        "",
        f"{'subtask':26s} {'status':16s} {'calls':>5s} {'cost':>9s} {'verified':>8s}  summary",
        "-" * 72,
    ]
    for child in result.children:
        lines.append(
            f"{child.subtask_id[:26]:26s} {_STATUS_LABEL[child.status]:16s} "
            f"{child.tool_calls:5d} {child.cost_usd:9.6f} "
            f"{('yes' if child.verified else 'no'):>8s}  {one_line(child.summary, 40)}"
        )

    failures = [
        child
        for child in result.children
        if child.status is SubtaskStatus.FAILED and child.error
    ]
    if failures:
        # The reason belongs on the page, not only in the data: "isolation refused" does
        # not tell anyone *which* file made the tree dirty, and that is the one thing the
        # reader needs in order to act.
        lines.extend(
            [
                "",
                "Failures:",
                *[f"  - {child.subtask_id}: {one_line(child.error, 160)}" for child in failures],
            ]
        )
    unverified = result.unverified
    if unverified:
        lines.extend(
            [
                "",
                "Unverified (finished, but nothing proved them):",
                *[f"  - {name}" for name in unverified],
            ]
        )
    skipped = [child for child in result.children if child.status is SubtaskStatus.SKIPPED]
    if skipped:
        lines.extend(
            [
                "",
                "Skipped (not run -- not a success):",
                *[f"  - {child.subtask_id}: {child.summary or 'dependency failed'}" for child in skipped],
            ]
        )
    await_decision = [child for child in result.children if child.patch_path or child.branch]
    if await_decision:
        lines.extend(
            [
                "",
                "Awaiting your decision (nothing was merged):",
                *[
                    f"  - {child.subtask_id}: branch {child.branch or '(none)'}"
                    + (f", patch {child.patch_path}" if child.patch_path else "")
                    for child in await_decision
                ],
            ]
        )
    if result.status is OrchestrationStatus.COMPLETE and unverified:
        lines.extend(
            [
                "",
                "Note: every subtask finished, but some are unverified -- see above.",
            ]
        )
    if show_notes and result.notes:
        lines.extend(["", "Notes:", *[f"  - {note}" for note in result.notes]])
    return "\n".join(lines)


def format_plan_preview(plan, *, limit: int = 20) -> str:  # noqa: ANN001 - OrchestrationPlan
    """What will run, before anything runs."""
    from minicode.orchestration.plan import resolve_isolation

    lines = [
        f"Plan: {plan.name}",
        "=" * 72,
        f"Subtasks: {len(plan.subtasks)}   concurrency: {plan.max_concurrent}   "
        f"max children: {plan.max_children}   cost cap: ${plan.max_cost_usd:.2f}",
        "",
    ]
    for spec in plan.subtasks[:limit]:
        isolation = resolve_isolation(spec, max_concurrent=plan.max_concurrent)
        budget = getattr(spec, "budget", None)
        cap = f"{budget.max_turns}t/${budget.max_cost_usd:.2f}" if budget is not None else "default"
        lines.append(
            f"  {spec.id[:24]:24s} skill={spec.skill[:22]:22s} "
            f"{'writes' if spec.writes else 'reads ':6s} {isolation.value:8s} {cap}"
        )
    if len(plan.subtasks) > limit:
        lines.append(f"  ... ({len(plan.subtasks) - limit} more)")
    if plan.issues:
        lines.extend(["", "Issues:", *[f"  - {issue}" for issue in plan.issues]])
    return "\n".join(lines)


def format_results(results: Sequence[ChildResult]) -> str:
    return "\n".join(
        f"{child.subtask_id}: {_STATUS_LABEL[child.status]} ({child.summary or child.error})"
        for child in results
    )
