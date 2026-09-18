"""Shared vocabulary for the distillation loop.

The loop moves data through four shapes, and naming them once keeps the modules
readable:

``TrajectoryRef``
    A distilled-down view of one recorded turn -- enough to mine with, without
    carrying the whole step list around.
``FeedbackSignal``
    Something that says "that was wrong" (an explicit correction, a failed turn, a
    blocked action).  Signals are what make a cluster worth distilling.
``CandidateCluster``
    A group of similar trajectories that passed the mining gate, plus the
    confidence the *evidence* supports before any model is asked.
``SkillDraft``
    A proposed skill, its provenance, and its review state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "DRAFTS_DIRNAME",
    "STATE_FILENAME",
    "CandidateCluster",
    "DraftStatus",
    "FeedbackSignal",
    "SkillDraft",
    "TrajectoryRef",
]

DRAFTS_DIRNAME = "skill-drafts"
STATE_FILENAME = "distillation-state.json"


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(slots=True)
class TrajectoryRef:
    """One recorded turn, reduced to what mining needs."""

    session_id: str
    turn: int
    prompt: str
    outcome: str = "unknown"  # success | failure | unknown
    stop_reason: str = ""
    tool_calls: tuple[str, ...] = ()
    tool_errors: int = 0
    correction: str = ""  # the user's correction, when one was detected
    synthetic: bool = False
    started_at: float = 0.0  # ordering matters: "corrected, then succeeded"

    @property
    def failed(self) -> bool:
        return self.outcome == "failure" or bool(self.stop_reason and self.stop_reason != "done")

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"

    @property
    def cost_signal(self) -> int:
        """Tool calls, the quantity the acceptance metric measures."""
        return len(self.tool_calls)

    def describe(self) -> str:
        marker = "ok" if self.succeeded else ("fail" if self.failed else "?")
        return (
            f"{self.session_id}#{self.turn} [{marker}] {self.cost_signal} tool call(s), "
            f"{self.tool_errors} error(s): {self.prompt[:60]}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "turn": self.turn,
            "prompt": self.prompt,
            "outcome": self.outcome,
            "stopReason": self.stop_reason,
            "toolCalls": list(self.tool_calls),
            "toolErrors": self.tool_errors,
            "correction": self.correction,
            "synthetic": self.synthetic,
        }


@dataclass(frozen=True, slots=True)
class FeedbackSignal:
    """One reason to look closer at a trajectory."""

    kind: str  # explicit_correction | failed_turn | tool_errors | blocked_action
    session_id: str
    turn: int
    detail: str = ""

    def describe(self) -> str:
        return f"{self.kind} at {self.session_id}#{self.turn}: {self.detail}"


@dataclass
class CandidateCluster:
    """Similar trajectories that passed the mining gate."""

    signature: str
    trajectories: list[TrajectoryRef] = field(default_factory=list)
    signals: list[FeedbackSignal] = field(default_factory=list)
    reason: str = ""

    @property
    def size(self) -> int:
        return len(self.trajectories)

    @property
    def successes(self) -> list[TrajectoryRef]:
        return [ref for ref in self.trajectories if ref.succeeded]

    @property
    def failures(self) -> list[TrajectoryRef]:
        return [ref for ref in self.trajectories if ref.failed]

    @property
    def corrections(self) -> list[TrajectoryRef]:
        return [ref for ref in self.trajectories if ref.correction]

    @property
    def mean_cost(self) -> float:
        if not self.trajectories:
            return 0.0
        return sum(ref.cost_signal for ref in self.trajectories) / len(self.trajectories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "size": self.size,
            "reason": self.reason,
            "trajectories": [ref.to_dict() for ref in self.trajectories],
        }


@dataclass
class SkillDraft:
    """A proposed skill awaiting review."""

    id: str
    name: str
    description: str = ""
    triggers: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    risk_level: str = "low"
    steps: tuple[str, ...] = ()
    confidence: float = 0.0
    prior_confidence: float = 0.0
    rationale: str = ""
    skill_markdown: str = ""
    status: DraftStatus = DraftStatus.PENDING
    created_at: float = 0.0
    sources: list[dict[str, Any]] = field(default_factory=list)
    cluster_signature: str = ""
    issues: tuple[str, ...] = ()
    security_note: str = ""
    review_note: str = ""

    @property
    def pending(self) -> bool:
        return self.status is DraftStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "tools": list(self.tools),
            "riskLevel": self.risk_level,
            "steps": list(self.steps),
            "confidence": round(self.confidence, 3),
            "priorConfidence": round(self.prior_confidence, 3),
            "rationale": self.rationale,
            "status": self.status.value,
            "createdAt": self.created_at,
            "sources": self.sources,
            "clusterSignature": self.cluster_signature,
            "issues": list(self.issues),
            "securityNote": self.security_note,
            "reviewNote": self.review_note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillDraft":
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            triggers=tuple(str(item) for item in payload.get("triggers") or ()),
            tools=tuple(str(item) for item in payload.get("tools") or ()),
            risk_level=str(payload.get("riskLevel") or "low"),
            steps=tuple(str(item) for item in payload.get("steps") or ()),
            confidence=float(payload.get("confidence") or 0.0),
            prior_confidence=float(payload.get("priorConfidence") or 0.0),
            rationale=str(payload.get("rationale") or ""),
            status=DraftStatus(str(payload.get("status") or "pending")),
            created_at=float(payload.get("createdAt") or 0.0),
            sources=list(payload.get("sources") or []),
            cluster_signature=str(payload.get("clusterSignature") or ""),
            issues=tuple(str(item) for item in payload.get("issues") or ()),
            security_note=str(payload.get("securityNote") or ""),
            review_note=str(payload.get("reviewNote") or ""),
        )
