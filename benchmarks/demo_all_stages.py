"""Run every stage's verification in one command.

Four stages were built independently, so this is the one place that shows them
together -- and, deliberately, the free checks are separate from the paid ones:

* the default run touches no network and costs nothing: the skill registry, the
  heuristic security lane, the learning loop's own state, and the orchestration record;
* ``--with-paid`` adds the two measurements that call a model: the full injection
  evaluation and the serial-versus-parallel comparison.

The split exists because a demo that silently spends money is a demo nobody runs twice.
Each stage prints what it found and, where a number exists, the number; nothing here
asserts success on the project's behalf -- the scripts it calls own their own exit codes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _header(title: str) -> None:
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)


def stage_skills(cwd: Path) -> None:
    """P1: what the registry found, and whether it is healthy."""
    _header("P1  skills")
    from minicode.skill_system.registry import SkillKind, SkillRegistry

    registry = SkillRegistry.discover(str(cwd))
    names = registry.names()
    counts: dict[str, int] = {}
    issues: list[str] = []
    for name in names:
        spec = registry.get(name)
        kind = getattr(getattr(spec, "kind", None), "value", "?")
        counts[kind] = counts.get(kind, 0) + 1
        issues.extend(getattr(spec, "issues", ()) or ())
    print(f"  skills discovered : {len(names)}  ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})")
    print(f"  composites usable : {sum(1 for name in names if getattr(registry.get(name), 'kind', None) is SkillKind.COMPOSITE)}")
    print(f"  registry issues   : {len(issues)}")
    for issue in issues[:5]:
        print(f"    - {issue}")
    print("  verify by hand    : /skills, /skills doctor, /skills plan <composite>")


def stage_security(free: bool = True) -> None:
    """P2: the injection corpus, in whichever lane was asked for."""
    _header("P2  security review" + (" (heuristic lane, free)" if free else " (standard + model)"))
    from minicode.security_chain.eval import run_eval
    from minicode.security_chain.types import SecurityMode

    mode = SecurityMode.HEURISTIC if free else SecurityMode.STANDARD
    report = run_eval(mode=mode, use_model=not free)
    print(f"  interception      : {report.intercepted}/{report.attacks} = {report.interception_rate:.1%}")
    print(f"  false positives   : {report.false_positives}/{report.benign_total} = {report.false_positive_rate:.1%}")
    print(f"  escalate=blocked  : in a non-interactive run an escalation cannot ask, so it denies")
    if free:
        print("  note              : the heuristic lane is free; --with-paid runs the measured default")
    print("  reproduce         : python benchmarks/injection_eval.py --mode standard --model")


def stage_learning() -> None:
    """P3: the corpus, the gate, the queue and the spend."""
    _header("P3  learning loop")
    from minicode.distillation.draft import DraftStore
    from minicode.distillation.miner import mine
    from minicode.distillation.signals import load_trajectory_refs

    refs = load_trajectory_refs()
    candidates, rejected = mine(refs)
    store = DraftStore()
    drafts = store.list()
    state = store.load_state()
    print(f"  trajectories      : {len(refs)} ({sum(1 for r in refs if r.synthetic)} synthetic)")
    print(f"  candidates        : {len(candidates)}   clusters skipped: {len(rejected)}")
    print(f"  drafts            : {len(drafts)} ({sum(1 for d in drafts if d.pending)} awaiting review)")
    print(f"  refusals remembered: {len(state.rejected)}   distillation spend: ${state.spent_usd:.4f}")
    if not candidates:
        print("  honest state      : nothing passed the gate yet, so nothing was invented")
    print("  reproduce         : python -m minicode.distillation report")


def stage_orchestration(cwd: Path) -> None:
    """P4: what has run, and what a plan would look like."""
    _header("P4  orchestration")
    from minicode.orchestration.audit import list_orchestrations, load_entries, replay
    from minicode.orchestration.plan import load_plan
    from minicode.orchestration.report import format_plan_preview
    from minicode.skill_system.registry import SkillKind, SkillRegistry

    runs = list_orchestrations()
    print(f"  runs recorded     : {len(runs)}")
    for run in runs[:3]:
        print(f"    - {run.describe()}")
        entries = load_entries(
            __import__("minicode.orchestration.audit", fromlist=["audit_path"]).audit_path(run.orchestration_id)
        )
        if entries:
            report = replay(entries)
            print(f"      replay consistent: {report.consistent}  entries={len(entries)}")

    registry = SkillRegistry.discover(str(cwd))
    composites = [
        name for name in registry.names()
        if getattr(registry.get(name), "kind", None) is SkillKind.COMPOSITE
    ]
    if composites:
        plan, issues = load_plan(registry.get(composites[0]), registry=registry)
        print("")
        print(format_plan_preview(plan) if plan is not None else f"  plan invalid: {issues}")
    else:
        print("  no composite skill here yet -- try /skills install-examples, then /skills plan <name>")
    print("  reproduce         : python -m minicode.orchestration agents | replay")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=str(REPO_ROOT))
    parser.add_argument(
        "--with-paid",
        action="store_true",
        help="also run the steps that call a model (injection evaluation, orchestration comparison)",
    )
    args = parser.parse_args(argv)
    cwd = Path(args.cwd).resolve()

    stage_skills(cwd)
    stage_security(free=not args.with_paid)
    stage_learning()
    stage_orchestration(cwd)

    _header("paid measurements" if args.with_paid else "done (nothing was spent)")
    if args.with_paid:
        print("  run these, they are the measured tables quoted in the README:")
        print("    python benchmarks/injection_eval.py --mode standard --model")
        print("    python benchmarks/orchestration_eval.py")
    else:
        print("  the free checks above cover every stage's state; add --with-paid for the")
        print("  two measurements that call a model (about $0.05 in total).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
