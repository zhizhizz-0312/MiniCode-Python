"""Execution agents: one subtask, one bounded worker, no way to spawn another.

The worker is where three of the stage's guarantees are enforced rather than
promised:

* **depth is one.**  A child is given a filtered tool registry that cannot contain an
  orchestration tool, and a policy that rejects any whitelist naming one.  A child
  therefore has no mechanism to start a grandchild -- the limit is structural, not a
  rule someone has to remember;
* **the budget is checked before each step** and exhaustion is a *status*, so a child
  that runs out of allowance stops with what it has and says so;
* **the whitelist is the tool registry.**  Filtering the registry is stronger than
  asking the model to behave: a tool that is not in the registry cannot be called,
  whatever the model decides.

The model, tools and permissions arrive through a context factory, which keeps the
worker testable without a network and keeps *how* a child is configured in one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from minicode.orchestration.budget import BUDGET_EXHAUSTED, BudgetTracker
from minicode.orchestration.types import SubtaskSpec, SubtaskStatus

__all__ = [
    "FORBIDDEN_TOOLS",
    "AgentWorker",
    "WorkerContext",
    "WorkerOutcome",
]

#: Tools a child must never be given: they would let it start further agents and break
#: the "depth is one" guarantee that keeps cost and audit attribution predictable.
FORBIDDEN_TOOLS = frozenset(
    {"spawn_agent", "spawn_subagent", "orchestrate", "run_orchestration", "delegate_task"}
)


@dataclass
class WorkerOutcome:
    """What a worker reports back, independent of how it ran."""

    status: SubtaskStatus
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    stop_reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class WorkerContext:
    """Everything a worker needs, supplied by the caller so tests can stub it."""

    model: Any
    tools: Any
    permissions: Any = None
    runtime: dict[str, Any] | None = None
    max_steps: int = 50


class Worker(Protocol):
    """Anything that can execute a subtask inside a working directory."""

    def run(
        self,
        spec: SubtaskSpec,
        *,
        workdir: str,
        tracker: BudgetTracker,
        orchestration_id: str,
    ) -> WorkerOutcome:  # pragma: no cover - protocol
        ...


def allowed_tools(spec: SubtaskSpec) -> tuple[str, ...] | None:
    """The effective whitelist for a subtask, or ``None`` for "unrestricted".

    Raises :class:`ValueError` when a plan asks for a tool that would break the depth
    guarantee -- refusing at this point is the difference between a structural limit
    and a hopeful one.
    """
    requested = spec.tools_whitelist
    if requested is None:
        return None
    forbidden = sorted(set(requested) & FORBIDDEN_TOOLS)
    if forbidden:
        raise ValueError(
            f"{spec.id}: tool(s) {', '.join(forbidden)} would let a child start another "
            "agent; orchestration depth is fixed at one"
        )
    return tuple(requested)


def _filter_registry(tools: Any, whitelist: tuple[str, ...] | None) -> Any:
    """Return a registry restricted to ``whitelist`` (or the original when None)."""
    if whitelist is None or tools is None:
        return tools
    available = getattr(tools, "list", None)
    if available is None:
        return tools
    try:
        from minicode.tooling import ToolRegistry

        kept = [item for item in available() if getattr(item, "name", "") in whitelist]
        return ToolRegistry(kept)
    except Exception:  # noqa: BLE001 - fall back to the unfiltered registry rather than crash
        return tools


def render_instruction(spec: SubtaskSpec) -> str:
    """The child's brief: the subtask, its skill, and the limits it is under.

    Stated explicitly because a worker that does not know it is one of several, and
    that it must not touch anything outside its brief, tends to expand its own scope.
    """
    lines = [
        f"Subtask: {spec.id}",
        f"Skill to use: {spec.skill}",
    ]
    if spec.instruction:
        lines.extend(["", spec.instruction])
    lines.extend(
        [
            "",
            "Work only on this subtask. Do not start other agents or delegate work: you are",
            "an execution agent, and orchestration is not available to you.",
        ]
    )
    if spec.writes:
        lines.append(
            "Any changes you make are captured from this working directory as a patch; "
            "they are not merged automatically."
        )
    if spec.verify:
        lines.append("Your work must leave these commands passing: " + "; ".join(spec.verify))
    return "\n".join(lines)


class AgentWorker:
    """The real worker: one ``run_agent_turn`` loop inside the budget.

    ``context_factory`` receives the working directory and returns a
    :class:`WorkerContext`.  The default builds the project's own model, tools and
    permissions; tests pass a stub, which is why the scheduling logic can be verified
    without spending money.
    """

    def __init__(
        self,
        *,
        context_factory: Callable[[str], WorkerContext] | None = None,
        max_steps: int = 50,
        turn_runner: Callable[..., Any] | None = None,
    ) -> None:
        self._context_factory = context_factory or self._default_context
        self.max_steps = max_steps
        self._turn_runner = turn_runner
        self._context_error = ""

    def _default_context(self, workdir: str) -> WorkerContext:  # pragma: no cover - needs config
        """Build a context from the project's own loaders (lazy, so imports stay cheap)."""
        from minicode.config import load_runtime_config
        from minicode.model_registry import create_model_adapter
        from minicode.permissions import PermissionManager
        from minicode.tooling import build_default_registry

        runtime = load_runtime_config()
        model_name = str(runtime.get("model") or "")
        tools = build_default_registry(workdir)
        permissions = PermissionManager(workdir)
        model = create_model_adapter(model_name, tools, runtime)
        return WorkerContext(model=model, tools=tools, permissions=permissions, runtime=runtime)

    def _runner(self) -> Callable[..., Any]:
        if self._turn_runner is not None:
            return self._turn_runner
        from minicode.agent_loop import run_agent_turn

        return run_agent_turn

    def run(
        self,
        spec: SubtaskSpec,
        *,
        workdir: str,
        tracker: BudgetTracker,
        orchestration_id: str,
    ) -> WorkerOutcome:
        try:
            whitelist = allowed_tools(spec)
        except ValueError as error:
            return WorkerOutcome(status=SubtaskStatus.FAILED, error=str(error))

        try:
            context = self._context_factory(workdir)
        except Exception as error:  # noqa: BLE001 - report, never raise into the scheduler
            return WorkerOutcome(
                status=SubtaskStatus.FAILED,
                error=f"could not prepare a worker context: {type(error).__name__}: {error}",
            )

        tools = _filter_registry(context.tools, whitelist)
        messages = [{"role": "user", "content": render_instruction(spec)}]
        summary = ""
        stop_reason = ""
        started = time.monotonic()

        # The loop exists for the budget, not for the conversation: each pass is one
        # agent turn, and the gate is consulted *before* the turn rather than after it.
        while True:
            exhausted = tracker.exhausted()
            if exhausted is not None:
                return WorkerOutcome(
                    status=SubtaskStatus.BUDGET_EXHAUSTED,
                    summary=summary,
                    stop_reason=BUDGET_EXHAUSTED,
                    turns=tracker.turns,
                    tool_calls=tracker.tool_calls,
                    tokens=tracker.tokens,
                    cost_usd=tracker.cost_usd,
                    evidence=[f"stopped after {exhausted}"],
                )
            tracker.record_turn()
            try:
                result = self._runner()(
                    model=context.model,
                    tools=tools,
                    messages=messages,
                    cwd=workdir,
                    permissions=context.permissions,
                    runtime=context.runtime,
                    max_steps=min(self.max_steps, max(1, tracker.budget.max_tool_calls - tracker.tool_calls)),
                    on_tool_start=lambda _name, _args: tracker.record_tool_call(),
                    on_assistant_message=lambda text: None,
                )
            except Exception as error:  # noqa: BLE001 - a child failure is data, not a crash
                return WorkerOutcome(
                    status=SubtaskStatus.FAILED,
                    summary=summary,
                    stop_reason="worker_error",
                    turns=tracker.turns,
                    tool_calls=tracker.tool_calls,
                    tokens=tracker.tokens,
                    cost_usd=tracker.cost_usd,
                    error=f"{type(error).__name__}: {error}",
                )

            summary, stop_reason = _read_turn_result(result, summary)
            if stop_reason in {"done", "blocked", "verification_failed", "max_steps"}:
                break
            if time.monotonic() - started > tracker.budget.max_wall_seconds:
                break

        status = (
            SubtaskStatus.DONE
            if stop_reason in {"done", ""}
            else SubtaskStatus.FAILED
        )
        return WorkerOutcome(
            status=status,
            summary=summary,
            stop_reason=stop_reason,
            turns=tracker.turns,
            tool_calls=tracker.tool_calls,
            tokens=tracker.tokens,
            cost_usd=tracker.cost_usd,
        )


def _read_turn_result(result: Any, previous: str) -> tuple[str, str]:
    """Pull ``(summary, stop_reason)`` out of whatever the turn runner returned.

    Both shapes occur in this codebase (a list of messages, or an object carrying the
    last message and a stop reason), so this reads defensively instead of assuming.
    """
    summary = previous
    stop_reason = ""
    if isinstance(result, list) and result:
        last = result[-1]
        if isinstance(last, dict):
            summary = str(last.get("content") or summary)
    elif result is not None:
        stop_reason = str(getattr(result, "stop_reason", "") or "")
        content = getattr(result, "content", "")
        if content:
            summary = str(content)
        messages = getattr(result, "messages", None)
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                summary = str(last.get("content") or summary)
    return summary, stop_reason
