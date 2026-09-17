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

__all__ = [
    "DEFAULT_VERSION",
    "RiskLevel",
    "SkillKind",
    "SkillMatch",
    "SkillRegistry",
    "SkillSpec",
    "parse_frontmatter",
    "parse_skill_file",
    "skill_roots",
]
