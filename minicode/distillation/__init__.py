"""Self-learning distillation loop: feedback -> candidate skills -> human review.

Module map:

* ``signals``    -- what in a trajectory says "this attempt is worth learning from"
* ``miner``      -- group similar tasks and apply the "was a lesson learned?" gate
* ``distiller``  -- the offline model batch that writes a proposed skill
* ``draft``      -- the draft area, plus the loop's memory of refusals and spend
* ``review``     -- the human gate: screen, approve, reject (the only write path)
* ``types``      -- the shared vocabulary

The loop is deliberately unable to install anything by itself.  ``SkillDraft``
objects live in a draft area under ``~/.mini-code/skill-drafts/`` until a person
approves them, and approval is the only code path that writes into a workspace's
skill root.  That is the防污染 requirement from the design document expressed as a
structure rather than a promise.
"""

from __future__ import annotations

from minicode.distillation.draft import DistillationState, DraftStore, drafts_dir
from minicode.distillation.distiller import Distiller
from minicode.distillation.miner import MiningGate, mine
from minicode.distillation.review import (
    ReviewOutcome,
    approve_draft,
    reject_draft,
    screen_draft,
)
from minicode.distillation.signals import collect_signals, load_trajectory_refs
from minicode.distillation.types import (
    CandidateCluster,
    DraftStatus,
    FeedbackSignal,
    SkillDraft,
    TrajectoryRef,
)

__all__ = [
    "CandidateCluster",
    "DistillationState",
    "Distiller",
    "DraftStatus",
    "DraftStore",
    "FeedbackSignal",
    "MiningGate",
    "ReviewOutcome",
    "SkillDraft",
    "TrajectoryRef",
    "approve_draft",
    "collect_signals",
    "drafts_dir",
    "load_trajectory_refs",
    "mine",
    "reject_draft",
    "screen_draft",
]
