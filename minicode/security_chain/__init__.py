"""Four-layer security review chain for the coding agent.

Layer map:

* ``rules``       -- L1: deterministic patterns and structured deny reasons
* ``preflight``   -- L2: deterministic checks against the target resource
* ``classifier``  -- L3: feature channel + model channel, fused into one verdict
* ``gate``        -- L4 plus orchestration: combine layers, ask the human, audit
* ``taint``       -- provenance: was this action driven by something the agent read?
* ``audit``       -- every verdict, both to the product's audit chain and to JSONL
* ``eval``        -- the attack/benign corpus harness and its report
* ``types``       -- the shared vocabulary the layers speak

Two boundaries are enforced, not one: content is judged (and marked) as it enters
the context, and actions are judged as they leave.  Judging only actions cannot
tell "the user asked for this" apart from "a poisoned document asked for this",
which is the entire attack.
"""

from __future__ import annotations

from minicode.security_chain.classifier import (
    RiskClassifier,
    get_classifier,
    reset_classifier,
)
from minicode.security_chain.gate import SecurityGate, get_gate, reset_gate
from minicode.security_chain.taint import TaintRegistry, get_registry, reset_registry
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

#: Subcommands of the ``/security`` slash command, in help order.
#:
#: Declared here rather than in the slash-command registry because these are
#: arguments of an existing command, not commands of their own: they would
#: otherwise each need a ``/help`` entry and a dispatch site while all routing
#: through the same ``/security`` branch.  The dispatcher's usage line and the
#: terminal's Tab completion both read this tuple, so they cannot drift.
SECURITY_SUBCOMMANDS = ("audit", "mode", "eval", "taint")

__all__ = [
    "ActionRequest",
    "ContentRequest",
    "Decision",
    "RiskClassifier",
    "RiskLevel",
    "SECURITY_SUBCOMMANDS",
    "SecurityDecision",
    "SecurityGate",
    "SecurityMode",
    "Stage",
    "TaintRegistry",
    "get_classifier",
    "get_gate",
    "get_registry",
    "mode_from_env",
    "reset_classifier",
    "reset_gate",
    "reset_registry",
]
