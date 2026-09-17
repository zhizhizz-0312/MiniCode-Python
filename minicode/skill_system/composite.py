"""L2 composite skills: declarative skill orchestrations compiled onto ``task_graph``.

A composite skill is a high-level capability ("publish a Python package") that
declares which sub-skills it needs and in what order.  The orchestration lives in
a ``workflow.json`` next to the composite ``SKILL.md`` rather than in nested YAML
frontmatter: JSON is standard library, trivially validated, and keeps the L1
frontmatter reader flat.

Example::

    {
      "name": "release-flow",
      "steps": [
        {"id": "structure", "skill": "check-structure"},
        {"id": "tests", "skill": "run-tests", "depends_on": ["structure"]},
        {"id": "tag", "skill": "tag-release", "depends_on": ["tests"]},
        {"id": "rollback", "skill": "rollback-release", "when": "on_failure:tag"}
      ]
    }

Execution order is compiled onto the upstream ``minicode.task_graph`` graph, so
the composite layer reuses the existing DAG/priority/slot machinery instead of
growing a second scheduler.  The step-to-task mapping is carried in
``TaskDefinition.metadata`` because ``add_task`` assigns ids itself.

Conditional branching is a deliberately small, checkable DSL:

``deps`` (default)
    every dependency must have **succeeded** before this step runs.  This is the
    CI ``needs:`` semantic and the safe default: continuing a release build after
    the tag step failed is never what anyone wants.
``always``
    dependencies only need to have **finished** -- success or failure.  This is
    the explicit opt-in for reporting, cleanup and rollback steps.
``on_success:<step>`` / ``on_failure:<step>``
    gate on one step's outcome.  Naming a step also makes it an implicit
    dependency, so evaluation order can never be ambiguous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from minicode.skill_system.registry import SkillKind, SkillRegistry, SkillSpec
from minicode.task_graph import TaskGraph, TaskPriority

__all__ = [
    "WORKFLOW_FILENAME",
    "ConditionKind",
    "CompositeStep",
    "CompositeWorkflow",
    "load_workflow",
    "parse_workflow",
    "discover_workflows",
    "topological_order",
    "ready_steps",
    "to_task_graph",
    "format_plan",
    "validate_against_registry",
]

WORKFLOW_FILENAME = "workflow.json"

_PRIORITY_BY_NAME = {
    "low": TaskPriority.LOW,
    "normal": TaskPriority.NORMAL,
    "high": TaskPriority.HIGH,
    "critical": TaskPriority.CRITICAL,
}


class ConditionKind(str, Enum):
    """When a step is allowed to run."""

    DEPS = "deps"
    ALWAYS = "always"
    ON_SUCCESS = "on_success"
    ON_FAILURE = "on_failure"


@dataclass(slots=True)
class CompositeStep:
    """One node of a composite workflow."""

    id: str
    skill: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    condition: ConditionKind = ConditionKind.DEPS
    condition_step: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    timeout_seconds: int = 300

    @property
    def condition_text(self) -> str:
        if self.condition is ConditionKind.DEPS:
            return "deps"
        if self.condition is ConditionKind.ALWAYS:
            return "always"
        return f"{self.condition.value}:{self.condition_step}"


@dataclass(slots=True)
class CompositeWorkflow:
    """A validated composite skill ready to be planned or compiled."""

    name: str
    steps: tuple[CompositeStep, ...]
    source_path: str = ""
    description: str = ""

    def step(self, step_id: str) -> CompositeStep | None:
        for candidate in self.steps:
            if candidate.id == step_id:
                return candidate
        return None

    @property
    def step_ids(self) -> set[str]:
        return {step.id for step in self.steps}

    @property
    def skills(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.skill not in seen:
                seen.append(step.skill)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def _parse_condition(
    raw: object, issues: list[str], where: str
) -> tuple[ConditionKind | None, str | None]:
    """Parse a ``when`` value.

    Returns ``None`` as the kind when the value is ambiguous, which the caller
    treats as fatal: guessing a fallback would silently change when a branch runs.
    """
    text = str(raw or "").strip()
    if not text or text in {"deps", "after", "needs"}:
        return ConditionKind.DEPS, None
    if text == "always":
        return ConditionKind.ALWAYS, None
    head, _, tail = text.partition(":")
    head = head.strip()
    tail = tail.strip()
    if head in {ConditionKind.ON_SUCCESS.value, ConditionKind.ON_FAILURE.value}:
        if not tail:
            issues.append(f"{where}: '{text}' must name a step, e.g. 'on_failure:build'")
            return None, None
        return ConditionKind(head), tail
    issues.append(
        f"{where}: unknown condition '{text}' "
        "(use always, on_success:<step> or on_failure:<step>; omit 'when' for dependency success)"
    )
    return None, None


def parse_workflow(payload: object, *, name: str, source_path: str = "") -> tuple[CompositeWorkflow | None, list[str]]:
    """Turn a decoded ``workflow.json`` payload into a validated workflow.

    Issues are split by consequence.  Anything that changes or ambiguates *when*
    a step runs -- a duplicate id, a self-dependency, an unknown ``when`` value, a
    condition that names no step -- rejects the workflow outright, because
    guessing a fallback would silently run a rollback branch on success or skip a
    reporting branch on failure.  Cosmetic problems (an unknown priority, a stray
    key) are reported while the workflow stays usable.
    """
    issues: list[str] = []
    fatal = False
    if not isinstance(payload, dict):
        return None, ["workflow.json must contain a JSON object"]

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, ["workflow.json must declare a non-empty 'steps' array"]

    steps: list[CompositeStep] = []
    seen: set[str] = set()
    for index, raw_step in enumerate(raw_steps):
        where = f"steps[{index}]"
        if not isinstance(raw_step, dict):
            issues.append(f"{where}: each step must be an object")
            fatal = True
            continue
        step_id = str(raw_step.get("id") or "").strip()
        skill = str(raw_step.get("skill") or "").strip()
        if not step_id:
            issues.append(f"{where}: 'id' is required")
            fatal = True
            continue
        if not skill:
            issues.append(f"{where} ({step_id}): 'skill' is required")
            fatal = True
            continue
        if step_id in seen:
            issues.append(f"{where}: duplicate step id '{step_id}'")
            fatal = True
            continue
        seen.add(step_id)
        where = f"step '{step_id}'"

        raw_deps = raw_step.get("depends_on") or []
        if isinstance(raw_deps, str):
            deps = (raw_deps.strip(),) if raw_deps.strip() else ()
        elif isinstance(raw_deps, list):
            deps = tuple(str(item).strip() for item in raw_deps if str(item).strip())
        else:
            issues.append(f"{where}: 'depends_on' must be a string or an array")
            fatal = True
            deps = ()
        if step_id in deps:
            # Keep the self-edge so the cycle check rejects the graph too.
            issues.append(f"{where}: a step cannot depend on itself")
            fatal = True

        condition, condition_step = _parse_condition(raw_step.get("when"), issues, where)
        if condition is None:
            fatal = True
            condition = ConditionKind.DEPS
        if condition_step is not None and condition_step not in deps:
            # Implicit dependency: a condition on X cannot be evaluated before X ran.
            deps = (*deps, condition_step)

        raw_priority = str(raw_step.get("priority") or "normal").strip().lower()
        priority = _PRIORITY_BY_NAME.get(raw_priority)
        if priority is None:
            issues.append(f"{where}: unknown priority '{raw_priority}'; using normal")
            priority = TaskPriority.NORMAL

        raw_timeout = raw_step.get("timeout_seconds", 300)
        timeout = int(raw_timeout) if isinstance(raw_timeout, (int, float)) and raw_timeout > 0 else 300

        steps.append(
            CompositeStep(
                id=step_id,
                skill=skill,
                description=str(raw_step.get("description") or "").strip(),
                depends_on=deps,
                condition=condition,
                condition_step=condition_step,
                priority=priority,
                timeout_seconds=timeout,
            )
        )

    if not steps:
        return None, issues or ["workflow.json declared no usable steps"]

    missing = sorted(
        {
            dep
            for step in steps
            for dep in step.depends_on
            if dep not in {candidate.id for candidate in steps}
        }
    )
    for dep in missing:
        issues.append(f"step dependency '{dep}' does not exist")

    workflow = CompositeWorkflow(
        name=str(payload.get("name") or name).strip() or name,
        steps=tuple(steps),
        source_path=source_path,
        description=str(payload.get("description") or "").strip(),
    )

    cycle = _find_cycle(workflow)
    if cycle:
        issues.append(f"dependency cycle detected: {' -> '.join(cycle)}")

    rejected = fatal or bool(missing) or bool(cycle)
    return (None if rejected else workflow), issues


def load_workflow(spec: SkillSpec) -> tuple[CompositeWorkflow | None, list[str]]:
    """Load the ``workflow.json`` that belongs to a composite ``SKILL.md``."""
    path = Path(spec.path).parent / WORKFLOW_FILENAME
    if not path.exists():
        return None, [f"{spec.name}: composite skill has no {WORKFLOW_FILENAME} next to SKILL.md"]
    try:
        # Tolerate a UTF-8 BOM: editors and PowerShell's `Set-Content -Encoding
        # utf8` both emit one, and a BOM would otherwise make the workflow
        # silently disappear from discovery.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, [f"{spec.name}: {WORKFLOW_FILENAME} could not be read: {exc}"]
    return parse_workflow(payload, name=spec.name, source_path=str(path))


def discover_workflows(
    registry: SkillRegistry,
) -> tuple[list[CompositeWorkflow], list[str]]:
    """Load every composite skill in a registry, collecting issues as we go."""
    workflows: list[CompositeWorkflow] = []
    issues: list[str] = []
    for spec in registry.by_kind(SkillKind.COMPOSITE):
        workflow, workflow_issues = load_workflow(spec)
        issues.extend(workflow_issues)
        if workflow is not None:
            workflows.append(workflow)
    return workflows, issues


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def _find_cycle(workflow: CompositeWorkflow) -> list[str]:
    """Return one dependency cycle as a path, or ``[]`` when the DAG is clean."""
    state: dict[str, int] = {}  # 0 = visiting, 1 = done
    stack: list[str] = []

    def visit(step_id: str) -> list[str]:
        marker = state.get(step_id)
        if marker == 1:
            return []
        if marker == 0:
            return [*stack[stack.index(step_id) :], step_id]
        state[step_id] = 0
        stack.append(step_id)
        step = workflow.step(step_id)
        for dep in step.depends_on if step else ():
            found = visit(dep)
            if found:
                return found
        stack.pop()
        state[step_id] = 1
        return []

    for step in workflow.steps:
        found = visit(step.id)
        if found:
            return found
    return []


def topological_order(workflow: CompositeWorkflow) -> list[CompositeStep]:
    """Kahn's algorithm, breaking ties by declaration order for determinism."""
    remaining = {step.id: set(step.depends_on) for step in workflow.steps}
    ordered: list[CompositeStep] = []
    while remaining:
        ready = [
            step
            for step in workflow.steps
            if step.id in remaining and not remaining[step.id]
        ]
        if not ready:
            break  # a cycle; parse_workflow already rejects these
        for step in ready:
            ordered.append(step)
            remaining.pop(step.id, None)
        for deps in remaining.values():
            deps.difference_update(step.id for step in ready)
    return ordered


def _condition_holds(step: CompositeStep, completed: set[str], failed: set[str]) -> bool:
    """Evaluate a step's gate.

    ``DEPS`` (the default) requires every dependency to have *succeeded*; a failed
    dependency blocks the step unless the author opted out with ``always`` or an
    ``on_failure:`` branch.
    """
    if step.condition is ConditionKind.ALWAYS:
        return True
    if step.condition is ConditionKind.ON_SUCCESS:
        if step.condition_step:
            # `failed` is authoritative: a step recorded as failed is never a success.
            return step.condition_step in completed and step.condition_step not in failed
        return all(dep in completed for dep in step.depends_on)
    if step.condition is ConditionKind.ON_FAILURE:
        return bool(step.condition_step and step.condition_step in failed)
    return all(dep in completed and dep not in failed for dep in step.depends_on)


def ready_steps(
    workflow: CompositeWorkflow,
    completed: Iterable[str] = (),
    failed: Iterable[str] = (),
) -> list[CompositeStep]:
    """Steps that may run now: every dependency finished and the condition holds.

    Dependency *completion* (not success) is the outer gate, so ``always``
    branches such as rollback, reporting and cleanup still fire after an upstream
    failure; whether success is required is then decided per condition.
    """
    done = set(completed)
    bad = set(failed)
    ready: list[CompositeStep] = []
    for step in workflow.steps:
        if step.id in done or step.id in bad:
            continue
        if any(dep not in done and dep not in bad for dep in step.depends_on):
            continue
        if not _condition_holds(step, done, bad):
            continue
        ready.append(step)
    return ready


def to_task_graph(workflow: CompositeWorkflow) -> TaskGraph:
    """Compile the workflow onto the upstream ``TaskGraph``.

    ``TaskGraph.add_task`` assigns its own task ids, so every step is added first
    and the generated ids are wired into ``dependencies`` afterwards.  The step
    identity travels in ``TaskDefinition.metadata`` for the orchestrator to read
    back when it needs to know which skill a task maps to.
    """
    graph = TaskGraph(name=workflow.name)
    task_id_by_step: dict[str, str] = {}

    for step in topological_order(workflow):
        definition = graph.add_task(
            name=step.skill,
            description=step.description or f"{workflow.name}: {step.id}",
            priority=step.priority,
            timeout_seconds=step.timeout_seconds,
        )
        definition.metadata = {
            "workflow": workflow.name,
            "step": step.id,
            "skill": step.skill,
            "condition": step.condition_text,
        }
        task_id_by_step[step.id] = definition.id

    for step in workflow.steps:
        definition = graph.definitions.get(task_id_by_step[step.id])
        if definition is not None:
            definition.dependencies = [
                task_id_by_step[dep] for dep in step.depends_on if dep in task_id_by_step
            ]

    return graph


def validate_against_registry(
    workflow: CompositeWorkflow,
    registry: SkillRegistry,
) -> list[str]:
    """Warn about steps whose skill is not present in the registry."""
    issues: list[str] = []
    for step in workflow.steps:
        if registry.get(step.skill) is None:
            issues.append(
                f"{workflow.name}: step '{step.id}' references unknown skill '{step.skill}'"
            )
    return issues


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_plan(workflow: CompositeWorkflow) -> str:
    """Render the workflow as an ordered plan, marking conditional branches."""
    lines = [
        f"Composite skill: {workflow.name}",
        "=" * 60,
    ]
    if workflow.description:
        lines.append(workflow.description)
        lines.append("")
    lines.append(f"Steps ({len(workflow.steps)}):")
    for index, step in enumerate(topological_order(workflow), start=1):
        condition = ""
        if step.condition is not ConditionKind.DEPS:
            condition = f"  [when {step.condition_text}]"
        lines.append(f"  {index}. {step.id} -> skill {step.skill}{condition}")
        if step.depends_on:
            lines.append(f"     after: {', '.join(step.depends_on)}")
        if step.description:
            lines.append(f"     {step.description}")
    return "\n".join(lines)
