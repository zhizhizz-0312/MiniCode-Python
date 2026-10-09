"""Measure what orchestration actually buys: three parallel tasks, run twice.

The design document's acceptance question is concrete -- "does splitting work across
execution agents beat doing it sequentially, and at what price?" -- so this script
answers exactly that and prints the arithmetic rather than a verdict in prose.

Both modes run the **same plan and the same subtasks**; only the concurrency differs.
That matters: comparing a parallel run against a differently-shaped sequential one would
measure the tasks, not the orchestration.

The honest cost of parallelism is stated up front, because it is real: each child pays
for its own context and system prompt, so a parallel run is *more* expensive per unit of
work.  The gate is therefore two-sided -- faster by 1.5x, and no more than twice the
cost -- and a result that misses either half is reported as a failure, not rounded off.

Usage note: if the turn runner reports no token or cost usage, the cost columns are
zero and the script says so rather than implying the children were free.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from minicode.orchestration.audit import OrchestrationAudit  # noqa: E402
from minicode.orchestration.orchestrator import Orchestrator  # noqa: E402
from minicode.orchestration.types import OrchestrationPlan, SubtaskSpec  # noqa: E402
from minicode.orchestration.worker import AgentWorker  # noqa: E402

#: Three read-only subtasks over files every checkout has, so the comparison is about
#: the orchestration rather than about the tasks.  Read-only on purpose: writers need
#: worktrees, which would add git's own cost to a measurement of agent concurrency.
DEFAULT_TASKS = (
    ("read-readme", "README.md", "MiniCode"),
    ("read-pyproject", "pyproject.toml", "minicode-py"),
    ("read-cli-commands", "minicode/cli_commands.py", "def "),
)


def build_plan(mode: str, *, budget_usd: float, max_turns: int) -> OrchestrationPlan:
    from minicode.orchestration.budget import Budget

    concurrent = 3 if mode == "parallel" else 1
    subtasks = tuple(
        SubtaskSpec(
            id=name,
            skill="(inline)",
            instruction=(
                f"Read {path} with the read_file tool and reply with one line from it that "
                f"contains {expected!r}. Reply with nothing else."
            ),
            tools_whitelist=("read_file",),
            writes=False,
            budget=Budget(max_turns=max_turns, max_tool_calls=6, max_cost_usd=budget_usd / 3),
        )
        for name, path, expected in DEFAULT_TASKS
    )
    return OrchestrationPlan(
        name=f"orchestration-eval-{mode}",
        subtasks=subtasks,
        max_children=3,
        max_concurrent=concurrent,
        max_cost_usd=budget_usd,
    )


def run_mode(
    mode: str,
    *,
    cwd: Path,
    home: Path,
    budget_usd: float,
    max_turns: int,
) -> dict[str, Any]:
    plan = build_plan(mode, budget_usd=budget_usd, max_turns=max_turns)
    audit = OrchestrationAudit(f"eval-{mode}", home=home)
    started = time.monotonic()
    result = Orchestrator(
        plan=plan, repo_root=cwd, worker=AgentWorker(), orchestration_id=f"eval-{mode}", audit=audit
    ).run()
    wall = time.monotonic() - started
    expected = {name: token for name, _path, token in DEFAULT_TASKS}
    children = []
    for child in result.children:
        children.append(
            {
                "id": child.subtask_id,
                "status": child.status.value,
                "correct": child.status.value == "done"
                and expected.get(child.subtask_id, "") in (child.summary or ""),
                "turns": child.turns,
                "toolCalls": child.tool_calls,
                "tokens": child.tokens,
                "costUsd": child.cost_usd,
                "summary": (child.summary or "")[:60],
            }
        )
    return {
        "mode": mode,
        "wallSeconds": round(wall, 3),
        "status": result.status.value,
        "costUsd": round(result.cost_usd, 6),
        "tokens": result.tokens,
        "correct": sum(1 for item in children if item["correct"]),
        "total": len(children),
        "children": children,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=str(REPO_ROOT), help="repository to run against")
    parser.add_argument("--home", default="", help="where to write the audit trails")
    parser.add_argument("--budget", type=float, default=0.30, help="USD cap for each mode")
    parser.add_argument("--max-turns", type=int, default=4, help="turns per child")
    parser.add_argument("--min-speedup", type=float, default=1.5, help="required wall-clock gain")
    parser.add_argument("--max-cost-ratio", type=float, default=2.0, help="allowed cost multiple")
    parser.add_argument("--json", metavar="PATH", default="")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    home = Path(args.home) if args.home else (REPO_ROOT / ".temp" / "orchestration-eval")
    sequential = run_mode("sequential", cwd=cwd, home=home, budget_usd=args.budget, max_turns=args.max_turns)
    parallel = run_mode("parallel", cwd=cwd, home=home, budget_usd=args.budget, max_turns=args.max_turns)

    speedup = sequential["wallSeconds"] / parallel["wallSeconds"] if parallel["wallSeconds"] else 0.0
    cost_ratio = (
        parallel["costUsd"] / sequential["costUsd"] if sequential["costUsd"] else 0.0
    )
    faster = speedup >= args.min_speedup
    affordable = cost_ratio <= args.max_cost_ratio if sequential["costUsd"] else None
    correctness_held = parallel["correct"] >= sequential["correct"]

    lines = [
        "Orchestration payoff evaluation",
        "=" * 72,
        f"cwd: {cwd}",
        f"tasks: {len(DEFAULT_TASKS)} read-only subtasks, identical in both modes",
        "",
        f"{'mode':12s} {'wall':>8s} {'cost':>10s} {'tokens':>8s} {'correct':>8s} {'status':>10s}",
        "-" * 72,
    ]
    for row in (sequential, parallel):
        lines.append(
            f"{row['mode']:12s} {row['wallSeconds']:7.1f}s {row['costUsd']:10.6f} "
            f"{row['tokens']:8d} {row['correct']:4d}/{row['total']:<3d} {row['status']:>10s}"
        )
    lines.extend(["", f"speedup        : {speedup:.2f}x   (required >= {args.min_speedup:.2f}x)  "
                      f"{'PASS' if faster else 'FAIL'}"])
    if sequential["costUsd"]:
        lines.append(
            f"cost multiple  : {cost_ratio:.2f}x   (allowed <= {args.max_cost_ratio:.2f}x)  "
            f"{'PASS' if affordable else 'FAIL'}"
        )
    else:
        lines.append(
            "cost multiple  : unknown -- the turn runner reported no usage, so the cost "
            "columns are zero and the money half of this gate cannot be evaluated"
        )
    lines.append(f"correctness    : {'held' if correctness_held else 'DEGRADED'}")
    lines.extend(["", "Per child:"])
    for mode, row in (("sequential", sequential), ("parallel", parallel)):
        for child in row["children"]:
            lines.append(
                f"  {mode:11s} {child['id']:20s} {child['status']:14s} "
                f"{child['turns']}t/{child['toolCalls']}c  {'ok' if child['correct'] else 'MISS'}  "
                f"{child['summary']!r}"
            )
    lines.append("")
    lines.append("RESULT: " + ("PASS" if faster and correctness_held and affordable is not False else "FAIL"))
    print("\n".join(lines))

    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "sequential": sequential,
                    "parallel": parallel,
                    "speedup": round(speedup, 3),
                    "costMultiple": round(cost_ratio, 3) if sequential["costUsd"] else None,
                    "minSpeedup": args.min_speedup,
                    "maxCostRatio": args.max_cost_ratio,
                    "pass": bool(faster and correctness_held and affordable is not False),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON written to {target}")

    return 0 if (faster and correctness_held and affordable is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
