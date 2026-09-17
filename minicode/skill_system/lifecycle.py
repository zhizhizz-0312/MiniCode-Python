"""Skill lifecycle: enable/disable and version tracking, persisted outside the repo.

State lives in ``~/.mini-code/skill-state.json`` -- the same directory the
product already uses for settings, history and sessions -- so it survives a
checkout and never dirties the repository.  Only skills whose state differs from
the default are recorded, which keeps the file small and makes "why is this
disabled?" answerable by looking at one key.

Every read tolerates a missing or corrupt file: a broken state file must degrade
to "everything enabled", never take the CLI down.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from minicode.config import MINI_CODE_DIR
from minicode.skill_system.registry import SkillRegistry, SkillSpec

__all__ = [
    "STATE_FILENAME",
    "PATH_ENV",
    "SkillLifecycleState",
    "state_path",
    "load_state",
    "save_state",
    "set_enabled",
    "record_version",
    "partition",
    "format_state",
]

STATE_FILENAME = "skill-state.json"
PATH_ENV = "MINI_CODE_SKILL_STATE_PATH"


def state_path(path: str | Path | None = None) -> Path:
    """Resolve the state file: explicit path, then env override, then ``~/.mini-code``."""
    if path is not None:
        return Path(path)
    override = os.environ.get(PATH_ENV, "").strip()
    return Path(override) if override else MINI_CODE_DIR / STATE_FILENAME


@dataclass
class SkillLifecycleState:
    """Enable/disable overrides plus the version each skill was last seen at."""

    enabled: dict[str, bool] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    updated_at: float = 0.0

    def is_enabled(self, name: str) -> bool:
        """Skills are enabled unless explicitly disabled."""
        return self.enabled.get(name, True)

    def known_version(self, name: str) -> str | None:
        return self.versions.get(name)

    def to_dict(self) -> dict:
        return {
            "enabled": dict(sorted(self.enabled.items())),
            "versions": dict(sorted(self.versions.items())),
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SkillLifecycleState":
        if not isinstance(payload, dict):
            return cls()
        raw_enabled = payload.get("enabled")
        raw_versions = payload.get("versions")
        enabled = (
            {str(key): bool(value) for key, value in raw_enabled.items()}
            if isinstance(raw_enabled, dict)
            else {}
        )
        versions = (
            {str(key): str(value) for key, value in raw_versions.items()}
            if isinstance(raw_versions, dict)
            else {}
        )
        raw_updated = payload.get("updatedAt")
        updated_at = float(raw_updated) if isinstance(raw_updated, (int, float)) else 0.0
        return cls(enabled=enabled, versions=versions, updated_at=updated_at)


def load_state(path: str | Path | None = None) -> SkillLifecycleState:
    """Read the state file, falling back to the default state on any problem."""
    target = state_path(path)
    if not target.exists():
        return SkillLifecycleState()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return SkillLifecycleState()
    return SkillLifecycleState.from_dict(payload)


def save_state(state: SkillLifecycleState, path: str | Path | None = None) -> Path:
    """Persist the state file, creating ``~/.mini-code`` if needed."""
    target = state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    target.write_text(
        json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def set_enabled(
    name: str,
    enabled: bool,
    *,
    path: str | Path | None = None,
    state: SkillLifecycleState | None = None,
) -> SkillLifecycleState:
    """Enable or disable one skill and persist the result.

    Enabling a skill removes the override entirely (that is the default), so the
    file only ever lists deliberate deviations.
    """
    current = state if state is not None else load_state(path)
    key = name.strip()
    if not key:
        return current
    if enabled:
        current.enabled.pop(key, None)
    else:
        current.enabled[key] = False
    save_state(current, path)
    return current


def record_version(
    name: str,
    version: str,
    *,
    path: str | Path | None = None,
    state: SkillLifecycleState | None = None,
) -> SkillLifecycleState:
    """Remember the version a skill was last observed at (drives version drift)."""
    current = state if state is not None else load_state(path)
    key = name.strip()
    if key:
        current.versions[key] = version
        save_state(current, path)
    return current


def partition(
    registry: SkillRegistry,
    state: SkillLifecycleState | None = None,
) -> tuple[list[SkillSpec], list[SkillSpec]]:
    """Split a registry into ``(enabled, disabled)`` specs."""
    current = state if state is not None else load_state()
    enabled: list[SkillSpec] = []
    disabled: list[SkillSpec] = []
    for skill in registry.all():
        (enabled if current.is_enabled(skill.name) else disabled).append(skill)
    return enabled, disabled


def format_state(
    state: SkillLifecycleState | None = None,
    registry: SkillRegistry | None = None,
) -> str:
    """Human-readable lifecycle summary, including version drift when known."""
    current = state if state is not None else load_state()
    disabled = sorted(name for name, enabled in current.enabled.items() if not enabled)
    lines = [
        "Skill lifecycle",
        "=" * 60,
        f"State file: {state_path()}",
        f"Disabled skills: {len(disabled)}",
    ]
    lines.extend(f"  - {name}" for name in disabled)
    if not disabled:
        lines.append("  (none)")
    if registry is not None and registry.all():
        lines.append("")
        lines.append("Version drift:")
        drift = False
        for skill in sorted(registry.all(), key=lambda item: item.name):
            seen = current.known_version(skill.name)
            if seen is None:
                continue
            if seen != skill.version:
                drift = True
                lines.append(f"  - {skill.name}: recorded {seen} -> on disk {skill.version}")
        if not drift:
            lines.append("  (none)")
    return "\n".join(lines)
