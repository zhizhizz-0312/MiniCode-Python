"""``python -m minicode.orchestration`` -- plan, run and replay outside a session.

    python -m minicode.orchestration plan <composite>      # what would run (free)
    python -m minicode.orchestration run <composite>       # run it (spends money)
    python -m minicode.orchestration run <composite> --dry-run   # validate, do not run
    python -m minicode.orchestration agents                # runs so far, newest first
    python -m minicode.orchestration replay [<run-id>]     # the trail, with its checks

``plan`` and ``replay`` are free and therefore the defaults for inspection: the point
of a plan-driven design is that a human can look before anything expensive happens.
``run`` says up front what it may cost, and refuses to start on a plan that failed
validation rather than discovering the problem halfway through.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from minicode.orchestration.audit import (
    audit_path,
    format_replay,
    latest_orchestration,
    list_orchestrations,
    load_entries,
    replay,
)
from minicode.orchestration.orchestrator import Orchestrator
from minicode.orchestration.plan import load_plan
from minicode.orchestration.pricing import session_currency
from minicode.orchestration.report import format_plan_preview, format_result
from minicode.orchestration.worker import AgentWorker

__all__ = ["build_parser", "main"]


def _find_spec(name: str, cwd: str):
    """Locate a composite skill by name in the workspace registry."""
    from minicode.skill_system.registry import SkillKind, SkillRegistry

    registry = SkillRegistry.discover(cwd)
    spec = registry.get(name)
    if spec is None:
        return None, registry, f"no skill named {name!r} in this workspace"
    if spec.kind is not SkillKind.COMPOSITE:
        return None, registry, f"{name!r} is a {spec.kind.value} skill, not a composite plan"
    return spec, registry, ""


def _cmd_plan(args: argparse.Namespace) -> int:
    cwd = str(Path(args.cwd).resolve())
    spec, registry, error = _find_spec(args.name, cwd)
    if spec is None:
        print(error, file=sys.stderr)
        return 2
    plan, issues = load_plan(spec, registry=registry)
    if plan is None:
        print(f"The plan for {args.name!r} cannot be run:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print(format_plan_preview(plan, currency=session_currency()))
    for issue in issues:
        print(f"note: {issue}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nplan written to {args.json}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cwd = str(Path(args.cwd).resolve())
    spec, registry, error = _find_spec(args.name, cwd)
    if spec is None:
        print(error, file=sys.stderr)
        return 2
    plan, issues = load_plan(spec, registry=registry)
    if plan is None:
        print(f"Refusing to run: the plan for {args.name!r} failed validation.", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    for issue in issues:
        print(f"note: {issue}")
    print(format_plan_preview(plan, currency=session_currency()))
    if args.max_concurrent:
        plan.max_concurrent = max(1, args.max_concurrent)
    if args.dry_run:
        print("\nDry run: the plan is valid and nothing was executed, and nothing was spent.")
        return 0

    print(
        f"\nRunning {len(plan.subtasks)} subtask(s) with at most {plan.max_concurrent} at a time; "
        f"cost cap ${plan.max_cost_usd:.2f}. Child budgets: "
        f"{plan.subtasks[0].budget.max_turns if plan.subtasks and plan.subtasks[0].budget else 'default'} turns each."
    )
    orchestrator = Orchestrator(
        plan=plan,
        repo_root=cwd,
        worker=AgentWorker(),
        orchestration_id=args.run_id or "",
        base_dir=args.worktree_base or None,
    )
    result = orchestrator.run()
    print("")
    print(format_result(result))
    print("")
    print(format_replay(replay(
        load_entries(audit_path(result.orchestration_id, home=args.home or None)),
        expected_cost_usd=result.cost_usd,
        expected_children=[child.subtask_id for child in result.children],
    )))
    return 0 if result.status.value == "complete" else 1


def _cmd_agents(args: argparse.Namespace) -> int:
    runs = list_orchestrations(home=args.home or None)
    if not runs:
        print("No orchestrations recorded yet.")
        return 0
    print(f"Orchestrations ({len(runs)}), newest first:")
    for run in runs:
        print(f"  {run.describe()}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    run_id = args.run_id or latest_orchestration(home=args.home or None)
    if not run_id:
        print("No orchestration to replay.", file=sys.stderr)
        return 1
    entries = load_entries(audit_path(run_id, home=args.home or None))
    if not entries:
        print(f"No audit trail for {run_id!r}.", file=sys.stderr)
        return 1
    report = replay(entries)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(format_replay(report))
    return 0 if report.consistent else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m minicode.orchestration",
        description="Run a composite skill's plan with several execution agents.",
    )
    parser.add_argument("--cwd", default=".", help="workspace to run in")
    parser.add_argument("--home", default="", help="where orchestration trails live")
    parser.add_argument("--json", default="", help="also write machine-readable output here")
    sub = parser.add_subparsers(dest="command")

    plan = sub.add_parser("plan", help="show what would run (free)")
    plan.add_argument("name", help="composite skill name")

    run = sub.add_parser("run", help="execute the plan (spends money)")
    run.add_argument("name", help="composite skill name")
    run.add_argument("--dry-run", action="store_true", help="validate only")
    run.add_argument("--max-concurrent", type=int, default=0)
    run.add_argument("--run-id", default="")
    run.add_argument("--worktree-base", default="")

    sub.add_parser("agents", help="list recorded orchestrations")
    replay_cmd = sub.add_parser("replay", help="replay an orchestration's audit trail")
    replay_cmd.add_argument("run_id", nargs="?", default="")
    return parser


_COMMANDS = {
    "plan": _cmd_plan,
    "run": _cmd_run,
    "agents": _cmd_agents,
    "replay": _cmd_replay,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(getattr(args, "command", None) or "")
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - a diagnostic tool must not traceback
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
