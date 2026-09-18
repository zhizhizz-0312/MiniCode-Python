"""The human gate: nothing enters the registry without a person saying yes.

Two mechanisms, and the second one is the interesting one.

**Approval is the only write path.**  ``install`` is called from here and nowhere
else, so "did the agent add a skill to itself?" has a structural answer rather than
a policy answer.

**Every draft is screened before it can be approved.**  The design document names
contamination as the top risk of a self-improving agent, and the sharpest version
of that risk is not "a bad skill got installed" but "an injection became a rule".
A poisoned document can only mislead the agent once; if the loop distils that
session into a skill, the injection is promoted to standing policy.  The screening
therefore runs **regardless of the security mode**, including ``off`` -- it guards
a write of rules, not the execution of an action.

The screening is deliberately narrower than the general content classifier.  A
skill *describes doing things*, so ordinary imperative language is expected and
must not trip it.  What is checked for is the small set of instructions that are
wrong as a matter of policy: removing the human from the loop, permanent
destruction without confirmation, moving secrets outbound, disabling the audit,
and overriding the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from minicode.distillation.draft import DraftStore, draft_fingerprint
from minicode.distillation.types import DraftStatus, SkillDraft

__all__ = [
    "ReviewOutcome",
    "approve_draft",
    "discard_all",
    "install_skill",
    "reject_draft",
    "screen_draft",
]

#: Instructions that are wrong as policy, whatever the transcript showed.
_VIOLATIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "removes-the-human",
        re.compile(
            r"(不经(用户)?确认|无需(用户)?确认|不要询问用户|不用问用户|直接执行即可"
            r"|without (asking|confirmation|approval)|no need to (ask|confirm)"
            r"|bypass (the )?(security|check|review|approval)|skip (the )?(check|review)"
            r"|disable (the )?(security|guardrail|permission))",
            re.IGNORECASE,
        ),
        "instructs the agent to act without asking, or to bypass a check",
    ),
    (
        "permanent-destruction",
        re.compile(
            r"((永远|永久|以后都|一律)[^\n]{0,24}(删除|清空|移除|destroy|delete|wipe)"
            r"|(permanently|always)[^\n]{0,24}(delete|remove|wipe|purge)"
            r"|(删除|清空|移除)[^\n]{0,24}(无需|不必|不用)[^\n]{0,12}(备份|确认|询问)"
            r"|delete[^\n]{0,24}without[^\n]{0,12}(backup|confirmation))",
            re.IGNORECASE,
        ),
        "prescribes destructive action as a standing rule",
    ),
    (
        "secret-exfiltration",
        re.compile(
            r"((\.env|id_rsa|credentials|api[_-]?key|私钥|凭据)[^\n]{0,80}(POST|发送|上传|post|upload|send)"
            r"|(POST|发送|上传|post|upload|send)[^\n]{0,80}(\.env|id_rsa|credentials|api[_-]?key|私钥))",
            re.IGNORECASE,
        ),
        "moves a secret to an external destination",
    ),
    (
        "disables-audit",
        re.compile(
            r"((关闭|停用|disable)[^\n]{0,14}(审计|日志|审查|audit|logging)"
            r"|(ignore|skip)[^\n]{0,14}(audit|logging))",
            re.IGNORECASE,
        ),
        "disables auditing or logging",
    ),
    (
        "overrides-the-user",
        re.compile(
            r"((忽略|无视|覆盖)[^\n]{0,14}(用户|指令|规则|要求)"
            r"|(ignore|override)[^\n]{0,14}(the )?(user|instruction|rule))",
            re.IGNORECASE,
        ),
        "tells the agent to ignore the user or its rules",
    ),
)

_DESTRUCTIVE_STEP = re.compile(r"(rm\s+-rf|del\s+/|remove-item|format |mkfs|清空目录)", re.IGNORECASE)


@dataclass
class ReviewOutcome:
    """Result of an approve/reject request."""

    draft: SkillDraft | None
    message: str
    ok: bool = False

    @property
    def violations(self) -> list[str]:
        return [line for line in self.message.splitlines() if line.startswith("  - ")]


def screen_draft(draft: SkillDraft) -> list[str]:
    """Return policy violations in a draft; empty means it may be approved."""
    text = draft.skill_markdown or "\n".join(draft.steps)
    violations: list[str] = []
    for rule_id, pattern, description in _VIOLATIONS:
        match = pattern.search(text)
        if match:
            violations.append(f"{rule_id}: {description} (matched {match.group(0)[:60]!r})")

    # The general content rules still apply: an impostor role marker or an
    # explicit instruction-override has no business in a skill file either.
    try:
        from minicode.security_chain.rules import check_content
        from minicode.security_chain.types import ContentRequest

        decision = check_content(ContentRequest(text=text, source="distilled-draft"))
        if decision is not None:
            violations.append(
                f"security-chain {decision.rule_id}: {decision.reason}"
            )
    except Exception:  # noqa: BLE001 - a broken import must not silently pass a draft
        violations.append("security-chain screening unavailable; refusing to approve blind")

    # A skill that can run anything but claims to be low risk is a mislabelling.
    if draft.risk_level == "low" and "run_command" in draft.tools:
        if _DESTRUCTIVE_STEP.search(text):
            violations.append(
                "risk-mislabelled: claims low risk while running destructive commands"
            )
    return violations


def install_skill(draft: SkillDraft, *, cwd: str | Path, scope: str = "project") -> Path:
    """Write an approved draft into the workspace's skill root."""
    root = (
        Path(cwd) / ".mini-code" / "skills"
        if scope == "project"
        else Path.home() / ".mini-code" / "skills"
    )
    target = root / draft.name
    target.mkdir(parents=True, exist_ok=True)
    skill_file = target / "SKILL.md"
    skill_file.write_text(draft.skill_markdown, encoding="utf-8")
    provenance = target / "DISTILLED.md"
    provenance.write_text(
        "\n".join(
            [
                f"# Distilled skill: {draft.name}",
                "",
                f"Approved from draft `{draft.id}`.",
                f"Confidence: {draft.confidence:.2f} "
                f"(evidence prior {draft.prior_confidence:.2f}, model {1.0 - draft.prior_confidence:.2f} share)",
                "",
                "## Source sessions",
                "",
                *[
                    f"- {item.get('sessionId')}#{item.get('turn')} "
                    f"[{item.get('outcome')}] {str(item.get('prompt'))[:120]}"
                    for item in draft.sources
                ],
                "",
                "Remove this file if you no longer want the provenance trail.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return skill_file


def approve_draft(
    store: DraftStore,
    draft_id: str,
    *,
    cwd: str | Path,
    scope: str = "project",
) -> ReviewOutcome:
    """Screen, install and mark approved. Refuses on any violation."""
    draft = store.get(draft_id)
    if draft is None:
        return ReviewOutcome(None, f"No draft with id {draft_id!r}.", ok=False)
    if draft.status is DraftStatus.APPROVED:
        return ReviewOutcome(draft, f"Draft {draft_id} was already approved.", ok=True)

    violations = screen_draft(draft)
    if violations:
        lines = [
            f"Refusing to install '{draft.name}': the screening found policy problems.",
            *[f"  - {item}" for item in violations],
            "",
            "The draft is unchanged. Edit its SKILL.md and approve again, or reject it so",
            "the distiller stops proposing the same thing.",
        ]
        store.set_status(draft_id, DraftStatus.PENDING, note="blocked by screening")
        return ReviewOutcome(draft, "\n".join(lines), ok=False)

    try:
        skill_file = install_skill(draft, cwd=cwd, scope=scope)
    except OSError as error:
        return ReviewOutcome(draft, f"Install failed: {error}", ok=False)

    # Verify the installed file is one the registry can actually load.
    issues: list[str] = []
    try:
        from minicode.skill_system.registry import parse_skill_file

        spec = parse_skill_file(skill_file, name=draft.name, source="project")
        issues = list(spec.issues)
    except Exception as error:  # noqa: BLE001
        issues = [f"installed file could not be re-read: {type(error).__name__}"]

    draft.security_note = "screened clean"
    store.set_status(draft_id, DraftStatus.APPROVED, note="approved")
    store.reject_fingerprint(draft_fingerprint(draft), "approved")
    message = f"Installed '{draft.name}' to {skill_file}"
    if issues:
        message += "\nRegistry notes:\n" + "\n".join(f"  - {issue}" for issue in issues)
    return ReviewOutcome(draft, message, ok=True)


def reject_draft(store: DraftStore, draft_id: str, reason: str = "") -> ReviewOutcome:
    """Record a refusal so the same proposal is not offered again."""
    draft = store.get(draft_id)
    if draft is None:
        return ReviewOutcome(None, f"No draft with id {draft_id!r}.", ok=False)
    note = reason.strip() or "rejected without a reason"
    store.set_status(draft_id, DraftStatus.REJECTED, note=note)
    store.reject_fingerprint(draft_fingerprint(draft), note)
    return ReviewOutcome(
        draft,
        f"Rejected '{draft.name}'. The distiller will not propose this same content again.",
        ok=True,
    )


def discard_all(store: DraftStore) -> int:
    """Remove every draft (tests, or a user starting over)."""
    count = 0
    for draft in store.list():
        if store.delete(draft.id):
            count += 1
    return count
