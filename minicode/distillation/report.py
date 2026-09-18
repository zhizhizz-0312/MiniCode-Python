"""Human-readable rendering for the distillation loop.

Kept out of the CLI so the slash command and ``python -m minicode.distillation``
show exactly the same thing -- two descriptions of the same state is how a
learning system starts lying about what it did.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from minicode.distillation.draft import DraftStore
from minicode.distillation.types import CandidateCluster, DraftStatus, SkillDraft, TrajectoryRef

__all__ = [
    "format_candidates",
    "format_draft_detail",
    "format_drafts",
    "format_loop_report",
]

_STATUS_MARK = {
    DraftStatus.PENDING: "pending",
    DraftStatus.APPROVED: "approved",
    DraftStatus.REJECTED: "rejected",
}


def format_drafts(drafts: Sequence[SkillDraft]) -> str:
    """The review queue: what is waiting, what was decided."""
    if not drafts:
        return (
            "No distilled drafts yet.\n"
            "  Run /skills distill to mine recorded sessions for reusable procedures."
        )
    lines = [f"Distilled drafts ({len(drafts)}):", ""]
    for draft in drafts:
        lines.append(
            f"- [{_STATUS_MARK[draft.status]}] {draft.id}  {draft.name}  "
            f"confidence={draft.confidence:.2f} (evidence {draft.prior_confidence:.2f})"
        )
        lines.append(f"    {draft.description or '(no description)'}")
        if draft.triggers:
            lines.append(f"    triggers: {', '.join(draft.triggers)}")
        if draft.sources:
            sessions = ", ".join(
                sorted({f"{item.get('sessionId')}#{item.get('turn')}" for item in draft.sources})
            )
            lines.append(f"    from: {sessions}")
        if draft.issues:
            lines.append(f"    flags: {len(draft.issues)} ({draft.issues[0][:90]})")
        if draft.review_note:
            lines.append(f"    review: {draft.review_note[:90]}")
    lines.extend(
        [
            "",
            "Review one with /skills draft <id>, then /skills approve <id> or "
            "/skills reject <id> <reason>.",
        ]
    )
    return "\n".join(lines)


def format_draft_detail(draft: SkillDraft) -> str:
    """Everything a reviewer needs: the proposal, its provenance, its flags."""
    lines = [
        f"Draft: {draft.id}",
        f"Status: {_STATUS_MARK[draft.status]}",
        f"Name: {draft.name}",
        f"Description: {draft.description or '(none)'}",
        f"Risk level: {draft.risk_level}",
        f"Triggers: {', '.join(draft.triggers) if draft.triggers else '(none)'}",
        f"Tools: {', '.join(draft.tools) if draft.tools else 'unrestricted'}",
        "",
        f"Confidence: {draft.confidence:.2f} "
        f"(evidence prior {draft.prior_confidence:.2f}; the model's self-assessment "
        f"carries the smaller share by design)",
    ]
    if draft.rationale:
        lines.append(f"Rationale: {draft.rationale}")
    lines.append("")
    lines.append("Procedure:")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(draft.steps, start=1))
    lines.append("")
    lines.append(f"Source sessions ({len(draft.sources)}):")
    for item in draft.sources:
        lines.append(
            f"  - {item.get('sessionId')}#{item.get('turn')} [{item.get('outcome')}] "
            f"{len(item.get('toolCalls') or [])} tool call(s), "
            f"{item.get('toolErrors', 0)} error(s): {str(item.get('prompt'))[:90]}"
        )
    if draft.issues:
        lines.append("")
        lines.append("Flags raised while distilling:")
        lines.extend(f"  - {issue}" for issue in draft.issues)
    if draft.security_note:
        lines.append("")
        lines.append(f"Screening: {draft.security_note}")
    if draft.review_note:
        lines.append(f"Review note: {draft.review_note}")
    lines.extend(["", "Full text:", "", draft.skill_markdown.strip()])
    return "\n".join(lines)


def format_candidates(
    candidates: Iterable[CandidateCluster],
    rejected: Iterable[tuple[str, str]],
    *,
    trajectory_count: int,
    limit: int = 10,
) -> str:
    """Why the miner accepted or skipped each cluster -- so 'nothing found' is explainable."""
    accepted = list(candidates)
    skipped = list(rejected)
    lines = [
        "Distillation candidates",
        "=" * 60,
        f"Trajectories examined: {trajectory_count}",
        f"Candidates: {len(accepted)}",
        f"Skipped clusters: {len(skipped)}",
        "",
    ]
    if accepted:
        lines.append("Candidates (same task seen more than once, with a lesson):")
        for candidate in accepted[:limit]:
            lines.append(
                f"  - {candidate.signature[:70]!r}: {candidate.reason}, "
                f"mean {candidate.mean_cost:.1f} tool call(s)"
            )
    else:
        lines.append("No candidate passed the gate.")
        lines.append(
            "  A candidate needs: the same kind of task at least twice, one success, and"
        )
        lines.append("  either a correction or a failure -- then a later attempt that succeeded.")
    if skipped:
        lines.append("")
        lines.append(f"Skipped ({len(skipped)}):")
        for signature, reason in skipped[:limit]:
            lines.append(f"  - {signature[:60]!r}: {reason}")
    return "\n".join(lines)


def format_loop_report(
    *,
    trajectories: Sequence[TrajectoryRef],
    candidates: Sequence[CandidateCluster],
    rejected: Sequence[tuple[str, str]],
    store: DraftStore,
) -> str:
    """The whole loop at a glance: corpus size, gate outcome, queue, refusals, spend."""
    drafts = store.list()
    state = store.load_state()
    by_status: dict[str, int] = {}
    for draft in drafts:
        by_status[_STATUS_MARK[draft.status]] = by_status.get(_STATUS_MARK[draft.status], 0) + 1
    synthetic = sum(1 for ref in trajectories if ref.synthetic)
    lines = [
        "Distillation loop",
        "=" * 60,
        f"Draft area: {store.root}",
        f"State file: {store.state_file}",
        "",
        "Corpus:",
        f"  trajectories: {len(trajectories)} ({synthetic} synthetic)",
        f"  corrections detected: {sum(1 for ref in trajectories if ref.correction)}",
        f"  failures: {sum(1 for ref in trajectories if ref.failed)}",
        "",
        "Mining:",
        f"  candidates: {len(candidates)}",
        f"  skipped clusters: {len(rejected)}",
        "",
        "Review queue:",
        "  " + (", ".join(f"{key}={value}" for key, value in sorted(by_status.items())) or "(empty)"),
        f"  refusals remembered: {len(state.rejected)}",
        f"  clusters already distilled: {len(state.processed)}",
        f"  distillation spend: ${state.spent_usd:.4f}",
    ]
    if synthetic:
        lines.extend(
            [
                "",
                "Note: synthetic trajectories are in this corpus. They exercise the pipeline;",
                "they are not evidence about your own usage.",
            ]
        )
    return "\n".join(lines)


def draft_counts(store: DraftStore) -> dict[str, Any]:
    """Small machine-readable summary for callers that only need the numbers."""
    drafts = store.list()
    state = store.load_state()
    return {
        "total": len(drafts),
        "pending": sum(1 for draft in drafts if draft.pending),
        "approved": sum(1 for draft in drafts if draft.status is DraftStatus.APPROVED),
        "rejected": sum(1 for draft in drafts if draft.status is DraftStatus.REJECTED),
        "refusalsRemembered": len(state.rejected),
        "processedClusters": len(state.processed),
        "spentUsd": round(state.spent_usd, 6),
    }
