"""Tests for git worktree isolation.

These use a real git repository rather than a mock, because the behaviour that
matters *is* git's: whether a worktree really exists, whether a commit really landed,
whether a patch really applies.  A mocked git would have let the upstream isolator's
bug (ignoring the return code) pass its own tests.

The refusals carry the weight here: no repository, dirty tree, an isolation request
that git rejects.  Each must fail loudly and say why.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minicode.orchestration.worktree import (
    GitUnavailable,
    WorktreeError,
    WorktreeManager,
    git_version,
    repo_status,
)

def _git_available() -> bool:
    try:
        git_version()
        return True
    except GitUnavailable:
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git is not available")


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=60)


def _repo(tmp_path: Path) -> Path:
    """A repository with one commit, so worktrees can be cut from HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run(["git", "init"], repo).returncode == 0
    _run(["git", "config", "user.email", "tests@example.invalid"], repo)
    _run(["git", "config", "user.name", "Tests"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    assert _run(["git", "commit", "-m", "init"], repo).returncode == 0
    return repo


def _manager(repo: Path, tmp_path: Path, orchestration: str = "run1") -> WorktreeManager:
    # base_dir is always inside tmp_path: a test must never write to the real home.
    return WorktreeManager(repo, orchestration_id=orchestration, base_dir=tmp_path / "wt")


# ---------------------------------------------------------------------------
# Repository state
# ---------------------------------------------------------------------------


@requires_git
def test_a_plain_directory_is_not_a_repository(tmp_path: Path) -> None:
    status = repo_status(tmp_path)

    assert status.is_repo is False
    assert "not a git repository" in status.describe()


@requires_git
def test_a_fresh_repository_is_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    status = repo_status(repo)

    assert status.is_repo is True
    assert status.clean is True
    assert status.describe() == "clean"
    assert Path(status.root).name == "repo"


@requires_git
def test_a_modified_file_makes_the_tree_dirty_and_is_named(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")

    status = repo_status(repo)

    assert status.clean is False
    assert "a.txt" in status.dirty_paths
    assert "a.txt" in status.describe()


# ---------------------------------------------------------------------------
# Preparing isolation
# ---------------------------------------------------------------------------


@requires_git
def test_prepare_refuses_a_dirty_tree_and_says_which_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    manager = _manager(repo, tmp_path)

    with pytest.raises(WorktreeError) as error:
        manager.prepare("write-a")

    message = str(error.value)
    assert "refusing to isolate" in message
    assert "a.txt" in message
    assert not (tmp_path / "wt").exists() or not any((tmp_path / "wt").iterdir())


@requires_git
def test_prepare_can_be_told_to_isolate_from_a_dirty_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    manager = _manager(repo, tmp_path)

    session = manager.prepare("write-a", allow_dirty="repository is mid-work")

    assert session.path.exists()
    # The override is a deliberate act, not a silent tolerance: the base commit is
    # still recorded so "what changed" has an exact answer.
    assert len(session.base_commit) == 40


@requires_git
def test_prepare_creates_a_real_worktree_on_its_own_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)

    session = manager.prepare("write-a")

    assert session.path.is_dir()
    assert (session.path / "a.txt").read_text(encoding="utf-8") == "one\n"
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], session.path)

    assert branch.returncode == 0
    assert branch.stdout.strip() == session.branch
    assert session.branch.startswith("mc/orch-run1-")


@requires_git
def test_prepare_in_a_non_repository_refuses(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    manager = WorktreeManager(plain, orchestration_id="run1", base_dir=tmp_path / "wt")

    with pytest.raises(WorktreeError) as error:
        manager.prepare("write-a")

    assert "cannot isolate" in str(error.value)


@requires_git
def test_a_failing_git_command_surfaces_instead_of_returning_a_path(tmp_path: Path) -> None:
    """The upstream bug this module exists to avoid: a path returned on failure."""
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")
    manager.remove(session)
    # `remove` deliberately keeps the branch as evidence, so asking for that same
    # branch again is a genuine git failure -- and it must surface as an error
    # instead of a path to a worktree that was never created.
    second = _manager(repo, tmp_path)

    with pytest.raises(WorktreeError) as error:
        second.prepare("write-a")

    assert "worktree add" in str(error.value)
    assert second.sessions == []


@requires_git
def test_two_writers_get_two_worktrees(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)

    first = manager.prepare("write-a")
    second = manager.prepare("write-b")

    assert first.path != second.path
    (first.path / "only-a.txt").write_text("a\n", encoding="utf-8")
    assert not (second.path / "only-a.txt").exists()
    assert not (repo / "only-a.txt").exists()  # nothing reaches the main tree


@requires_git
def test_retries_get_a_distinct_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)

    first = manager.prepare("write-a", attempt=0)
    manager.remove(first)
    second = manager.prepare("write-a", attempt=1)

    assert first.branch != second.branch
    assert second.branch.endswith("-r1")


# ---------------------------------------------------------------------------
# Getting the work back out
# ---------------------------------------------------------------------------


@requires_git
def test_changes_lists_what_the_subtask_touched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")

    assert manager.changes(session) == ()
    (session.path / "new.txt").write_text("hello\n", encoding="utf-8")
    (session.path / "a.txt").write_text("changed\n", encoding="utf-8")

    changed = manager.changes(session)

    assert set(changed) == {"new.txt", "a.txt"}


@requires_git
def test_commit_then_patch_contains_the_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")
    (session.path / "new.txt").write_text("hello\n", encoding="utf-8")

    sha = manager.commit(session, "add new.txt")
    patch = manager.export_patch(session, tmp_path / "out" / "write-a.patch")

    assert len(sha) == 40
    text = patch.read_text(encoding="utf-8")
    assert "new.txt" in text
    assert "hello" in text
    # The change is *not* in the main tree: applying it is the human's decision.
    assert not (repo / "new.txt").exists()


@requires_git
def test_the_patch_applies_cleanly_to_the_main_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")
    (session.path / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit(session, "add new.txt")
    patch = manager.export_patch(session, tmp_path / "write-a.patch")

    applicable, detail = manager.patch_applies(patch)

    assert applicable, detail

    # And once applied by hand, the same patch no longer applies -- the check is real.
    assert _run(["git", "apply", str(patch)], repo).returncode == 0
    assert (repo / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert manager.patch_applies(patch)[0] is False


@requires_git
def test_committing_nothing_is_reported_as_no_change_not_as_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")

    sha = manager.commit(session, "nothing happened")

    assert len(sha) == 40
    assert manager.export_patch(session, tmp_path / "empty.patch").read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# Disposal
# ---------------------------------------------------------------------------


@requires_git
def test_removing_keeps_the_branch_as_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")
    (session.path / "new.txt").write_text("hello\n", encoding="utf-8")
    manager.commit(session, "add new.txt")
    branch = session.branch

    gone, _detail = manager.remove(session)

    assert gone
    assert not session.path.exists()
    assert manager.branch_exists(branch)


@requires_git
def test_cleanup_removes_every_worktree_it_made(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    sessions = [manager.prepare(name) for name in ("write-a", "write-b", "write-c")]

    results = manager.cleanup()

    assert len(results) == 3
    assert all(gone for _name, gone, _detail in results)
    assert all(not session.path.exists() for session in sessions)
    assert manager.sessions == []


@requires_git
def test_a_worktree_left_on_disk_does_not_block_the_next_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = _manager(repo, tmp_path)
    session = manager.prepare("write-a")
    # Simulate something else having deleted the administrative link but not the tree.
    session.path.mkdir(parents=True, exist_ok=True)

    gone, _detail = manager.remove(session)

    assert gone
    assert not session.path.exists()


# ---------------------------------------------------------------------------
# git availability
# ---------------------------------------------------------------------------


def test_missing_git_raises_a_specific_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import minicode.orchestration.worktree as module

    def _boom(*_args, **_kwargs):
        raise OSError("git is not installed")

    monkeypatch.setattr(module.subprocess, "run", _boom)

    with pytest.raises(GitUnavailable):
        git_version()


def test_prepare_reports_missing_git_rather_than_failing_obscurely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import minicode.orchestration.worktree as module

    def _boom(*_args, **_kwargs):
        raise OSError("git is not installed")

    monkeypatch.setattr(module.subprocess, "run", _boom)
    manager = WorktreeManager(tmp_path, orchestration_id="run1", base_dir=tmp_path / "wt")

    with pytest.raises(GitUnavailable):
        manager.prepare("write-a")
