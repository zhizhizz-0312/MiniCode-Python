"""Audit trail for every security verdict.

Two sinks, on purpose:

* the repository's own ``DecisionAuditor`` -- which existed but had never been
  called by anything (``~/.mini-code/audit/`` was an empty directory).  The
  security chain is what finally makes that audit chain real, which also
  satisfies the "every review decision is auditable" requirement.
* a dedicated append-only JSONL at ``~/.mini-code/security-audit.jsonl`` so the
  trail can be read back line by line (for the evaluation report and for
  ``/security``) without depending on the auditor's per-session file model.

Both writes are best-effort: auditing must never break a turn, and it must never
be the reason an action fails.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from minicode.config import MINI_CODE_DIR
from minicode.security_chain.types import SecurityDecision

__all__ = [
    "AUDIT_FILENAME",
    "DISABLE_ENV",
    "PATH_ENV",
    "audit_path",
    "record",
    "load",
    "format_report",
    "clear",
]

AUDIT_FILENAME = "security-audit.jsonl"
PATH_ENV = "MINI_CODE_SECURITY_AUDIT_PATH"
DISABLE_ENV = "MINI_CODE_SECURITY_AUDIT_DISABLED"
_MAX_EVIDENCE_CHARS = 300


def audit_path(path: str | Path | None = None) -> Path:
    """Resolve the audit file: explicit path, then env override, then defaults."""
    if path is not None:
        return Path(path)
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override) if override else MINI_CODE_DIR / AUDIT_FILENAME


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in {"", "0", "false", "False"}


def _clip(text: Any, limit: int = _MAX_EVIDENCE_CHARS) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def record(
    decision: SecurityDecision,
    *,
    kind: str,
    tool_name: str = "",
    session_id: str = "",
    tainted: bool = False,
    extra: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> None:
    """Append one verdict to both sinks; never raises."""
    if _disabled():
        return
    entry = {
        "kind": kind,  # content | action
        "tool": tool_name,
        "sessionId": session_id,
        "tainted": tainted,
        **decision.to_dict(),
    }
    if extra:
        entry.update({key: _clip(value) for key, value in extra.items()})

    # Sink 1: the append-only JSONL used by the report and the CLI.
    try:
        target = audit_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # Sink 2: the repository's audit chain (previously unused).
    try:
        from minicode.decision_audit import DecisionType, get_auditor

        auditor = get_auditor()
        if session_id:
            auditor.start_session(session_id)
        record_obj = auditor.record(
            DecisionType.PERMISSION,
            reasoning=decision.reason,
            selected_option=decision.decision.value,
            available_options=["allow", "escalate", "deny"],
            input_context={
                "kind": kind,
                "tool": tool_name,
                "risk": decision.risk.value,
                "stage": decision.stage.value,
                "ruleId": decision.rule_id,
                "evidence": _clip(decision.evidence, 200),
                "channelA": decision.channel_a,
                "channelB": decision.channel_b,
                "disagreement": decision.disagreement,
                "tainted": tainted,
            },
            confidence=1.0 if decision.decision.value == "deny" else 0.5,
        )
        # The auditor records the outcome separately; keep the chain consistent.
        try:
            from minicode.decision_audit import DecisionOutcome

            record_id = getattr(record_obj, "id", "")
            if record_id:
                auditor.update_outcome(
                    record_id,
                    DecisionOutcome.SUCCESS
                    if decision.decision.value == "allow"
                    else DecisionOutcome.FAILURE,
                    result_summary=decision.rule_id or decision.stage.value,
                )
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - the audit chain is an add-on, not a dependency
        pass


def load(path: str | Path | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Read audit entries back, oldest first, skipping unreadable lines."""
    target = audit_path(path)
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    if limit is not None and limit >= 0:
        return entries[-limit:]
    return entries


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def format_report(*, path: str | Path | None = None, limit: int = 10) -> str:
    """Human-readable summary of the trail: what fired, and how often."""
    entries = load(path)
    if not entries:
        return (
            "Security audit\n"
            + "=" * 60
            + "\nNo decisions recorded yet.\n"
            + f"Trail: {audit_path(path)}"
        )
    decisions = _count(str(entry.get("decision", "?")) for entry in entries)
    rules = _count(str(entry.get("ruleId") or "-") for entry in entries)
    risks = _count(str(entry.get("risk", "?")) for entry in entries)
    lines = [
        "Security audit",
        "=" * 60,
        f"Trail: {audit_path(path)}",
        f"Decisions: {len(entries)}",
        "By decision: " + ", ".join(f"{key}={value}" for key, value in decisions.items()),
        "By risk: " + ", ".join(f"{key}={value}" for key, value in risks.items()),
        "",
        "Rules that fired:",
    ]
    lines.extend(f"  - {key}: {value}" for key, value in list(rules.items())[:10])
    tainted = sum(1 for entry in entries if entry.get("tainted"))
    lines.append("")
    lines.append(f"Tainted actions: {tainted}")
    lines.append("")
    lines.append(f"Last {min(limit, len(entries))} decision(s):")
    for entry in entries[-limit:]:
        stamp = entry.get("latencyMs", 0)
        lines.append(
            f"  [{entry.get('decision')}/{entry.get('risk')}@{entry.get('stage')}] "
            f"{entry.get('tool') or entry.get('kind')} {entry.get('ruleId') or ''} "
            f"({stamp}ms) {str(entry.get('reason'))[:90]}"
        )
    return "\n".join(lines)


def clear(path: str | Path | None = None) -> bool:
    """Delete the trail (used by tests and by a user resetting the baseline)."""
    target = audit_path(path)
    try:
        target.unlink()
        return True
    except (OSError, FileNotFoundError):
        return False


@dataclass(slots=True)
class AuditStats:
    """Aggregate view used by ``/security``."""

    total: int = 0
    denies: int = 0
    escalations: int = 0
    allows: int = 0

    @classmethod
    def from_entries(cls, entries: Iterable[dict[str, Any]]) -> "AuditStats":
        stats = cls()
        for entry in entries:
            stats.total += 1
            decision = str(entry.get("decision") or "")
            if decision == "deny":
                stats.denies += 1
            elif decision == "escalate":
                stats.escalations += 1
            elif decision == "allow":
                stats.allows += 1
        return stats
