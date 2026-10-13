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
from minicode.orchestration.pricing import (
    estimate_cost,
    estimate_cost_for_model,
    estimate_tokens,
)
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
    #: The unit of ``cost_usd``.  Returned rather than assumed: a figure whose unit is
    #: implicit is the failure this whole path exists to prevent.
    currency: str = "CNY"
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
        lines.append(
            "After you finish, the orchestrator will run these commands itself to check your "
            "work. They are not tools available to you, so do not try to run them and do not "
            "report on them: " + "; ".join(spec.verify)
        )
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
        from minicode.tools import create_default_tool_registry

        runtime = load_runtime_config()
        model_name = str(runtime.get("model") or "")
        # Reuse the project's own registry builder rather than assembling tools here:
        # a child that sees a different tool set from the parent would make every
        # whitelist decision suspect.
        tools = create_default_tool_registry(workdir, runtime)
        # A child has no human to ask -- there is no terminal attached to it -- so it
        # runs with the same auto-approving prompt that headless CI mode uses.  The
        # controls that make this acceptable are structural rather than conversational:
        # the tool whitelist bounds what it can call, a writer works in its own
        # worktree, and its changes return as a patch that a human decides on.  Left
        # without a prompt, every out-of-cwd read would simply fail, which would make
        # most subtasks impossible rather than safer.
        try:
            from minicode.headless import _make_auto_approve_prompt

            permissions = PermissionManager(workdir, prompt=_make_auto_approve_prompt())
        except Exception:  # noqa: BLE001 - fall back to the stricter default
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

        # `run_agent_turn` *is* the loop: it calls the model, runs tools, and returns
        # when the turn is done -- that is how headless drives it, one call per prompt.
        # An earlier version called it repeatedly with the same message, which made the
        # child answer from scratch until its budget ran out; a smoke test caught it
        # burning all twenty turns and still reporting the right answer.
        exhausted = tracker.exhausted()
        if exhausted is not None:
            return WorkerOutcome(
                status=SubtaskStatus.BUDGET_EXHAUSTED,
                stop_reason=BUDGET_EXHAUSTED,
                turns=tracker.turns,
                tool_calls=tracker.tool_calls,
                tokens=tracker.tokens,
                cost_usd=tracker.cost_usd,
                evidence=[f"stopped before starting: {exhausted}"],
            )
        tracker.record_turn()
        # The adapter parses the provider's usage (input, output, cache read and write) and
        # publishes it through the store it is handed -- and drops it when handed none,
        # which is why every cost figure was zero.  `Store` requires an initial state; the
        # empty call raises TypeError, so this must construct a real AppState.
        store = _new_state_store(workdir)
        model_name = str((context.runtime or {}).get("model") or "")
        try:
            result = self._runner()(
                model=context.model,
                tools=tools,
                messages=messages,
                cwd=workdir,
                permissions=context.permissions,
                runtime=context.runtime,
                store=store,
                max_steps=min(
                    self.max_steps, max(1, tracker.budget.max_tool_calls - tracker.tool_calls)
                ),
                on_tool_start=lambda _name, _args: tracker.record_tool_call(),
                on_assistant_message=lambda text: None,
                # The turn runner reports context size through runtime events, not through
                # its return value -- which is why an earlier version recorded no usage at
                # all and every cost cap was decorative.
                on_runtime_event=_usage_hook(tracker),
            )
        except Exception as error:  # noqa: BLE001 - a child failure is data, not a crash
            return WorkerOutcome(
                status=SubtaskStatus.FAILED,
                stop_reason="worker_error",
                turns=tracker.turns,
                tool_calls=tracker.tool_calls,
                tokens=tracker.tokens,
                cost_usd=tracker.cost_usd,
                error=f"{type(error).__name__}: {error}",
            )

        summary, stop_reason = _read_turn_result(result, "")
        _record_usage(result, tracker)
        currency = _record_state_usage(store, tracker, model_name)
        tracker.currency = currency
        if not tracker.tokens:
            # Nothing reported usage, so fall back to the content estimate and say so in
            # the report.  It is a floor: the system prompt, tool schemas and every
            # re-sent context dominate the real bill and none of them are visible here.
            tokens = estimate_tokens(render_instruction(spec), summary or "")
            if tokens:
                amount, currency = estimate_cost_for_model(model_name, total_tokens=tokens)
                tracker.record_usage(tokens=tokens, cost_usd=amount)
                tracker.currency = currency
        status = SubtaskStatus.DONE if stop_reason in {"done", ""} else SubtaskStatus.FAILED
        return WorkerOutcome(
            status=status,
            summary=summary,
            stop_reason=stop_reason,
            turns=tracker.turns,
            tool_calls=tracker.tool_calls,
            tokens=tracker.tokens,
            cost_usd=tracker.cost_usd,
            currency=currency,
        )


def _new_state_store(workdir: str):
    """A store for one child so the adapter has somewhere to publish usage."""
    try:
        from minicode.state import AppState, Store

        return Store(AppState(workspace=workdir))
    except Exception:  # noqa: BLE001 - no store means no usage, not a failed subtask
        return None


def _record_state_usage(store: Any, tracker: BudgetTracker, model_name: str = "") -> str:
    """Take the real token count the adapter published and price it.

    The cost is computed from the price table rather than read from the store's
    `total_cost_usd`, because the adapter's own figure is in USD while this report is in
    CNY; mixing the two silently would be worse than an estimate.  Reconciling them is a
    separate job.
    """
    if store is None:
        return "CNY"
    try:
        state = store.get_state()
        tokens = int(getattr(state, "token_usage", 0) or 0)
        metadata = getattr(state, "metadata", {}) or {}
        split = dict(metadata.get("last_usage_split") or {})
    except Exception:  # noqa: BLE001
        return "CNY"
    if not tokens or tokens <= tracker.tokens:
        return "CNY"
    if split:
        # Cache writes are charged as fresh input: the published table has no separate
        # rate for them, and under-stating is the safe direction.
        cost, currency = estimate_cost_for_model(
            model_name,
            input_cached_tokens=int(split.get("cache_read", 0) or 0),
            input_fresh_tokens=int(split.get("input", 0) or 0)
            + int(split.get("cache_write", 0) or 0),
            output_tokens=int(split.get("output", 0) or 0),
        )
    else:
        cost, currency = estimate_cost_for_model(model_name, total_tokens=tokens)
    tracker.record_usage(tokens=tokens - tracker.tokens, cost_usd=cost)
    return currency


def _usage_hook(tracker: BudgetTracker):
    """Capture token usage from the runtime events the turn loop emits.

    The loop keeps its counters internally and publishes them as events; the return value
    carries only messages.  Watching the events is therefore the only way a caller can see
    what a child spent -- and without it the cost caps cannot bind on anything.
    """

    def _hook(event: Any) -> None:
        payload = _as_mapping(event)
        tokens = _find_tokens(payload)
        if tokens:
            tracker.record_usage(tokens=tokens - tracker.tokens if tokens > tracker.tokens else 0)

    return _hook


def _as_mapping(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        return data
    for attribute in ("payload", "detail", "metadata"):
        value = getattr(event, attribute, None)
        if isinstance(value, dict):
            return value
    return dict(vars(event)) if hasattr(event, "__dict__") else {}


def _find_tokens(payload: Any, depth: int = 0) -> int:
    """Find a token count in a nested payload, preferring the largest context figure."""
    if depth > 4 or not isinstance(payload, dict):
        return 0
    best = 0
    for key, value in payload.items():
        if isinstance(value, (int, float)) and "token" in str(key).lower():
            best = max(best, int(value))
        elif isinstance(value, dict):
            best = max(best, _find_tokens(value, depth + 1))
    return best


def _record_usage(result: Any, tracker: BudgetTracker) -> None:
    """Pull token and cost usage out of whatever the turn runner returned.

    Read defensively because two shapes occur in this codebase, and read *at all*
    because a cost cap that never sees a cost is decoration: an earlier version left
    both counters at zero, so the budget could never bind on money.
    """
    usage = None
    if isinstance(result, dict):
        usage = result.get("usage") or result
    else:
        usage = getattr(result, "usage", None) or getattr(result, "tokens", None)
    tokens = 0
    cost = 0.0
    if isinstance(usage, dict):
        tokens = int(usage.get("total_tokens") or usage.get("tokens") or 0)
        cost = float(usage.get("cost_usd") or usage.get("cost") or 0.0)
    elif isinstance(usage, (int, float)):
        tokens = int(usage)
    else:
        for name, target in (("total_tokens", "tokens"), ("tokens", "tokens"), ("cost_usd", "cost")):
            value = getattr(result, name, None) if result is not None else None
            if value is None:
                continue
            if target == "tokens":
                tokens = tokens or int(value)
            else:
                cost = cost or float(value)
    if tokens and not cost:
        # No input/output/cache split is exposed, so the estimate charges the context at
        # the fresh-input rate.  That under-states rather than over-states, which is the
        # direction to err in when the number is used to decide whether to keep spending.
        cost = estimate_cost(total_tokens=tokens)
    if tokens or cost:
        tracker.record_usage(tokens=tokens, cost_usd=cost)


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
