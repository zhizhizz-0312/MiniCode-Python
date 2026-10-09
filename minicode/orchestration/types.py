"""Shared vocabulary for multi-agent orchestration.

Four shapes carry the whole design:

``SubtaskSpec``
    One node of the plan: what to do, who may do it (skill and tool whitelists),
    how much it may spend, whether it must run isolated, and how to verify it.
``OrchestrationPlan``
    The whole DAG plus the caps that apply across it.  A plan is a file a human can
    read before anything runs -- that is the point of plan-driven orchestration.
``ChildResult``
    What one execution agent came back with, including the evidence and the cost.
``OrchestrationResult``
    The aggregation: every subtask's terminal status, what was verified, and what
    explicitly was not.

Two status vocabularies exist because the distinction matters at the top level:
``SKIPPED`` is not success, and ``BUDGET_EXHAUSTED`` is not failure -- it is "stopped
on purpose, here is the partial work".  Collapsing those into done/failed is how an
orchestrator ends up reporting success it cannot support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "ChildResult",
    "Isolation",
    "OrchestrationPlan",
    "OrchestrationResult",
    "OrchestrationStatus",
    "SubtaskSpec",
    "SubtaskStatus",
]


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # a dependency failed; not success, not failure
    BUDGET_EXHAUSTED = "budget_exhausted"  # stopped deliberately, partial work kept


class OrchestrationStatus(str, Enum):
    COMPLETE = "complete"  # every required subtask finished
    PARTIAL = "partial"  # some finished, some did not (failed/skipped/exhausted)
    FAILED = "failed"  # nothing usable came out


class Isolation(str, Enum):
    AUTO = "auto"  # decide from the subtask's writes flag and the concurrency
    SHARED = "shared"  # run in the main working tree (read-only work)
    WORKTREE = "worktree"  # run in its own git worktree


TERMINAL_STATUSES = frozenset(
    {
        SubtaskStatus.DONE,
        SubtaskStatus.FAILED,
        SubtaskStatus.SKIPPED,
        SubtaskStatus.BUDGET_EXHAUSTED,
    }
)


@dataclass(slots=True)
class SubtaskSpec:
    """One node of an orchestration plan."""

    id: str
    skill: str
    instruction: str = ""
    depends_on: tuple[str, ...] = ()
    condition: str = ""  # P1's condition DSL, evaluated by the plan, not re-invented
    skills_whitelist: tuple[str, ...] = ()
    #: ``None`` means "inherit whatever the referenced skill declares" -- P1's own
    #: convention, where ``None`` is unrestricted and an empty tuple is "no tools".
    #: Keeping the distinction matters: a subtask with no tools at all is a legitimate
    #: and useful plan, and collapsing it into "unrestricted" would be a security bug.
    tools_whitelist: tuple[str, ...] | None = None
    isolation: Isolation = Isolation.AUTO
    writes: bool = False
    model: str = ""
    budget: Any = None  # orchestration.budget.Budget; loose to keep import graph flat
    verify: tuple[str, ...] = ()  # commands that must pass inside this subtask
    retry: int = 0

    def describe(self) -> str:
        parts = [f"{self.id}: {self.skill}"]
        if self.depends_on:
            parts.append(f"after {', '.join(self.depends_on)}")
        if self.writes:
            parts.append("writes")
        if self.isolation is not Isolation.AUTO:
            parts.append(f"isolation={self.isolation.value}")
        if self.verify:
            parts.append(f"verify={len(self.verify)}")
        return " | ".join(parts)


@dataclass(slots=True)
class OrchestrationPlan:
    """A whole plan: the DAG plus the caps that apply across it."""

    name: str
    source_path: str = ""
    subtasks: tuple[SubtaskSpec, ...] = ()
    max_children: int = 10
    max_concurrent: int = 3
    max_cost_usd: float = 0.50
    model: str = ""
    issues: tuple[str, ...] = ()

    def get(self, subtask_id: str) -> SubtaskSpec | None:
        for spec in self.subtasks:
            if spec.id == subtask_id:
                return spec
        return None

    @property
    def writers(self) -> tuple[SubtaskSpec, ...]:
        return tuple(spec for spec in self.subtasks if spec.writes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sourcePath": self.source_path,
            "maxChildren": self.max_children,
            "maxConcurrent": self.max_concurrent,
            "maxCostUsd": self.max_cost_usd,
            "model": self.model,
            "issues": list(self.issues),
            "subtasks": [
                {
                    "id": spec.id,
                    "skill": spec.skill,
                    "instruction": spec.instruction,
                    "dependsOn": list(spec.depends_on),
                    "condition": spec.condition,
                    "skillsWhitelist": list(spec.skills_whitelist),
                    "toolsWhitelist": list(spec.tools_whitelist),
                    "isolation": spec.isolation.value,
                    "writes": spec.writes,
                    "model": spec.model,
                    "verify": list(spec.verify),
                    "retry": spec.retry,
                }
                for spec in self.subtasks
            ],
        }


@dataclass(slots=True)
class ChildResult:
    """What one execution agent returned."""

    subtask_id: str
    status: SubtaskStatus = SubtaskStatus.PENDING
    summary: str = ""
    branch: str = ""
    worktree: str = ""
    patch_path: str = ""
    changed_files: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    stop_reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    verified: bool = False
    verify_output: str = ""
    error: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtaskId": self.subtask_id,
            "status": self.status.value,
            "summary": self.summary,
            "branch": self.branch,
            "worktree": self.worktree,
            "patchPath": self.patch_path,
            "changedFiles": list(self.changed_files),
            "evidence": list(self.evidence),
            "stopReason": self.stop_reason,
            "turns": self.turns,
            "toolCalls": self.tool_calls,
            "tokens": self.tokens,
            "costUsd": round(self.cost_usd, 6),
            "durationSeconds": round(self.duration, 3),
            "verified": self.verified,
            "error": self.error,
        }


@dataclass(slots=True)
class OrchestrationResult:
    """The aggregation, including what was *not* established."""

    orchestration_id: str
    plan: OrchestrationPlan | None = None
    children: list[ChildResult] = field(default_factory=list)
    status: OrchestrationStatus = OrchestrationStatus.FAILED
    started_at: float = 0.0
    finished_at: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def cost_usd(self) -> float:
        return sum(child.cost_usd for child in self.children)

    @property
    def tokens(self) -> int:
        return sum(child.tokens for child in self.children)

    @property
    def unverified(self) -> tuple[str, ...]:
        """Subtasks that finished but whose verification did not run or did not pass.

        Reported explicitly because "done" and "verified" are different claims, and
        an aggregation that blurs them is worse than no aggregation.
        """
        return tuple(
            child.subtask_id
            for child in self.children
            if child.status is SubtaskStatus.DONE and not child.verified
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "orchestrationId": self.orchestration_id,
            "status": self.status.value,
            "costUsd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "durationSeconds": round(max(0.0, self.finished_at - self.started_at), 3),
            "unverified": list(self.unverified),
            "notes": list(self.notes),
            "children": [child.to_dict() for child in self.children],
        }
