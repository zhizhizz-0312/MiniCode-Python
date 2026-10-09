"""Centralised multi-agent orchestration: plans, budgets, isolation, audit.

Module map:

* ``types``    -- plan, subtask, result and status vocabulary
* ``budget``   -- the two-layer hard gate (per child, per run)
* ``plan``     -- a composite skill's DAG plus the metadata execution needs
* ``worktree`` -- git isolation for subtasks that change the tree
* ``worker``   -- one bounded execution agent, unable to start another
* ``orchestrator`` -- readiness, dispatch, verification and aggregation

Still to come: ``audit`` (the shared trail and its replay) and ``report``
(rendering), then the command surface and the web panel.

The design decisions live in the stage document; the short version is that this is
centralised on purpose -- one audit sink, one budget, one place to attribute a
failure -- and that isolation is git's, not something invented here.
"""

from __future__ import annotations

from minicode.orchestration.budget import Budget, BudgetTracker, OrchestrationBudget
from minicode.orchestration.plan import load_plan, plan_from_workflow, validate_plan
from minicode.orchestration.report import (
    format_plan_preview,
    format_result,
    format_status_line,
)
from minicode.orchestration.types import (
    ChildResult,
    Isolation,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskSpec,
    SubtaskStatus,
)
from minicode.orchestration.audit import (
    AuditEntry,
    OrchestrationAudit,
    ReplayReport,
    audit_path,
    format_replay,
    format_timeline,
    format_tree,
    load_entries,
    orchestration_dir,
    replay,
)
from minicode.orchestration.orchestrator import Orchestrator, ready_subtask_ids, run_verification
from minicode.orchestration.worker import (
    FORBIDDEN_TOOLS,
    AgentWorker,
    WorkerContext,
    WorkerOutcome,
    allowed_tools,
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
    "AgentWorker",
    "AuditEntry",
    "Budget",
    "BudgetTracker",
    "ChildResult",
    "FORBIDDEN_TOOLS",
    "GitUnavailable",
    "Isolation",
    "OrchestrationAudit",
    "OrchestrationBudget",
    "OrchestrationPlan",
    "OrchestrationResult",
    "OrchestrationStatus",
    "Orchestrator",
    "ReplayReport",
    "RepoStatus",
    "SubtaskSpec",
    "SubtaskStatus",
    "WorkerContext",
    "WorkerOutcome",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeSession",
    "allowed_tools",
    "audit_path",
    "format_plan_preview",
    "format_replay",
    "format_result",
    "format_status_line",
    "format_timeline",
    "format_tree",
    "load_entries",
    "load_plan",
    "orchestration_dir",
    "plan_from_workflow",
    "ready_subtask_ids",
    "replay",
    "repo_status",
    "run_verification",
    "validate_plan",
]
