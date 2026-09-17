"""L2 preflight: deterministic checks against the *resources* an action touches.

L1 judges the shape of an action; L2 judges its target.  Everything here is
filesystem-level and millisecond-scale, and every rule either denies outright
(structurally broken or self-defeating requests) or attaches evidence the audit
trail can carry.

Two rules carry most of the weight:

``L2-PROTECTED-TARGET``
    The agent must not be able to edit its own guardrails or credentials.  An
    injected instruction that says "disable the security mode" or "rewrite
    settings.json" is only dangerous if the write succeeds; this rule makes that
    specific escalation impossible rather than merely suspicious.

``L2-DELETE-WORKSPACE-ROOT``
    ``rm -rf .`` inside the workspace resolves to a delete of the whole project.
    The upstream permission layer would ask, but a confirmation prompt on a
    catastrophic-and-irreversible action is a bad last line of defence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from minicode.config import MINI_CODE_DIR
from minicode.security_chain.types import (
    ActionRequest,
    Decision,
    RiskLevel,
    SecurityDecision,
    Stage,
)

__all__ = ["check_action", "RULES", "SIDE_EFFECTS", "declared_effects"]

RULES = (
    "L2-PROTECTED-TARGET",
    "L2-MISSING-PARAM",
    "L2-DELETE-WORKSPACE-ROOT",
    "L2-TARGET-IS-DIRECTORY",
    "L2-EDIT-TARGET-MISSING",
    "L2-VERSION-CONTROL-STATE",
)

#: What each tool actually does, so the report can state effects instead of
#: inferring them.  ``write``/``execute``/``network`` are the ones that can leak
#: or destroy; ``read`` never leaves the machine.
SIDE_EFFECTS: dict[str, str] = {
    "read_file": "read",
    "list_files": "read",
    "grep_files": "read",
    "load_skill": "read",
    "web_fetch": "network-read",
    "web_search": "network-read",
    "write_file": "write",
    "modify_file": "write",
    "edit_file": "write",
    "patch_file": "write",
    "run_command": "execute",
    "ask_user": "interactive",
}

_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "modify_file": ("path", "content"),
    "edit_file": ("path", "search", "replace"),
    "patch_file": ("path", "replacements"),
    "run_command": ("command",),
    "grep_files": ("pattern",),
}

_WRITE_TOOLS = frozenset({"write_file", "modify_file", "edit_file", "patch_file"})
_EDIT_TOOLS = frozenset({"edit_file", "patch_file", "modify_file"})

_MAX_OVERWRITE_BYTES = 2 * 1024 * 1024  # 2 MiB: above this, ask before clobbering
_vcs_cache: dict[str, str] = {}


def declared_effects(tool_name: str) -> str:
    return SIDE_EFFECTS.get(tool_name, "unknown")


def _protected_paths() -> tuple[Path, ...]:
    """Paths the agent must never write: its own configuration and guardrails."""
    here = Path(__file__).resolve().parent
    candidates = [
        MINI_CODE_DIR / "settings.json",
        MINI_CODE_DIR / "permissions.json",
        MINI_CODE_DIR / "skill-state.json",
        MINI_CODE_DIR / "skill-stats.json",
        here,  # the security chain's own source
    ]
    resolved: list[Path] = []
    for candidate in candidates:
        try:
            resolved.append(candidate.resolve())
        except (OSError, RuntimeError):
            continue
    return tuple(resolved)


def _resolve(value: str, cwd: str) -> Path | None:
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path if cwd else path
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _is_protected(target: Path) -> bool:
    for protected in _protected_paths():
        try:
            target.relative_to(protected)
            return True
        except ValueError:
            continue
    return False


def _path_argument(request: ActionRequest) -> str:
    payload = request.tool_input
    if isinstance(payload, dict):
        for key in ("path", "file", "target", "cwd"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _command_targets(request: ActionRequest) -> list[str]:
    """Targets of a delete-style command, for the workspace-root rule."""
    payload = request.tool_input
    if not isinstance(payload, dict) or request.tool_name != "run_command":
        return []
    command = str(payload.get("command") or "").strip()
    if not command:
        return []
    parts = command.split()
    if not parts:
        return []
    head = parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if head not in {"rm", "rmdir", "del", "erase", "remove-item", "rd"}:
        return []
    return [part for part in parts[1:] if not part.startswith("-")]


def _version_control_state(target: Path) -> str:
    """Best-effort ``tracked``/``untracked``; cached, bounded, never fatal."""
    key = str(target)
    cached = _vcs_cache.get(key)
    if cached is not None:
        return cached
    state = "unknown"
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(target)],
            cwd=str(target.parent),
            capture_output=True,
            text=True,
            timeout=0.3,
        )
        state = "tracked" if completed.returncode == 0 else "untracked"
    except (OSError, subprocess.SubprocessError):
        state = "unknown"
    _vcs_cache[key] = state
    return state


def check_action(request: ActionRequest) -> SecurityDecision | None:
    """Return a deterministic verdict for the action's target, or ``None``."""
    payload = request.tool_input if isinstance(request.tool_input, dict) else {}

    # 1. Structural completeness.
    required = _REQUIRED_PARAMS.get(request.tool_name, ())
    missing = [
        name
        for name in required
        if name not in payload or payload.get(name) in (None, "", [], {})
    ]
    if missing:
        return SecurityDecision(
            decision=Decision.DENY,
            risk=RiskLevel.LOW,
            stage=Stage.L2_PREFLIGHT,
            reason=f"{request.tool_name} is missing required parameter(s): {', '.join(missing)}",
            rule_id="L2-MISSING-PARAM",
            evidence=", ".join(sorted(payload.keys())),
        )

    # 2. Self-protection: guardrails and credentials are off limits.
    raw_target = _path_argument(request)
    target = _resolve(raw_target, request.cwd) if raw_target else None
    if target is not None and request.has_side_effects and _is_protected(target):
        return SecurityDecision(
            decision=Decision.DENY,
            risk=RiskLevel.HIGH,
            stage=Stage.L2_PREFLIGHT,
            reason=f"target is a protected guardrail/credential path: {target}",
            rule_id="L2-PROTECTED-TARGET",
            evidence=str(target),
        )

    # 3. Deleting the workspace root (or above it).
    cwd = Path(request.cwd).resolve() if request.cwd else None
    for raw in _command_targets(request):
        resolved = _resolve(raw, request.cwd)
        if resolved is None or cwd is None:
            continue
        if resolved == cwd or resolved in cwd.parents:
            return SecurityDecision(
                decision=Decision.DENY,
                risk=RiskLevel.HIGH,
                stage=Stage.L2_PREFLIGHT,
                reason=f"command would delete the workspace root or its parent: {resolved}",
                rule_id="L2-DELETE-WORKSPACE-ROOT",
                evidence=raw,
            )

    # 4. Writing over a directory, or editing a file that is not there.
    if target is not None and request.tool_name in _WRITE_TOOLS:
        try:
            if target.is_dir():
                return SecurityDecision(
                    decision=Decision.DENY,
                    risk=RiskLevel.MEDIUM,
                    stage=Stage.L2_PREFLIGHT,
                    reason=f"write target is an existing directory: {target}",
                    rule_id="L2-TARGET-IS-DIRECTORY",
                    evidence=str(target),
                )
        except OSError:
            pass
    if target is not None and request.tool_name in _EDIT_TOOLS:
        try:
            if not target.exists():
                return SecurityDecision(
                    decision=Decision.DENY,
                    risk=RiskLevel.LOW,
                    stage=Stage.L2_PREFLIGHT,
                    reason=f"edit target does not exist: {target}",
                    rule_id="L2-EDIT-TARGET-MISSING",
                    evidence=str(target),
                )
        except OSError:
            pass

    # 5. Overwriting an existing, non-trivial file: state whether it can be
    #    recovered.  This informs rather than blocks -- the upstream checkpoint
    #    system is what actually makes the change reversible.
    if target is not None and request.tool_name == "write_file":
        try:
            if target.is_file():
                size = target.stat().st_size
                state = _version_control_state(target)
                if state == "untracked" and size > _MAX_OVERWRITE_BYTES:
                    return SecurityDecision(
                        decision=Decision.ESCALATE,
                        risk=RiskLevel.MEDIUM,
                        stage=Stage.L2_PREFLIGHT,
                        reason=(
                            f"overwriting a large ({size} bytes) file that is not under "
                            "version control"
                        ),
                        rule_id="L2-VERSION-CONTROL-STATE",
                        evidence=str(target),
                    )
        except OSError:
            pass

    return None
