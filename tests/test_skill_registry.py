"""Tests for the L1 skill registry (``minicode/skill_system/registry.py``).

The registry replaces the legacy ``minicode.skills`` loader, so the first
concern here is *compatibility*: a SKILL.md with no frontmatter -- which is what
``tests/test_skills.py`` and the repository's own
``.claude/skills/minicode/SKILL.md`` both are -- must keep loading exactly as it
did before, with the description coming from the legacy heuristic.

Everything after that covers the new L1 contract: frontmatter parsing,
validation diagnostics, discovery precedence, and trigger-aware matching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicode.skill_system.registry import (
    DEFAULT_VERSION,
    RiskLevel,
    SkillKind,
    SkillRegistry,
    parse_frontmatter,
    parse_skill_file,
    skill_roots,
)

FRONTMATTER_SKILL = """---
name: release-flow
version: 1.2.0
description: Publish a Python package end to end
triggers: [release, publish package, 发版]
tools:
  - run_command
  - read_file
risk_level: medium
kind: composite
---

# Release flow

Steps go here.
"""


def _write_skill(root: Path, name: str, content: str, source_dir: str = ".mini-code/skills") -> Path:
    skill_dir = root / source_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def _roots(tmp_path: Path) -> list[tuple[Path, str]]:
    """Isolated roots so the developer's real ~/.mini-code never leaks in.

    The project root is ``tmp_path/.mini-code/skills`` -- exactly where
    ``_write_skill`` puts a skill by default -- so the helper and the roots
    cannot drift apart.
    """
    return [
        (tmp_path / ".mini-code" / "skills", "project"),
        (tmp_path / "home" / ".mini-code" / "skills", "user"),
    ]


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_skill_without_frontmatter_still_loads() -> None:
    metadata, body, has_frontmatter, issues = parse_frontmatter("# Demo\n\nProject description\n")

    assert metadata == {}
    assert has_frontmatter is False
    assert issues == []
    assert body.startswith("# Demo")


def test_legacy_markdown_opening_with_a_rule_is_not_mistaken_for_frontmatter() -> None:
    text = "---\n\nJust a horizontal rule, not metadata.\n"

    metadata, body, has_frontmatter, issues = parse_frontmatter(text)

    assert metadata == {}
    assert has_frontmatter is False
    assert body == text
    assert issues, "treating a rule as body text must be explained, not silent"


def test_legacy_skill_description_uses_the_summary_heuristic(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "demo", "# Demo\n\nLegacy description\n")

    spec = parse_skill_file(path, name="demo", source="project")

    assert spec.description == "Legacy description"
    assert spec.has_frontmatter is False
    assert spec.version == DEFAULT_VERSION
    assert spec.kind is SkillKind.ATOMIC


def test_registry_discovery_matches_the_legacy_project_over_user_precedence(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo", "# Demo\n\nProject description\n", ".mini-code/skills")
    _write_skill(tmp_path, "demo", "# Demo\n\nUser description\n", "home/.mini-code/skills")

    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    assert registry.names() == ["demo"]
    spec = registry.get("demo")
    assert spec is not None
    assert spec.description == "Project description"
    assert spec.source == "project"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_frontmatter_parses_every_l1_field(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "release-flow", FRONTMATTER_SKILL)

    spec = parse_skill_file(path, name="release-flow", source="project")

    assert spec.has_frontmatter is True
    assert spec.name == "release-flow"
    assert spec.version == "1.2.0"
    assert spec.description == "Publish a Python package end to end"
    assert spec.triggers == ("release", "publish package", "发版")
    assert spec.tools == ("run_command", "read_file")
    assert spec.risk_level is RiskLevel.MEDIUM
    assert spec.kind is SkillKind.COMPOSITE
    assert spec.body.startswith("# Release flow")
    assert spec.issues == ()


def test_inline_and_block_lists_are_equivalent(tmp_path: Path) -> None:
    inline = _write_skill(
        tmp_path,
        "inline",
        "---\nname: inline\ndescription: d\ntriggers: [a, b]\n---\n\nbody\n",
    )
    block = _write_skill(
        tmp_path,
        "block",
        "---\nname: block\ndescription: d\ntriggers:\n  - a\n  - b\n---\n\nbody\n",
    )

    assert parse_skill_file(inline, name="inline", source="project").triggers == ("a", "b")
    assert parse_skill_file(block, name="block", source="project").triggers == ("a", "b")


def test_quoted_values_keep_commas_and_drop_comments() -> None:
    metadata, _body, has_frontmatter, issues = parse_frontmatter(
        '---\ndescription: "one, two"  # trailing note\nname: x\n---\nbody\n'
    )

    assert has_frontmatter is True
    assert metadata["description"] == "one, two"
    assert issues == []


def test_hash_comments_follow_yaml_rules_and_quoting_preserves_them() -> None:
    # A '#' only starts a comment when whitespace precedes it, so "C#" survives.
    unquoted, _body, _has, _issues = parse_frontmatter(
        "---\nname: csharp\ndescription: C# and #1234 is a comment\n---\nbody\n"
    )
    quoted, _body2, _has2, _issues2 = parse_frontmatter(
        '---\nname: csharp\ndescription: "C# and #1234 stay"\n---\nbody\n'
    )

    assert unquoted["description"] == "C# and"
    assert quoted["description"] == "C# and #1234 stay"


def test_unterminated_frontmatter_is_treated_as_body_text() -> None:
    text = "---\nname: x\ndescription: y\n\n# Body without a closing fence\n"

    metadata, body, has_frontmatter, issues = parse_frontmatter(text)

    assert metadata == {}
    assert has_frontmatter is False
    assert body == text
    assert any("never closed" in issue for issue in issues)


def test_unparsable_lines_are_reported_not_swallowed() -> None:
    _metadata, _body, has_frontmatter, issues = parse_frontmatter(
        "---\nname: x\nthis line has no colon\n---\nbody\n"
    )

    assert has_frontmatter is True
    assert any("unparsable" in issue for issue in issues)


# ---------------------------------------------------------------------------
# Validation diagnostics
# ---------------------------------------------------------------------------


def test_unknown_tool_in_whitelist_is_reported(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path,
        "risky",
        "---\nname: risky\ndescription: d\ntriggers: [x]\ntools: [read_file, teleport]\n---\nbody\n",
    )

    spec = parse_skill_file(path, name="risky", source="project", known_tools={"read_file"})

    assert any("teleport" in issue for issue in spec.issues)


def test_empty_tools_whitelist_fails_closed_and_says_so(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path, "locked", "---\nname: locked\ndescription: d\ntriggers: [x]\ntools: []\n---\nbody\n"
    )

    spec = parse_skill_file(path, name="locked", source="project")

    assert spec.tools == ()
    assert spec.allowed_tools == ()
    assert any("empty" in issue for issue in spec.issues)


def test_absent_tools_means_unrestricted(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path, "free", "---\nname: free\ndescription: d\ntriggers: [x]\n---\nbody\n"
    )

    spec = parse_skill_file(path, name="free", source="project")

    assert spec.tools is None
    assert spec.allowed_tools is None


def test_invalid_risk_level_and_kind_fall_back_with_issues(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path,
        "odd",
        "---\nname: odd\ndescription: d\ntriggers: [x]\nrisk_level: catastrophic\nkind: wizardry\n---\nbody\n",
    )

    spec = parse_skill_file(path, name="odd", source="project")

    assert spec.risk_level is RiskLevel.LOW
    assert spec.kind is SkillKind.ATOMIC
    assert any("risk_level" in issue for issue in spec.issues)
    assert any("kind" in issue for issue in spec.issues)


def test_non_semver_version_is_kept_and_reported(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path, "v", "---\nname: v\ndescription: d\ntriggers: [x]\nversion: latest\n---\nbody\n"
    )

    spec = parse_skill_file(path, name="v", source="project")

    assert spec.version == "latest"
    assert any("semver" in issue for issue in spec.issues)


def test_directory_name_wins_over_a_mismatched_frontmatter_name(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path, "dir-name", "---\nname: other-name\ndescription: d\ntriggers: [x]\n---\nbody\n"
    )

    spec = parse_skill_file(path, name="dir-name", source="project")

    assert spec.name == "dir-name"
    assert any("differs from directory name" in issue for issue in spec.issues)


def test_missing_description_falls_back_and_is_reported(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path, "nodesc", "---\nname: nodesc\ntriggers: [x]\n---\n\n# Title\n\nBody paragraph\n"
    )

    spec = parse_skill_file(path, name="nodesc", source="project")

    assert spec.description == "Body paragraph"
    assert any("no 'description'" in issue for issue in spec.issues)


def test_unknown_frontmatter_keys_are_preserved_in_metadata(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path,
        "extra",
        "---\nname: extra\ndescription: d\ntriggers: [x]\nowner: platform-team\n---\nbody\n",
    )

    spec = parse_skill_file(path, name="extra", source="project")

    assert spec.metadata["owner"] == "platform-team"
    assert any("owner" in issue for issue in spec.issues)


def test_invalid_utf8_is_reported_instead_of_crashing(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".mini-code" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"---\nname: broken\n\xff\xfe not utf8\n")

    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    spec = registry.get("broken")
    assert spec is not None
    assert any("UTF-8" in issue for issue in spec.issues)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_missing_roots_are_tolerated(tmp_path: Path) -> None:
    registry = SkillRegistry.discover(tmp_path, roots=[(tmp_path / "nope", "project")])

    assert registry.all() == []
    assert registry.names() == []


def test_directories_without_skill_md_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "project" / ".mini-code" / "skills" / "empty").mkdir(parents=True)

    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    assert registry.names() == []


def test_extra_roots_have_the_lowest_precedence(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo", "# Demo\n\nProject description\n", ".mini-code/skills")
    extra = tmp_path / "extra"
    _write_skill(extra, "demo", "# Demo\n\nFallback description\n", "skills")

    registry = SkillRegistry.discover(
        tmp_path,
        roots=[*_roots(tmp_path), (extra / "skills", "bundled")],
    )

    assert registry.get("demo").description == "Project description"  # type: ignore[union-attr]


def test_skill_roots_order_is_project_user_compat_project_compat_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    roots = skill_roots(tmp_path)

    assert [source for _path, source in roots] == [
        "project",
        "user",
        "compat_project",
        "compat_user",
    ]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _matcher(tmp_path: Path) -> SkillRegistry:
    _write_skill(
        tmp_path,
        "release-flow",
        "---\nname: release-flow\ndescription: Publish a Python package\n"
        "triggers: [release, publish package, 发版流程]\n---\nbody\n",
    )
    _write_skill(
        tmp_path,
        "code-review",
        "---\nname: code-review\ndescription: Review a diff for correctness\n"
        "triggers: [code review, 代码评审]\n---\nbody\n",
    )
    _write_skill(
        tmp_path,
        "env-triage",
        "---\nname: env-triage\ndescription: Diagnose a broken local environment\n"
        "triggers: [environment troubleshooting, 环境排障]\n---\nbody\n",
    )
    return SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))


def test_trigger_phrase_match_ranks_first(tmp_path: Path) -> None:
    matches = _matcher(tmp_path).match("how do I publish package to pypi")

    assert matches
    assert matches[0].skill.name == "release-flow"
    assert any("trigger phrase" in reason for reason in matches[0].reasons)


def test_chinese_trigger_matches_by_bigram(tmp_path: Path) -> None:
    matches = _matcher(tmp_path).match("帮我做一下代码评审")

    assert matches
    assert matches[0].skill.name == "code-review"


def test_unrelated_query_matches_nothing(tmp_path: Path) -> None:
    assert _matcher(tmp_path).match("hello there") == []
    assert _matcher(tmp_path).match("") == []


def test_match_respects_limit_and_reports_scores(tmp_path: Path) -> None:
    registry = _matcher(tmp_path)

    matches = registry.match("release code review environment troubleshooting", limit=2)

    assert len(matches) <= 2
    assert all(match.score > 0 for match in matches)
    assert [match.score for match in matches] == sorted(
        (match.score for match in matches), reverse=True
    )


def test_match_finds_skill_by_name(tmp_path: Path) -> None:
    matches = _matcher(tmp_path).match("run env triage please")

    assert matches
    assert matches[0].skill.name == "env-triage"
    assert any("name" in reason for reason in matches[0].reasons)


def test_match_is_deterministic_for_equal_scores(tmp_path: Path) -> None:
    registry = _matcher(tmp_path)

    first = [match.skill.name for match in registry.match("code")]
    second = [match.skill.name for match in registry.match("code")]

    assert first == second


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_format_index_lists_skills_with_state_and_flags(tmp_path: Path) -> None:
    registry = _matcher(tmp_path)

    rendered = registry.format_index(enabled={"release-flow": False})

    assert "release-flow v0.0.0 [atomic, risk=low]" in rendered
    assert "[disabled]" in rendered
    assert "triggers: release, publish package, 发版流程" in rendered
    assert "legacy (no frontmatter)" not in rendered


def test_format_index_handles_an_empty_registry(tmp_path: Path) -> None:
    assert "No skills discovered" in SkillRegistry().format_index()


def test_format_detail_reports_whitelist_and_issues(tmp_path: Path) -> None:
    registry = _matcher(tmp_path)

    rendered = registry.format_detail("env-triage")

    assert "Skill: env-triage" in rendered
    assert "Tools: unrestricted (no whitelist declared)" in rendered
    assert "Issues: none" in rendered


def test_format_detail_handles_unknown_skill() -> None:
    assert SkillRegistry().format_detail("nope") == "Unknown skill: nope"


def test_format_doctor_summarises_clean_and_dirty_skills(tmp_path: Path) -> None:
    _matcher(tmp_path)
    _write_skill(tmp_path, "legacy-one", "# Legacy\n\nNo frontmatter here\n")

    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))
    doctor = registry.format_doctor()

    assert "legacy-one" in doctor
    assert "Legacy (no frontmatter): 1" in doctor
    assert "Discovered: 4" in doctor


def test_format_doctor_on_empty_registry() -> None:
    assert "no skills discovered" in SkillRegistry().format_doctor()


def test_summaries_keep_the_legacy_prompt_shape(tmp_path: Path) -> None:
    summaries = _matcher(tmp_path).summaries()

    assert all(set(summary) == {"name", "description", "source"} for summary in summaries)


@pytest.mark.parametrize("kind", [SkillKind.ATOMIC, SkillKind.COMPOSITE, SkillKind.DOMAIN_PACK])
def test_by_kind_filters(tmp_path: Path, kind: SkillKind) -> None:
    registry = _matcher(tmp_path)

    assert all(skill.kind is kind for skill in registry.by_kind(kind))


# ---------------------------------------------------------------------------
# CJK filler words must not manufacture matches
# ---------------------------------------------------------------------------


def test_cjk_filler_bigrams_do_not_create_false_matches(tmp_path: Path) -> None:
    """Reported by the user: "今天天气怎么样" matched a "怎么修" trigger.

    Bigram matching means any shared two-character run scores, and filler words
    like 怎么 appear in everything.  "How is the weather" must match nothing.
    """
    _write_skill(
        tmp_path,
        "suggest-fixes",
        "---\nname: suggest-fixes\ndescription: Turn a diagnosis into repair commands\n"
        "triggers: [修复建议, 解决方案, 怎么修, remediation]\n---\nbody\n",
    )
    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    assert registry.match("今天天气怎么样") == []
    assert registry.match("这是什么东西") == []


def test_cjk_filler_filtering_keeps_topical_matches(tmp_path: Path) -> None:
    """The fix must not cost recall: real topic words still match."""
    _write_skill(
        tmp_path,
        "check-dependencies",
        "---\nname: check-dependencies\ndescription: Verify installed distributions\n"
        "triggers: [依赖冲突, dependencies, 依赖检查]\n---\nbody\n",
    )
    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    matches = registry.match("依赖冲突怎么解决")

    assert [match.skill.name for match in matches] == ["check-dependencies"]
    assert matches[0].score >= 4


def test_text_tokens_drops_cjk_filler() -> None:
    from minicode.skill_system.registry import text_tokens

    tokens = text_tokens("怎么修 什么原因")

    assert "怎么" not in tokens
    assert "什么" not in tokens
    assert "么修" in tokens  # the content-bearing remainder survives


def test_format_index_groups_composites_before_atomics(tmp_path: Path) -> None:
    """Reported by the user: the composites were buried at the bottom of a long list."""
    _write_skill(
        tmp_path,
        "release-flow",
        "---\nname: release-flow\ndescription: Publish a package\ntriggers: [release]\nkind: composite\n---\nbody\n",
    )
    _write_skill(
        tmp_path,
        "run-tests",
        "---\nname: run-tests\ndescription: Run the suite\ntriggers: [test]\n---\nbody\n",
    )
    registry = SkillRegistry.discover(tmp_path, roots=_roots(tmp_path))

    rendered = registry.format_index()

    assert "Composite skills (1):" in rendered
    assert "Atomic skills (1):" in rendered
    assert rendered.index("release-flow") < rendered.index("run-tests")
