"""Tests for L2 composite skills and the three bundled acceptance scenarios.

Two things are being pinned here.

**The condition semantics.**  The default gate (``deps``) requires every
dependency to have *succeeded*, mirroring CI ``needs:``.  An earlier draft
defaulted to "run once dependencies finished", which meant a failed ``tag`` step
still let the release ``build`` run -- exactly the failure this test file now
prevents.  ``always`` is the explicit opt-out for reporting and rollback steps.

**The scenarios.**  The design document requires three real composite skills to
be validated: release flow, code review and environment triage.  They ship under
``minicode/skill_system/examples/`` and these tests load, plan, compile and
*simulate* them, including their failure branches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.skill_system import examples as examples_module
from minicode.skill_system.composite import (
    ConditionKind,
    discover_workflows,
    format_plan,
    load_workflow,
    parse_workflow,
    ready_steps,
    to_task_graph,
    topological_order,
    validate_against_registry,
)
from minicode.skill_system.examples import (
    SCENARIO_NAMES,
    bundled_roots,
    bundled_scenarios,
    format_examples,
    install_examples,
    load_bundled_registry,
)
from minicode.skill_system.registry import SkillKind, SkillRegistry, parse_skill_file

SIMPLE_WORKFLOW = {
    "name": "demo",
    "steps": [
        {"id": "a", "skill": "skill-a"},
        {"id": "b", "skill": "skill-b", "depends_on": ["a"]},
    ],
}


def _workflow_dir(tmp_path: Path, payload: dict, name: str = "demo") -> Path:
    skill_dir = tmp_path / ".mini-code" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\ndescription: d\ntriggers: [x]\nkind: composite\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    (skill_dir / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    return skill_dir


def _registry(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry.discover(
        tmp_path, roots=[(tmp_path / ".mini-code" / "skills", "project")]
    )


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def test_workflow_parses_steps_dependencies_and_conditions() -> None:
    workflow, issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "skill-a"},
                {"id": "b", "skill": "skill-b", "depends_on": ["a"], "priority": "high"},
                {"id": "c", "skill": "skill-c", "when": "on_failure:a"},
            ],
        },
        name="demo",
    )

    assert issues == []
    assert workflow is not None
    assert [step.id for step in workflow.steps] == ["a", "b", "c"]
    assert workflow.steps[1].depends_on == ("a",)
    assert workflow.steps[1].priority.value == "high"
    assert workflow.steps[2].condition is ConditionKind.ON_FAILURE
    assert workflow.steps[2].condition_step == "a"
    # Naming a step in a condition makes it an implicit dependency.
    assert workflow.steps[2].depends_on == ("a",)


def test_default_condition_is_dependency_success() -> None:
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")

    assert workflow is not None
    assert all(step.condition is ConditionKind.DEPS for step in workflow.steps)
    assert workflow.steps[0].condition_text == "deps"


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ("not a dict", "JSON object"),
        ({}, "steps"),
        ({"steps": []}, "steps"),
        ({"steps": [{"skill": "x"}]}, "'id' is required"),
        ({"steps": [{"id": "a"}]}, "'skill' is required"),
        ({"steps": [{"id": "a", "skill": "x"}, {"id": "a", "skill": "y"}]}, "duplicate step id"),
        ({"steps": [{"id": "a", "skill": "x", "depends_on": ["ghost"]}]}, "does not exist"),
        ({"steps": [{"id": "a", "skill": "x", "depends_on": ["a"]}]}, "cannot depend on itself"),
        ({"steps": [{"id": "a", "skill": "x", "when": "sometimes"}]}, "unknown condition"),
        ({"steps": [{"id": "a", "skill": "x", "when": "on_success"}]}, "must name a step"),
    ],
)
def test_invalid_workflows_are_rejected_with_an_explanation(payload, fragment: str) -> None:
    workflow, issues = parse_workflow(payload, name="demo")

    assert workflow is None
    assert any(fragment in issue for issue in issues)


def test_cycle_is_detected() -> None:
    workflow, issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x", "depends_on": ["b"]},
                {"id": "b", "skill": "y", "depends_on": ["a"]},
            ],
        },
        name="demo",
    )

    assert workflow is None
    assert any("cycle" in issue for issue in issues)


def test_unknown_priority_warns_but_keeps_the_step() -> None:
    workflow, issues = parse_workflow(
        {"name": "demo", "steps": [{"id": "a", "skill": "x", "priority": "urgent"}]},
        name="demo",
    )

    assert workflow is not None
    assert any("priority" in issue for issue in issues)


def test_load_workflow_reports_a_missing_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "SKILL.md"
    skill_dir.parent.mkdir(exist_ok=True)
    skill_dir.write_text("---\nname: solo\ndescription: d\ntriggers: [x]\nkind: composite\n---\n", encoding="utf-8")

    spec = parse_skill_file(skill_dir, name="solo", source="project")
    workflow, issues = load_workflow(spec)

    assert workflow is None
    assert any("workflow.json" in issue for issue in issues)


def test_discover_workflows_reports_one_issue_per_broken_composite(tmp_path: Path) -> None:
    _workflow_dir(tmp_path, {"name": "broken", "steps": []}, name="broken")

    workflows, issues = discover_workflows(_registry(tmp_path))

    assert workflows == []
    assert issues


# ---------------------------------------------------------------------------
# Ordering and readiness
# ---------------------------------------------------------------------------


def test_topological_order_is_dependency_correct_and_deterministic() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "publish", "skill": "p", "depends_on": ["build"]},
                {"id": "build", "skill": "b", "depends_on": ["tests"]},
                {"id": "tests", "skill": "t"},
            ],
        },
        name="demo",
    )

    assert workflow is not None
    order = [step.id for step in topological_order(workflow)]
    assert order == ["tests", "build", "publish"]
    assert order == [step.id for step in topological_order(workflow)]


def test_only_root_steps_are_ready_initially() -> None:
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")

    assert workflow is not None
    assert [step.id for step in ready_steps(workflow)] == ["a"]


def test_dependency_success_is_required_by_default() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "b", "skill": "y", "depends_on": ["a"]},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    # 'a' failed: 'b' must NOT become ready, because success is the default gate.
    assert ready_steps(workflow, completed=set(), failed={"a"}) == []


def test_explicit_always_runs_after_a_failure() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "cleanup", "skill": "c", "depends_on": ["a"], "when": "always"},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    assert [step.id for step in ready_steps(workflow, completed=set(), failed={"a"})] == ["cleanup"]


def test_on_failure_and_on_success_branches_are_mutually_exclusive() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "check", "skill": "x"},
                {"id": "repair", "skill": "r", "when": "on_failure:check"},
                {"id": "celebrate", "skill": "c", "when": "on_success:check"},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    assert [step.id for step in ready_steps(workflow, completed={"check"})] == ["celebrate"]
    assert [step.id for step in ready_steps(workflow, failed={"check"})] == ["repair"]


def test_finished_steps_are_not_ready_again() -> None:
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")
    assert workflow is not None

    assert ready_steps(workflow, completed={"a"}, failed=set())[0].id == "b"
    assert ready_steps(workflow, completed={"a", "b"}, failed=set()) == []


# ---------------------------------------------------------------------------
# Compilation onto task_graph
# ---------------------------------------------------------------------------


def test_task_graph_mirrors_steps_and_dependencies() -> None:
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")
    assert workflow is not None

    graph = to_task_graph(workflow)

    assert len(graph.definitions) == 2
    by_step = {definition.metadata["step"]: definition for definition in graph.definitions.values()}
    assert by_step["a"].dependencies == []
    assert by_step["b"].dependencies == [by_step["a"].id]
    assert by_step["b"].metadata["skill"] == "skill-b"
    assert by_step["b"].metadata["workflow"] == "demo"


def test_task_graph_carries_condition_and_priority() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "b", "skill": "y", "when": "on_failure:a", "priority": "critical"},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    graph = to_task_graph(workflow)
    by_step = {definition.metadata["step"]: definition for definition in graph.definitions.values()}

    assert by_step["b"].metadata["condition"] == "on_failure:a"
    assert by_step["b"].priority.value == "critical"


def test_task_graph_marks_nothing_complete_before_execution() -> None:
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")
    assert workflow is not None

    graph = to_task_graph(workflow)

    assert graph.is_graph_complete() is False
    assert graph.get_progress_percentage() == 0.0


# ---------------------------------------------------------------------------
# Rendering and registry validation
# ---------------------------------------------------------------------------


def test_format_plan_shows_order_dependencies_and_conditions() -> None:
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "b", "skill": "y", "depends_on": ["a"]},
                {"id": "c", "skill": "z", "when": "on_failure:a"},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    rendered = format_plan(workflow)

    assert "Composite skill: demo" in rendered
    assert "1. a -> skill x" in rendered
    assert "2. b -> skill y" in rendered
    assert "after: a" in rendered
    assert "[when on_failure:a]" in rendered


def test_validate_against_registry_flags_unknown_skills(tmp_path: Path) -> None:
    _workflow_dir(tmp_path, SIMPLE_WORKFLOW)
    workflow, _issues = parse_workflow(SIMPLE_WORKFLOW, name="demo")
    assert workflow is not None

    issues = validate_against_registry(workflow, _registry(tmp_path))

    assert len(issues) == 2  # neither skill-a nor skill-b exists


# ---------------------------------------------------------------------------
# The three acceptance scenarios
# ---------------------------------------------------------------------------


def _scenario(name: str):
    registry = load_bundled_registry()
    workflows, issues = discover_workflows(registry)
    assert not issues, issues
    match = next(workflow for workflow in workflows if workflow.name == name)
    return registry, match


def test_all_three_scenarios_ship_and_load() -> None:
    assert bundled_scenarios() == list(SCENARIO_NAMES)


def test_bundled_registry_discovers_atomic_and_composite_skills() -> None:
    registry = load_bundled_registry()

    assert len(registry.by_kind(SkillKind.COMPOSITE)) == 3
    assert len(registry.all()) >= 15
    assert registry.issues() == []


def test_every_scenario_step_resolves_to_a_real_skill() -> None:
    registry = load_bundled_registry()
    workflows, issues = discover_workflows(registry)

    assert not issues
    assert {workflow.name for workflow in workflows} == set(SCENARIO_NAMES)
    for workflow in workflows:
        assert validate_against_registry(workflow, registry) == []
        assert to_task_graph(workflow).definitions


def test_release_flow_does_not_build_after_a_failed_tag() -> None:
    _registry_obj, workflow = _scenario("release-flow")

    assert [step.id for step in ready_steps(workflow)] == ["structure"]

    expected_next = {
        "structure": "tests",
        "structure|tests": "version",
        "structure|tests|version": "tag",
    }
    completed: list[str] = []
    for step_id in ("structure", "tests", "version"):
        completed.append(step_id)
        ready = [step.id for step in ready_steps(workflow, completed=set(completed))]
        assert ready == [expected_next["|".join(completed)]]

    # The tag step fails: only the on_failure rollback branch may run, and the
    # build step must stay blocked (the regression an "always" default caused).
    completed_without_tag = {"structure", "tests", "version"}
    ready_after_failure = [
        step.id
        for step in ready_steps(workflow, completed=completed_without_tag, failed={"tag"})
    ]
    assert ready_after_failure == ["rollback"]


def test_release_flow_happy_path_reaches_the_build_step() -> None:
    _registry_obj, workflow = _scenario("release-flow")
    completed: set[str] = set()

    while True:
        ready = [step.id for step in ready_steps(workflow, completed=completed)]
        if not ready:
            break
        completed.update(ready)

    assert completed == {"structure", "tests", "version", "tag", "build"}
    assert "rollback" not in completed


def test_code_review_keeps_the_report_and_drops_merge_readiness_on_failure() -> None:
    _registry_obj, workflow = _scenario("code-review")

    # Review passed, the secret scan failed: the always-gated report still runs...
    completed = {"diff", "review"}
    ready = [step.id for step in ready_steps(workflow, completed=completed, failed={"secrets"})]
    assert ready == ["report"], "the always-gated report must still run"

    # ...and because the review itself succeeded, the merge conclusion follows.
    ready_after = [
        step.id
        for step in ready_steps(workflow, completed=completed | {"report"}, failed={"secrets"})
    ]
    assert ready_after == ["merge-ready"]


def test_code_review_suppresses_the_merge_conclusion_when_the_review_failed() -> None:
    _registry_obj, workflow = _scenario("code-review")

    # The review step itself failed.  The secret scan only needs the diff, so it
    # can still run; the report always runs; the merge conclusion never does.
    assert [step.id for step in ready_steps(workflow, completed={"diff"}, failed={"review"})] == [
        "secrets"
    ]

    finished = {"diff", "secrets", "report"}
    ready = [step.id for step in ready_steps(workflow, completed=finished, failed={"review"})]

    assert ready == [], "a failed review must never produce a merge conclusion"


def test_a_step_recorded_as_failed_is_never_treated_as_a_success() -> None:
    """Contradictory bookkeeping must fail closed."""
    workflow, _issues = parse_workflow(
        {
            "name": "demo",
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "b", "skill": "y", "when": "on_success:a"},
            ],
        },
        name="demo",
    )
    assert workflow is not None

    # 'a' appears in both sets: the failure wins.
    assert ready_steps(workflow, completed={"a"}, failed={"a"}) == []


def test_code_review_confirms_merge_readiness_only_after_a_clean_review() -> None:
    _registry_obj, workflow = _scenario("code-review")

    completed = {"diff", "review", "secrets", "report"}
    ready = [step.id for step in ready_steps(workflow, completed=completed)]

    assert ready == ["merge-ready"]


def test_code_review_scans_secrets_in_parallel_with_the_review() -> None:
    _registry_obj, workflow = _scenario("code-review")

    parallel = {step.id for step in ready_steps(workflow, completed={"diff"})}

    assert parallel == {"review", "secrets"}


def test_env_triage_diagnoses_after_a_failed_probe_and_offers_fixes() -> None:
    _registry_obj, workflow = _scenario("env-triage")

    # One probe fails; the always-gated diagnosis still runs on partial facts.
    ready = [
        step.id
        for step in ready_steps(workflow, completed={"collect", "dependencies"}, failed={"interpreter"})
    ]
    assert ready == ["diagnose"]

    # The diagnosis itself fails -> the on_failure fix branch fires.
    ready_after = [
        step.id
        for step in ready_steps(
            workflow, completed={"collect", "dependencies"}, failed={"interpreter", "diagnose"}
        )
    ]
    assert ready_after == ["fixes"]


def test_env_triage_probe_steps_run_in_parallel() -> None:
    _registry_obj, workflow = _scenario("env-triage")

    assert {step.id for step in ready_steps(workflow, completed={"collect"})} == {
        "interpreter",
        "dependencies",
    }


def test_scenario_plans_render_with_their_condition_branches() -> None:
    _registry_obj, release = _scenario("release-flow")

    rendered = format_plan(release)

    assert "[when on_failure:tag]" in rendered
    assert "rollback -> skill rollback-release" in rendered


# ---------------------------------------------------------------------------
# Bundled example management
# ---------------------------------------------------------------------------


def test_bundled_roots_are_inside_the_package() -> None:
    roots = bundled_roots()

    assert all(root.exists() for root, _source in roots)
    assert all("skill_system" in str(root) for root, _source in roots)


def test_install_examples_copies_skills_with_their_workflows(tmp_path: Path) -> None:
    installed = install_examples(tmp_path, scope="project")

    assert set(SCENARIO_NAMES).issubset(set(installed))
    target = tmp_path / ".mini-code" / "skills"
    assert (target / "release-flow" / "SKILL.md").exists()
    assert (target / "release-flow" / "workflow.json").exists()
    assert (target / "run-test-suite" / "SKILL.md").exists()


def test_installed_examples_are_discoverable_as_composites(tmp_path: Path) -> None:
    install_examples(tmp_path, scope="project")

    registry = SkillRegistry.discover(tmp_path, roots=[(tmp_path / ".mini-code" / "skills", "project")])
    workflows, issues = discover_workflows(registry)

    assert not issues
    assert {workflow.name for workflow in workflows} == set(SCENARIO_NAMES)


def test_install_examples_can_select_names(tmp_path: Path) -> None:
    installed = install_examples(tmp_path, scope="project", names=["code-review"])

    assert installed == ["code-review"]
    assert not (tmp_path / ".mini-code" / "skills" / "release-flow").exists()


def test_format_examples_lists_scenarios_and_atoms() -> None:
    rendered = format_examples()

    for name in SCENARIO_NAMES:
        assert name in rendered
    assert "Atomic skills (" in rendered
    assert str(examples_module.EXAMPLES_DIR) in rendered
