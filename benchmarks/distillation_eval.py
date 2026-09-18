"""Measure the distillation loop's payoff: same task, second time, fewer steps.

The acceptance question from the design document is concrete -- "after the loop has
learned, does the second attempt at the same kind of task cost less?" -- so this
script answers exactly that and nothing else:

* for each scenario, the **cold** attempts (what it took before the lesson) and the
  **warm** attempts (after) are compared on tool calls, the quantity "重复执行成本"
  really means here;
* the miner is run over the same corpus to show whether the pipeline *found* the
  lesson (a scenario whose cluster fails the gate cannot have been learned from);
* per-scenario numbers are printed, not just an average, because a mean hides the
  case that got worse.

By default it reads the bundled synthetic corpus, which exercises the whole pipeline
without network access or spend.  ``--corpus`` points it at real recorded sessions
once there are enough of them; the arithmetic is identical either way.

Exit code is 0 when the reduction meets the threshold and 1 otherwise, so it can
become a gate without edits.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from minicode.distillation.miner import mine  # noqa: E402
from minicode.distillation.signals import load_trajectory_refs  # noqa: E402
from minicode.distillation.types import TrajectoryRef  # noqa: E402

DEFAULT_CORPUS = "benchmarks/data/correction_trajectories.jsonl"
DEFAULT_SCENARIOS = "benchmarks/data/correction_scenarios.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _mean_calls(refs: list[TrajectoryRef]) -> float:
    if not refs:
        return 0.0
    return statistics.fmean(len(ref.tool_calls) for ref in refs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default=DEFAULT_CORPUS, help="trajectory JSONL to measure")
    parser.add_argument("--scenarios", default=DEFAULT_SCENARIOS, help="scenario index JSON")
    parser.add_argument("--min-reduction", type=float, default=0.30, help="required mean reduction")
    parser.add_argument("--json", metavar="PATH", default=None, help="also write the metrics as JSON")
    args = parser.parse_args(argv)

    corpus_path = REPO_ROOT / args.corpus if not Path(args.corpus).is_absolute() else Path(args.corpus)
    scenarios_path = (
        REPO_ROOT / args.scenarios if not Path(args.scenarios).is_absolute() else Path(args.scenarios)
    )
    records = _read_jsonl(corpus_path)
    if not records:
        print(f"No trajectories found at {corpus_path}.")
        print("Run some sessions first, or use the bundled synthetic corpus.")
        return 1

    refs = load_trajectory_refs(trajectories=records)
    by_session = {f"{ref.session_id}#{ref.turn}": ref for ref in refs}
    by_id = {ref.session_id: ref for ref in refs}

    scenarios: list[dict[str, Any]] = []
    if scenarios_path.exists():
        payload = json.loads(scenarios_path.read_text(encoding="utf-8"))
        scenarios = list(payload.get("scenarios") or [])

    candidates, rejected = mine(refs)
    # A scenario was "learned from" when one of its attempts ended up inside a
    # candidate cluster -- i.e. the gate accepted that group as a lesson.
    learned: dict[str, bool] = {}
    for candidate in candidates:
        member_sessions = {ref.session_id for ref in candidate.trajectories}
        for scenario in scenarios:
            scenario_ids = set(scenario.get("coldTrajectoryIds") or []) | set(
                scenario.get("warmTrajectoryIds") or []
            )
            if member_sessions & scenario_ids:
                learned[scenario["id"]] = True

    rows: list[dict[str, Any]] = []
    cold_attempts = 0
    warm_attempts = 0
    for scenario in scenarios:
        cold = [by_id[tid] for tid in scenario.get("coldTrajectoryIds") or [] if tid in by_id]
        warm = [by_id[tid] for tid in scenario.get("warmTrajectoryIds") or [] if tid in by_id]
        if not cold or not warm:
            continue
        cold_attempts += len(cold)
        warm_attempts += len(warm)
        cold_mean, warm_mean = _mean_calls(cold), _mean_calls(warm)
        reduction = 1 - (warm_mean / cold_mean) if cold_mean else 0.0
        rows.append(
            {
                "id": scenario["id"],
                "title": scenario.get("title", ""),
                "coldToolCalls": round(cold_mean, 2),
                "warmToolCalls": round(warm_mean, 2),
                "reduction": round(reduction, 4),
                "coldSuccess": sum(1 for ref in cold if ref.succeeded),
                "warmSuccess": sum(1 for ref in warm if ref.succeeded),
                "gatePassed": learned.get(scenario["id"], False),
            }
        )

    if not rows:
        print("Scenario index and corpus do not line up; nothing to measure.")
        return 1

    reductions = [row["reduction"] for row in rows]
    mean_reduction = statistics.fmean(reductions)
    median_reduction = statistics.median(reductions)
    warm_success = sum(row["warmSuccess"] for row in rows)
    cold_success = sum(row["coldSuccess"] for row in rows)

    header = [
        "Distillation payoff evaluation",
        "=" * 72,
        f"Corpus: {corpus_path.name} ({len(refs)} trajectories, "
        f"{sum(1 for ref in refs if ref.synthetic)} synthetic)",
        f"Scenarios measured: {len(rows)}   candidates mined: {len(candidates)}   "
        f"clusters skipped: {len(rejected)}",
        "",
        f"{'scenario':32s} {'cold':>6s} {'warm':>6s} {'reduction':>10s}  gate",
        "-" * 72,
    ]
    for row in rows:
        header.append(
            f"{row['id'][:32]:32s} {row['coldToolCalls']:6.1f} {row['warmToolCalls']:6.1f} "
            f"{row['reduction']:9.0%}  {'yes' if row['gatePassed'] else 'NO'}"
        )

    suggestions = sum(1 for row in rows if row["gatePassed"])
    footer = [
        "-" * 72,
        f"Mean reduction   : {mean_reduction:.1%}   (target >= {args.min_reduction:.0%})",
        f"Median reduction : {median_reduction:.1%}",
        f"Success rate     : cold {cold_success}/{cold_attempts} attempts, "
        f"warm {warm_success}/{warm_attempts} attempts",
        f"Gate passed      : {suggestions}/{len(rows)} scenarios became distillation candidates",
        "",
        "Note: a scenario whose cluster fails the gate cannot have been learned from;",
        "the tool-call numbers come from the recorded attempts and are the acceptance metric.",
        "",
        "RESULT: " + ("PASS" if mean_reduction >= args.min_reduction else "FAIL"),
    ]
    print("\n".join([*header, *footer]))

    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "corpus": str(corpus_path),
                    "trajectories": len(refs),
                    "synthetic": sum(1 for ref in refs if ref.synthetic),
                    "candidates": len(candidates),
                    "skippedClusters": len(rejected),
                    "scenarios": rows,
                    "meanReduction": round(mean_reduction, 4),
                    "medianReduction": round(median_reduction, 4),
                    "minReduction": args.min_reduction,
                    "pass": mean_reduction >= args.min_reduction,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON written to {target}")

    return 0 if mean_reduction >= args.min_reduction else 1


if __name__ == "__main__":
    raise SystemExit(main())
