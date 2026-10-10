"""Regression tests for the defects a real run against a dirty tree exposed.

Both of these were invisible to the offline suite, so they are pinned here with the
exact shape that produced them: a writer whose isolation is refused, with dependents
queued behind it, and a plan whose remaining nodes can never become ready.

The first one breaks a promise this stage makes explicitly -- "no silent gaps, every
subtask has a terminal status" -- and it broke it by dropping nodes from the result
entirely rather than by mislabelling them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minicode.orchestration.audit import OrchestrationAudit, load_entries, replay
from minicode.orchestration.orchestrator import Orchestrator
from minicode.orchestration.plan import _Overrides, plan_from_workflow
from minicode.orchestration.report import format_result
from minicode.orchestration.types import OrchestrationStatus, SubtaskStatus
from minicode.skill_system.composite import parse_workflow

#: The shape from the real run: one writer, then a chain behind it, then a branch that
#: depends on a node in the middle of that chain.
CHAINED = {
    "steps": [
        {"id": "diff", "skill": "s", "description": "collect"},
        {"id": "review", "skill": "s", "depends_on": ["diff"]},
        {"id": "secrets", "skill": "s", "depends_on": ["review"]},
        {"id": "report", "skill": "s", "depends_on": ["secrets"]},
        {"id": "merge-ready", "skill": "s", "depends_on": ["report"]},
    ]
}


class NeverCalledWorker:
    """Fails the test if it runs: the whole point is that nothing should start."""

    def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
        raise AssertionError(f"worker must not run for {spec.id}")


def _repo(tmp_path: Path, *, dirty: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=str(repo), capture_output=True, timeout=60)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, timeout=60)
    code = subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, timeout=60
    ).returncode
    if code != 0:
        pytest.skip("git is not usable here")
    if dirty:
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    return repo


def _plan():
    workflow, issues = parse_workflow(CHAINED, name="code-review")
    assert workflow is not None, issues
    overrides = _Overrides(
        subtasks={name: {"writes": True} for name in ("diff", "review", "secrets", "report", "merge-ready")}
    )
    plan = plan_from_workflow(workflow, name="code-review", overrides=overrides)
    plan.max_concurrent = 3
    return plan


def test_every_subtask_gets_a_terminal_status_even_when_the_chain_is_blocked(
    tmp_path: Path,
) -> None:
    """The silent gap: nodes beyond the first blocked ring used to vanish entirely."""
    repo = _repo(tmp_path, dirty=True)
    plan = _plan()

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    assert len(result.children) == len(plan.subtasks), "a subtask was dropped from the result"
    statuses = {child.subtask_id: child.status for child in result.children}
    assert statuses["diff"] is SubtaskStatus.FAILED
    for name in ("review", "secrets", "report", "merge-ready"):
        assert statuses[name] is SubtaskStatus.SKIPPED, f"{name} should be skipped, not missing"
    assert result.status is OrchestrationStatus.FAILED


def test_a_skipped_subtask_names_the_dependency_that_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, dirty=True)

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    by_id = {child.subtask_id: child for child in result.children}
    # `review` names `diff`; the transitive ones name the node in their own chain.
    assert "diff" in by_id["review"].summary
    assert "review" in by_id["secrets"].summary
    assert "condition not satisfied" not in by_id["review"].summary


def test_the_trail_covers_every_subtask_so_replay_stays_consistent(tmp_path: Path) -> None:
    """Refused and skipped subtasks used to write nothing, so replay called it incomplete."""
    repo = _repo(tmp_path, dirty=True)
    audit = OrchestrationAudit("run1", home=tmp_path)
    plan = _plan()

    result = Orchestrator(
        plan=plan,
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
        audit=audit,
    ).run()

    entries = load_entries(audit.path)
    report = replay(
        entries,
        expected_cost_usd=result.cost_usd,
        expected_children=[child.subtask_id for child in result.children],
    )

    assert report.consistent, report.to_dict()
    assert set(report.per_child) >= {"diff", "review", "secrets", "report", "merge-ready"}
    # The parent's own terminal state is recognised now.
    assert report.per_child["main"]["terminal"] == "failed"


def test_the_rendering_shows_why_isolation_was_refused(tmp_path: Path) -> None:
    """`isolation refused` alone does not tell the reader which file to deal with."""
    repo = _repo(tmp_path, dirty=True)

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=NeverCalledWorker(),
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    rendered = format_result(result)

    assert "Failures:" in rendered
    assert "refusing to isolate" in rendered
    assert "a.txt" in rendered  # the dirty path is named
    assert "Skipped (not run -- not a success):" in rendered


def test_a_plan_referencing_an_uninstalled_skill_is_refused_at_load_time(
    tmp_path: Path,
) -> None:
    """Found in a real run: the "not in the registry" check could never fire.

    The allow-list was built from the names the workflow *references*, so every plan
    passed validation and the missing skills surfaced later, inside `load_skill`, as
    "Unknown skill: review-diff" -- in the middle of a paid run instead of before it.
    """
    from minicode.orchestration.plan import load_plan
    from minicode.skill_system.registry import SkillKind, SkillSpec

    class Registry:
        def names(self) -> list[str]:
            return ["installed-skill"]

        def get(self, name: str):  # noqa: ANN201
            if name == "installed-skill":
                return type("Spec", (), {"allowed_tools": ("read_file",)})()
            return None

    skill_dir = tmp_path / "skills" / "plan-demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: plan-demo\nkind: composite\ndescription: d\n---\n\n# plan-demo\n",
        encoding="utf-8",
    )
    (skill_dir / "workflow.json").write_text(
        '{"steps": [{"id": "a", "skill": "never-installed"}]}', encoding="utf-8"
    )
    spec = SkillSpec(
        name="plan-demo",
        description="d",
        path=str(skill_md),
        source="project",
        kind=SkillKind.COMPOSITE,
    )

    plan, issues = load_plan(spec, registry=Registry())

    assert plan is None
    assert any("is not in the registry" in issue for issue in issues), issues


def test_a_clean_tree_still_runs_the_chain(tmp_path: Path) -> None:
    """The guard must not become a blanket refusal: the same plan works when clean."""
    from minicode.orchestration.types import SubtaskStatus as S
    from minicode.orchestration.worker import WorkerOutcome

    class OkWorker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, spec, *, workdir, tracker, orchestration_id):  # noqa: ANN001, ANN201
            self.calls.append(spec.id)
            tracker.record_turn()
            return WorkerOutcome(status=S.DONE, summary=f"{spec.id} ok", stop_reason="done")

    repo = _repo(tmp_path, dirty=False)
    worker = OkWorker()

    result = Orchestrator(
        plan=_plan(),
        repo_root=repo,
        worker=worker,
        orchestration_id="run1",
        base_dir=tmp_path / "wt",
    ).run()

    assert worker.calls == ["diff", "review", "secrets", "report", "merge-ready"]
    assert result.status is OrchestrationStatus.COMPLETE
    # Every subtask here declares `writes` with no verification command -- exactly the
    # situation the plan preview warns about -- so the run reports them all as
    # unverified rather than implying that finishing proved anything.
    assert set(result.unverified) == {"diff", "review", "secrets", "report", "merge-ready"}

def test_model_output_cannot_change_how_the_report_is_drawn() -> None:
    """A child's summary contained a markdown fence and the TUI's renderer went into
    code-block mode, garbling the replay and swallowing the next command's output.

    Report lines carry model output, so they must be collapsed to one line and stripped
    of the markers that steer a renderer.
    """
    from minicode.orchestration.audit import AuditEntry
    from minicode.orchestration.types import one_line

    entry = AuditEntry(
        seq=1,
        event="child_end",
        orchestration_id="run1",
        detail="Here is one line:\n\n```python\nd\n```\nand more",
    )

    line = entry.describe()

    assert "\n" not in line
    assert "```" not in line
    assert "`" not in line
    assert one_line("a\nb") == "a b"
    assert len(one_line("x" * 500)) == 80

def test_an_isolated_writer_can_still_see_the_project_skills_and_they_stay_out_of_its_patch(
    tmp_path: Path,
) -> None:
    """A real run showed `Unknown skill: review-diff` inside every writer sandbox.

    `git worktree add` materialises tracked content only, and this project keeps its
    skills under a gitignored `.mini-code/`.  The mirror fixes the child's view; the
    scaffolding record keeps it out of the diff, because what the child needed in order
    to work is not what the child produced.
    """
    from minicode.orchestration.worktree import WorktreeManager

    repo = _repo(tmp_path, dirty=False)
    skills = repo / ".mini-code" / "skills" / "describe-file"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: describe-file\nkind: atomic\n---\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".mini-code/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(repo), capture_output=True, timeout=60)
    subprocess.run(["git", "commit", "-m", "ignore"], cwd=str(repo), capture_output=True, timeout=60)

    manager = WorktreeManager(repo, orchestration_id="run1", base_dir=tmp_path / "wt")
    session = manager.prepare("write-a")

    # Without the mirror the skill is simply absent, which is the defect.
    assert not (session.path / ".mini-code").exists()
    copied = manager.mirror_scaffolding(session, (".mini-code/skills",))
    session.scaffolding = tuple(copied)

    assert (session.path / ".mini-code" / "skills" / "describe-file" / "SKILL.md").exists()
    # It must not be mistaken for the subtask's work.
    assert manager.changes(session) == ()
    (session.path / "new.txt").write_text("real work\n", encoding="utf-8")
    assert manager.changes(session) == ("new.txt",)
    sha = manager.commit(session, "work")
    patch = manager.export_patch(session, tmp_path / "write-a.patch").read_text(encoding="utf-8")
    assert "new.txt" in patch
    assert ".mini-code" not in patch
    assert len(sha) == 40

def test_the_instruction_says_the_orchestrator_runs_the_verification() -> None:
    """A real child read "your work must leave these commands passing" as "run this tool"
    and answered "The verification command isn't available to me as a tool" -- while the
    orchestrator had in fact run it and passed.  The brief has to say who runs it.
    """
    from minicode.orchestration.types import SubtaskSpec
    from minicode.orchestration.worker import render_instruction

    text = render_instruction(
        SubtaskSpec(id="a", skill="s", verify=("pytest -q",), writes=True)
    )

    assert "the orchestrator will run these commands" in text
    assert "not tools available to you" in text
    assert "pytest -q" in text
