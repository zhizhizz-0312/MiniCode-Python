"""Data assembly for the web panel.

Every view is built from artefacts that already exist on disk -- the skill registry, the
security trail, the trajectory corpus, the P3 draft queue, the P4 run trails.  The panel
holds no state of its own, so it cannot become a second source of truth about the
project, and restarting it changes nothing.

The two write actions are deliberately the only ones, and both go through the gates
that already own them: approving a draft runs P3's `review` (including the screening
that is not switchable off), and enabling or disabling a skill runs P1's lifecycle.
The panel adds no new write path -- if it did, the gates it bypassed would stop meaning
anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "approve_draft",
    "disable_skill",
    "draft_detail",
    "drafts",
    "enable_skill",
    "overview",
    "reject_draft",
    "runs",
    "security_view",
    "skills_view",
    "trajectories_view",
]


def _registry(cwd: str):
    from minicode.skill_system.registry import SkillRegistry

    return SkillRegistry.discover(cwd)


def _lifecycle_state():
    from minicode.skill_system import lifecycle

    return lifecycle, lifecycle.load_state()


def overview(cwd: str = ".") -> dict[str, Any]:
    """The four stages at a glance: what exists, and what is waiting for a human."""
    registry = _registry(cwd)
    skills = registry.names()
    from minicode.distillation.draft import DraftStore
    from minicode.orchestration.audit import list_orchestrations
    from minicode.security_chain import audit as security_audit
    from minicode.security_chain.types import mode_from_env

    store = DraftStore()
    drafts_list = store.list()
    runs_list = list_orchestrations()
    entries = security_audit.load()
    return {
        "cwd": str(Path(cwd).resolve()),
        "skills": {
            "total": len(skills),
            "composites": sum(1 for name in skills if getattr(registry.get(name), "kind", None) and registry.get(name).kind.value == "composite"),
        },
        "security": {
            "mode": mode_from_env().value,
            "decisions": len(entries),
            "denied": sum(1 for entry in entries if entry.get("decision") == "deny"),
        },
        "learning": {
            "drafts": len(drafts_list),
            "pending": sum(1 for draft in drafts_list if draft.pending),
            "spentUsd": round(store.load_state().spent_usd, 6),
        },
        "orchestration": {
            "runs": len(runs_list),
            "latest": runs_list[0].orchestration_id if runs_list else "",
        },
    }


def skills_view(cwd: str = ".") -> list[dict[str, Any]]:
    """Every skill with its lifecycle state and its usage numbers."""
    registry = _registry(cwd)
    _lifecycle, state = _lifecycle_state()
    from minicode.skill_system.stats import load_stats

    book = load_stats()
    rows: list[dict[str, Any]] = []
    for name in registry.names():
        spec = registry.get(name)
        stat = book.entries.get(name) if hasattr(book, "entries") else None
        rows.append(
            {
                "name": name,
                "kind": getattr(getattr(spec, "kind", None), "value", ""),
                "description": getattr(spec, "description", ""),
                "triggers": list(getattr(spec, "triggers", ()) or ()),
                "tools": list(getattr(spec, "allowed_tools", ()) or ()),
                "riskLevel": getattr(getattr(spec, "risk_level", None), "value", ""),
                "enabled": state.is_enabled(name),
                "matches": getattr(stat, "matches", 0) if stat is not None else 0,
                "successes": getattr(stat, "successes", 0) if stat is not None else 0,
                "failures": getattr(stat, "failures", 0) if stat is not None else 0,
                "issues": list(getattr(spec, "issues", ()) or ()),
            }
        )
    return rows


def security_view(*, limit: int = 25) -> dict[str, Any]:
    """The security chain: current mode and the most recent decisions."""
    from minicode.security_chain import audit as security_audit
    from minicode.security_chain.types import mode_from_env

    entries = security_audit.load(limit=limit)
    recent = [
        {
            "decision": entry.get("decision", ""),
            "risk": entry.get("risk", ""),
            "ruleId": entry.get("ruleId", ""),
            "tool": entry.get("toolName", ""),
            "reason": entry.get("reason", ""),
            "kind": entry.get("kind", ""),
        }
        for entry in reversed(entries)
    ]
    return {
        "mode": mode_from_env().value,
        "auditPath": str(security_audit.audit_path()),
        "total": len(security_audit.load()),
        "recent": recent,
    }


def trajectories_view() -> dict[str, Any]:
    """The learning corpus: how much material the distiller has to work with."""
    from minicode.distillation.signals import load_trajectory_refs

    refs = load_trajectory_refs()
    return {
        "turns": len(refs),
        "synthetic": sum(1 for ref in refs if ref.synthetic),
        "corrections": sum(1 for ref in refs if ref.correction),
        "failures": sum(1 for ref in refs if ref.failed),
        "sessions": sorted({ref.session_id for ref in refs})[:20],
    }


def drafts() -> list[dict[str, Any]]:
    """The review queue."""
    from minicode.distillation.draft import DraftStore

    return [
        {
            "id": draft.id,
            "name": draft.name,
            "status": draft.status.value,
            "confidence": draft.confidence,
            "priorConfidence": draft.prior_confidence,
            "description": draft.description,
            "triggers": list(draft.triggers),
            "tools": list(draft.tools),
            "sources": len(draft.sources),
            "issues": list(draft.issues),
        }
        for draft in DraftStore().list()
    ]


def draft_detail(draft_id: str) -> dict[str, Any] | None:
    """One draft in full, including the text a reviewer has to read."""
    from minicode.distillation.draft import DraftStore

    draft = DraftStore().get(draft_id)
    if draft is None:
        return None
    payload = draft.to_dict()
    payload["skillMarkdown"] = draft.skill_markdown
    return payload


def runs() -> list[dict[str, Any]]:
    """Recorded orchestrations, newest first."""
    from minicode.orchestration.audit import list_orchestrations

    return [
        {
            "id": run.orchestration_id,
            "status": run.status,
            "children": run.children,
            "costUsd": run.cost_usd,
            "events": run.events,
        }
        for run in list_orchestrations()
    ]


# ---------------------------------------------------------------------------
# The two actions, both routed through the gate that owns them
# ---------------------------------------------------------------------------


def approve_draft(draft_id: str, *, cwd: str = ".") -> dict[str, Any]:
    """Install a distilled draft -- through P3's review gate, screening included."""
    from minicode.distillation.draft import DraftStore
    from minicode.distillation.review import approve_draft as approve

    outcome = approve(DraftStore(), draft_id, cwd=cwd)
    return {"ok": outcome.ok, "message": outcome.message}


def reject_draft(draft_id: str, reason: str = "") -> dict[str, Any]:
    """Refuse a draft and remember the decision."""
    from minicode.distillation.draft import DraftStore
    from minicode.distillation.review import reject_draft as reject

    outcome = reject(DraftStore(), draft_id, reason)
    return {"ok": outcome.ok, "message": outcome.message}


def enable_skill(name: str, *, enabled: bool = True) -> dict[str, Any]:
    """Enable or disable a skill through P1's lifecycle."""
    try:
        from minicode.skill_system import lifecycle

        lifecycle.set_enabled(name, enabled)
        return {"ok": True, "message": f"{name} {'enabled' if enabled else 'disabled'}"}
    except Exception as error:  # noqa: BLE001 - reported to the page, never a crash
        return {"ok": False, "message": f"could not change {name}: {type(error).__name__}: {error}"}


def disable_skill(name: str) -> dict[str, Any]:
    return enable_skill(name, enabled=False)
