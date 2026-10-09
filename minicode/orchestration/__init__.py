"""Centralised multi-agent orchestration: plans, budgets, isolation, audit.

Module map:

* ``types``    -- plan, subtask, result and status vocabulary
* ``budget``   -- the two-layer hard gate (per child, per run)
* ``plan``     -- a composite skill's DAG plus the metadata execution needs
* ``worktree`` -- git isolation for subtasks that change the tree

Still to come: ``worker`` (a bounded execution agent), ``orchestrator`` (scheduling
and aggregation), ``audit`` (the shared trail and its replay) and ``report``
(rendering).

The design decisions live in the stage document; the short version is that this is
centralised on purpose -- one audit sink, one budget, one place to attribute a
failure -- and that isolation is git's, not something invented here.
"""

from __future__ import annotations

from minicode.orchestration.budget import Budget, BudgetTracker, OrchestrationBudget
from minicode.orchestration.plan import load_plan, plan_from_workflow, validate_plan
from minicode.orchestration.types import (
    ChildResult,
    Isolation,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskSpec,
    SubtaskStatus,
)
from minicode.orchestration.worktree import (
    GitUnavailable,
    RepoStatus,
    WorktreeError,
    WorktreeManager,
    WorktreeSession,
    repo_status,
)

__all__ = [
    "Budget",
    "BudgetTracker",
    "ChildResult",
    "GitUnavailable",
    "Isolation",
    "OrchestrationBudget",
    "OrchestrationPlan",
    "OrchestrationResult",
    "OrchestrationStatus",
    "RepoStatus",
    "SubtaskSpec",
    "SubtaskStatus",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeSession",
    "load_plan",
    "plan_from_workflow",
    "repo_status",
    "validate_plan",
]
