"""Hard budgets: the reason a multi-agent loop cannot quietly burn money.

Two layers, because they fail differently.  A **child** budget bounds one execution
agent (turns, tool calls, tokens, wall clock, cost); an **orchestration** budget
bounds the whole run (how many children may ever be started, how many at once, and
the total spend).  Without the second layer, ten children each inside their own
budget still add up to ten times the cost.

Three rules make this a gate rather than a report:

* the check happens **before** the next model or tool call, on the live counters,
  so a child cannot overshoot by one expensive step;
* exhaustion produces a **status**, not an exception -- the child stops with
  ``budget_exhausted`` and keeps whatever it already produced, because partial work
  plus an honest label is more useful than a traceback and nothing;
* the reason string names the dimension that ran out, so the report can say *which*
  budget was hit instead of "it stopped".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUDGET_EXHAUSTED",
    "Budget",
    "BudgetTracker",
    "OrchestrationBudget",
]

#: Stop reason recorded when a child stops because its budget ran out.  Distinct
#: from a failure: the work is partial, not wrong.
BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(slots=True)
class Budget:
    """Caps for a single execution agent."""

    max_turns: int = 20
    max_tool_calls: int = 50
    max_tokens: int = 60_000
    max_wall_seconds: float = 300.0
    max_cost_usd: float = 0.10

    def to_dict(self) -> dict[str, Any]:
        return {
            "maxTurns": self.max_turns,
            "maxToolCalls": self.max_tool_calls,
            "maxTokens": self.max_tokens,
            "maxWallSeconds": self.max_wall_seconds,
            "maxCostUsd": self.max_cost_usd,
        }

    @classmethod
    def from_dict(cls, payload: object, *, base: "Budget | None" = None) -> "Budget":
        """Build from a plan's overrides, falling back to ``base`` (or the defaults)."""
        budget = base or cls()
        if not isinstance(payload, dict):
            return budget
        def _num(key: str, fallback: float) -> float:
            value = payload.get(key)
            try:
                return float(value) if value is not None else fallback
            except (TypeError, ValueError):
                return fallback

        return cls(
            max_turns=int(_num("maxTurns", budget.max_turns)),
            max_tool_calls=int(_num("maxToolCalls", budget.max_tool_calls)),
            max_tokens=int(_num("maxTokens", budget.max_tokens)),
            max_wall_seconds=_num("maxWallSeconds", budget.max_wall_seconds),
            max_cost_usd=_num("maxCostUsd", budget.max_cost_usd),
        )


class BudgetTracker:
    """Live counters for one child, checked before every step."""

    def __init__(self, budget: Budget, *, clock: Any = None) -> None:
        import time

        self.budget = budget
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.turns = 0
        self.tool_calls = 0
        self.tokens = 0
        self.cost_usd = 0.0

    # -- accounting --------------------------------------------------------

    def record_turn(self) -> None:
        self.turns += 1

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_usage(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.tokens += max(0, int(tokens))
        self.cost_usd += max(0.0, float(cost_usd))

    @property
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started_at)

    # -- the gate ----------------------------------------------------------

    def exhausted(self) -> str | None:
        """Return the reason to stop, or ``None`` to continue.

        Called before a step rather than after it: a budget that is only consulted
        afterwards is a receipt, not a limit.
        """
        budget = self.budget
        if self.turns >= budget.max_turns:
            return f"turns: {self.turns}/{budget.max_turns}"
        if self.tool_calls >= budget.max_tool_calls:
            return f"tool calls: {self.tool_calls}/{budget.max_tool_calls}"
        if self.tokens >= budget.max_tokens:
            return f"tokens: {self.tokens}/{budget.max_tokens}"
        if self.elapsed >= budget.max_wall_seconds:
            return f"wall clock: {self.elapsed:.0f}s/{budget.max_wall_seconds:.0f}s"
        if self.cost_usd >= budget.max_cost_usd:
            return f"cost: ${self.cost_usd:.4f}/${budget.max_cost_usd:.4f}"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "toolCalls": self.tool_calls,
            "tokens": self.tokens,
            "costUsd": round(self.cost_usd, 6),
            "elapsedSeconds": round(self.elapsed, 3),
            "exhausted": self.exhausted(),
        }


@dataclass
class OrchestrationBudget:
    """Caps across a whole run, plus the fan-out arithmetic."""

    max_children: int = 10
    max_concurrent: int = 3
    max_cost_usd: float = 0.50
    child_defaults: Budget = field(default_factory=Budget)

    started_children: int = 0
    spent_usd: float = 0.0

    def can_start_child(self) -> str | None:
        """Reason a new child may not start, or ``None``."""
        if self.started_children >= self.max_children:
            return f"children: {self.started_children}/{self.max_children}"
        if self.spent_usd >= self.max_cost_usd:
            return f"cost: ${self.spent_usd:.4f}/${self.max_cost_usd:.4f}"
        return None

    def start_child(self) -> BudgetTracker:
        """Reserve a child slot and hand back its tracker.

        The child inherits ``child_defaults``; the orchestrator applies per-subtask
        overrides before the child runs, so the reservation itself is never
        unbounded.
        """
        reason = self.can_start_child()
        if reason is not None:
            raise RuntimeError(f"orchestration budget exhausted ({reason})")
        self.started_children += 1
        return BudgetTracker(self.child_defaults)

    def absorb(self, tracker: BudgetTracker) -> None:
        """Fold a finished child's spend into the run total."""
        self.spent_usd += tracker.cost_usd

    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd - self.spent_usd)

    def snapshot(self) -> dict[str, Any]:
        return {
            "children": self.started_children,
            "maxChildren": self.max_children,
            "maxConcurrent": self.max_concurrent,
            "spentUsd": round(self.spent_usd, 6),
            "maxCostUsd": self.max_cost_usd,
            "remainingUsd": round(self.remaining_usd(), 6),
            "blocked": self.can_start_child(),
        }
