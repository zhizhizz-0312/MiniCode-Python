"""Plans: a composite workflow plus the metadata multi-agent execution needs.

A plan is **a file a human can read before anything runs**, which is the whole reason
this stage is plan-driven rather than "let the model spawn workers".  So the plan
model adds only what the DAG itself cannot express:

* who may do the work (skill and tool whitelists),
* what it may spend (a per-subtask budget),
* whether it must run isolated (writers),
* and how its result is verified (commands that must pass).

**No second DSL.**  The DAG, dependencies and conditions come from P1's composite
workflow, parsed by P1's own parser; the orchestration metadata lives in a sibling
``orchestration.json`` so the composite format keeps working unchanged.  P1 already
rejects a workflow whose conditions are ambiguous, and P4 inherits that instead of
re-implementing it.

**Where this refuses to guess.**  A writer that declares shared isolation while
several children run at once is rejected at load time rather than at run time: two
agents editing one working tree is the failure this stage exists to prevent, and
"we noticed too late" is not a defence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from minicode.orchestration.budget import Budget
from minicode.orchestration.types import Isolation, OrchestrationPlan, SubtaskSpec
from minicode.skill_system.composite import (
    CompositeWorkflow,
    load_workflow,
    topological_order,
)

__all__ = [
    "ORCHESTRATION_FILENAME",
    "WRITE_TOOLS",
    "load_plan",
    "orchestration_file",
    "plan_from_workflow",
    "resolve_isolation",
    "validate_plan",
]

ORCHESTRATION_FILENAME = "orchestration.json"

#: Tools whose presence in a skill's declaration means the subtask changes the tree.
#: Used only to *infer* ``writes`` when the plan does not say, because getting the
#: isolation wrong is the expensive kind of mistake.
WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "patch_file", "modify_file", "run_command"}
)

_KNOWN_TOOLS: set[str] | None = None


def _known_tools() -> set[str]:
    """The canonical tool list: the security chain's declared-effects table.

    One source of truth for "is this a real tool", so a plan cannot whitelist a name
    that no layer would ever accept.
    """
    global _KNOWN_TOOLS
    if _KNOWN_TOOLS is None:
        try:
            from minicode.security_chain.preflight import SIDE_EFFECTS

            _KNOWN_TOOLS = set(SIDE_EFFECTS)
        except Exception:  # noqa: BLE001 - absence means "cannot check", not "reject all"
            _KNOWN_TOOLS = set()
    return _KNOWN_TOOLS


@dataclass
class _Overrides:
    """The decoded ``orchestration.json``: global caps plus per-subtask settings."""

    subtasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_budget: Budget = field(default_factory=Budget)
    max_children: int = 10
    max_concurrent: int = 3
    max_cost_usd: float = 0.50
    model: str = ""


def orchestration_file(spec_path: str | Path) -> Path:
    """``orchestration.json`` next to the composite ``SKILL.md``."""
    return Path(spec_path).parent / ORCHESTRATION_FILENAME


def _read_overrides(path: Path) -> tuple[_Overrides, list[str]]:
    if not path.exists():
        return _Overrides(), []
    try:
        # utf-8-sig for the same reason P1 does it: editors and PowerShell emit BOMs,
        # and a BOM must not make a plan silently vanish.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return _Overrides(), [f"{ORCHESTRATION_FILENAME} could not be read: {error}"]
    if not isinstance(payload, dict):
        return _Overrides(), [f"{ORCHESTRATION_FILENAME} must contain a JSON object"]

    issues: list[str] = []
    subtasks = payload.get("subtasks")
    if subtasks is not None and not isinstance(subtasks, dict):
        issues.append(f"{ORCHESTRATION_FILENAME}: 'subtasks' must be an object keyed by step id")
        subtasks = {}

    def _number(key: str, fallback: float) -> float:
        value = payload.get(key)
        if value is None:
            return fallback
        try:
            return float(value)
        except (TypeError, ValueError):
            issues.append(f"{ORCHESTRATION_FILENAME}: '{key}' must be a number")
            return fallback

    return (
        _Overrides(
            subtasks={str(key): value for key, value in (subtasks or {}).items() if isinstance(value, dict)},
            default_budget=Budget.from_dict(payload.get("budget")),
            max_children=int(_number("maxChildren", 10)),
            max_concurrent=int(_number("maxConcurrent", 3)),
            max_cost_usd=_number("maxCostUsd", 0.50),
            model=str(payload.get("model") or ""),
        ),
        issues,
    )


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def resolve_isolation(spec: SubtaskSpec, *, max_concurrent: int) -> Isolation:
    """Turn ``AUTO`` into a decision.

    **A writer is always isolated.**  Its changes must reach the workspace as a patch a
    human applies, never as an edit that already happened, and that promise cannot depend
    on a concurrency number: with the old rule the same plan wrote straight into the
    working tree at ``maxConcurrent: 1`` and produced a patch at ``3``, so whether the
    user got to review anything was a side effect of a tuning knob.  A reader is not
    copied -- duplicating a tree for work that cannot modify it is pure cost.

    ``max_concurrent`` is kept in the signature because callers reason about scheduling,
    but it deliberately no longer decides this.  A direct write remains possible only by
    declaring ``isolation: shared`` explicitly, and the validator still refuses that
    while more than one child may run at once.
    """
    if spec.isolation is not Isolation.AUTO:
        return spec.isolation
    if spec.writes:
        return Isolation.WORKTREE
    return Isolation.SHARED


def plan_from_workflow(
    workflow: CompositeWorkflow,
    *,
    name: str,
    source_path: str = "",
    overrides: _Overrides | None = None,
    skill_tools: dict[str, tuple[str, ...] | None] | None = None,
) -> OrchestrationPlan:
    """Build a plan from P1's workflow plus optional orchestration metadata.

    ``skill_tools`` maps a referenced skill to its declared tool list, which is how
    ``writes`` gets inferred when the plan stays silent about it.
    """
    settings = overrides or _Overrides()
    known_skills = skill_tools or {}
    subtasks: list[SubtaskSpec] = []

    for step in topological_order(workflow):
        override = settings.subtasks.get(step.id, {})
        declared = known_skills.get(step.skill)
        writes = override.get("writes")
        if writes is None:
            writes = bool(declared and WRITE_TOOLS.intersection(declared))
        tools = override.get("tools")
        tools_whitelist = None if tools is None else _as_tuple(tools)
        raw_isolation = str(override.get("isolation") or Isolation.AUTO.value).strip().lower()
        try:
            isolation = Isolation(raw_isolation)
        except ValueError:
            isolation = Isolation.AUTO

        subtasks.append(
            SubtaskSpec(
                id=step.id,
                skill=step.skill,
                instruction=str(override.get("instruction") or step.description or ""),
                depends_on=tuple(step.depends_on),
                condition=step.condition_text,
                skills_whitelist=_as_tuple(override.get("skills")),
                tools_whitelist=tools_whitelist,
                isolation=isolation,
                writes=bool(writes),
                model=str(override.get("model") or ""),
                budget=Budget.from_dict(override.get("budget"), base=settings.default_budget),
                verify=_as_tuple(override.get("verify")),
                retry=max(0, int(override.get("retry") or 0)),
            )
        )

    return OrchestrationPlan(
        name=name,
        source_path=source_path,
        subtasks=tuple(subtasks),
        max_children=settings.max_children,
        max_concurrent=max(1, settings.max_concurrent),
        max_cost_usd=settings.max_cost_usd,
        model=settings.model,
        workflow=workflow,
    )


def validate_plan(
    plan: OrchestrationPlan,
    *,
    known_skills: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(blocking, warnings)``.

    Blocking means "do not run this": a plan that cannot be executed safely or at all.
    Warnings mean "this will work, but you probably want to know".
    """
    blocking: list[str] = []
    warnings: list[str] = []
    known = set(known_skills) if known_skills is not None else None
    known_tools = _known_tools()

    seen: set[str] = set()
    for spec in plan.subtasks:
        if spec.id in seen:
            blocking.append(f"duplicate subtask id '{spec.id}'")
        seen.add(spec.id)
        if known is not None and spec.skill not in known:
            blocking.append(f"{spec.id}: skill '{spec.skill}' is not in the registry")
        if spec.tools_whitelist:
            unknown = sorted(set(spec.tools_whitelist) - known_tools) if known_tools else []
            if unknown:
                blocking.append(f"{spec.id}: unknown tool(s) {', '.join(unknown)}")
        if spec.writes and spec.isolation is Isolation.SHARED and plan.max_concurrent > 1:
            blocking.append(
                f"{spec.id}: writes the tree but declares shared isolation while "
                f"{plan.max_concurrent} children may run at once"
            )
        if spec.writes and not spec.verify:
            warnings.append(
                f"{spec.id}: writes without a verification command, so its result "
                "will be reported as unverified"
            )
        if spec.retry > 1:
            warnings.append(f"{spec.id}: retry={spec.retry} multiplies cost on failure")
        if not spec.writes and spec.isolation is Isolation.WORKTREE:
            warnings.append(f"{spec.id}: read-only work in a worktree pays for a copy it cannot need")

    for spec in plan.subtasks:
        for dependency in spec.depends_on:
            if dependency not in seen:
                blocking.append(f"{spec.id}: depends on unknown subtask '{dependency}'")

    if len(plan.subtasks) > plan.max_children:
        blocking.append(
            f"plan declares {len(plan.subtasks)} subtasks but maxChildren is {plan.max_children}"
        )
    if plan.max_concurrent < 1:
        blocking.append("maxConcurrent must be at least 1")
    return blocking, warnings


def load_plan(
    spec: Any,
    *,
    registry: Any = None,
) -> tuple[OrchestrationPlan | None, list[str]]:
    """Load a plan for a composite ``SkillSpec`` (P1's loader does the DAG parsing)."""
    workflow, issues = load_workflow(spec)
    if workflow is None:
        return None, issues

    overrides, override_issues = _read_overrides(orchestration_file(spec.path))
    issues.extend(override_issues)

    skill_tools: dict[str, tuple[str, ...] | None] = {}
    known_skills: tuple[str, ...] = ()
    if registry is not None:
        # The names that *exist*, not the names the workflow references.  Building this
        # list from the referenced names made the "skill is not in the registry" check
        # incapable of firing -- which is how a plan full of uninstalled skills reached
        # execution and failed inside `load_skill` instead of at load time.
        known_skills = tuple(registry.names())
        for step in workflow.steps:
            referenced = registry.get(step.skill)
            skill_tools[step.skill] = tuple(referenced.allowed_tools) if (
                referenced is not None and referenced.allowed_tools is not None
            ) else None

    plan = plan_from_workflow(
        workflow,
        name=spec.name,
        source_path=str(spec.path),
        overrides=overrides,
        skill_tools=skill_tools,
    )
    blocking, warnings = validate_plan(plan, known_skills=known_skills or None)
    issues.extend(f"error: {item}" for item in blocking)
    issues.extend(f"warning: {item}" for item in warnings)
    if blocking:
        return None, issues
    return plan, issues
