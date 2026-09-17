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

__all__ = [
    "ActionRequest",
    "ContentRequest",
    "Decision",
    "RiskClassifier",
    "RiskLevel",
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
