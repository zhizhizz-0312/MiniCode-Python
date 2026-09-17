"""The gate: runs L1 -> L2 -> L3 -> L4 and owns the outcome.

Combination rule, deliberately blunt:

* **deny anywhere wins** -- nothing downstream can talk a hard denial back up;
* **escalate anywhere sends the action to a human** -- a later layer saying "looks
  fine to me" never silently overrides an earlier layer's doubt;
* allow only when no layer objected.

That asymmetry is the point.  A chain in which a later layer can downgrade an
earlier one is a chain where the *last* classifier decides everything, and the
cheap deterministic layers stop mattering.

L4 reuses the product's existing approval prompt (the same one
``permissions.ensure_command`` uses) rather than growing a second dialog, and it
degrades honestly: with no interactive prompt available -- headless, piped stdin
-- a side-effecting action that needs a human is **denied**, because "nobody could
be asked" is not consent.
"""

from __future__ import annotations

from typing import Any

from minicode.security_chain import audit as security_audit
from minicode.security_chain import preflight, rules
from minicode.security_chain.classifier import RiskClassifier, get_classifier
from minicode.security_chain.taint import TaintRegistry, get_registry
from minicode.security_chain.types import (
    ActionRequest,
    ContentRequest,
    Decision,
    RiskLevel,
    SecurityDecision,
    SecurityMode,
    Stage,
    mode_from_env,
)

__all__ = ["SecurityGate", "get_gate", "reset_gate", "action_text"]


def action_text(request: ActionRequest) -> str:
    """Flatten an action's arguments for taint inspection."""
    payload = request.tool_input
    if isinstance(payload, dict):
        return " ".join(str(value) for value in payload.values())
    return "" if payload is None else str(payload)


class SecurityGate:
    """Orchestrates the four layers and keeps the session's caches."""

    def __init__(
        self,
        *,
        mode: SecurityMode | None = None,
        classifier: RiskClassifier | None = None,
        registry: TaintRegistry | None = None,
        audit_enabled: bool = True,
        audit_path: str | None = None,
    ) -> None:
        self.mode = mode if mode is not None else mode_from_env()
        self.classifier = classifier if classifier is not None else get_classifier()
        self.registry = registry
        self.audit_enabled = audit_enabled
        self.audit_path = audit_path
        self._approved: set[str] = set()  # actions a human already allowed this session
        self.counts: dict[str, int] = {}

    # -- helpers -----------------------------------------------------------

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _finish(self, decision: SecurityDecision, *, kind: str, request: Any, tainted: bool = False) -> SecurityDecision:
        self._bump(f"{kind}:{decision.decision.value}")
        if decision.rule_id:
            self._bump(f"rule:{decision.rule_id}")
        if self.audit_enabled:
            security_audit.record(
                decision,
                kind=kind,
                tool_name=getattr(request, "tool_name", ""),
                session_id=getattr(request, "session_id", ""),
                tainted=tainted,
                path=self.audit_path,
            )
        return decision

    @staticmethod
    def _allow(stage: Stage, reason: str, rule_id: str = "") -> SecurityDecision:
        return SecurityDecision(
            decision=Decision.ALLOW,
            risk=RiskLevel.LOW,
            stage=stage,
            reason=reason,
            rule_id=rule_id,
        )

    def _stamp_taint(self, request: ActionRequest) -> None:
        """Ask the registry whether this action's arguments came from something read."""
        if request.tainted:
            return
        registry = self.registry if self.registry is not None else get_registry()
        hits = registry.hits(action_text(request), limit=3)
        if hits:
            request.tainted = True
            request.taint_sources = tuple(hit.describe() for hit in hits)

    # -- entry boundary ----------------------------------------------------

    def check_content(self, request: ContentRequest) -> SecurityDecision | None:
        """Judge content entering the context.

        Never prompts a human: a warning prompt on every suspicious document
        would be unusable, and blocking a *read* does not stop the attack.  The
        caller marks the content as untrusted and warns, and the exit boundary is
        where the action actually gets stopped.
        """
        if self.mode is SecurityMode.OFF:
            return None
        first = rules.check_content(request)
        if first is not None and first.decision is Decision.DENY:
            return self._finish(first, kind="content", request=request)
        second = self.classifier.classify_content(request)
        decision = self._combine(first, None, second)
        if decision is None or decision.decision is Decision.ALLOW:
            return None
        return self._finish(decision, kind="content", request=request)

    # -- exit boundary -----------------------------------------------------

    def check_action(self, request: ActionRequest, *, context: Any | None = None) -> SecurityDecision:
        """Judge a tool call; the returned decision is final for the caller."""
        if self.mode is SecurityMode.OFF:
            return self._allow(Stage.BYPASS, "security chain disabled")
        if request.signature() in self._approved:
            return self._allow(Stage.CACHE, "already approved by the user this session", "L4-SESSION-APPROVAL")

        self._stamp_taint(request)

        first = rules.check_action(request)
        if first is not None and first.decision is Decision.ALLOW and first.rule_id == "L1-READONLY-FAST-ALLOW":
            # The fast lane: ordinary read-only work never reaches a model call or
            # a prompt, which is what keeps the chain invisible day to day.
            return self._finish(first, kind="action", request=request)

        second = preflight.check_action(request)
        third = self.classifier.classify_action(request)
        decision = self._combine(first, second, third)
        if decision is None:
            decision = self._allow(
                Stage.L3_CLASSIFIER, "no layer objected", "L3-NO-OBJECTION"
            )

        if decision.decision is Decision.ESCALATE:
            decision = self._confirm(decision, request, context=context)
        return self._finish(decision, kind="action", request=request, tainted=request.tainted)

    # -- combination and L4 ------------------------------------------------

    @staticmethod
    def _combine(*decisions: SecurityDecision | None) -> SecurityDecision | None:
        chosen: SecurityDecision | None = None
        for decision in decisions:
            if decision is not None and decision.decision is Decision.DENY:
                chosen = decision
                break
        if chosen is None:
            for decision in decisions:
                if decision is not None and decision.decision is Decision.ESCALATE:
                    chosen = decision
                    break
        if chosen is None:
            for decision in decisions:
                if decision is not None and decision.decision is Decision.ALLOW:
                    chosen = decision
                    break

        # Carry the classifier's diagnostics onto the winning verdict.  A
        # deterministic rule often fires first and would otherwise hide the fact
        # that the two channels disagreed -- which is precisely the signal that
        # says "the pattern lane would have missed this one", and therefore the
        # signal that drives tuning.
        richest = next(
            (decision for decision in decisions if decision is not None and decision.disagreement),
            None,
        )
        if chosen is not None and richest is not None and not chosen.disagreement:
            chosen.disagreement = True
            if chosen.channel_a is None:
                chosen.channel_a = richest.channel_a
            if not chosen.channel_b:
                chosen.channel_b = richest.channel_b
        return chosen

    def _confirm(
        self,
        decision: SecurityDecision,
        request: ActionRequest,
        *,
        context: Any | None = None,
    ) -> SecurityDecision:
        """L4: ask the human, through the product's existing approval prompt."""
        permissions = getattr(context, "permissions", None)
        prompt = getattr(permissions, "prompt", None)
        if prompt is None:
            return SecurityDecision(
                decision=Decision.DENY,
                risk=decision.risk,
                stage=Stage.L4_CONFIRM,
                reason=(
                    "needs human confirmation but no interactive prompt is available "
                    f"({decision.rule_id or decision.stage.value}: {decision.reason})"
                ),
                rule_id="L4-NO-PROMPT",
                evidence=decision.evidence,
                channel_a=decision.channel_a,
                channel_b=decision.channel_b,
                disagreement=decision.disagreement,
            )

        payload = {
            "kind": "security",
            "summary": f"mini-code security review flagged {request.tool_name}",
            "details": [
                f"risk: {decision.risk.value}",
                f"rule: {decision.rule_id or decision.stage.value}",
                f"reason: {decision.reason}",
                f"evidence: {decision.evidence[:200]}" if decision.evidence else "evidence: (none)",
                f"tainted: {'yes' if request.tainted else 'no'}",
                f"tool: {request.tool_name}",
                f"arguments: {action_text(request)[:200]}",
            ],
            "scope": request.signature(),
            "choices": [
                {"key": "y", "label": "allow once", "decision": "allow_once"},
                {"key": "a", "label": "allow this action for the session", "decision": "allow_always"},
                {"key": "n", "label": "deny once", "decision": "deny_once"},
                {"key": "d", "label": "deny this action for the session", "decision": "deny_always"},
            ],
        }
        try:
            result = prompt(payload) or {}
        except Exception as error:  # noqa: BLE001 - a broken prompt is not consent
            return SecurityDecision(
                decision=Decision.DENY,
                risk=decision.risk,
                stage=Stage.L4_CONFIRM,
                reason=f"approval prompt failed ({type(error).__name__}); denying",
                rule_id="L4-PROMPT-FAILED",
                evidence=decision.evidence,
            )

        answer = str(result.get("decision") or "")
        allowed = answer in {"allow_once", "allow_always"}
        if allowed and answer == "allow_always":
            self._approved.add(request.signature())
        return SecurityDecision(
            decision=Decision.ALLOW if allowed else Decision.DENY,
            risk=decision.risk,
            stage=Stage.L4_CONFIRM,
            reason=(
                f"human {'allowed' if allowed else 'denied'} a {decision.risk.value}-risk action "
                f"({decision.reason})"
            ),
            rule_id="L4-HUMAN-DECISION",
            evidence=decision.evidence,
            category=decision.category,
            channel_a=decision.channel_a,
            channel_b=decision.channel_b,
            disagreement=decision.disagreement,
            escalated_to_human=True,
        )

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "counts": dict(sorted(self.counts.items())),
            "sessionApprovals": len(self._approved),
            "classifier": self.classifier.stats(),
        }

    def set_mode(self, mode: SecurityMode) -> None:
        """Switch lanes at runtime (used by ``/security mode``)."""
        self.mode = mode
        self.classifier.set_mode(mode)

    def format_status(self) -> str:
        lines = [
            "Security chain",
            "=" * 60,
            f"Mode: {self.mode.value}",
            f"Classifier: {self.classifier.stats()['model']} "
            f"(calls={self.classifier.stats()['modelCalls']}, "
            f"cacheHits={self.classifier.stats()['cacheHits']}, "
            f"disagreements={self.classifier.stats()['disagreements']})",
            f"Session approvals: {len(self._approved)}",
        ]
        if self.counts:
            lines.append("")
            lines.append("Decisions this session:")
            lines.extend(f"  - {key}: {value}" for key, value in sorted(self.counts.items()))
        return "\n".join(lines)


_GATE: SecurityGate | None = None


def get_gate() -> SecurityGate:
    """Process-wide gate used by the tool chokepoint."""
    global _GATE
    if _GATE is None:
        _GATE = SecurityGate()
    return _GATE


def reset_gate() -> None:
    global _GATE
    _GATE = None


# ``rules`` and ``preflight`` are re-exported for the CLI's status report.
__all__ += ["preflight", "rules"]
