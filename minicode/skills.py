"""Legacy skill loader, now a compatibility layer over ``minicode.skill_system``.

Every public name here is kept for existing callers (``minicode.tools``,
``minicode.manage_cli``, the prompt bundle, ``/skills``), but discovery and
loading are delegated to the L1 registry so both views agree.  Two things this
buys immediately:

* descriptions come from validated frontmatter when a skill declares it,
  instead of the legacy heuristic reporting ``---`` (the frontmatter fence read
  as the first paragraph) in ``/skills`` and the system prompt;
* discovery order is deterministic (name-sorted) rather than filesystem order.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillSummary:
    name: str
    description: str
    path: str
    source: str


@dataclass(slots=True)
class LoadedSkill(SkillSummary):
    content: str


def extract_description(markdown: str) -> str:
    """First non-heading paragraph of the body, ignoring frontmatter.

    A leading ``---`` metadata block is skipped so a skill that declares
    frontmatter never advertises ``---`` as its description.
    """
    from minicode.skill_system.registry import parse_frontmatter

    _metadata, body, _has_frontmatter, _issues = parse_frontmatter(markdown)
    normalized = body.replace("\r\n", "\n")
    paragraphs = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    for block in paragraphs:
        if block.startswith("#"):
            continue
        for line in [part.strip() for part in block.split("\n")]:
            if line and not line.startswith("#"):
                return line.replace("`", "")
    return "No description provided."


def _home_dir() -> Path:
    return Path.home()


def _skill_roots(cwd: str | Path) -> list[tuple[Path, str]]:
    base = Path(cwd)
    home = _home_dir()
    return [
        (base / ".mini-code" / "skills", "project"),
        (home / ".mini-code" / "skills", "user"),
        (base / ".claude" / "skills", "compat_project"),
        (home / ".claude" / "skills", "compat_user"),
    ]


def discover_skills(cwd: str | Path) -> list[SkillSummary]:
    """Legacy view of the L1 registry: same shape, frontmatter-aware, sorted."""
    from minicode.skill_system.registry import SkillRegistry

    registry = SkillRegistry.discover(cwd)
    return [
        SkillSummary(
            name=spec.name,
            description=spec.description,
            path=spec.path,
            source=spec.source,
        )
        for spec in sorted(registry.all(), key=lambda skill: skill.name)
    ]


def load_skill(cwd: str | Path, name: str) -> LoadedSkill | None:
    normalized_name = name.strip()
    if not normalized_name:
        return None
    from minicode.skill_system.registry import parse_skill_file

    for root, source in _skill_roots(cwd):
        skill_path = root / normalized_name / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            spec = parse_skill_file(skill_path, name=normalized_name, source=source)
        except OSError:
            continue
        return LoadedSkill(
            name=spec.name,
            description=spec.description,
            path=spec.path,
            source=spec.source,
            content=spec.content,
        )
    return None


def _managed_skill_root(scope: str, cwd: str | Path) -> Path:
    return (Path(cwd) / ".mini-code" / "skills") if scope == "project" else (_home_dir() / ".mini-code" / "skills")


def install_skill(cwd: str | Path, source_path: str, name: str | None = None, scope: str = "user") -> dict[str, str]:
    source = Path(source_path)
    if not source.is_absolute():
        source = Path(cwd) / source
    if source.is_dir():
        skill_file = source / "SKILL.md"
        inferred_name = source.name
    else:
        skill_file = source if source.name == "SKILL.md" else source / "SKILL.md"
        inferred_name = skill_file.parent.name
    if not skill_file.exists():
        raise RuntimeError(f"No SKILL.md found in {source}")

    skill_name = (name or inferred_name).strip()
    if not skill_name:
        raise RuntimeError("Skill name cannot be empty.")

    target_dir = _managed_skill_root(scope, cwd) / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_file, target_dir / "SKILL.md")
    return {"name": skill_name, "targetPath": str(target_dir / "SKILL.md")}


def remove_managed_skill(cwd: str | Path, name: str, scope: str = "user") -> dict[str, object]:
    target_path = _managed_skill_root(scope, cwd) / name
    if not target_path.exists():
        return {"removed": False, "targetPath": str(target_path)}
    shutil.rmtree(target_path)
    return {"removed": True, "targetPath": str(target_path)}

