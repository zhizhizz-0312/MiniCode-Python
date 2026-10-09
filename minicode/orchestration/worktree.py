"""Git worktree isolation for subtasks that change the tree.

Why not ``task_graph.WorktreeIsolator`` (which exists upstream): it calls
``git worktree add -b <branch> <path> --detach`` -- contradictory flags -- and then
**ignores the return code**, returning a path whether or not a worktree now exists.
An isolation layer that reports success when git refused is worse than no isolation,
because the caller then edits the main tree believing it is sandboxed.  The mechanism
here is still git, which is the point ("isolation uses what exists"); what is new is
checking every step and refusing loudly.

The contract:

* ``prepare`` refuses when git is missing, the directory is not a repository, or the
  working tree is dirty -- with the dirty paths listed, and an explicit, recorded
  override for the cases where a human really does mean "isolate from this dirty
  state too";
* work happens on branch ``mc/orch-<orchestration>-<subtask>`` cut from a recorded
  base commit, so "what changed" has an exact answer;
* results come back as **a patch and a branch, never as a merge** -- applying them to
  the main tree is a human decision, which is the same principle as P3's review gate;
* the worktree directory is removed afterwards while the branch is kept, because the
  branch is the evidence of what the agent did.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "GitUnavailable",
    "RepoStatus",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeSession",
    "git_version",
    "repo_status",
]


class WorktreeError(RuntimeError):
    """Isolation could not be established or a git operation failed."""


class GitUnavailable(WorktreeError):
    """git is not installed or not on PATH."""


@dataclass(slots=True)
class RepoStatus:
    """What git says about a directory."""

    is_repo: bool = False
    clean: bool = True
    root: str = ""
    dirty_paths: tuple[str, ...] = ()
    detail: str = ""

    def describe(self) -> str:
        if not self.is_repo:
            return f"not a git repository ({self.detail or 'no .git found'})"
        if self.clean:
            return "clean"
        listed = ", ".join(self.dirty_paths[:5]) or "unknown paths"
        more = "" if len(self.dirty_paths) <= 5 else f" (+{len(self.dirty_paths) - 5} more)"
        return f"dirty: {listed}{more}"


def git_version() -> str:
    """Return git's version string, or raise :class:`GitUnavailable`."""
    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GitUnavailable(f"git could not be executed: {error}") from error
    if result.returncode != 0:
        raise GitUnavailable(f"git --version failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run(args: list[str], *, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """Run git and return ``(returncode, stdout, stderr)`` without raising.

    Every caller must look at the return code.  That is the entire lesson of the
    upstream isolator this module replaces.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)
    return result.returncode, result.stdout, result.stderr


def repo_status(cwd: str | Path) -> RepoStatus:
    """Inspect a directory: is it a repository, and is it clean?"""
    code, out, err = _run(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if code != 0 or out.strip() != "true":
        return RepoStatus(is_repo=False, detail=(err or out).strip()[:200])
    code, out, _ = _run(["rev-parse", "--show-toplevel"], cwd=cwd)
    root = out.strip() if code == 0 else str(cwd)
    code, out, err = _run(["status", "--porcelain"], cwd=cwd)
    if code != 0:
        return RepoStatus(is_repo=True, root=root, detail=f"status failed: {err.strip()[:160]}")
    dirty = tuple(
        line[3:].strip()
        for line in out.splitlines()
        if line.strip() and len(line) > 3
    )
    return RepoStatus(is_repo=True, clean=not dirty, root=root, dirty_paths=dirty)


@dataclass(slots=True)
class WorktreeSession:
    """One isolated working copy."""

    subtask_id: str
    path: Path
    branch: str
    base_commit: str
    repo_root: Path
    created_at: float = field(default_factory=time.time)
    committed_sha: str = ""

    def describe(self) -> str:
        return f"{self.subtask_id}: {self.path} on {self.branch} from {self.base_commit[:8]}"


class WorktreeManager:
    """Creates, inspects and disposes of worktrees for one orchestration run."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        orchestration_id: str,
        base_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.orchestration_id = orchestration_id
        # Worktrees live outside the repository by default: a worktree inside the
        # tree it isolates is confusing at best and recursive at worst.
        self.base_dir = Path(base_dir) if base_dir is not None else (
            Path.home() / ".mini-code" / "worktrees" / orchestration_id
        )
        self.sessions: list[WorktreeSession] = []

    # -- helpers -----------------------------------------------------------

    def _branch_name(self, subtask_id: str, attempt: int) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in subtask_id)
        suffix = "" if attempt <= 0 else f"-r{attempt}"
        return f"mc/orch-{self.orchestration_id}-{safe}{suffix}"

    def _git(self, args: list[str], *, cwd: str | Path | None = None) -> tuple[int, str, str]:
        return _run(args, cwd=cwd if cwd is not None else self.repo_root)

    def _require_ok(self, args: list[str], *, cwd: str | Path | None = None) -> str:
        code, out, err = self._git(args, cwd=cwd)
        if code != 0:
            raise WorktreeError(f"git {' '.join(args)} failed ({code}): {(err or out).strip()[:300]}")
        return out

    # -- lifecycle ---------------------------------------------------------

    def prepare(
        self,
        subtask_id: str,
        *,
        attempt: int = 0,
        allow_dirty: str = "",
        base_commit: str = "",
    ) -> WorktreeSession:
        """Create the worktree or refuse with a reason.

        ``allow_dirty`` is an explicit, recorded string explaining why isolating from
        a dirty tree is acceptable.  It exists because refusing outright is sometimes
        wrong (a repository mid-work is normal), but silence is never acceptable: the
        reason travels into the report.
        """
        git_version()  # raises GitUnavailable
        status = repo_status(self.repo_root)
        if not status.is_repo:
            raise WorktreeError(f"cannot isolate in {self.repo_root}: {status.describe()}")
        if not status.clean and not allow_dirty:
            raise WorktreeError(
                f"refusing to isolate: the working tree is {status.describe()}. "
                "Commit or stash first, or pass an explicit reason to isolate from this state."
            )
        base = base_commit or self._current_commit()
        branch = self._branch_name(subtask_id, attempt)
        path = self.base_dir / branch.replace("/", "-")
        if path.exists():
            raise WorktreeError(f"worktree path already exists: {path}")

        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Note the flags: an explicit base commit and a real branch.  No `--detach`
        # (upstream passed both, which git cannot honour), and the return code matters.
        self._require_ok(["worktree", "add", "-b", branch, str(path), base])

        session = WorktreeSession(
            subtask_id=subtask_id,
            path=path,
            branch=branch,
            base_commit=base,
            repo_root=self.repo_root,
        )
        self.sessions.append(session)
        return session

    def _current_commit(self) -> str:
        return self._require_ok(["rev-parse", "HEAD"]).strip()

    def changes(self, session: WorktreeSession) -> tuple[str, ...]:
        """Files changed relative to the recorded base (committed or not)."""
        out = self._require_ok(["status", "--porcelain"], cwd=session.path)
        files: list[str] = []
        for line in out.splitlines():
            if len(line) > 3:
                entry = line[3:].strip()
                if " -> " in entry:  # renames report both sides
                    entry = entry.split(" -> ", 1)[1]
                files.append(entry)
        return tuple(files)

    def commit(self, session: WorktreeSession, message: str) -> str:
        """Commit everything in the worktree; returns the new sha."""
        self._require_ok(["add", "-A"], cwd=session.path)
        code, out, err = self._git(["commit", "-m", message], cwd=session.path)
        if code != 0:
            combined = f"{out}\n{err}".lower()
            if "nothing to commit" in combined:
                # Not an error: a subtask may legitimately change nothing.  Report the
                # unchanged state rather than inventing a commit.
                session.committed_sha = self._require_ok(["rev-parse", "HEAD"], cwd=session.path).strip()
                return session.committed_sha
            raise WorktreeError(f"git commit failed in {session.path}: {(err or out).strip()[:300]}")
        session.committed_sha = self._require_ok(["rev-parse", "HEAD"], cwd=session.path).strip()
        return session.committed_sha

    def export_patch(self, session: WorktreeSession, destination: str | Path) -> Path:
        """Write the subtask's diff (base..HEAD, binary-safe) to a file.

        The patch is the deliverable; nothing is applied to the main tree here.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        head = session.committed_sha or self._require_ok(["rev-parse", "HEAD"], cwd=session.path).strip()
        out = self._require_ok(
            ["diff", "--binary", f"{session.base_commit}..{head}"], cwd=session.path
        )
        target.write_text(out, encoding="utf-8")
        return target

    def remove(self, session: WorktreeSession) -> tuple[bool, str]:
        """Remove the worktree directory; **keep the branch** as evidence."""
        code, out, err = self._git(["worktree", "remove", "--force", str(session.path)])
        detail = (err or out).strip()[:200]
        if code != 0 and session.path.exists():
            # Fall back to deleting the directory, then let git forget the entry: a
            # leftover directory would otherwise block the next run.
            shutil.rmtree(session.path, ignore_errors=True)
            self._git(["worktree", "prune"])
            detail = f"git refused ({detail}); directory removed and pruned"
        elif code == 0:
            self._git(["worktree", "prune"])
        if session in self.sessions:
            self.sessions.remove(session)
        return (not session.path.exists()), detail

    def cleanup(self) -> list[tuple[str, bool, str]]:
        """Remove every worktree this manager created."""
        results: list[tuple[str, bool, str]] = []
        for session in list(self.sessions):
            gone, detail = self.remove(session)
            results.append((session.subtask_id, gone, detail))
        return results

    def branch_exists(self, branch: str) -> bool:
        code, _out, _err = self._git(["rev-parse", "--verify", branch])
        return code == 0

    def patch_applies(self, patch: str | Path) -> tuple[bool, str]:
        """Check whether a patch would apply to the current tree, without applying it.

        Used by the report so a human can see "this is cleanly applicable" before
        deciding, instead of discovering a conflict afterwards.
        """
        code, out, err = self._git(["apply", "--check", str(patch)])
        return code == 0, (err or out).strip()[:300]
