"""Three-layer skill capability system (L1 atomic / L2 composite / L3 domain pack).

Landed as ``minicode/skill_system/`` with the legacy ``minicode.skills`` module
kept as a compatibility layer.  Module map:

* ``registry``     -- L1: frontmatter validation, startup index, trigger matching
* ``lifecycle``    -- enable/disable/version state persisted outside the repo
* ``stats``        -- hit-rate / success-rate counters and the report
* ``composite``    -- L2: composite specs compiled onto ``minicode.task_graph``
* ``domain_pack``  -- L3: knowledge + entity labels + few-shot examples
* ``collector``    -- per-turn trajectory recording (raw material for distillation)

Everything here is standard library only, matching the repository's zero
runtime-dependency rule.
"""

from __future__ import annotations

from minicode.skill_system.registry import (
    DEFAULT_VERSION,
    RiskLevel,
    SkillKind,
    SkillMatch,
    SkillRegistry,
    SkillSpec,
    parse_frontmatter,
    parse_skill_file,
    skill_roots,
)

#: Subcommands of the ``/skills`` slash command, in help order.
#:
#: Declared here (not in the slash-command registry) because they are arguments
#: of an existing command rather than new commands: adding them to
#: ``SLASH_COMMANDS`` would demand matching ``/help`` entries and dispatch sites,
#: and they would all be reachable through the same ``/skills`` dispatch anyway.
#: The terminal's Tab completion and the help footer both read this tuple, so
#: there is a single source of truth.
SKILLS_SUBCOMMANDS = (
    "doctor",
    "show",
    "match",
    "stats",
    "state",
    "enable",
    "disable",
    "workflows",
    "plan",
    "packs",
    "examples",
    "install-examples",
)

__all__ = [
    "DEFAULT_VERSION",
    "RiskLevel",
    "SKILLS_SUBCOMMANDS",
    "SkillKind",
    "SkillMatch",
    "SkillRegistry",
    "SkillSpec",
    "parse_frontmatter",
    "parse_skill_file",
    "skill_roots",
]
