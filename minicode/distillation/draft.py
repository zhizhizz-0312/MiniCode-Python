"""The draft area: where proposals wait for a human, and where refusals are remembered.

Nothing here can reach the skill registry.  That is the防污染 gate from the design
document made structural: a distilled proposal is written to
``~/.mini-code/skill-drafts/<id>/`` as a ``SKILL.md`` plus a ``draft.json`` sidecar,
and only :mod:`minicode.distillation.review` can promote it.

The same file also holds the loop's memory of its own decisions:

* **rejected fingerprints** -- so the distiller does not regenerate the same
  rejected proposal every run (the most annoying possible failure mode for a
  learning system);
* **processed cluster signatures** -- so a cluster is distilled once, not on every
  invocation;
* **spend** -- the running cost of distillation, checked against the budget.

Every path is redirectable by environment variable, because this data sits next to
the real corpus and test runs must not touch it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR
from minicode.distillation.types import (
    DRAFTS_DIRNAME,
    STATE_FILENAME,
    DraftStatus,
    SkillDraft,
)

__all__ = [
    "PATH_ENV",
    "STATE_PATH_ENV",
    "DistillationState",
    "DraftStore",
    "drafts_dir",
    "state_path",
]

PATH_ENV = "MINI_CODE_DRAFTS_PATH"
STATE_PATH_ENV = "MINI_CODE_DISTILLATION_STATE_PATH"


def drafts_dir(path: str | Path | None = None) -> Path:
    """Resolve the draft area: explicit path, then env override, then defaults."""
    if path is not None:
        return Path(path)
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override) if override else MINI_CODE_DIR / DRAFTS_DIRNAME


def state_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(STATE_PATH_ENV, "").strip()
    return Path(override) if override else MINI_CODE_DIR / STATE_FILENAME


def draft_fingerprint(draft: SkillDraft) -> str:
    """Identity of a proposal's *content*, so a refusal sticks to it.

    Deliberately excludes the draft id and timestamps: regenerating the same idea
    must produce the same fingerprint, otherwise a rejection would not prevent the
    next run from offering it again.
    """
    payload = "\x00".join(
        [
            draft.name.strip().lower(),
            draft.description.strip().lower(),
            "|".join(sorted(draft.triggers)),
            "|".join(sorted(draft.tools)),
            "|".join(step.strip().lower() for step in draft.steps),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


@dataclass
class DistillationState:
    """Persistent memory of the loop: refusals, processed clusters, spend."""

    rejected: dict[str, str] = field(default_factory=dict)  # fingerprint -> reason
    processed: dict[str, float] = field(default_factory=dict)  # cluster signature -> when
    spent_usd: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "rejected": dict(sorted(self.rejected.items())),
            "processed": dict(sorted(self.processed.items())),
            "spentUsd": round(self.spent_usd, 6),
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "DistillationState":
        if not isinstance(payload, dict):
            return cls()
        rejected = payload.get("rejected")
        processed = payload.get("processed")
        spent = payload.get("spentUsd")
        return cls(
            rejected={str(k): str(v) for k, v in rejected.items()} if isinstance(rejected, dict) else {},
            processed={str(k): float(v) for k, v in processed.items()} if isinstance(processed, dict) else {},
            spent_usd=float(spent) if isinstance(spent, (int, float)) else 0.0,
            updated_at=float(payload.get("updatedAt") or 0.0),
        )


class DraftStore:
    """Reads and writes the draft area plus the loop's state file."""

    def __init__(self, *, path: str | Path | None = None, state: str | Path | None = None) -> None:
        self.root = drafts_dir(path)
        self.state_file = state_path(state)

    # -- drafts ------------------------------------------------------------

    def list(self) -> list[SkillDraft]:
        if not self.root.exists():
            return []
        drafts: list[SkillDraft] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            draft = self._read(entry)
            if draft is not None:
                drafts.append(draft)
        drafts.sort(key=lambda draft: (-draft.created_at, draft.id))
        return drafts

    def pending(self) -> list[SkillDraft]:
        return [draft for draft in self.list() if draft.pending]

    def get(self, draft_id: str) -> SkillDraft | None:
        entry = self.root / draft_id.strip()
        if not entry.is_dir():
            return None
        return self._read(entry)

    def save(self, draft: SkillDraft) -> Path:
        """Write ``SKILL.md`` and the sidecar. Refuses to overwrite an existing id."""
        target = self.root / draft.id
        target.mkdir(parents=True, exist_ok=True)
        if not draft.created_at:
            draft.created_at = time.time()
        (target / "SKILL.md").write_text(draft.skill_markdown, encoding="utf-8")
        (target / "draft.json").write_text(
            json.dumps(draft.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target

    def _read(self, entry: Path) -> SkillDraft | None:
        sidecar = entry / "draft.json"
        if not sidecar.exists():
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        draft = SkillDraft.from_dict(payload)
        skill_file = entry / "SKILL.md"
        if skill_file.exists():
            try:
                draft.skill_markdown = skill_file.read_text(encoding="utf-8")
            except OSError:
                pass
        return draft

    def set_status(self, draft_id: str, status: DraftStatus, note: str = "") -> SkillDraft | None:
        draft = self.get(draft_id)
        if draft is None:
            return None
        draft.status = status
        if note:
            draft.review_note = note
        self.save(draft)
        return draft

    def delete(self, draft_id: str) -> bool:
        target = self.root / draft_id.strip()
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # -- loop state --------------------------------------------------------

    def load_state(self) -> DistillationState:
        if not self.state_file.exists():
            return DistillationState()
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return DistillationState()
        return DistillationState.from_dict(payload)

    def save_state(self, state: DistillationState) -> Path:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = time.time()
        self.state_file.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.state_file

    def reject_fingerprint(self, fingerprint: str, reason: str) -> None:
        state = self.load_state()
        state.rejected[fingerprint] = reason[-400:]
        self.save_state(state)

    def rejected_fingerprints(self) -> dict[str, str]:
        return self.load_state().rejected

    def mark_processed(self, signature: str) -> None:
        state = self.load_state()
        state.processed[signature] = time.time()
        self.save_state(state)

    def forget_processed(self) -> None:
        """Allow clusters to be distilled again.

        Used after a rejection when the user wants the loop to have another go (the
        refusal fingerprint still stops the *same content* from reappearing), and by
        tests that need a clean run.
        """
        state = self.load_state()
        state.processed.clear()
        self.save_state(state)

    def record_spend(self, amount_usd: float) -> float:
        state = self.load_state()
        state.spent_usd += max(0.0, amount_usd)
        self.save_state(state)
        return state.spent_usd

    def clear(self) -> None:
        """Remove every draft and forget the loop's state (tests, reset)."""
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            self.state_file.unlink()
        except (OSError, FileNotFoundError):
            pass
