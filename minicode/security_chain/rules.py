"""L1 rules: deterministic, microsecond-scale verdicts with structured evidence.

Scope note, because it shapes every rule here.  The upstream ``permissions``
module *already* asks the user about dangerous commands and out-of-workspace
paths.  Duplicating that would mean two prompts for one action, which is exactly
the "antivirus that cries wolf" failure the design is trying to avoid (invariant
N1).  So L1 does not re-gate what the permission layer already gates.  What L1
adds is:

* **structured identity** -- a stable ``rule_id`` plus the evidence string, so a
  verdict can be explained and audited instead of being an opaque "denied";
* **a taint-aware escalation** -- the one thing the permission layer cannot see:
  whether the action came from something the agent *read* rather than from the
  user;
* **outright denials for the narrow cases where consulting a human is the wrong
  answer**, chiefly "send this secret somewhere external", where a confirmation
  dialog is a coin flip the attacker is happy to take.
"""

from __future__ import annotations

import re
from pathlib import Path

from minicode.permissions import _classify_dangerous_command  # noqa: PLC2701 - reuse the upstream classifier
from minicode.security_chain.types import (
    ActionRequest,
    ContentRequest,
    Decision,
    RiskLevel,
    SecurityDecision,
    Stage,
)

__all__ = ["check_action", "check_content", "is_security_sink", "RULES"]

RULES = (
    "L1-READONLY-FAST-ALLOW",
    "L1-EXFIL-ENDPOINT",
    "L1-DESTRUCTIVE-COMMAND",
    "L1-DOWNLOAD-EXEC",
    "L1-SECRET-ACCESS",
    "L1-WRITE-OUTSIDE-WORKSPACE",
    "L1-TAINT-SIDE-EFFECT",
    "L1-FAKE-ROLE-MARKER",
    "L1-INSTRUCTION-OVERRIDE",
    "L1-EXFIL-REQUEST",
    "L1-OBFUSCATION",
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERN = re.compile(
    r"(\.env\b|\.envrc|id_rsa|id_ed25519|\.pem\b|\.p12\b|credentials|"
    r"\.aws[/\\]|\.ssh[/\\]|settings\.json|secrets?\b|passwords?\b|"
    r"api[_-]?key|access[_-]?token|private[_-]?key)",
    re.IGNORECASE,
)
_EXTERNAL_TARGET = re.compile(
    r"(https?://|ftp://|smtp://|mailto:|@[\w.-]+\.[a-z]{2,}\b|\bnc\b|\bnetcat\b)",
    re.IGNORECASE,
)
_EXFIL_VERBS = re.compile(
    r"(curl|wget|Invoke-WebRequest|iwr|scp|sftp|ftp|upload|post\b|send\b|"
    r"requests\.post|urllib|http\.client|webhook)",
    re.IGNORECASE,
)
_DOWNLOAD_EXEC = re.compile(
    r"(curl|wget|Invoke-WebRequest|iwr)[^\n]{0,200}(\|\s*(ba)?sh|>\s*\S+\.(sh|py|ps1)\b)"
    r"|(ba)?sh\s+-c\s+[\"']?\$\(|iex\s*\(",
    re.IGNORECASE,
)
_FAKE_ROLE_MARKER = re.compile(
    r"(<\s*/?\s*system\s*>|\[\s*/?INST\s*\]|###\s*(system|assistant|developer)\s*:|"
    r"^\s*(system|assistant|developer)\s*:|<\|im_start\|>|<\|im_end\|>)",
    re.IGNORECASE | re.MULTILINE,
)
_INSTRUCTION_OVERRIDE = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+instructions?"
    r"|disregard\s+(all\s+)?(previous|prior|above)"
    r"|forget\s+(everything|your\s+instructions|your\s+previous)"
    r"|override\s+your\s+(instructions|rules|guardrails)"
    r"|忽略(以上|上面|之前|先前)的?(所有)?(指令|规则|设定|要求)"
    r"|忘记(你)?(之前|原来)的?(设定|指令|规则)"
    r"|(以上|上面)(指令|规则|设定)(作废|无效)"
    r"|不要(再)?(遵守|执行)(之前|以上)的?(指令|规则))",
    re.IGNORECASE,
)
_BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")
_HEX_BLOB = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_PACKAGE_INSTALL = re.compile(
    r"\b(pip3?\s+install|pip\s+install\s+--?[a-z-]+\s|npm\s+(install|i|add)\b|yarn\s+add\b|"
    r"uv\s+(add|pip\s+install)\b|poetry\s+add\b|pipx\s+install\b|gem\s+install\b|cargo\s+install\b)",
    re.IGNORECASE,
)
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")

# A security document, a detection rule, an audit report and a test fixture all
# contain attack strings *verbatim*.  A regex cannot tell an instruction from a
# quotation -- but the words around it usually can, and getting this wrong is the
# single biggest source of false positives in the corpus.
_QUOTED_CONTEXT = re.compile(
    r"(example|e\.?g\.?|for instance|quote[sd]?|sample|pattern|regex|detect|signature|"
    r"attack string|test fixture|payload|red[- ]team|checklist"
    r"|例如|比如|示例|举例|引用|原文|正则|模式|特征|检测规则|测试夹具|红队|清单|报告)",
    re.IGNORECASE,
)
_QUOTE_CHARS = "\"'`“”‘’「」『』《》"


def in_quoted_context(text: str, start: int, end: int, window: int = 90) -> bool:
    """Is this match being *discussed* rather than issued?"""
    before = text[max(0, start - window) : start]
    after = text[end : end + window]
    if any(char in before for char in _QUOTE_CHARS) or any(char in after for char in _QUOTE_CHARS):
        return True
    return bool(_QUOTED_CONTEXT.search(before) or _QUOTED_CONTEXT.search(after))


def _flags(**kwargs: bool) -> list[str]:
    return [name for name, value in kwargs.items() if value]


def _path_like_values(payload: object) -> list[str]:
    """Harvest path-shaped strings from a tool input, whatever its shape."""
    found: list[str] = []
    if isinstance(payload, str):
        found.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            found.extend(_path_like_values(value))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found.extend(_path_like_values(value))
    return found


def _command_text(request: ActionRequest) -> str:
    payload = request.tool_input
    if isinstance(payload, dict):
        for key in ("command", "cmd", "script"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return " ".join(str(value) for value in payload.values())
    return str(payload)


def _outside_workspace(request: ActionRequest) -> str:
    """Return the first path argument that escapes the workspace, else ''."""
    cwd = Path(request.cwd).resolve() if request.cwd else None
    if cwd is None:
        return ""
    for value in _path_like_values(request.tool_input):
        candidate = value.strip()
        if not candidate or len(candidate) > 400:
            continue
        looks_like_path = (
            "/" in candidate or "\\" in candidate or candidate.startswith("~")
        )
        if not looks_like_path:
            continue
        try:
            resolved = Path(candidate).expanduser()
            if not resolved.is_absolute():
                resolved = cwd / resolved
            resolved = resolved.resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        try:
            resolved.relative_to(cwd)
        except ValueError:
            return str(resolved)
    return ""


# ---------------------------------------------------------------------------
# Exit boundary (actions)
# ---------------------------------------------------------------------------


def check_action(request: ActionRequest) -> SecurityDecision | None:
    """Return a deterministic verdict, or ``None`` to pass the action downstream."""
    command = _command_text(request)
    payload_text = " ".join(_path_like_values(request.tool_input))
    combined = f"{command} {payload_text}"

    # 1. Fast allow: read-only, nothing suspicious, no taint.  This is the path
    #    that keeps ordinary work free of prompts and model calls.
    if request.read_only and not request.tainted:
        if not (_SECRET_PATTERN.search(combined) or _EXTERNAL_TARGET.search(combined)):
            return SecurityDecision(
                decision=Decision.ALLOW,
                risk=RiskLevel.LOW,
                stage=Stage.L1_RULES,
                reason=f"{request.tool_name} is read-only and shows no risky pattern",
                rule_id="L1-READONLY-FAST-ALLOW",
            )

    # 2. Exfiltration: a secret-class target AND an external endpoint in one
    #    action.  Denied outright -- asking a human here is a coin flip the
    #    attacker is content to take.
    if _SECRET_PATTERN.search(combined) and _EXTERNAL_TARGET.search(combined) and (
        _EXFIL_VERBS.search(combined) or request.tainted
    ):
        return SecurityDecision(
            decision=Decision.DENY,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason="action combines a secret-class target with an external endpoint",
            rule_id="L1-EXFIL-ENDPOINT",
            evidence=_excerpt(combined),
        )

    # 3. Destructive command patterns.  Two different answers on purpose:
    #
    #    * Driven by untrusted content -> denied outright.  "A document the agent
    #      read told it to run rm -rf" has no legitimate reading, and denying here
    #      avoids prompting a human for something they would only ever refuse.
    #    * Typed by the user -> allowed through, because the permission layer
    #      already prompts for exactly these commands.  Escalating here as well
    #      would ask the same question twice, which is how a security feature
    #      becomes something people turn off.
    destructive = _destructive_reason(request)
    if destructive:
        if request.tainted:
            return SecurityDecision(
                decision=Decision.DENY,
                risk=RiskLevel.HIGH,
                stage=Stage.L1_RULES,
                reason=f"destructive action driven by untrusted content: {destructive}",
                rule_id="L1-DESTRUCTIVE-COMMAND",
                evidence=_excerpt(combined),
            )
        return SecurityDecision(
            decision=Decision.ALLOW,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason=f"destructive but user-driven; the permission layer gates it ({destructive})",
            rule_id="L1-DESTRUCTIVE-COMMAND",
            evidence=_excerpt(combined),
        )

    # 4. Download-and-execute in one command.
    if _DOWNLOAD_EXEC.search(combined):
        tainted = request.tainted
        return SecurityDecision(
            decision=Decision.DENY if tainted else Decision.ESCALATE,
            risk=RiskLevel.HIGH if tainted else RiskLevel.MEDIUM,
            stage=Stage.L1_RULES,
            reason=(
                "download-and-execute command driven by untrusted content"
                if tainted
                else "command downloads and executes remote content in one step"
            ),
            rule_id="L1-DOWNLOAD-EXEC",
            evidence=_excerpt(combined),
        )

    # 5. Secret-class access.  Reading is not catastrophic, but it is the exact
    #    step every data-exfiltration payload needs, so a tainted read escalates.
    if _SECRET_PATTERN.search(combined):
        return SecurityDecision(
            decision=Decision.ESCALATE if request.tainted else Decision.ALLOW,
            risk=RiskLevel.HIGH if request.tainted else RiskLevel.MEDIUM,
            stage=Stage.L1_RULES,
            reason=(
                "secret-class target requested by untrusted content"
                if request.tainted
                else "secret-class target; recorded for audit"
            ),
            rule_id="L1-SECRET-ACCESS",
            evidence=_excerpt(combined),
        )

    # 6. Writing outside the workspace.
    outside = _outside_workspace(request)
    if outside and request.has_side_effects:
        return SecurityDecision(
            decision=Decision.ESCALATE if request.tainted else Decision.ALLOW,
            risk=RiskLevel.HIGH if request.tainted else RiskLevel.MEDIUM,
            stage=Stage.L1_RULES,
            reason=(
                f"out-of-workspace write requested by untrusted content: {outside}"
                if request.tainted
                else f"out-of-workspace target {outside}; the permission layer gates it"
            ),
            rule_id="L1-WRITE-OUTSIDE-WORKSPACE",
            evidence=outside,
        )

    # 7. Catch-all taint rule: untrusted input reaching a place where it can hurt.
    if request.tainted and is_security_sink(request):
        return SecurityDecision(
            decision=Decision.ESCALATE,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason="security-relevant action whose parameters derive from untrusted content",
            rule_id="L1-TAINT-SIDE-EFFECT",
            evidence=", ".join(request.taint_sources[:3]),
        )

    return None


def _destructive_reason(request: ActionRequest) -> str | None:
    payload = request.tool_input
    if not isinstance(payload, dict):
        return None
    if request.tool_name != "run_command":
        return None
    command = str(payload.get("command") or "").strip()
    if not command:
        return None
    parts = command.split()
    head, args = parts[0], parts[1:]
    # Strip a leading path so /bin/rm is still recognised as rm.
    head = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return _classify_dangerous_command(head, args)


def is_security_sink(request: ActionRequest) -> bool:
    """Would untrusted input reaching this action actually cause harm?

    Taint analysis needs *sinks*, not just sources.  "The agent read a document
    and then did something related to it" describes ordinary work -- re-running
    the tests a report mentioned, opening a path a README pointed at.  Elevating
    all of that is precisely the false positive that gets a security feature
    switched off.  Only these actions are worth escalating a taint into:
    destruction, download-and-execute, exposure of a secret to an outside
    endpoint, writes outside the workspace, and writes to a guardrail path.
    """
    if not request.has_side_effects:
        return False
    payload = request.tool_input if isinstance(request.tool_input, dict) else {}
    command = str(payload.get("command") or "")
    text = " ".join(_path_like_values(request.tool_input))
    combined = f"{command} {text}"
    if _DOWNLOAD_EXEC.search(combined):
        return True
    if _destructive_reason(request):
        return True
    # Installing a package runs arbitrary code in the environment, and the name
    # of the package is exactly the kind of detail a poisoned document supplies.
    if _PACKAGE_INSTALL.search(combined):
        return True
    if _SECRET_PATTERN.search(combined) and _EXTERNAL_TARGET.search(combined):
        return True
    if _SECRET_PATTERN.search(combined) and _EXFIL_VERBS.search(combined):
        return True
    if _outside_workspace(request):
        return True
    try:
        from minicode.security_chain.preflight import _is_protected, _resolve

        raw_target = ""
        for key in ("path", "file", "target"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                raw_target = value
                break
        if raw_target:
            resolved = _resolve(raw_target, request.cwd)
            if resolved is not None and _is_protected(resolved):
                return True
    except Exception:  # noqa: BLE001 - sink detection must never raise
        pass
    return False


# ---------------------------------------------------------------------------
# Entry boundary (content)
# ---------------------------------------------------------------------------


def check_content(request: ContentRequest) -> SecurityDecision | None:
    """Return a deterministic verdict for incoming content, or ``None``."""
    text = request.text or ""

    marker = _FAKE_ROLE_MARKER.search(text)
    if marker is not None and not in_quoted_context(text, marker.start(), marker.end()):
        return SecurityDecision(
            decision=Decision.ESCALATE,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason="content impersonates a system/developer role marker",
            rule_id="L1-FAKE-ROLE-MARKER",
            evidence=_excerpt(text),
        )

    override = _INSTRUCTION_OVERRIDE.search(text)
    if override is not None and not in_quoted_context(text, override.start(), override.end()):
        return SecurityDecision(
            decision=Decision.ESCALATE,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason="content contains an instruction-override attempt",
            rule_id="L1-INSTRUCTION-OVERRIDE",
            evidence=_excerpt(text),
        )

    if _SECRET_PATTERN.search(text) and _EXTERNAL_TARGET.search(text) and _EXFIL_VERBS.search(text):
        return SecurityDecision(
            decision=Decision.ESCALATE,
            risk=RiskLevel.HIGH,
            stage=Stage.L1_RULES,
            reason="content asks for a secret to be sent to an external endpoint",
            rule_id="L1-EXFIL-REQUEST",
            evidence=_excerpt(text),
        )

    if _INVISIBLE.search(text) or _BASE64_BLOB.search(text) or _HEX_BLOB.search(text):
        return SecurityDecision(
            decision=Decision.ESCALATE,
            risk=RiskLevel.MEDIUM,
            stage=Stage.L1_RULES,
            reason="content uses obfuscation (invisible characters or an encoded blob)",
            rule_id="L1-OBFUSCATION",
            evidence=_excerpt(text),
        )

    return None


def _excerpt(text: str, limit: int = 160) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed[:limit]
