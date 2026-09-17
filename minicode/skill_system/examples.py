"""Bundled example skills: three composite scenarios plus the atomic skills they use.

These ship inside the package (as real ``SKILL.md`` files rather than embedded
strings) so they can be loaded, planned and executed directly, and installed into
a workspace when someone wants them active::

    python -m minicode.skill_system examples install --scope project

They are deliberately **not** part of the default discovery roots.  Discovery
would push nineteen skills into every system prompt, which is exactly the
prompt-bloat the L3 domain-pack design exists to avoid; opting in through
``install_examples`` keeps the default path cheap.

Layout::

    examples/
        release-flow/{SKILL.md,workflow.json}      # composite
        code-review/{SKILL.md,workflow.json}       # composite
        env-triage/{SKILL.md,workflow.json}        # composite
        atomic/<skill-name>/SKILL.md               # 16 atomic skills
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from minicode.skill_system.registry import SkillKind, SkillRegistry

__all__ = [
    "EXAMPLES_DIR",
    "SCENARIO_NAMES",
    "bundled_roots",
    "load_bundled_registry",
    "bundled_scenarios",
    "install_examples",
    "format_examples",
]

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
ATOMIC_DIRNAME = "atomic"

#: The three acceptance scenarios from the design document.
SCENARIO_NAMES = ("release-flow", "code-review", "env-triage")


def bundled_roots() -> list[tuple[Path, str]]:
    """Discovery roots for the bundled material (atomic first, composites second)."""
    return [
        (EXAMPLES_DIR / ATOMIC_DIRNAME, "bundled_atomic"),
        (EXAMPLES_DIR, "bundled_composite"),
    ]


def load_bundled_registry() -> SkillRegistry:
    """Load every bundled skill, including the atomic skills the scenarios use."""
    return SkillRegistry.discover(EXAMPLES_DIR, roots=bundled_roots())


def bundled_scenarios() -> list[str]:
    """Names of the composite scenarios actually present on disk."""
    return [
        name
        for name in SCENARIO_NAMES
        if (EXAMPLES_DIR / name / "SKILL.md").exists()
    ]


def _managed_root(cwd: str | Path, scope: str) -> Path:
    if scope == "project":
        return Path(cwd) / ".mini-code" / "skills"
    return Path.home() / ".mini-code" / "skills"


def _bundled_skill_dirs() -> list[Path]:
    """Every bundled ``<name>/`` directory that holds a ``SKILL.md``."""
    found: list[Path] = []
    atomic_root = EXAMPLES_DIR / ATOMIC_DIRNAME
    if atomic_root.exists():
        found.extend(
            entry for entry in sorted(atomic_root.iterdir()) if (entry / "SKILL.md").exists()
        )
    for scenario in bundled_scenarios():
        found.append(EXAMPLES_DIR / scenario)
    return found


def install_examples(
    cwd: str | Path,
    *,
    scope: str = "project",
    names: Iterable[str] | None = None,
) -> list[str]:
    """Copy bundled skills into a discoverable root; returns the installed names.

    Copying the whole directory (not just ``SKILL.md``) is what makes an installed
    composite still work: its ``workflow.json`` has to travel with it.
    """
    target_root = _managed_root(cwd, scope)
    wanted = {name.strip() for name in names} if names is not None else None
    installed: list[str] = []
    for source in _bundled_skill_dirs():
        if wanted is not None and source.name not in wanted:
            continue
        destination = target_root / source.name
        destination.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if item.is_file():
                shutil.copyfile(item, destination / item.name)
        installed.append(source.name)
    return installed


def format_examples(installed: Iterable[str] = ()) -> str:
    """List the bundled scenarios and the atomic skills they depend on."""
    registry = load_bundled_registry()
    composites = sorted(registry.by_kind(SkillKind.COMPOSITE), key=lambda skill: skill.name)
    installed_names = set(installed)

    lines = [
        "Bundled example skills",
        "=" * 60,
        f"Location: {EXAMPLES_DIR}",
        f"Discovered: {len(registry.all())} skill(s)",
        "",
        "Composite scenarios:",
    ]
    if not composites:
        lines.append("  (none found)")
    for skill in composites:
        marker = ""
        if installed_names:
            marker = "  [just installed]" if skill.name in installed_names else ""
        lines.append(f"  - {skill.name} v{skill.version}: {skill.description}{marker}")

    atoms = sorted(registry.by_kind(SkillKind.ATOMIC), key=lambda skill: skill.name)
    lines.append("")
    lines.append(f"Atomic skills ({len(atoms)}):")
    lines.extend(f"  - {skill.name}: {skill.description}" for skill in atoms)
    lines.extend(
        [
            "",
            "Install them with:",
            "  python -m minicode.skill_system examples install --scope project",
            "  (or /skills install-examples inside a session)",
        ]
    )
    return "\n".join(lines)
