"""Candidate mining: decide which trajectories are worth asking the model about.

Two ideas carry this module, and both exist to stop the loop from learning
nonsense.

**Group before distilling.**  A single trajectory teaches nothing: "the agent once
ran pytest and it worked" is not a rule.  Similar *tasks* seen more than once are,
so trajectories are clustered by task similarity and only clusters are distilled.
With one sample, whatever happened is indistinguishable from luck.

**Require the shape of a lesson.**  A cluster is only a candidate when it contains
a correction or a failure *and* a success, and -- by default -- when the most
recent attempt in the cluster succeeded.  That is the design document's scenario
("corrected once, then meets the same task again") expressed as a filter, and it is
what separates "we learned something" from "we saw two unrelated things".

Clustering is greedy agglomeration over Jaccard similarity of task tokens, reusing
the skill registry's tokeniser so Chinese and English behave consistently.  It is
deterministic and standard-library only; a clustering library would add a runtime
dependency for no gain at this corpus size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from minicode.distillation.signals import collect_signals
from minicode.distillation.types import CandidateCluster, TrajectoryRef
from minicode.skill_system.registry import normalize_text, text_tokens

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "MiningGate",
    "cluster_refs",
    "cluster_signature",
    "mine",
    "prior_confidence",
    "similarity",
    "task_tokens",
]

DEFAULT_SIMILARITY_THRESHOLD = 0.34


def task_tokens(prompt: str) -> set[str]:
    """Tokens of a task description (shared tokeniser with skill matching)."""
    return text_tokens(normalize_text(prompt or ""))


def similarity(left: set[str], right: set[str]) -> float:
    """Overlap coefficient (containment), not Jaccard.

    A correction rephrases the task with *more* words -- "不对，这个项目要用 .venv 里的
    python，再跑一次 tests/ 的用例" against "把 tests/ 下所有用例跑一遍".  A
    union-based score punishes exactly that extra wording, which split the
    cold/corrected/warm triples this loop exists to find.  Dividing by the smaller
    set measures whether one description is contained in the other, which is the
    property that actually matters here.

    0.0 when either side is empty.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


@dataclass
class MiningGate:
    """The thresholds that decide what becomes a candidate."""

    min_trajectories: int = 2
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    require_success: bool = True
    require_progression: bool = True
    require_correction_or_failure: bool = True

    def evaluate(self, cluster: Sequence[TrajectoryRef]) -> tuple[bool, str]:
        """Return ``(accepted, reason)`` for one cluster."""
        if len(cluster) < self.min_trajectories:
            return False, f"only {len(cluster)} trajectory(ies); need {self.min_trajectories}"
        successes = [ref for ref in cluster if ref.succeeded]
        setbacks = [ref for ref in cluster if ref.failed or ref.correction]
        if self.require_success and not successes:
            return False, "no successful attempt to learn from"
        if self.require_correction_or_failure and not setbacks:
            return False, "no correction or failure; nothing was learned the hard way"
        if self.require_progression:
            latest = max(cluster, key=lambda ref: (ref.started_at, ref.turn))
            if not latest.succeeded:
                return False, "the most recent attempt in this cluster did not succeed"
        return (
            True,
            f"{len(cluster)} attempt(s), {len(successes)} success(es), {len(setbacks)} setback(s)",
        )


#: A correction is short and rephrased, so it is matched by *absolute* shared
#: evidence rather than by a ratio: three shared topic tokens out of seventeen is
#: real signal when the message is "no, use the project venv and run the tests
#: again", but no ratio-based score would say so.
CORRECTION_MIN_SHARED_TOKENS = 2


def cluster_refs(
    refs: Iterable[TrajectoryRef],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[TrajectoryRef]]:
    """Group attempts into clusters, by two different rules.

    **A correction belongs to what it corrects.**  "不对，应该用 venv 跑" is a reply to
    the previous attempt, not a new task, and its wording is mostly *new* words.  So
    a correction is first attached to the previous attempt in the same session, and
    failing that, to the cluster it shares the most topic tokens with.  Clustering it
    by prompt similarity instead was measured to leave it stranded in a cluster of
    one -- which silently discarded the single most valuable signal in the corpus.

    **Ordinary attempts are grouped by containment.**  Everything else is compared
    against the clusters so far with the overlap coefficient, which tolerates one
    phrasing being longer than the other.
    """
    ordered = sorted(refs, key=lambda ref: (ref.started_at, ref.session_id, ref.turn))
    clusters: list[list[TrajectoryRef]] = []
    cluster_tokens: list[set[str]] = []
    last_cluster_by_session: dict[str, int] = {}

    for ref in ordered:
        tokens = task_tokens(ref.prompt)
        index: int | None = None

        if ref.correction:
            index = last_cluster_by_session.get(ref.session_id)
            if index is None:
                best_index, best_shared = None, 0
                for candidate, group in enumerate(cluster_tokens):
                    shared = len(tokens & group)
                    if shared > best_shared:
                        best_index, best_shared = candidate, shared
                if best_index is not None and best_shared >= CORRECTION_MIN_SHARED_TOKENS:
                    index = best_index

        if index is None:
            best_index, best_score = None, 0.0
            for candidate, group in enumerate(cluster_tokens):
                score = similarity(tokens, group)
                if score >= threshold and score > best_score:
                    best_index, best_score = candidate, score
            index = best_index

        if index is None:
            clusters.append([ref])
            cluster_tokens.append(set(tokens))
            index = len(clusters) - 1
        else:
            clusters[index].append(ref)
            cluster_tokens[index] |= tokens
        last_cluster_by_session[ref.session_id] = index

    return clusters


def cluster_signature(cluster: Sequence[TrajectoryRef]) -> str:
    """A canonical name for the cluster: the shortest successful task phrasing.

    Shortest, because the shortest phrasing of a task tends to be the least
    incidental one; deterministic, so re-running the miner on the same corpus
    produces the same draft ids.
    """
    if not cluster:
        return ""
    successes = [ref for ref in cluster if ref.succeeded] or list(cluster)
    return min(successes, key=lambda ref: (len(ref.prompt), ref.prompt)).prompt.strip()


def prior_confidence(cluster: Sequence[TrajectoryRef]) -> float:
    """How much the *evidence* supports a skill, before a model says anything.

    Sample count dominates.  A rule inferred from two sessions is a guess no
    matter how fluent the write-up, so the prior is capped below the point where a
    model's self-reported confidence could carry a draft on its own.
    """
    base = min(0.5, 0.15 * len(cluster))
    if any(ref.succeeded for ref in cluster):
        base += 0.1
    if any(ref.correction for ref in cluster):
        base += 0.1
    return round(min(base, 0.65), 2)


def mine(
    refs: Iterable[TrajectoryRef],
    *,
    gate: MiningGate | None = None,
) -> tuple[list[CandidateCluster], list[tuple[str, str]]]:
    """Cluster trajectories and apply the gate.

    Returns ``(candidates, rejected)`` where each rejection is
    ``(signature, reason)`` -- a run that produces no drafts still has to be able
    to explain why, otherwise "the loop found nothing" is indistinguishable from
    "the loop is broken".
    """
    active = gate or MiningGate()
    candidates: list[CandidateCluster] = []
    rejected: list[tuple[str, str]] = []
    for cluster in cluster_refs(refs, threshold=active.similarity_threshold):
        accepted, reason = active.evaluate(cluster)
        signature = cluster_signature(cluster)
        if not accepted:
            rejected.append((signature, reason))
            continue
        candidates.append(
            CandidateCluster(
                signature=signature,
                trajectories=list(cluster),
                signals=collect_signals(cluster),
                reason=reason,
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.size, candidate.signature))
    return candidates, rejected
