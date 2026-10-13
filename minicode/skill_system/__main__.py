"""Module-level diagnostics CLI for the three-layer skill system.

Invoked as ``python -m minicode.skill_system <command>`` -- deliberately *not*
wired into ``pyproject.toml`` as a console script, so that the packaging
inventory stays untouched and this stays a pure diagnostic entry point.

The CLI is a thin, read-mostly shell over the already-tested modules of
``minicode.skill_system``: it discovers the registry once per command and then
renders whatever that module already renders.  Nothing here re-implements
scoring, parsing or persistence, and nothing here is a new source of truth.

Design rules that the command set follows:

* **Always readable.**  Every command catches its own failures, prints a single
  ``error: ...`` line to stderr and exits 1.  A diagnostic tool that leaks a
  traceback is useless exactly when it is needed most.
* **Exit codes carry meaning.**  ``0`` success, ``1`` a command-level failure
  (for example ``plan`` naming a workflow that does not exist), ``2`` a usage
  problem (no subcommand, unknown subcommand).
* **Writes are the user's explicit intent.**  Only ``match`` (hit counters),
  ``enable`` and ``disable`` (lifecycle state) touch ``~/.mini-code``; every
  other command is read-only.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Sequence

from minicode.skill_system import collector, lifecycle, stats
from minicode.skill_system.composite import (
    CompositeWorkflow,
    discover_workflows,
    format_plan,
    validate_against_registry,
)
from minicode.skill_system.registry import SkillKind, SkillRegistry

__all__ = ["main"]

_PROG = "python -m minicode.skill_system"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _common_options(*, suppress_default: bool) -> argparse.ArgumentParser:
    """Shared parent parser so ``--cwd`` works before *and* after a subcommand.

    The subparser copy must *not* carry the real default: since Python 3.7 a
    subparser parses into a fresh namespace and copies every value back onto the
    parent one, so a plain default here would silently overwrite a ``--cwd`` the
    user wrote *before* the subcommand.  ``SUPPRESS`` keeps the attribute out of
    that namespace unless the option was actually given.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--cwd",
        metavar="PATH",
        default=argparse.SUPPRESS if suppress_default else os.getcwd(),
        help="directory whose skill roots are scanned (default: the current directory)",
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser, subcommands included."""
    global_options = _common_options(suppress_default=False)
    sub_options = _common_options(suppress_default=True)
    parser = argparse.ArgumentParser(
        prog=_PROG,
        parents=[global_options],
        description="Diagnose the skill system: discovery, matching, state, statistics and composites.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    subparsers.add_parser("list", parents=[sub_options], help="list every discovered skill with its state")

    subparsers.add_parser("doctor", parents=[sub_options], help="report every diagnostic, registry and composite")

    match_parser = subparsers.add_parser(
        "match", parents=[sub_options], help="rank skills for a query and record the hits"
    )
    match_parser.add_argument("query", metavar="QUERY", help="natural-language query to match against skills")

    subparsers.add_parser("stats", parents=[sub_options], help="show the skill hit-rate report")

    plan_parser = subparsers.add_parser(
        "plan", parents=[sub_options], help="print the execution plan of a composite workflow"
    )
    plan_parser.add_argument("name", metavar="NAME", help="name of the composite skill (workflow) to plan")

    subparsers.add_parser("state", parents=[sub_options], help="show the persisted enable/disable state")

    enable_parser = subparsers.add_parser(
        "enable", parents=[sub_options], help="enable a skill and persist the decision"
    )
    enable_parser.add_argument("name", metavar="NAME", help="skill name to enable")

    disable_parser = subparsers.add_parser(
        "disable", parents=[sub_options], help="disable a skill and persist the decision"
    )
    disable_parser.add_argument("name", metavar="NAME", help="skill name to disable")

    trajectories_parser = subparsers.add_parser(
        "trajectories", parents=[sub_options], help="summarise the recorded turn trajectories"
    )
    trajectories_parser.add_argument(
        "--session", metavar="ID", default=None, help="only read the trajectory file of this session"
    )
    trajectories_parser.add_argument(
        "--limit", metavar="N", type=int, default=20, help="how many recent turns to consider (default: 20)"
    )

    examples_parser = subparsers.add_parser(
        "examples",
        parents=[sub_options],
        help="list the bundled example skills, or install them into the workspace",
    )
    examples_parser.add_argument(
        "--install",
        action="store_true",
        help="copy the bundled skills (with their workflow.json) into <cwd>/.mini-code/skills",
    )
    examples_parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help="restrict --install to these skill names (default: every bundled skill)",
    )

    return parser


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _registry(args: argparse.Namespace) -> SkillRegistry:
    """Discover the registry for the ``--cwd`` the user asked for."""
    cwd = getattr(args, "cwd", None) or os.getcwd()
    return SkillRegistry.discover(cwd)


def _find_workflow(workflows: Sequence[CompositeWorkflow], name: str) -> CompositeWorkflow | None:
    """Look a composite workflow up by its declared name (exact match wins)."""
    wanted = name.strip()
    for workflow in workflows:
        if workflow.name == wanted:
            return workflow
    for workflow in workflows:
        if workflow.name.strip().lower() == wanted.lower():
            return workflow
    return None


def _explain_missing_workflow(
    registry: SkillRegistry,
    workflows: Sequence[CompositeWorkflow],
    issues: Sequence[str],
    name: str,
) -> None:
    """Explain *why* ``plan <name>`` found nothing, on stderr.

    The one-line answer stays on stdout for scripting; the diagnosis that makes
    it actionable is supplementary and goes to stderr.
    """
    hints: list[str] = []
    spec = registry.get(name)
    if spec is not None and spec.kind is not SkillKind.COMPOSITE:
        hints.append(
            f"hint: {name!r} is discovered as kind={spec.kind.value}; "
            "only skills declared with kind: composite can be planned."
        )
    if workflows:
        hints.append("available composite workflows: " + ", ".join(sorted(item.name for item in workflows)))
    else:
        hints.append(
            "no composite workflow is currently discovered (a composite skill needs a "
            "workflow.json next to its SKILL.md)."
        )
    if issues:
        hints.append("composite workflow issues:")
        hints.extend(f"  - {issue}" for issue in issues)
    for hint in hints:
        print(hint, file=sys.stderr)


def _announce_state(name: str, enabled: bool) -> None:
    """Print the confirmation for ``enable`` / ``disable``, warning if unknown."""
    print(f"Enabled skill {name!r}." if enabled else f"Disabled skill {name!r}.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    state = lifecycle.load_state()
    enabled = {name: state.is_enabled(name) for name in registry.names()}
    print(registry.format_index(enabled=enabled))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    registry = _registry(args)
    print(registry.format_doctor())
    workflows, issues = discover_workflows(registry)
    print()
    if issues:
        print("Composite workflows:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        # This section lists problems.  Printing "none" when there were no problems read
        # as "there are no composites" -- which is how it was read, while `list` reported
        # five.  The count and the problems are stated separately.
        print(f"Composite workflows: {len(workflows)} discovered, no problems")
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    registry = _registry(args)
    query = args.query
    matches = registry.match(query)
    if not matches:
        print(f"No skill matched {query!r}.")
        return 0
    for match in matches:
        # Recording is what turns a diagnostic run into real demand signal, so
        # every surfaced skill is counted -- the report is what makes retirement
        # decisions possible later.
        stats.record_match(match.skill.name, query)
        print(f"{match.score} {match.skill.name}")
        print(f"    {'; '.join(match.reasons)}" if match.reasons else "    (no reasons recorded)")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    print(stats.format_report())
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    registry = _registry(args)
    # discover_workflows only walks SkillKind.COMPOSITE specs, so a plan can
    # never be produced for an atomic or domain-pack skill.
    workflows, issues = discover_workflows(registry)
    workflow = _find_workflow(workflows, args.name)
    if workflow is None:
        print(f"No composite workflow named {args.name!r}.")
        _explain_missing_workflow(registry, workflows, issues, args.name)
        return 1
    print(format_plan(workflow))
    warnings = validate_against_registry(workflow, registry)
    if warnings:
        print()
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    return 0


def _cmd_state(args: argparse.Namespace) -> int:
    registry = _registry(args)
    print(lifecycle.format_state(lifecycle.load_state(), registry))
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, True)


def _cmd_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, False)


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    """Persist an enable/disable decision, warning when the name is unknown.

    An unknown name is still recorded on purpose: the skill may simply not be
    discovered from this ``--cwd`` yet, and pre-recording the intent is more
    useful than refusing it.
    """
    registry = _registry(args)
    name = args.name
    known = registry.get(name) is not None
    lifecycle.set_enabled(name, enabled)
    _announce_state(name, enabled)
    if not known:
        print(f"Warning: no skill named {name!r} is currently discovered.")
    return 0


def _cmd_trajectories(args: argparse.Namespace) -> int:
    print(collector.format_summary(args.session, limit=args.limit))
    return 0


def _cmd_examples(args: argparse.Namespace) -> int:
    """List the bundled scenarios, or install them so a session can use them."""
    from minicode.skill_system import examples as bundled

    cwd = getattr(args, "cwd", None) or os.getcwd()
    installed: list[str] = []
    if args.install:
        installed = bundled.install_examples(cwd, scope="project", names=args.names or None)
    print(bundled.format_examples(installed))
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "list": _cmd_list,
    "doctor": _cmd_doctor,
    "match": _cmd_match,
    "stats": _cmd_stats,
    "plan": _cmd_plan,
    "state": _cmd_state,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "trajectories": _cmd_trajectories,
    "examples": _cmd_examples,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _soften_console_encoding() -> None:
    """Never let an unencodable character turn a report into a traceback.

    Skill names and descriptions are arbitrary UTF-8; a legacy Windows code page
    would otherwise raise ``UnicodeEncodeError`` halfway through a report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            continue


def main(argv: list[str] | None = None) -> int:
    """Run one diagnostic command; never raises and never prints a traceback."""
    _soften_console_encoding()
    parser = _build_parser()
    args = parser.parse_args(argv)

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return 2

    handler = _COMMANDS.get(command)
    if handler is None:  # defensive: unreachable while subcommands and handlers agree
        parser.print_help()
        return 2

    try:
        return handler(args)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI must always stay readable
        print(f"error: {str(exc).strip() or type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
