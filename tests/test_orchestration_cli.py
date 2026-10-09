"""Tests for ``python -m minicode.orchestration``.

The CLI is the surface a human inspects a plan from, so its *refusals* matter more
than its pretty-printing: a plan that failed validation must not start, a dry run must
spend nothing, and a replay that does not add up must exit non-zero.  These run
in-process (no subprocess) so they stay fast and the exit codes are the real ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.orchestration.__main__ import main
from minicode.orchestration.audit import OrchestrationAudit

WORKFLOW = {
    "steps": [
        {"id": "a", "skill": "atom"},
        {"id": "b", "skill": "atom", "depends_on": ["a"]},
    ]
}


def _workspace(tmp_path: Path, *, composite: bool = True, orchestration: dict | None = None) -> Path:
    """A workspace whose registry can find one composite skill."""
    skills = tmp_path / ".mini-code" / "skills"
    for name, kind in (("plan-demo", "composite"), ("atom", "atomic")):
        folder = skills / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\nkind: {kind}\ndescription: {name}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    if composite:
        (skills / "plan-demo" / "workflow.json").write_text(
            json.dumps(WORKFLOW), encoding="utf-8"
        )
    if orchestration is not None:
        (skills / "plan-demo" / "orchestration.json").write_text(
            json.dumps(orchestration), encoding="utf-8"
        )
    return tmp_path


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_previews_without_running_anything(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    workspace = _workspace(tmp_path)

    code = main(["--cwd", str(workspace), "plan", "plan-demo"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Plan: plan-demo" in out
    assert "Subtasks: 2" in out
    assert "concurrency" in out


def test_plan_can_write_the_plan_as_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    workspace = _workspace(tmp_path)
    target = tmp_path / "plan.json"

    code = main(["--cwd", str(workspace), "--json", str(target), "plan", "plan-demo"])

    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [item["id"] for item in payload["subtasks"]] == ["a", "b"]


def test_plan_rejects_a_skill_that_does_not_exist(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    workspace = _workspace(tmp_path)

    code = main(["--cwd", str(workspace), "plan", "nope"])
    err = capsys.readouterr().err

    assert code == 2
    assert "no skill named" in err


def test_plan_rejects_an_atomic_skill(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    workspace = _workspace(tmp_path)

    code = main(["--cwd", str(workspace), "plan", "atom"])
    err = capsys.readouterr().err

    assert code == 2
    assert "not a composite plan" in err


def test_plan_refuses_an_unsafe_plan_and_explains(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    workspace = _workspace(
        tmp_path,
        orchestration={"maxConcurrent": 3, "subtasks": {"a": {"writes": True, "isolation": "shared"}}},
    )

    code = main(["--cwd", str(workspace), "plan", "plan-demo"])
    captured = capsys.readouterr()

    assert code == 1
    assert "cannot be run" in captured.err
    assert "shared isolation" in captured.err


# ---------------------------------------------------------------------------
# run (dry)
# ---------------------------------------------------------------------------


def test_a_dry_run_validates_and_spends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    workspace = _workspace(tmp_path)

    code = main(["--cwd", str(workspace), "--home", str(tmp_path / "runs"), "run", "plan-demo", "--dry-run"])
    out = capsys.readouterr().out

    assert code == 0
    assert "nothing was executed, and nothing was spent" in out
    assert not (tmp_path / "runs").exists()  # no trail, because nothing ran


def test_a_dry_run_still_refuses_an_invalid_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    workspace = _workspace(
        tmp_path,
        orchestration={"maxConcurrent": 2, "subtasks": {"a": {"writes": True, "isolation": "shared"}}},
    )

    code = main(["--cwd", str(workspace), "run", "plan-demo", "--dry-run"])
    err = capsys.readouterr().err

    assert code == 1
    assert "Refusing to run" in err


# ---------------------------------------------------------------------------
# agents / replay
# ---------------------------------------------------------------------------


def _trail(home: Path, run_id: str, *, gap: bool = False, cost: float = 0.02) -> Path:
    audit = OrchestrationAudit(run_id, home=home)
    audit.record("run_start")
    audit.record("child_start", subtask_id="a")
    if not gap:
        from minicode.orchestration.types import ChildResult, SubtaskStatus

        audit.record_result(ChildResult(subtask_id="a", status=SubtaskStatus.DONE, cost_usd=cost))
    audit.record("run_end", decision="complete" if not gap else "partial", cost_usd=0.0 if gap else cost)
    return audit.path


def test_agents_says_so_when_there_is_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["--home", str(tmp_path / "runs"), "agents"])
    out = capsys.readouterr().out

    assert code == 0
    assert "No orchestrations recorded" in out


def test_agents_lists_runs_with_their_status(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    home = tmp_path / "runs"
    _trail(home, "run-old")
    _trail(home, "run-new")

    code = main(["--home", str(home), "agents"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Orchestrations (2)" in out
    assert "run-old" in out and "run-new" in out
    assert "complete" in out


def test_replay_shows_a_consistent_trail(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    home = tmp_path / "runs"
    _trail(home, "run1")

    code = main(["--home", str(home), "replay", "run1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Orchestration replay" in out
    assert "every started subtask reported" in out


def test_replay_exits_non_zero_when_the_trail_has_a_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    home = tmp_path / "runs"
    _trail(home, "run1", gap=True)

    code = main(["--home", str(home), "replay", "run1"])
    out = capsys.readouterr().out

    assert code == 1
    assert "GAP" in out


def test_replay_defaults_to_the_latest_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    home = tmp_path / "runs"
    _trail(home, "run1")
    import time

    time.sleep(0.02)
    _trail(home, "run2")
    from minicode.orchestration.audit import latest_orchestration

    # The timeline deliberately does not print the run id (entries are attributed to
    # subtasks), so "which run" is asserted on the lookup rather than on the rendering.
    assert latest_orchestration(home=home) == "run2"

    code = main(["--home", str(home), "replay"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Orchestration replay" in out


def test_replay_reports_an_unknown_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["--home", str(tmp_path / "runs"), "replay", "ghost"])
    err = capsys.readouterr().err

    assert code == 1
    assert "No audit trail" in err


def test_replay_can_write_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    home = tmp_path / "runs"
    _trail(home, "run1")
    target = tmp_path / "replay.json"

    code = main(["--home", str(home), "--json", str(target), "replay", "run1"])

    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["consistent"] is True
    assert payload["perChild"]["a"]["terminal"] == "done"
    assert payload["totalCostUsd"] == pytest.approx(0.02)


def test_no_subcommand_prints_help(capsys: pytest.CaptureFixture) -> None:
    code = main([])
    out = capsys.readouterr().out

    assert code == 2
    assert "usage:" in out.lower()
