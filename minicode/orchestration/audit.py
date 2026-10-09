"""The audit trail: one writer, one file, every action attributed.

The design decision this module implements is *centralised* orchestration, and the
reason is auditability rather than capability: with a single sink there is one trusted
source for "who did what, and what did it cost", failure attribution is a lookup
instead of a reconstruction, and the parent's own actions sit in the same sequence as
its children's.

Every entry therefore carries ``orchestrationId``, ``childId``, ``subtaskId`` and a
gap-free ``seq`` -- the fields that make attribution possible after the fact rather
than by inference.  Security decisions carry P2's ``ruleId`` through unchanged, and are
*also* written into P2's own trail, so the security history stays complete without
becoming a second, divergent source for orchestration events.

``replay`` is the payoff: a timeline, per-child totals, and cross-checks that the run
total matches the sum of its children and that every child which started also finished.
"Reconstructable" is a claim, so it is tested as one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from minicode.config import MINI_CODE_DIR

__all__ = [
    "AUDIT_FILENAME",
    "HOME_ENV",
    "AuditEntry",
    "OrchestrationAudit",
    "ReplayReport",
    "RunSummary",
    "audit_path",
    "format_replay",
    "format_timeline",
    "format_tree",
    "latest_orchestration",
    "list_orchestrations",
    "load_entries",
    "orchestration_dir",
    "replay",
    "summarise",
]

HOME_ENV = "MINI_CODE_ORCHESTRATION_HOME"
ORCHESTRATIONS_DIRNAME = "orchestrations"
AUDIT_FILENAME = "audit.jsonl"
START_EVENT = "child_start"
TERMINAL_EVENTS = frozenset({"child_end", "child_failed", "child_skipped"})
#: Events that *roll up* what already happened.  Their cost and token fields are
#: ignored when replaying: a summary that re-adds its parts doubles the run total, which
#: is exactly the kind of quiet arithmetic error an audit trail must not have.
SUMMARY_EVENTS = frozenset({"run_end"})


def _home(home: str | Path | None = None) -> Path:
    if home is not None:
        return Path(home)
    override = os.environ.get(HOME_ENV, "").strip()
    return Path(override) if override else MINI_CODE_DIR / ORCHESTRATIONS_DIRNAME


def orchestration_dir(orchestration_id: str, *, home: str | Path | None = None) -> Path:
    return _home(home) / orchestration_id


def audit_path(orchestration_id: str, *, home: str | Path | None = None) -> Path:
    return orchestration_dir(orchestration_id, home=home) / AUDIT_FILENAME


@dataclass(slots=True)
class AuditEntry:
    """One recorded action, attributed to whoever performed it."""

    seq: int
    event: str
    orchestration_id: str
    timestamp: float = 0.0
    child_id: str = ""
    subtask_id: str = ""
    tool: str = ""
    decision: str = ""
    rule_id: str = ""
    detail: str = ""
    tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event": self.event,
            "orchestrationId": self.orchestration_id,
            "childId": self.child_id,
            "subtaskId": self.subtask_id,
            "tool": self.tool,
            "decision": self.decision,
            "ruleId": self.rule_id,
            "detail": self.detail,
            "tokens": self.tokens,
            "costUsd": round(self.cost_usd, 6),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AuditEntry":
        return cls(
            seq=int(payload.get("seq") or 0),
            event=str(payload.get("event") or ""),
            orchestration_id=str(payload.get("orchestrationId") or ""),
            timestamp=float(payload.get("timestamp") or 0.0),
            child_id=str(payload.get("childId") or ""),
            subtask_id=str(payload.get("subtaskId") or ""),
            tool=str(payload.get("tool") or ""),
            decision=str(payload.get("decision") or ""),
            rule_id=str(payload.get("ruleId") or ""),
            detail=str(payload.get("detail") or ""),
            tokens=int(payload.get("tokens") or 0),
            cost_usd=float(payload.get("costUsd") or 0.0),
        )

    def describe(self) -> str:
        who = self.subtask_id or self.child_id or "main"
        bits = [f"#{self.seq:<4}", who, self.event]
        if self.tool:
            bits.append(f"tool={self.tool}")
        if self.decision:
            bits.append(f"decision={self.decision}")
        if self.rule_id:
            bits.append(f"rule={self.rule_id}")
        if self.detail:
            bits.append(self.detail[:80])
        return "  ".join(bits)


class OrchestrationAudit:
    """Append-only trail for one run.  Parent and children share this object."""

    def __init__(
        self,
        orchestration_id: str,
        *,
        home: str | Path | None = None,
        path: str | Path | None = None,
        enabled: bool = True,
        clock: Any = None,
    ) -> None:
        self.orchestration_id = orchestration_id
        self.path = Path(path) if path is not None else audit_path(orchestration_id, home=home)
        self.enabled = enabled
        self._clock = clock or time.time
        self._seq = 0
        self._buffer: list[AuditEntry] = []

    # -- writing -----------------------------------------------------------

    def record(
        self,
        event: str,
        *,
        child_id: str = "",
        subtask_id: str = "",
        tool: str = "",
        decision: str = "",
        rule_id: str = "",
        detail: str = "",
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> AuditEntry:
        """Record one action and return it (so callers can assert on what was written)."""
        self._seq += 1
        entry = AuditEntry(
            seq=self._seq,
            event=event,
            orchestration_id=self.orchestration_id,
            timestamp=self._clock(),
            child_id=child_id,
            subtask_id=subtask_id,
            tool=tool,
            decision=decision,
            rule_id=rule_id,
            detail=detail,
            tokens=tokens,
            cost_usd=cost_usd,
        )
        self._buffer.append(entry)
        if not self.enabled:
            return entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            # An unwritable audit file must not stop the work, but it also must not be
            # invisible: the entry stays in the buffer and shows up in the run report.
            pass
        return entry

    def record_result(self, child: Any) -> AuditEntry:
        """Terminal event for a child, carrying its cost so replay can add up."""
        status = getattr(child, "status", None)
        status_value = getattr(status, "value", str(status))
        event = "child_end" if status_value == "done" else (
            "child_skipped" if status_value == "skipped" else "child_failed"
        )
        return self.record(
            event,
            child_id=getattr(child, "subtask_id", ""),
            subtask_id=getattr(child, "subtask_id", ""),
            decision=status_value,
            detail=getattr(child, "summary", "") or getattr(child, "error", ""),
            tokens=int(getattr(child, "tokens", 0) or 0),
            cost_usd=float(getattr(child, "cost_usd", 0.0) or 0.0),
        )

    def record_security(
        self,
        decision: Any,
        *,
        child_id: str = "",
        subtask_id: str = "",
        tool: str = "",
    ) -> AuditEntry:
        """Record a security decision and mirror it into P2's trail.

        The orchestration trail needs the ``ruleId`` to explain *why* an action was
        stopped; P2's trail needs the event so the security history stays complete.
        Both are written, and the orchestration entry is the one replay reads.
        """
        entry = self.record(
            "security",
            child_id=child_id,
            subtask_id=subtask_id,
            tool=tool,
            decision=getattr(getattr(decision, "decision", ""), "value", "unknown"),
            rule_id=str(getattr(decision, "rule_id", "") or ""),
            detail=str(getattr(decision, "reason", "") or ""),
        )
        try:
            from minicode.security_chain import audit as security_audit

            security_audit.record(
                decision,
                kind="orchestration",
                tool_name=tool,
                session_id=self.orchestration_id,
                path=None,
            )
        except Exception:  # noqa: BLE001 - the mirror is best effort, never a failure
            pass
        return entry

    # -- reading -----------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    def buffered(self) -> list[AuditEntry]:
        return list(self._buffer)

    def entries(self) -> list[AuditEntry]:
        """Everything in the trail: the file, or the buffer when writing failed."""
        loaded = load_entries(self.path)
        return loaded or self.buffered()


@dataclass(slots=True)
class RunSummary:
    """A run as seen from its trail alone -- enough to list runs without their plans."""

    orchestration_id: str
    status: str = "unknown"
    events: int = 0
    children: int = 0
    cost_usd: float = 0.0
    updated_at: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.orchestration_id:28s} {self.status:10s} {self.children:3d} child(ren)  "
            f"${self.cost_usd:.4f}  {self.events} event(s)"
        )


def summarise(entries: Iterable[AuditEntry], orchestration_id: str = "") -> RunSummary:
    """Summarise one trail: the cheapest way to answer "what runs exist and how did they go"."""
    ordered = sorted(entries, key=lambda entry: entry.seq)
    summary = RunSummary(orchestration_id=orchestration_id or (ordered[0].orchestration_id if ordered else ""))
    summary.events = len(ordered)
    children = {
        entry.subtask_id or entry.child_id
        for entry in ordered
        if entry.event in {START_EVENT, *TERMINAL_EVENTS} and (entry.subtask_id or entry.child_id)
    }
    summary.children = len(children)
    summary.cost_usd = sum(entry.cost_usd for entry in ordered)
    summary.updated_at = max((entry.timestamp for entry in ordered), default=0.0)
    for entry in ordered:
        if entry.event == "run_end":
            summary.status = entry.decision or "ended"
    return summary


def list_orchestrations(*, home: str | Path | None = None) -> list[RunSummary]:
    """Every run with a trail, newest first."""
    root = _home(home)
    if not root.exists():
        return []
    summaries: list[RunSummary] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        summaries.append(summarise(load_entries(entry / AUDIT_FILENAME), entry.name))
    summaries.sort(key=lambda item: item.updated_at, reverse=True)
    return summaries


def latest_orchestration(*, home: str | Path | None = None) -> str:
    runs = list_orchestrations(home=home)
    return runs[0].orchestration_id if runs else ""


def load_entries(path: str | Path, *, limit: int | None = None) -> list[AuditEntry]:
    """Read a trail, skipping unreadable lines rather than failing the replay."""
    target = Path(path)
    if not target.exists():
        return []
    entries: list[AuditEntry] = []
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(AuditEntry.from_dict(payload))
    if limit is not None:
        return entries[-limit:]
    return entries


@dataclass
class ReplayReport:
    """The reconstruction, plus the cross-checks that make it trustworthy."""

    entries: list[AuditEntry] = field(default_factory=list)
    per_child: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    started: tuple[str, ...] = ()
    finished: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    mismatches: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.gaps and not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "perChild": self.per_child,
            "totalCostUsd": round(self.total_cost_usd, 6),
            "totalTokens": self.total_tokens,
            "started": list(self.started),
            "finished": list(self.finished),
            "gaps": list(self.gaps),
            "mismatches": list(self.mismatches),
            "consistent": self.consistent,
        }


#: Fallback labels for terminal events whose entry carries no decision -- a hand-written
#: or older entry should still read as a status, not as an event name.
_TERMINAL_LABEL = {
    "child_end": "done",
    "child_failed": "failed",
    "child_skipped": "skipped",
}


def replay(
    entries: Iterable[AuditEntry],
    *,
    expected_cost_usd: float | None = None,
    expected_children: Iterable[str] | None = None,
) -> ReplayReport:
    """Reconstruct a run from its trail and check that it holds together.

    The checks are the point.  A trail that merely *looks* complete is worth little:
    what matters is that every child which started also reported, and that the run
    total equals the sum of its children -- the two ways an audit chain silently lies.
    """
    ordered = sorted(entries, key=lambda entry: entry.seq)
    report = ReplayReport(entries=ordered)
    started: list[str] = []
    finished: list[str] = []

    for entry in ordered:
        who = entry.subtask_id or entry.child_id or "main"
        bucket = report.per_child.setdefault(
            who, {"events": 0, "costUsd": 0.0, "tokens": 0, "terminal": ""}
        )
        bucket["events"] += 1
        if entry.event not in SUMMARY_EVENTS:
            report.total_cost_usd += entry.cost_usd
            report.total_tokens += entry.tokens
        if entry.event == START_EVENT and who != "main":
            started.append(who)
        if entry.event in TERMINAL_EVENTS and who != "main":
            finished.append(who)
            bucket["terminal"] = entry.decision or _TERMINAL_LABEL.get(entry.event, entry.event)
            bucket["costUsd"] += entry.cost_usd
            bucket["tokens"] += entry.tokens

    report.started = tuple(started)
    report.finished = tuple(finished)
    report.gaps = tuple(sorted(set(started) - set(finished)))

    if expected_children is not None:
        expected = set(expected_children)
        missing = sorted(expected - (set(started) | set(finished)))
        if missing:
            report.mismatches += tuple(f"no audit events for {name}" for name in missing)
    if expected_cost_usd is not None and abs(report.total_cost_usd - expected_cost_usd) > 1e-6:
        report.mismatches += (
            f"trail total ${report.total_cost_usd:.6f} does not match the run total "
            f"${expected_cost_usd:.6f}",
        )
    return report


def format_timeline(report: ReplayReport, *, limit: int | None = None) -> str:
    lines = [
        "Orchestration replay (timeline)",
        "=" * 72,
        f"entries: {len(report.entries)}   children: {len(report.per_child)}",
        f"cost: ${report.total_cost_usd:.6f}   tokens: {report.total_tokens}",
        "",
    ]
    shown = report.entries if limit is None else report.entries[:limit]
    lines.extend(f"  {entry.describe()}" for entry in shown)
    if limit is not None and len(report.entries) > limit:
        lines.append(f"  ... ({len(report.entries) - limit} more)")
    return "\n".join(lines)


def format_tree(report: ReplayReport) -> str:
    lines = [
        "Orchestration replay (attribution)",
        "=" * 72,
    ]
    for name, bucket in sorted(report.per_child.items()):
        terminal = bucket["terminal"] or "(no terminal event)"
        lines.append(
            f"  {name:28s} {bucket['events']:3d} event(s)  {terminal:16s} "
            f"${bucket['costUsd']:.6f}  {bucket['tokens']} tokens"
        )
    return "\n".join(lines)


def format_replay(report: ReplayReport) -> str:
    """The full rendering: both views plus the cross-checks."""
    lines = [format_timeline(report), "", format_tree(report), ""]
    lines.append("Consistency:")
    if report.consistent:
        lines.append("  every started subtask reported, and the totals add up")
    for gap in report.gaps:
        lines.append(f"  GAP: {gap} started but never reported a terminal event")
    for mismatch in report.mismatches:
        lines.append(f"  MISMATCH: {mismatch}")
    return "\n".join(lines)
