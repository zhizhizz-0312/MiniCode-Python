"""Tests for the orchestration foundation: the plan model and the hard budgets.

Both modules exist to *refuse* things, so that is mostly what is tested: a plan that
would put two writers in one working tree, a tool name no layer would accept, a child
that has spent its allowance.  A gate is only worth having if the refusal path is
covered; the happy paths are here too, but they are the easy half.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.orchestration.budget import (
    BUDGET_EXHAUSTED,
    Budget,
    BudgetTracker,
    OrchestrationBudget,
)
from minicode.orchestration.plan import (
    orchestration_file,
    plan_from_workflow,
    resolve_isolation,
    validate_plan,
)
from minicode.orchestration.types import (
    ChildResult,
    Isolation,
    OrchestrationResult,
    OrchestrationStatus,
    SubtaskSpec,
    SubtaskStatus,
)
from minicode.skill_system.composite import parse_workflow
from minicode.skill_system.registry import SkillKind, SkillSpec

WORKFLOW = {
    "steps": [
        {"id": "survey", "skill": "read-tests"},
        {"id": "write-a", "skill": "add-tests", "depends_on": ["survey"]},
        {"id": "write-b", "skill": "add-tests", "depends_on": ["survey"]},
        {"id": "report", "skill": "summarise", "depends_on": ["write-a", "write-b"],
         "when": "on_failure:write-a"},
    ]
}


def _workflow(payload: dict | None = None):
    workflow, issues = parse_workflow(payload or WORKFLOW, name="demo")
    assert workflow is not None, issues
    return workflow


class _Clock:
    """A clock the test advances by hand, so wall-clock budgets are deterministic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Per-child budget
# ---------------------------------------------------------------------------


def test_a_fresh_tracker_is_not_exhausted() -> None:
    assert BudgetTracker(Budget()).exhausted() is None


def test_turns_budget_stops_at_the_limit() -> None:
    tracker = BudgetTracker(Budget(max_turns=2))

    tracker.record_turn()
    assert tracker.exhausted() is None
    tracker.record_turn()
    assert tracker.exhausted() == "turns: 2/2"


def test_tool_call_budget_stops_at_the_limit() -> None:
    tracker = BudgetTracker(Budget(max_tool_calls=1))
    tracker.record_tool_call()

    assert "tool calls: 1/1" == tracker.exhausted()


def test_token_budget_stops_at_the_limit() -> None:
    tracker = BudgetTracker(Budget(max_tokens=100))
    tracker.record_usage(tokens=100, cost_usd=0.0)

    assert tracker.exhausted() == "tokens: 100/100"


def test_cost_budget_stops_at_the_limit() -> None:
    tracker = BudgetTracker(Budget(max_cost_usd=0.05))
    tracker.record_usage(cost_usd=0.05)

    assert tracker.exhausted().startswith("cost: $0.0500")


def test_wall_clock_budget_uses_the_clock_not_the_calendar() -> None:
    clock = _Clock()
    tracker = BudgetTracker(Budget(max_wall_seconds=30), clock=clock)

    clock.advance(29)
    assert tracker.exhausted() is None
    clock.advance(1)
    assert tracker.exhausted().startswith("wall clock: 30s/30s")


def test_usage_never_goes_negative() -> None:
    tracker = BudgetTracker(Budget())
    tracker.record_usage(tokens=-10, cost_usd=-1.0)

    assert tracker.tokens == 0
    assert tracker.cost_usd == 0.0


def test_snapshot_reports_counters_and_the_reason() -> None:
    tracker = BudgetTracker(Budget(max_turns=1))
    tracker.record_turn()
    tracker.record_usage(tokens=5, cost_usd=0.001)

    snapshot = tracker.snapshot()

    assert snapshot["turns"] == 1
    assert snapshot["tokens"] == 5
    assert snapshot["exhausted"] == "turns: 1/1"


def test_budget_from_dict_falls_back_for_missing_and_invalid_values() -> None:
    base = Budget(max_turns=7, max_cost_usd=0.2)

    assert Budget.from_dict({"maxTurns": 3}, base=base).max_turns == 3
    assert Budget.from_dict({"maxTurns": "nonsense"}, base=base).max_turns == 7
    assert Budget.from_dict(None, base=base).max_cost_usd == 0.2
    assert Budget.from_dict({}).max_turns == Budget().max_turns


# ---------------------------------------------------------------------------
# Run-level budget
# ---------------------------------------------------------------------------


def test_run_budget_refuses_more_children_than_allowed() -> None:
    budget = OrchestrationBudget(max_children=1)
    budget.start_child()

    assert budget.can_start_child() == "children: 1/1"
    with pytest.raises(RuntimeError):
        budget.start_child()


def test_run_budget_refuses_once_the_cost_cap_is_reached() -> None:
    budget = OrchestrationBudget(max_cost_usd=0.10)
    tracker = budget.start_child()
    tracker.record_usage(cost_usd=0.10)
    budget.absorb(tracker)

    assert budget.can_start_child().startswith("cost: $0.1000")
    assert budget.remaining_usd() == 0.0


def test_run_budget_absorbs_child_spend() -> None:
    budget = OrchestrationBudget(max_cost_usd=1.0)
    tracker = budget.start_child()
    tracker.record_usage(cost_usd=0.30)

    budget.absorb(tracker)

    assert budget.spent_usd == pytest.approx(0.30)
    assert budget.remaining_usd() == pytest.approx(0.70)


def test_children_inherit_the_default_child_budget() -> None:
    budget = OrchestrationBudget(child_defaults=Budget(max_turns=4))

    assert budget.start_child().budget.max_turns == 4


def test_budget_exhausted_constant_is_stable() -> None:
    # The stop reason is written into audit records and reports; renaming it would
    # silently change how existing runs read.
    assert BUDGET_EXHAUSTED == "budget_exhausted"


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def test_plan_follows_topological_order_and_inherits_dependencies() -> None:
    plan = plan_from_workflow(_workflow(), name="demo")

    assert [spec.id for spec in plan.subtasks] == ["survey", "write-a", "write-b", "report"]
    report = plan.get("report")
    assert report is not None
    assert report.depends_on == ("write-a", "write-b")
    assert report.condition == "on_failure:write-a"


def test_writes_is_inferred_from_the_referenced_skills_tools() -> None:
    plan = plan_from_workflow(
        _workflow(),
        name="demo",
        skill_tools={"read-tests": ("read_file", "grep_files"), "add-tests": ("write_file",)},
    )

    assert plan.get("survey").writes is False
    assert plan.get("write-a").writes is True


def test_a_skill_without_a_declared_tool_list_is_not_assumed_to_write() -> None:
    plan = plan_from_workflow(_workflow(), name="demo", skill_tools={"read-tests": None})

    assert plan.get("survey").writes is False


def test_overrides_replace_inference() -> None:
    plan = plan_from_workflow(
        _workflow(),
        name="demo",
        skill_tools={"add-tests": ()},  # inference would say "not a writer"
        overrides=__import__(
            "minicode.orchestration.plan", fromlist=["_Overrides"]
        )._Overrides(subtasks={"write-a": {"writes": True, "verify": ["pytest -q"]}}),
    )

    spec = plan.get("write-a")
    assert spec.writes is True
    assert spec.verify == ("pytest -q",)


def test_plan_carries_run_level_caps_from_the_file(tmp_path: Path) -> None:
    config = tmp_path / "orchestration.json"
    config.write_text(
        json.dumps({"maxChildren": 2, "maxConcurrent": 1, "maxCostUsd": 0.25, "model": "cheap"}),
        encoding="utf-8",
    )
    # The loader is exercised in the end-to-end test below; here the plumbing that
    # reads the file is what matters.
    from minicode.orchestration.plan import _read_overrides

    overrides, issues = _read_overrides(config)
    plan = plan_from_workflow(_workflow(), name="demo", overrides=overrides)

    assert issues == []
    assert plan.max_children == 2
    assert plan.max_concurrent == 1
    assert plan.max_cost_usd == 0.25
    assert plan.model == "cheap"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_a_writer_becomes_isolated_when_children_can_run_at_once() -> None:
    spec = SubtaskSpec(id="a", skill="s", writes=True)

    assert resolve_isolation(spec, max_concurrent=3) is Isolation.WORKTREE


def test_a_writer_may_share_when_nothing_runs_beside_it() -> None:
    spec = SubtaskSpec(id="a", skill="s", writes=True)

    assert resolve_isolation(spec, max_concurrent=1) is Isolation.SHARED


def test_a_reader_is_never_copied() -> None:
    spec = SubtaskSpec(id="a", skill="s", writes=False)

    assert resolve_isolation(spec, max_concurrent=8) is Isolation.SHARED


def test_an_explicit_choice_is_respected() -> None:
    spec = SubtaskSpec(id="a", skill="s", writes=True, isolation=Isolation.SHARED)

    assert resolve_isolation(spec, max_concurrent=8) is Isolation.SHARED


# ---------------------------------------------------------------------------
# Validation: what the plan must refuse
# ---------------------------------------------------------------------------


def _plan(**kwargs):  # noqa: ANN001 - test helper
    return plan_from_workflow(_workflow(), name="demo", **kwargs)


def test_unknown_tools_block_the_plan() -> None:
    plan = _plan()
    plan.subtasks = (
        SubtaskSpec(id="a", skill="s", tools_whitelist=("read_file", "teleport")),
    )

    blocking, _warnings = validate_plan(plan)

    assert any("unknown tool" in item and "teleport" in item for item in blocking)


def test_an_empty_tool_whitelist_is_not_unrestricted() -> None:
    """``None`` inherits; ``()`` means no tools at all -- and must not mean "any"."""
    plan = _plan()
    plan.subtasks = (SubtaskSpec(id="a", skill="s", tools_whitelist=()),)

    blocking, _warnings = validate_plan(plan)

    assert blocking == []


def test_a_concurrent_shared_writer_blocks_the_plan() -> None:
    plan = _plan()
    plan.subtasks = (
        SubtaskSpec(id="a", skill="s", writes=True, isolation=Isolation.SHARED),
    )
    plan.max_concurrent = 3

    blocking, _warnings = validate_plan(plan)

    assert any("shared isolation" in item for item in blocking)


def test_the_same_writer_is_fine_when_runs_are_serial() -> None:
    plan = _plan()
    plan.subtasks = (
        SubtaskSpec(id="a", skill="s", writes=True, isolation=Isolation.SHARED),
    )
    plan.max_concurrent = 1

    blocking, _warnings = validate_plan(plan)

    assert blocking == []


def test_a_writer_without_verification_is_warned_about() -> None:
    plan = _plan()
    plan.subtasks = (SubtaskSpec(id="a", skill="s", writes=True),)

    blocking, warnings = validate_plan(plan)

    assert blocking == []
    assert any("unverified" in item for item in warnings)


def test_a_readonly_worktree_is_wasteful_and_says_so() -> None:
    plan = _plan()
    plan.subtasks = (
        SubtaskSpec(id="a", skill="s", writes=False, isolation=Isolation.WORKTREE),
    )

    _blocking, warnings = validate_plan(plan)

    assert any("pays for a copy" in item for item in warnings)


def test_retries_beyond_one_are_warned_about() -> None:
    plan = _plan()
    plan.subtasks = (SubtaskSpec(id="a", skill="s", retry=3),)

    _blocking, warnings = validate_plan(plan)

    assert any("multiplies cost" in item for item in warnings)


def test_duplicate_ids_and_unknown_dependencies_block() -> None:
    plan = _plan()
    plan.subtasks = (
        SubtaskSpec(id="a", skill="s"),
        SubtaskSpec(id="a", skill="s"),
        SubtaskSpec(id="b", skill="s", depends_on=("ghost",)),
    )

    blocking, _warnings = validate_plan(plan)

    assert any("duplicate subtask id" in item for item in blocking)
    assert any("unknown subtask 'ghost'" in item for item in blocking)


def test_more_subtasks_than_max_children_blocks() -> None:
    plan = _plan()
    plan.max_children = 1

    blocking, _warnings = validate_plan(plan)

    assert any("maxChildren is 1" in item for item in blocking)


def test_a_missing_skill_blocks_when_a_registry_is_known() -> None:
    plan = _plan()
    plan.subtasks = (SubtaskSpec(id="a", skill="not-a-skill"),)

    blocking, _warnings = validate_plan(plan, known_skills={"read-tests"})

    assert any("is not in the registry" in item for item in blocking)


# ---------------------------------------------------------------------------
# Loading a plan from a composite skill on disk
# ---------------------------------------------------------------------------


def _write_composite(tmp_path: Path, orchestration: dict | None = None) -> SkillSpec:
    skill_dir = tmp_path / "skills" / "parallel-tests"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: parallel-tests\nkind: composite\ndescription: add tests in parallel\n---\n\n# parallel-tests\n",
        encoding="utf-8",
    )
    (skill_dir / "workflow.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    if orchestration is not None:
        # utf-8-sig on purpose: a BOM is what PowerShell's Set-Content writes, and it
        # must not make the plan disappear.
        (skill_dir / "orchestration.json").write_text(
            json.dumps(orchestration), encoding="utf-8-sig"
        )
    return SkillSpec(
        name="parallel-tests",
        description="add tests in parallel",
        path=str(skill_md),
        source="project",
        kind=SkillKind.COMPOSITE,
    )


def test_orchestration_file_sits_beside_the_skill(tmp_path: Path) -> None:
    spec = _write_composite(tmp_path)

    assert orchestration_file(spec.path).name == "orchestration.json"
    assert orchestration_file(spec.path).parent == Path(spec.path).parent


def test_load_plan_without_overrides_still_produces_a_runable_plan(tmp_path: Path) -> None:
    from minicode.orchestration.plan import load_plan

    spec = _write_composite(tmp_path)

    plan, issues = load_plan(spec)

    assert plan is not None, issues
    assert len(plan.subtasks) == 4


class _StubRegistry:
    """Minimal registry: ``load_plan`` only asks for a skill's declared tools."""

    def __init__(self, tools_by_skill: dict[str, tuple[str, ...]]) -> None:
        self._tools = tools_by_skill

    def names(self) -> list[str]:
        """The installed set -- `load_plan` asks for it to check that a plan only
        references skills that exist."""
        return list(self._tools)

    def get(self, name: str):  # noqa: ANN201 - duck-typed SkillSpec
        tools = self._tools.get(name)
        if tools is None:
            return None
        return type("Spec", (), {"allowed_tools": tools})()


def test_load_plan_reads_overrides_and_infers_writers_from_the_registry(tmp_path: Path) -> None:
    from minicode.orchestration.plan import load_plan

    spec = _write_composite(
        tmp_path,
        {
            "maxConcurrent": 2,
            "subtasks": {"write-a": {"verify": ["pytest -q tests/test_a.py"]}},
        },
    )
    registry = _StubRegistry(
        {
            "read-tests": ("read_file", "grep_files"),
            "add-tests": ("write_file", "read_file"),
            "summarise": ("read_file",),
        }
    )

    plan, issues = load_plan(spec, registry=registry)

    assert plan is not None, issues
    assert plan.max_concurrent == 2
    assert plan.get("survey").writes is False
    assert plan.get("write-a").writes is True  # inferred: the skill may write
    assert plan.get("write-a").verify == ("pytest -q tests/test_a.py",)
    # write-b uses the same writing skill but declares no verification command, so it
    # is reported as unverified rather than quietly counted as proven.
    assert any("unverified" in issue for issue in issues)


def test_without_a_registry_writes_cannot_be_inferred_and_nothing_pretends_otherwise(
    tmp_path: Path,
) -> None:
    """Honest limitation: with no registry there is no tool declaration to read.

    The plan is still usable, but every subtask looks read-only, so the orchestrator
    must pass a registry before trusting isolation decisions.
    """
    from minicode.orchestration.plan import load_plan

    spec = _write_composite(tmp_path)

    plan, issues = load_plan(spec)

    assert plan is not None, issues
    assert all(spec.writes is False for spec in plan.subtasks)


def test_load_plan_refuses_an_unsafe_plan(tmp_path: Path) -> None:
    from minicode.orchestration.plan import load_plan

    spec = _write_composite(
        tmp_path,
        {
            "maxConcurrent": 3,
            "subtasks": {"write-a": {"writes": True, "isolation": "shared"}},
        },
    )

    plan, issues = load_plan(spec)

    assert plan is None
    assert any("shared isolation" in issue for issue in issues)


# ---------------------------------------------------------------------------
# Aggregation vocabulary
# ---------------------------------------------------------------------------


def test_unverified_lists_only_finished_but_unproven_subtasks() -> None:
    result = OrchestrationResult(
        orchestration_id="run-1",
        children=[
            ChildResult(subtask_id="a", status=SubtaskStatus.DONE, verified=True),
            ChildResult(subtask_id="b", status=SubtaskStatus.DONE, verified=False),
            ChildResult(subtask_id="c", status=SubtaskStatus.FAILED, verified=False),
            ChildResult(subtask_id="d", status=SubtaskStatus.SKIPPED),
        ],
    )

    assert result.unverified == ("b",)


def test_result_totals_add_up() -> None:
    result = OrchestrationResult(
        orchestration_id="run-1",
        status=OrchestrationStatus.PARTIAL,
        started_at=100.0,
        finished_at=112.5,
        children=[
            ChildResult(subtask_id="a", cost_usd=0.01, tokens=100),
            ChildResult(subtask_id="b", cost_usd=0.02, tokens=250),
        ],
    )

    payload = result.to_dict()

    assert payload["costUsd"] == pytest.approx(0.03)
    assert payload["tokens"] == 350
    assert payload["durationSeconds"] == pytest.approx(12.5)
    assert payload["status"] == "partial"


def test_skipped_is_not_success() -> None:
    """The distinction the whole status vocabulary exists for."""
    result = OrchestrationResult(
        orchestration_id="run-1",
        children=[ChildResult(subtask_id="a", status=SubtaskStatus.SKIPPED)],
    )

    assert result.unverified == ()
    assert SubtaskStatus.SKIPPED is not SubtaskStatus.DONE
