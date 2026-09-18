"""Feedback signals: what in a trajectory says "that attempt is worth learning from".

Three sources, in descending order of trustworthiness:

* **explicit correction** -- the user said "no, do it this way".  The strongest
  signal available, and the one the design document's acceptance scenario is built
  on ("corrected once, then meets the same task again").
* **task failure** -- the turn ended in ``failure``/``blocked``/``max_steps``, or
  accumulated tool errors.  Says "this went badly", not "here is the right way".
* **blocked action** -- the security chain refused something.  The path was wrong,
  and the record of *what* was refused is useful negative evidence.

Detection of a correction is deliberately conservative: it looks for the shapes a
human uses when rejecting work ("不对", "应该", "别", "重新", "instead", "should
have"), not for sentiment.  A missed correction costs one candidate; a false one
puts noise into the corpus the distiller learns from.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from minicode.distillation.types import FeedbackSignal, TrajectoryRef

__all__ = [
    "CORRECTION_MARKERS",
    "collect_signals",
    "detect_correction",
    "load_trajectory_refs",
    "trajectory_to_ref",
]

#: Shapes a human uses when they reject work.  Both languages, since this project
#: is used in both, and conservative on purpose.
CORRECTION_MARKERS = re.compile(
    r"(不对|不是这样|别这样|别直接|别急着|别乱|不应该|不该|应该先|应该用|重来|重新做|错了"
    r"|你会错意|理解错|我没让你|谁让你|不要再|以后别|注意这个项目"
    r"|that'?s (not|wrong)|not what i|instead|should have|don'?t do that|no, |stop doing"
    r"|you (misunderstood|missed)|try again|redo)",
    re.IGNORECASE,
)


def detect_correction(prompt: str) -> str:
    """Return the matched correction marker, or an empty string.

    The matched text is kept rather than a boolean: a rejected draft should be
    explainable, and "which words made us think this was a correction" is part of
    that explanation.
    """
    if not prompt:
        return ""
    match = CORRECTION_MARKERS.search(prompt)
    return match.group(0) if match else ""


def trajectory_to_ref(payload: dict[str, Any]) -> TrajectoryRef:
    """Turn one stored trajectory record into a mining-friendly reference."""
    prompt = str(payload.get("prompt") or "")
    tool_calls = payload.get("toolCalls")
    if not isinstance(tool_calls, list):
        tool_calls = [
            str(step.get("tool"))
            for step in payload.get("steps") or []
            if isinstance(step, dict) and step.get("kind") == "tool_start" and step.get("tool")
        ]
    try:
        tool_errors = int(payload.get("toolErrors") or 0)
    except (TypeError, ValueError):
        tool_errors = 0
    return TrajectoryRef(
        session_id=str(payload.get("sessionId") or "unknown"),
        turn=int(payload.get("turn") or 0),
        prompt=prompt,
        outcome=str(payload.get("outcome") or "unknown"),
        stop_reason=str(payload.get("stopReason") or ""),
        tool_calls=tuple(str(name) for name in tool_calls),
        tool_errors=tool_errors,
        correction=detect_correction(prompt),
        synthetic=bool(payload.get("synthetic")),
        started_at=float(payload.get("startedAt") or 0.0),
    )


def load_trajectory_refs(
    session_id: str | None = None,
    *,
    path: str | Path | None = None,
    trajectories: Iterable[dict[str, Any]] | None = None,
) -> list[TrajectoryRef]:
    """Load trajectory references from the collector's store (or from given records)."""
    if trajectories is not None:
        return [trajectory_to_ref(item) for item in trajectories]
    from minicode.skill_system.collector import load_trajectories

    turns = load_trajectories(session_id, path=path)
    return [trajectory_to_ref(turn.to_dict()) for turn in turns]


def collect_signals(refs: Iterable[TrajectoryRef]) -> list[FeedbackSignal]:
    """Everything in these trajectories that argues for distilling them."""
    signals: list[FeedbackSignal] = []
    for ref in refs:
        if ref.correction:
            signals.append(
                FeedbackSignal(
                    kind="explicit_correction",
                    session_id=ref.session_id,
                    turn=ref.turn,
                    detail=f"user correction marker {ref.correction!r}",
                )
            )
        if ref.failed:
            signals.append(
                FeedbackSignal(
                    kind="failed_turn",
                    session_id=ref.session_id,
                    turn=ref.turn,
                    detail=f"outcome={ref.outcome} stop={ref.stop_reason or 'implicit'}",
                )
            )
        if ref.tool_errors:
            signals.append(
                FeedbackSignal(
                    kind="tool_errors",
                    session_id=ref.session_id,
                    turn=ref.turn,
                    detail=f"{ref.tool_errors} tool error(s)",
                )
            )
    return signals
