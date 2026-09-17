"""Tests for skill lifecycle state and skill statistics.

Both modules persist to ``~/.mini-code/`` in production, so every test passes an
explicit ``path`` into ``tmp_path``.  The properties that matter most are the
degradation rules: a missing file means "defaults", a corrupt file must not
raise, and the statistics helpers -- which are called from the matching path --
must never propagate an exception into a session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.skill_system import lifecycle, stats
from minicode.skill_system.lifecycle import SkillLifecycleState
from minicode.skill_system.registry import SkillRegistry
from minicode.skill_system.stats import SkillStat, SkillStatsBook


def _write_skill(tmp_path: Path, name: str, version: str = "1.0.0") -> None:
    skill_dir = tmp_path / ".mini-code" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: {version}\ndescription: d\ntriggers: [x]\n---\n\nbody\n",
        encoding="utf-8",
    )


def _registry(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry.discover(
        tmp_path, roots=[(tmp_path / ".mini-code" / "skills", "project")]
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_missing_state_file_means_everything_is_enabled(tmp_path: Path) -> None:
    state = lifecycle.load_state(tmp_path / "skill-state.json")

    assert state.is_enabled("anything") is True
    assert state.enabled == {}


def test_disable_then_enable_only_records_deviations(tmp_path: Path) -> None:
    target = tmp_path / "skill-state.json"

    lifecycle.set_enabled("demo", False, path=target)
    assert lifecycle.load_state(target).is_enabled("demo") is False
    assert json.loads(target.read_text(encoding="utf-8"))["enabled"] == {"demo": False}

    lifecycle.set_enabled("demo", True, path=target)
    assert lifecycle.load_state(target).is_enabled("demo") is True
    assert json.loads(target.read_text(encoding="utf-8"))["enabled"] == {}


def test_corrupt_state_file_degrades_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "skill-state.json"
    target.write_text("{not json at all", encoding="utf-8")

    state = lifecycle.load_state(target)

    assert state.enabled == {}
    assert state.is_enabled("demo") is True


def test_state_file_with_wrong_shapes_is_ignored_field_by_field(tmp_path: Path) -> None:
    target = tmp_path / "skill-state.json"
    target.write_text(json.dumps({"enabled": ["demo"], "versions": 5, "updatedAt": "soon"}), encoding="utf-8")

    state = lifecycle.load_state(target)

    assert state.enabled == {}
    assert state.versions == {}
    assert state.updated_at == 0.0


def test_blank_name_is_a_no_op(tmp_path: Path) -> None:
    target = tmp_path / "skill-state.json"

    lifecycle.set_enabled("   ", False, path=target)
    lifecycle.record_version("", "1.0.0", path=target)

    assert lifecycle.load_state(target).enabled == {}
    assert lifecycle.load_state(target).versions == {}


def test_record_version_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "skill-state.json"

    lifecycle.record_version("demo", "1.2.3", path=target)

    assert lifecycle.load_state(target).known_version("demo") == "1.2.3"


def test_partition_splits_registry_by_state(tmp_path: Path) -> None:
    _write_skill(tmp_path, "keep")
    _write_skill(tmp_path, "drop")
    target = tmp_path / "skill-state.json"
    lifecycle.set_enabled("drop", False, path=target)

    enabled, disabled = lifecycle.partition(_registry(tmp_path), lifecycle.load_state(target))

    assert [skill.name for skill in enabled] == ["keep"]
    assert [skill.name for skill in disabled] == ["drop"]


def test_format_state_lists_disabled_skills(tmp_path: Path) -> None:
    state = SkillLifecycleState(enabled={"drop": False, "keep": True})

    rendered = lifecycle.format_state(state)

    assert "Disabled skills: 1" in rendered
    assert "- drop" in rendered
    assert "- keep" not in rendered


def test_format_state_reports_version_drift(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo", version="2.0.0")
    state = SkillLifecycleState(versions={"demo": "1.0.0"})

    rendered = lifecycle.format_state(state, _registry(tmp_path))

    assert "demo: recorded 1.0.0 -> on disk 2.0.0" in rendered


def test_format_state_without_drift_says_none(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo", version="2.0.0")
    state = SkillLifecycleState(versions={"demo": "2.0.0"})

    assert "(none)" in lifecycle.format_state(state, _registry(tmp_path))


# ---------------------------------------------------------------------------
# Statistics: counters and derived metrics
# ---------------------------------------------------------------------------


def test_stat_rates_are_zero_without_observations() -> None:
    stat = SkillStat()

    assert stat.hit_rate == 0.0
    assert stat.conversion == 0.0


def test_stat_rates_are_computed_from_counters() -> None:
    stat = SkillStat(matches=10, runs=4, successes=3, failures=1)

    assert stat.hit_rate == pytest.approx(0.75)
    assert stat.conversion == pytest.approx(0.4)


def test_record_match_and_run_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "skill-stats.json"
    book = SkillStatsBook()

    book.record_match("demo", "publish the package")
    book.record_run("demo", success=True)
    book.record_run("demo", success=False)
    book.save(target)

    reloaded = SkillStatsBook.load(target)
    stat = reloaded.stat_for("demo")
    assert stat.matches == 1
    assert stat.runs == 2
    assert stat.successes == 1
    assert stat.failures == 1
    assert stat.last_query == "publish the package"


def test_long_queries_are_truncated(tmp_path: Path) -> None:
    target = tmp_path / "skill-stats.json"
    book = SkillStatsBook()
    book.record_match("demo", "x" * 400)
    book.save(target)

    assert len(SkillStatsBook.load(target).stat_for("demo").last_query) == stats._MAX_QUERY_CHARS


def test_corrupt_stats_file_degrades_to_empty(tmp_path: Path) -> None:
    target = tmp_path / "skill-stats.json"
    target.write_text("[]", encoding="utf-8")

    assert SkillStatsBook.load(target).skills == {}


def test_helpers_never_raise_when_the_target_is_unwritable(tmp_path: Path) -> None:
    target = tmp_path / "skill-stats.json"
    target.mkdir()  # a directory where a file is expected: every write fails

    stats.record_match("demo", "q", path=target)
    stats.record_run("demo", True, path=target)

    # Reaching this line is the assertion: neither call propagated.


def test_disable_switch_skips_recording_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "skill-stats.json"
    monkeypatch.setenv(stats.DISABLE_ENV, "1")

    stats.record_match("demo", "q", path=target)
    stats.record_run("demo", True, path=target)

    assert not target.exists()


def test_disable_switch_treats_falsey_values_as_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "skill-stats.json"
    monkeypatch.setenv(stats.DISABLE_ENV, "0")

    stats.record_match("demo", "q", path=target)

    assert target.exists()


# ---------------------------------------------------------------------------
# Statistics: reporting
# ---------------------------------------------------------------------------


def test_format_report_on_empty_book_explains_where_counters_go(tmp_path: Path) -> None:
    rendered = stats.format_report(path=tmp_path / "skill-stats.json")

    assert "No skill has been matched or run yet." in rendered
    assert "skill-stats.json" in rendered
    assert "=" * 60 in rendered


def test_format_report_orders_by_matches_and_shows_rates() -> None:
    book = SkillStatsBook()
    book.record_match("quiet")
    for _ in range(5):
        book.record_match("busy")
        book.record_run("busy", success=True)

    rendered = stats.format_report(book)

    assert rendered.index("- busy:") < rendered.index("- quiet:")
    assert "success_rate=100%" in rendered
    assert "conversion=100%" in rendered
    assert "last query: " not in rendered  # no query recorded in this test


def test_format_report_lists_retire_candidates() -> None:
    book = SkillStatsBook()
    for _ in range(6):
        book.record_match("never-used", "something")

    rendered = stats.format_report(book)

    assert "Retire candidates:" in rendered
    assert "never-used" in rendered
    assert "conversion 0%" in rendered


def test_retire_candidates_flags_unconverted_skills() -> None:
    book = SkillStatsBook()
    for _ in range(6):
        book.record_match("broad", "q")

    candidates = stats.retire_candidates(book)

    assert [candidate["name"] for candidate in candidates] == ["broad"]
    assert candidates[0]["signal"] == "unconverted"


def test_retire_candidates_flags_unreliable_skills() -> None:
    book = SkillStatsBook()
    book.record_match("flaky", "q")
    for _ in range(4):
        book.record_run("flaky", success=False)

    candidates = stats.retire_candidates(book)

    assert [candidate["name"] for candidate in candidates] == ["flaky"]
    assert candidates[0]["signal"] == "unreliable"
    assert "0% success" in str(candidates[0]["reason"])


def test_retire_candidates_keeps_healthy_skills() -> None:
    book = SkillStatsBook()
    for _ in range(4):
        book.record_match("good", "q")
        book.record_run("good", success=True)

    assert stats.retire_candidates(book) == []


def test_retire_candidates_respects_custom_thresholds() -> None:
    book = SkillStatsBook()
    for _ in range(3):
        book.record_match("mild", "q")

    assert stats.retire_candidates(book) == []
    assert stats.retire_candidates(book, min_matches=3, max_conversion=0.0)


# ---------------------------------------------------------------------------
# Path overrides: a test run or isolated workspace must not write the real home
# ---------------------------------------------------------------------------


def test_stats_path_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere-stats.json"
    monkeypatch.setenv(stats.PATH_ENV, str(target))

    assert stats.stats_path() == target
    stats.record_match("demo", "q")

    assert target.exists()
    assert SkillStatsBook.load().stat_for("demo").matches == 1


def test_lifecycle_path_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere-state.json"
    monkeypatch.setenv(lifecycle.PATH_ENV, str(target))

    assert lifecycle.state_path() == target
    lifecycle.set_enabled("demo", False)

    assert target.exists()
    assert lifecycle.load_state().is_enabled("demo") is False


def test_explicit_path_beats_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stats.PATH_ENV, str(tmp_path / "from-env.json"))
    explicit = tmp_path / "explicit.json"

    assert stats.stats_path(explicit) == explicit


# ---------------------------------------------------------------------------
# The usage half of the metric: load_skill records what was actually used
# ---------------------------------------------------------------------------


def _make_skill(workspace: Path, name: str) -> None:
    skill_dir = workspace / ".mini-code" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\ndescription: d\ntriggers: [x]\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user skill roots at the temp directory too."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    stats_file = tmp_path / "counters.json"
    monkeypatch.setenv(stats.PATH_ENV, str(stats_file))
    return stats_file


def test_load_skill_tool_records_a_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minicode.tools.load_skill import create_load_skill_tool

    counters = _isolate_home(tmp_path, monkeypatch)
    _make_skill(tmp_path, "demo")

    result = create_load_skill_tool(str(tmp_path)).run({"name": "demo"}, None)

    assert result.ok is True
    stat = SkillStatsBook.load(counters).stat_for("demo")
    assert (stat.matches, stat.runs, stat.successes, stat.failures) == (0, 1, 1, 0)


def test_load_skill_tool_records_an_unknown_skill_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minicode.tools.load_skill import create_load_skill_tool

    counters = _isolate_home(tmp_path, monkeypatch)

    result = create_load_skill_tool(str(tmp_path)).run({"name": "ghost"}, None)

    assert result.ok is False
    stat = SkillStatsBook.load(counters).stat_for("ghost")
    assert (stat.runs, stat.successes, stat.failures) == (1, 0, 1)


def test_recorded_runs_produce_a_conversion_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matched twice, used once: the report must show that gap."""
    from minicode.tools.load_skill import create_load_skill_tool

    counters = _isolate_home(tmp_path, monkeypatch)
    _make_skill(tmp_path, "demo")

    stats.record_match("demo", "do the thing")
    stats.record_match("demo", "do the thing again")
    create_load_skill_tool(str(tmp_path)).run({"name": "demo"}, None)

    stat = SkillStatsBook.load(counters).stat_for("demo")

    assert stat.matches == 2
    assert stat.conversion == pytest.approx(0.5)
    assert stat.hit_rate == pytest.approx(1.0)
