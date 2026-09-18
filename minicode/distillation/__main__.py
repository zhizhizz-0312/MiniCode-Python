"""``python -m minicode.distillation`` -- the loop outside a session.

Subcommands mirror the ``/skills`` ones, so a user can prepare and review skills
before starting the agent, and the numbers they see are the same either way.

    python -m minicode.distillation report
    python -m minicode.distillation mine              # what would be distilled (no model)
    python -m minicode.distillation distill            # run the batch (model, budgeted)
    python -m minicode.distillation list
    python -m minicode.distillation show <id>
    python -m minicode.distillation approve <id> --cwd .
    python -m minicode.distillation reject <id> --reason "too specific to one incident"

``--synthetic`` reads the bundled corpus under ``benchmarks/data`` instead of the
recorded sessions: it exercises the whole pipeline without waiting for real usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from minicode.distillation.draft import DraftStore
from minicode.distillation.distiller import Distiller
from minicode.distillation.miner import MiningGate, mine
from minicode.distillation.report import (
    format_candidates,
    format_draft_detail,
    format_drafts,
    format_loop_report,
)
from minicode.distillation.review import approve_draft, reject_draft
from minicode.distillation.signals import load_trajectory_refs

_SYNTHETIC_CORPUS = "benchmarks/data/correction_trajectories.jsonl"


def _load_refs(args: argparse.Namespace):  # noqa: ANN001 - argparse namespace
    if args.synthetic:
        path = Path(args.corpus or _SYNTHETIC_CORPUS)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        if not path.exists():
            return []
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return load_trajectory_refs(trajectories=records)
    if args.corpus:
        path = Path(args.corpus)
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return load_trajectory_refs(trajectories=records)
    return load_trajectory_refs()


def _mine(args: argparse.Namespace):  # noqa: ANN001
    refs = _load_refs(args)
    gate = MiningGate(min_trajectories=args.min_trajectories)
    return refs, mine(refs, gate=gate)


def _cmd_report(args: argparse.Namespace) -> int:
    refs, (candidates, rejected) = _mine(args)
    store = DraftStore()
    print(format_loop_report(
        trajectories=refs, candidates=candidates, rejected=rejected, store=store
    ))
    return 0


def _cmd_mine(args: argparse.Namespace) -> int:
    refs, (candidates, rejected) = _mine(args)
    print(format_candidates(candidates, rejected, trajectory_count=len(refs)))
    return 0


def _cmd_distill(args: argparse.Namespace) -> int:
    refs, (candidates, rejected) = _mine(args)
    store = DraftStore()
    distiller = Distiller(budget_usd=args.budget)
    report = distiller.distill(candidates, store=store)
    print(
        f"Distilled {report.produced} draft(s) from {len(candidates)} candidate(s); "
        f"model calls: {report.model_calls}, spend: ${report.spent_usd:.4f}, "
        f"{report.elapsed_s:.1f}s"
    )
    if report.skipped:
        print("\nSkipped:")
        for signature, reason in report.skipped[:10]:
            print(f"  - {signature[:60]!r}: {reason}")
    if report.failures:
        print("\nFailed:")
        for signature, reason in report.failures[:10]:
            print(f"  - {signature[:60]!r}: {reason}")
    if report.drafts:
        print("")
        print(format_drafts(report.drafts))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    print(format_drafts(DraftStore().list()))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    draft = DraftStore().get(args.draft_id)
    if draft is None:
        print(f"No draft with id {args.draft_id!r}.", file=sys.stderr)
        return 1
    print(format_draft_detail(draft))
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    outcome = approve_draft(DraftStore(), args.draft_id, cwd=args.cwd or os.getcwd())
    print(outcome.message)
    return 0 if outcome.ok else 1


def _cmd_reject(args: argparse.Namespace) -> int:
    outcome = reject_draft(DraftStore(), args.draft_id, args.reason or "")
    print(outcome.message)
    return 0 if outcome.ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m minicode.distillation",
        description="Mine recorded sessions for reusable skills and review the drafts.",
    )
    parser.add_argument("--cwd", default=None, help="workspace to install an approved skill into")
    parser.add_argument("--corpus", default=None, help="trajectory JSONL file to mine instead of the store")
    parser.add_argument("--synthetic", action="store_true", help=f"mine {_SYNTHETIC_CORPUS}")
    parser.add_argument("--json", action="store_true", help="print machine-readable output where supported")
    parser.add_argument("--min-trajectories", type=int, default=2, help="cluster size required to distil")
    parser.add_argument("--budget", type=float, default=0.5, help="USD cap for one distill run")
    sub = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("report", "summarise the loop: corpus, gate, queue, refusals, spend"),
        ("mine", "list candidates and skipped clusters without calling a model"),
        ("distill", "distil candidates into drafts (calls the model, budgeted)"),
        ("list", "list drafts awaiting review"),
    ):
        sub.add_parser(name, help=help_text)

    show = sub.add_parser("show", help="print one draft in full")
    show.add_argument("draft_id", metavar="ID")

    approve = sub.add_parser("approve", help="screen and install a draft into the workspace")
    approve.add_argument("draft_id", metavar="ID")

    reject = sub.add_parser("reject", help="refuse a draft and remember the decision")
    reject.add_argument("draft_id", metavar="ID")
    reject.add_argument("--reason", default="", help="why it was refused (kept for the loop)")

    return parser


_COMMANDS = {
    "report": _cmd_report,
    "mine": _cmd_mine,
    "distill": _cmd_distill,
    "list": _cmd_list,
    "show": _cmd_show,
    "approve": _cmd_approve,
    "reject": _cmd_reject,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
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
