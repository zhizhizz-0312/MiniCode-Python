"""Shared vocabulary for the security chain.

Every layer speaks these types, so they live in one module rather than being
redefined per stage (and rather than being imported from ``__init__``, which
would create a cycle).

The two *request* types encode the design's two boundaries:

* :class:`ContentRequest` -- untrusted content about to enter the context
  (a tool result, a memory entry, a fetched page).  Judged on the way in.
* :class:`ActionRequest` -- a tool call about to execute.  Judged on the way out,
  together with its taint state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "MODE_ENV",
    "MODEL_ENV",
    "Decision",
    "ContentRequest",
    "ActionRequest",
    "RiskLevel",
    "SecurityDecision",
    "SecurityMode",
    "Stage",
    "mode_from_env",
    "is_read_only_tool",
    "tool_has_side_effects",
]

MODE_ENV = "MINI_CODE_SECURITY_MODE"
MODEL_ENV = "MINI_CODE_SECURITY_MODEL"

#: Tools that only observe.  A read-only action never needs a human gate, which
#: is what keeps the chain from interrupting ordinary work (invariant N1).
_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep_files",
        "web_fetch",
        "web_search",
        "load_skill",
    }
)

#: Tools whose effects leave the process: they write, execute or send.  These are
#: the actions that get escalated when the chain is unsure (invariant N2 keeps
#: read-only work flowing even when the classifier is unavailable).
_SIDE_EFFECTING_TOOLS = frozenset(
    {
        "write_file",
        "modify_file",
        "edit_file",
        "patch_file",
        "run_command",
        "ask_user",
    }
)


class SecurityMode(str, Enum):
    """How much of the chain runs."""

    OFF = "off"  # bypass everything (tests, troubleshooting)
    HEURISTIC = "heuristic"  # L1 + L2 + feature channel only, no model calls
    STANDARD = "standard"  # model channel on suspicion (default)
    STRICT = "strict"  # model channel on every side-effecting action


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"  # hand to L4, the human gate


class Stage(str, Enum):
    """Which layer produced the verdict -- recorded so every decision is auditable."""

    BYPASS = "bypass"
    CACHE = "cache"
    L1_RULES = "l1_rules"
    L2_PREFLIGHT = "l2_preflight"
    L3_CLASSIFIER = "l3_classifier"
    L4_CONFIRM = "l4_confirm"


def mode_from_env(default: SecurityMode = SecurityMode.STANDARD) -> SecurityMode:
    """Read the configured mode, falling back to ``default`` on anything unknown."""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    if not raw:
        return default
    try:
        return SecurityMode(raw)
    except ValueError:
        return default


def is_read_only_tool(tool_name: str) -> bool:
    return tool_name in _READ_ONLY_TOOLS


def tool_has_side_effects(tool_name: str) -> bool:
    """Unknown tools are treated as side-effecting: fail closed on novelty."""
    if tool_name in _READ_ONLY_TOOLS:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ContentRequest:
    """Untrusted content about to enter the context (entry boundary)."""

    text: str
    source: str = "unknown"  # tool_result | memory | web | user
    tool_name: str = ""
    session_id: str = ""
    truncated: bool = False

    def excerpt(self, limit: int = 2000) -> str:
        if len(self.text) <= limit:
            return self.text
        return self.text[:limit]


@dataclass(slots=True)
class ActionRequest:
    """A tool call about to execute (exit boundary)."""

    tool_name: str
    tool_input: Any = field(default_factory=dict)
    cwd: str = ""
    session_id: str = ""
    tainted: bool = False  # parameters derive from untrusted content
    taint_sources: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return is_read_only_tool(self.tool_name)

    @property
    def has_side_effects(self) -> bool:
        return tool_has_side_effects(self.tool_name)

    def signature(self) -> str:
        """Stable identity for caching a verdict about this exact action."""
        import hashlib
        import json

        try:
            payload = json.dumps(self.tool_input, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(self.tool_input)
        return hashlib.sha256(f"{self.tool_name}\x00{payload}".encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class SecurityDecision:
    """The verdict for one request, with everything needed to explain and audit it."""

    decision: Decision
    risk: RiskLevel
    stage: Stage
    reason: str
    rule_id: str = ""
    evidence: str = ""
    category: str = ""
    channel_a: float | None = None
    channel_b: str = ""
    disagreement: bool = False
    from_cache: bool = False
    escalated_to_human: bool = False
    latency_ms: float = 0.0

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.DENY

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "risk": self.risk.value,
            "stage": self.stage.value,
            "reason": self.reason,
            "ruleId": self.rule_id,
            "evidence": self.evidence,
            "category": self.category,
            "channelA": self.channel_a,
            "channelB": self.channel_b,
            "disagreement": self.disagreement,
            "fromCache": self.from_cache,
            "escalatedToHuman": self.escalated_to_human,
            "latencyMs": round(self.latency_ms, 3),
        }

    def format_short(self) -> str:
        return f"[{self.decision.value}/{self.risk.value}@{self.stage.value}] {self.reason}"
