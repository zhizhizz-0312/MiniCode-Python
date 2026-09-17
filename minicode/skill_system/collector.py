"""Per-turn trajectory collection -- the raw material for the P3 distillation loop.

The distillation design needs evidence of *what actually happened*: which prompt
came in, which tools ran, what failed, how the turn ended.  None of that was
persisted before: ``TurnRecurrentState`` and ``TurnCodaSummary`` live only in
memory for the duration of ``run_agent_turn``, and ``DecisionAuditor`` writes a
fresh JSON file per session instead of an append stream.

Design constraints that shaped this module:

* **Append-only JSONL.**  One line per finished turn, appended under
  ``~/.mini-code/trajectories/<session_id>.jsonl``.  A crash mid-session loses at
  most the in-flight turn, and reading is a line scan rather than a full parse.
  (The only other append precedent in the repository is
  ``hooks.create_logging_hook``; everything else rewrites whole files.)
* **Exception-safe.**  Collection runs inside the agent loop's hot path, so every
  public entry point swallows its own errors.  Losing a data point must never
  break a user's session.  ``MINI_CODE_TRAJECTORY_DISABLED=1`` turns it off.
* **Truncated by default.**  Prompts and tool outputs are clipped before they
  reach disk: trajectories are training material, not a second transcript, and
  the privacy/disk cost of archiving full payloads is not worth it.

The collector accepts duck-typed ``TurnEvent``-like objects (``kind``, ``step``,
``tool_name``, ``is_error``, ``output``, ``content``, ``stop_reason``,
``runtime_event``) so it can be driven from the TUI event handler, from
``run_agent_turn(callbacks=...)``, or from a test without importing the CLI.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from minicode.config import MINI_CODE_DIR

__all__ = [
    "SCHEMA_VERSION",
    "TRAJECTORY_DIRNAME",
    "DISABLE_ENV",
    "MAX_PROMPT_CHARS",
    "MAX_CONTENT_CHARS",
    "TrajectoryStep",
    "TurnTrajectory",
    "TrajectoryCollector",
    "TrajectorySink",
    "trajectory_dir",
    "trajectory_path",
    "load_trajectories",
    "iter_trajectories",
    "format_summary",
]

SCHEMA_VERSION = 1
TRAJECTORY_DIRNAME = "trajectories"
DISABLE_ENV = "MINI_CODE_TRAJECTORY_DISABLED"

MAX_PROMPT_CHARS = 300
MAX_CONTENT_CHARS = 500

# Stream/thinking chunks arrive per token and carry no trajectory signal; the
# settled "assistant" event already holds the final text.
_IGNORED_KINDS = frozenset({"assistant_stream", "thinking"})
_RECORDED_KINDS = frozenset({"tool_start", "tool_result", "assistant", "progress", "runtime", "done"})


def _enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() in {"", "0", "false", "False"}


def trajectory_dir(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else MINI_CODE_DIR / TRAJECTORY_DIRNAME


def trajectory_path(session_id: str, path: str | Path | None = None) -> Path:
    """JSONL file for one session (``<dir>/<session_id>.jsonl``)."""
    return trajectory_dir(path) / f"{session_id or 'unknown'}.jsonl"


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


@dataclass
class TrajectoryStep:
    """One observation inside a turn."""

    index: int
    kind: str
    step: int | None = None
    tool_name: str = ""
    ok: bool | None = None
    content: str = ""
    stop_reason: str = ""
    phase: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "index": self.index,
            "kind": self.kind,
            "timestamp": self.timestamp,
        }
        if self.step is not None:
            payload["step"] = self.step
        if self.tool_name:
            payload["tool"] = self.tool_name
        if self.ok is not None:
            payload["ok"] = self.ok
        if self.content:
            payload["content"] = self.content
        if self.stop_reason:
            payload["stopReason"] = self.stop_reason
        if self.phase:
            payload["phase"] = self.phase
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TrajectoryStep":
        return cls(
            index=int(payload.get("index", 0)),
            kind=str(payload.get("kind", "")),
            step=payload.get("step"),
            tool_name=str(payload.get("tool", "")),
            ok=payload.get("ok"),
            content=str(payload.get("content", "")),
            stop_reason=str(payload.get("stopReason", "")),
            phase=str(payload.get("phase", "")),
            timestamp=float(payload.get("timestamp", 0.0)),
        )


@dataclass
class TurnTrajectory:
    """Everything one user turn produced, plus its outcome label."""

    session_id: str
    turn: int
    prompt: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    outcome: str = "unknown"  # success | failure | unknown
    stop_reason: str = ""
    task_state: str = ""
    model: str = ""
    matched_skills: list[str] = field(default_factory=list)
    steps: list[TrajectoryStep] = field(default_factory=list)

    # -- derived signals ---------------------------------------------------

    @property
    def tool_calls(self) -> list[str]:
        return [step.tool_name for step in self.steps if step.kind == "tool_start" and step.tool_name]

    @property
    def tool_errors(self) -> int:
        return sum(1 for step in self.steps if step.kind == "tool_result" and step.ok is False)

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "sessionId": self.session_id,
            "turn": self.turn,
            "prompt": self.prompt,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "outcome": self.outcome,
            "stopReason": self.stop_reason,
            "taskState": self.task_state,
            "model": self.model,
            "matchedSkills": list(self.matched_skills),
            "toolCalls": self.tool_calls,
            "toolErrors": self.tool_errors,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TurnTrajectory":
        raw_steps = payload.get("steps")
        steps = [
            TrajectoryStep.from_dict(item)
            for item in raw_steps
            if isinstance(item, dict)
        ] if isinstance(raw_steps, list) else []
        raw_skills = payload.get("matchedSkills")
        return cls(
            session_id=str(payload.get("sessionId", "")),
            turn=int(payload.get("turn", 0)),
            prompt=str(payload.get("prompt", "")),
            started_at=float(payload.get("startedAt", 0.0)),
            finished_at=float(payload.get("finishedAt", 0.0)),
            outcome=str(payload.get("outcome", "unknown")),
            stop_reason=str(payload.get("stopReason", "")),
            task_state=str(payload.get("taskState", "")),
            model=str(payload.get("model", "")),
            matched_skills=[str(item) for item in raw_skills] if isinstance(raw_skills, list) else [],
            steps=steps,
        )

    def summary_line(self) -> str:
        outcome = {"success": "ok", "failure": "FAILED"}.get(self.outcome, "?")
        return (
            f"turn {self.turn}: [{outcome}] {len(self.tool_calls)} tool call(s), "
            f"{self.tool_errors} error(s), stop={self.stop_reason or 'implicit'}, "
            f"{self.duration_seconds:.1f}s, prompt={self.prompt[:60]!r}"
        )


class TrajectoryCollector:
    """Accumulates one session's turns and appends them to a JSONL file."""

    def __init__(
        self,
        session_id: str,
        *,
        path: str | Path | None = None,
        model: str = "",
        enabled: bool | None = None,
    ) -> None:
        self.session_id = session_id or "unknown"
        self.path = trajectory_path(self.session_id, path)
        self.model = model
        self._enabled = _enabled() if enabled is None else enabled
        self._turn = self._load_turn_count()
        self._current: TurnTrajectory | None = None

    # -- state -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> TurnTrajectory | None:
        """The in-flight turn, if any."""
        return self._current

    @property
    def completed_turns(self) -> int:
        return self._turn

    def _load_turn_count(self) -> int:
        try:
            return sum(1 for _ in self._read_lines())
        except OSError:
            return 0

    def _read_lines(self) -> Iterator[str]:
        if not self.path.exists():
            return iter(())
        return iter(self.path.read_text(encoding="utf-8").splitlines())

    # -- recording ---------------------------------------------------------

    def begin_turn(self, prompt: str = "") -> TurnTrajectory:
        """Open a new turn (turn numbering continues across process restarts)."""
        self._turn += 1
        self._current = TurnTrajectory(
            session_id=self.session_id,
            turn=self._turn,
            prompt=_clip(prompt, MAX_PROMPT_CHARS),
            model=self.model,
        )
        return self._current

    def record_event(self, event: Any) -> TrajectoryStep | None:
        """Record one ``TurnEvent``-like object; returns the stored step or None."""
        if not self._enabled:
            return None
        try:
            return self._record_event(event)
        except Exception:  # noqa: BLE001 - never break the agent loop
            return None

    def _record_event(self, event: Any) -> TrajectoryStep | None:
        kind = str(getattr(event, "kind", "") or "")
        if kind in _IGNORED_KINDS or kind not in _RECORDED_KINDS:
            return None
        if self._current is None:
            self.begin_turn()

        turn = self._current
        assert turn is not None  # begin_turn above guarantees this
        runtime_event = getattr(event, "runtime_event", None)
        phase = str(getattr(runtime_event, "phase", "") or "") if runtime_event is not None else ""
        step = TrajectoryStep(
            index=len(turn.steps),
            kind=kind,
            step=getattr(event, "step", None),
            phase=phase,
        )
        if kind == "tool_start":
            step.tool_name = str(getattr(event, "tool_name", "") or "")
        elif kind == "tool_result":
            step.tool_name = str(getattr(event, "tool_name", "") or "")
            step.ok = not bool(getattr(event, "is_error", False))
            step.content = _clip(getattr(event, "output", ""), MAX_CONTENT_CHARS)
        elif kind == "assistant":
            step.content = _clip(getattr(event, "content", ""), MAX_CONTENT_CHARS)
        elif kind == "runtime":
            step.content = _clip(getattr(runtime_event, "message", ""), MAX_CONTENT_CHARS)
            step.stop_reason = str(getattr(runtime_event, "stop_reason", "") or "")
        elif kind == "done":
            step.stop_reason = str(getattr(event, "stop_reason", "") or "")

        turn.steps.append(step)
        return step

    def finalize(
        self,
        *,
        success: bool | None = None,
        stop_reason: str = "",
        task_state: str = "",
        matched_skills: Iterable[str] = (),
        flush: bool = True,
    ) -> TurnTrajectory | None:
        """Close the in-flight turn, label it, and append it to disk."""
        turn = self._current
        if turn is None:
            return None
        turn.finished_at = time.time()
        if stop_reason:
            turn.stop_reason = stop_reason
        elif not turn.stop_reason:
            done_step = next(
                (step for step in reversed(turn.steps) if step.kind == "done" and step.stop_reason),
                None,
            )
            turn.stop_reason = done_step.stop_reason if done_step else ""
        turn.task_state = task_state
        turn.matched_skills = list(matched_skills)
        if success is None:
            turn.outcome = _infer_outcome(turn)
        else:
            turn.outcome = "success" if success else "failure"
        self._current = None
        if flush:
            self.flush(turn)
        return turn

    def flush(self, turn: TurnTrajectory) -> Path | None:
        """Append one finished turn as a JSON line; never raises."""
        if not self._enabled:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(turn.to_dict(), ensure_ascii=False) + "\n")
            return self.path
        except Exception:  # noqa: BLE001
            return None


def _infer_outcome(turn: TurnTrajectory) -> str:
    """Label a turn when the caller could not supply a verdict.

    A recorded failure beats the absence of errors: an explicit error tool result
    or a non-``done`` stop reason means the turn did not cleanly succeed, and
    guessing "success" would poison the distillation corpus with false positives.
    """
    if turn.stop_reason and turn.stop_reason not in {"done"}:
        return "failure"
    if turn.tool_errors:
        return "failure"
    if any(step.kind == "done" for step in turn.steps) or turn.stop_reason == "done":
        return "success"
    return "unknown"


class TrajectorySink:
    """``AgentTurnEventSink`` adapter for ``run_agent_turn(callbacks=...)``.

    Wraps an optional inner sink so an existing consumer (the TUI's
    ``TurnEventQueue``, a test recorder) keeps receiving every event unchanged.
    """

    def __init__(self, collector: TrajectoryCollector, inner: Any | None = None) -> None:
        self.collector = collector
        self.inner = inner

    def on_event(self, event: Any) -> None:
        self.collector.record_event(event)
        if self.inner is not None:
            handler = getattr(self.inner, "on_event", None)
            if callable(handler):
                handler(event)


# ---------------------------------------------------------------------------
# Reading trajectories back
# ---------------------------------------------------------------------------


def iter_trajectories(
    session_id: str | None = None,
    *,
    path: str | Path | None = None,
) -> Iterator[TurnTrajectory]:
    """Yield stored turns, oldest first, skipping any unreadable line."""
    directory = trajectory_dir(path)
    if session_id:
        files = [trajectory_path(session_id, path)]
    else:
        files = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    for file_path in files:
        if not file_path.exists():
            continue
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield TurnTrajectory.from_dict(payload)


def load_trajectories(
    session_id: str | None = None,
    *,
    path: str | Path | None = None,
    limit: int | None = None,
) -> list[TurnTrajectory]:
    """Materialise stored trajectories, keeping the most recent ``limit``."""
    turns = list(iter_trajectories(session_id, path=path))
    if limit is not None and limit >= 0:
        return turns[-limit:]
    return turns


def format_summary(
    session_id: str | None = None,
    *,
    path: str | Path | None = None,
    limit: int = 20,
) -> str:
    """Readiness report for the distillation stage: is there usable data yet?"""
    turns = load_trajectories(session_id, path=path)
    header = ["Trajectory store", "=" * 60, f"Directory: {trajectory_dir(path)}"]
    if not turns:
        header.append("No trajectories recorded yet.")
        header.append("They are appended as each turn finishes (disable with " f"{DISABLE_ENV}=1).")
        return "\n".join(header)

    successes = sum(1 for turn in turns if turn.outcome == "success")
    failures = sum(1 for turn in turns if turn.outcome == "failure")
    unknown = len(turns) - successes - failures
    tool_counts: dict[str, int] = {}
    for turn in turns:
        for name in turn.tool_calls:
            tool_counts[name] = tool_counts.get(name, 0) + 1

    lines = [
        *header,
        f"Turns recorded: {len(turns)}",
        f"Outcomes: {successes} success, {failures} failure, {unknown} unknown",
        f"Tool calls: {sum(tool_counts.values())} "
        f"(errors: {sum(turn.tool_errors for turn in turns)})",
    ]
    if tool_counts:
        top = sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        lines.append("Most used tools: " + ", ".join(f"{name} x{count}" for name, count in top))
    matched = sorted({skill for turn in turns for skill in turn.matched_skills})
    if matched:
        lines.append("Skills seen in trajectories: " + ", ".join(matched))
    lines.append("")
    lines.append(f"Last {min(limit, len(turns))} turn(s):")
    lines.extend(f"  {turn.summary_line()}" for turn in turns[-limit:])
    return "\n".join(lines)
