from __future__ import annotations

import json
from pathlib import Path

from Main.MinicodeFrontline.Src.Application.Entry.LocalCommandSurface import (
    SLASH_COMMANDS,
)
from minicode.config import (
    CLAUDE_SETTINGS_PATH,
    MINI_CODE_HISTORY_PATH,
    MINI_CODE_MCP_PATH,
    MINI_CODE_PERMISSIONS_PATH,
    MINI_CODE_SETTINGS_PATH,
    load_runtime_config,
    save_mini_code_settings,
)
from minicode.history import load_history_entries
from minicode.product_surfaces import (
    build_product_snapshot,
    extension_manifest_payload,
    resolve_extension_manifest,
    set_extension_enabled,
)
from minicode.session import (
    format_rewind_preview,
    format_session_checkpoints,
    format_session_inspect,
    format_session_list,
    format_session_replay,
    format_session_resume,
    get_latest_session,
    list_sessions,
    load_session,
    rewind_session,
    rewind_session_data,
)


def format_slash_commands() -> str:
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║  📚 Available Commands                                  ║",
        "╠══════════════════════════════════════════════════════════╣",
    ]
    
    command_groups = {
        "🔧 Core Commands": [
            ("/help", "Show this help message"),
            ("/exit", "Exit mini-code"),
            ("/clear", "Clear the current transcript view"),
            ("/collapse", "Collapse expanded tool output"),
            ("/history", "Show recent prompt history"),
        ],
        "🛠️ Tool Commands": [
            ("/tools", "List all available tools"),
            ("/skills", "List discovered SKILL.md workflows"),
            ("/mcp", "Show MCP servers and connection state"),
            ("/cmd", "Run development commands directly"),
        ],
        "📊 Status & Info": [
            ("/status", "Show application state summary"),
            ("/state", "Show detailed application state"),
            ("/model", "Show or change current model"),
            ("/user", "Show or manage user profile"),
            ("/cost", "Show API cost and usage report"),
            ("/context", "Show context window usage"),
            ("/cybernetics", "Show control-system status"),
            ("/tasks", "Show current task list"),
            ("/memory", "Show memory system status"),
            ("/config", "Show configuration diagnostics"),
            ("/debug", "Show scroll/terminal diagnostics"),
            ("/security", "Show security review status"),
        ],
        "✏️ File Operations": [
            ("/ls [path]", "List files in directory"),
            ("/grep <pattern>", "Search text in files"),
            ("/read <path>", "Read a file directly"),
            ("/write <path>", "Write content to file"),
            ("/edit <path>", "Edit file by exact replacement"),
            ("/patch <path>", "Apply multiple replacements in one go"),
            ("/modify <path>", "Replace file with reviewable diff"),
        ],
        "💾 Session Management": [
            ("/session", "Inspect current session state"),
            ("/session <id>", "Inspect saved session or latest"),
            ("/session-replay", "Replay active session timeline"),
            ("/session-replay <id>", "Replay saved session timeline"),
            ("/sessions", "List saved sessions for workspace"),
            ("/instructions", "Inspect active instruction layering"),
            ("/hooks", "Inspect hook telemetry and failures"),
            ("/delegation", "Inspect background task capacity"),
            ("/extensions", "Inspect local extension manifests"),
            ("/extension-inspect <name>", "Inspect one extension in detail"),
            ("/extension-enable <name>", "Enable a local extension"),
            ("/extension-disable <name>", "Disable a local extension"),
            ("/readiness", "Inspect provider/runtime readiness"),
            ("/checkpoints", "List active session checkpoints"),
            ("/checkpoints <id>", "List saved session checkpoints"),
            ("/rewind-preview [arg]", "Preview active session rewind plan"),
            ("/rewind [arg]", "Rewind active session file edits"),
            ("/session-rewind-preview <id> [arg]", "Preview saved session rewind plan"),
            ("/session-rewind <id> [arg]", "Rewind saved session file edits"),
            ("/transcript-save <path>", "Save transcript to text file"),
            ("/retry", "Retry the last prompt"),
            ("/permissions", "Show permission storage path"),
            ("/config-paths", "Show settings file paths"),
        ],
    }
    
    for group_name, commands in command_groups.items():
        lines.append(f"║  {group_name:<54}║")
        for cmd, desc in commands:
            cmd_display = f"    {cmd}"
            lines.append(f"║  {cmd_display:<20} {desc:<33} ║")
        lines.append("╠══════════════════════════════════════════════════════════╣")
    
    lines.extend([
        "║  💡 Tips:                                              ║",
        "║  - Use Tab to autocomplete commands                    ║",
        "║  - Prefix with / to access any command                 ║",
        "║  - Type naturally - I'll understand Chinese & English  ║",
        "╚══════════════════════════════════════════════════════════╝",
    ])
    
    return "\n".join(lines)


#: ``/skills`` subcommands contributed by the distillation loop.  Kept here rather
#: than in the skill package so the skill system does not have to know that a
#: learning loop exists; the completion table and the dispatcher's help text both
#: read the combined list, so they cannot disagree.
SKILLS_DISTILL_SUBCOMMANDS = ("drafts", "draft", "approve", "reject", "distill")


def _skills_subcommand_names() -> tuple[str, ...]:
    from minicode.skill_system import SKILLS_SUBCOMMANDS

    return (*SKILLS_SUBCOMMANDS, *SKILLS_DISTILL_SUBCOMMANDS)


def _subcommand_choices(command: str) -> tuple[str, ...]:
    """Subcommands declared by a command that takes arguments.

    One source of truth per command: the dispatchers render their usage lines from
    these tuples and the completion machinery reads them, so help and Tab cannot
    drift apart.
    """
    if command == "/skills":
        return _skills_subcommand_names()
    if command == "/security":
        from minicode.security_chain import SECURITY_SUBCOMMANDS

        return SECURITY_SUBCOMMANDS
    return ()


def _subcommand_completions(user_input: str) -> list[str]:
    """Complete ``/cmd <sub>`` for commands that declare subcommands.

    Two shapes, because the two situations differ:

    * bare ``/security`` -- the user has the command and wants to know what it
      accepts, so the command itself is listed first (Enter still runs it) and its
      subcommands follow.  Requiring a trailing space to reveal them was a
      discoverability bug: nobody guesses that.
    * ``/security m`` -- the user is typing an argument, so only matching
      subcommands are offered.
    """
    head, separator, rest = user_input.partition(" ")
    head = head.strip().lower()
    choices = _subcommand_choices(head)
    if not choices:
        return []
    if not separator:
        return [head, *(f"{head} {name}" for name in choices)]
    typed = rest.strip().lower()
    return [f"{head} {name}" for name in choices if name.startswith(typed)]


def find_matching_slash_commands(user_input: str) -> list[str]:
    """Find slash commands matching user input.

    Two rules matter here.  A command that declares subcommands completes those
    (``/skills d`` -> ``/skills disable``, ``/skills doctor``).  And once the input
    contains a space the user is typing an *argument*, where subsequence matching
    is meaningless: ``/skills doctor`` used to fuzzy-match ``/rewind [latest|...]``
    simply because those characters appear in order, filling the autocomplete
    dropdown with nonsense and making the subcommands look unsupported.

    Returns an empty list when nothing matches, so callers (autocomplete dropdown,
    unknown-command hint) can tell "no match" apart from "all".
    """
    subcommands = _subcommand_completions(user_input)
    if subcommands:
        return subcommands
    commands = [c.usage for c in SLASH_COMMANDS]
    prefix_matches = [c for c in commands if c.startswith(user_input)]
    if prefix_matches:
        return prefix_matches
    if " " in user_input:
        # Argument context: never fuzzy-match, and offer no dropdown.
        return []
    # Fuzzy fallback: subsequence match (e.g., "mem" matches "/memory")
    lower = user_input.lower()
    fuzzy = [c for c in commands if all(ch in c.lower() for ch in lower)]
    return fuzzy


def complete_slash_command(line: str) -> tuple[list[str], str]:
    subcommands = _subcommand_completions(line)
    if subcommands:
        return (subcommands, line)
    if " " in line:
        return ([], line)
    commands = [c.usage for c in SLASH_COMMANDS]
    hits = [c for c in commands if c.startswith(line)]
    if not hits and line:
        lower = line.lower()
        hits = [c for c in commands if all(ch in c.lower() for ch in lower)]
    return (hits if hits else commands, line)


def format_prompt_history(entries, limit: int = 20) -> str:
    """Format recent prompt-history entries with 1-based numbering."""
    history = [str(entry) for entry in (entries or [])]
    if not history:
        return (
            "No prompt history yet.\n"
            f"History is stored at {MINI_CODE_HISTORY_PATH} and written as you submit prompts."
        )
    start = max(0, len(history) - limit)
    lines = [f"Prompt history (last {len(history) - start} of {len(history)}):", ""]
    lines.extend(
        f"{start + index + 1}. {entry}"
        for index, entry in enumerate(history[start:])
    )
    return "\n".join(lines)


def format_cost_report(state=None, runtime=None, detailed: bool = False) -> str:
    """Format the live session cost/usage report.

    Cost and token counters live on ``AppState``: the model adapters push
    ``add_cost`` / ``update_context_usage`` into the store they are handed, so
    the store snapshot is the authoritative local source.  A ``CostTracker``
    instance is deliberately *not* used here -- the TUI constructs one but
    nothing ever feeds it, so its report would read zero no matter how much
    was actually spent.
    """
    from minicode.cost_tracker import MODEL_PRICING

    runtime_model = str((runtime or {}).get("model") or "")
    session_model = str(getattr(state, "model", "") or "")
    # Price the model the runtime will actually call next; the store only
    # records what the session started with, which goes stale after /model.
    model = runtime_model or session_model or "unknown"

    total_cost = float(getattr(state, "total_cost_usd", 0.0) or 0.0)
    api_calls = int(getattr(state, "api_calls", 0) or 0)
    api_errors = int(getattr(state, "api_errors", 0) or 0)
    tokens = int(getattr(state, "token_usage", 0) or 0)
    window = int(getattr(state, "context_window_size", 0) or 0)
    usage_percentage = float(getattr(state, "context_usage_percentage", 0.0) or 0.0)
    session_id = str(getattr(state, "session_id", "") or "")

    lines = [
        "Cost & Usage Report",
        "=" * 60,
        "",
        "Session:",
        f"  Session: {session_id[:8] if session_id else 'new'}",
        f"  Model: {model}",
    ]
    if session_model and runtime_model and session_model != runtime_model:
        lines.append(f"  Session started with: {session_model}")
    lines.extend(
        [
            "",
            "Usage:",
            f"  Total cost: ${total_cost:.4f}",
            f"  API calls: {api_calls} ({api_errors} error(s))",
        ]
    )
    if window:
        lines.append(f"  Context tokens: {tokens:,} / {window:,} ({usage_percentage:.1f}%)")
    else:
        lines.append(f"  Context tokens: {tokens:,}")

    lines.extend(["", "Pricing (per 1M tokens):"])
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        fallback = MODEL_PRICING.get("default", {})
        lines.append(f"  {model}: no explicit entry in MODEL_PRICING")
        lines.append(
            "  fallback rate in use: "
            f"${float(fallback.get('input', 0.0)):.2f} in / "
            f"${float(fallback.get('output', 0.0)):.2f} out"
        )
        lines.append(
            "  Register the model id in MODEL_PRICING (minicode/cost_tracker.py) so the "
            "estimate stops using the fallback rate."
        )
    else:
        lines.append(
            f"  {model}: ${float(pricing.get('input', 0.0)):.2f} in / "
            f"${float(pricing.get('output', 0.0)):.2f} out"
        )
        lines.append(
            f"  cache: ${float(pricing.get('cache_read', 0.0)):.3f} read / "
            f"${float(pricing.get('cache_write', 0.0)):.3f} write"
        )

    if detailed:
        lines.extend(["", "Registered model prices:", "-" * 60])
        for name in sorted(MODEL_PRICING):
            entry = MODEL_PRICING[name]
            lines.append(
                f"  {name}: ${float(entry.get('input', 0.0)):.3f} in / "
                f"${float(entry.get('output', 0.0)):.3f} out"
            )
        lines.extend(
            [
                "",
                "Note: the deepseek-flash / deepseek-v4-flash rates are approximations",
                "until official DeepSeek pricing is published; recalibrate in cost_tracker.",
            ]
        )

    lines.extend(
        [
            "",
            "-" * 60,
            "Estimated locally from token counts; not provider billing data.",
        ]
    )
    return "\n".join(lines)


def format_task_report(
    delegated_tasks=None,
    delegation_status=None,
    background_tasks=None,
    background_slots=None,
    task_graphs=None,
) -> str:
    """Format the current task list across the three task surfaces."""
    delegated = list(delegated_tasks or [])
    shell_tasks = list(background_tasks or [])
    graphs = list(task_graphs or [])
    delegation = dict(delegation_status or {})
    slots = dict(background_slots or {})

    lines = ["Task List", "=" * 60, ""]
    lines.append(f"Delegated agents: {len(delegated)} tracked")
    if delegation:
        lines.append(
            f"  running {delegation.get('running_tasks', 0)} | "
            f"slots {delegation.get('available_slots', 0)}/{delegation.get('max_slots', 0)} free"
        )
    if delegated:
        for task in delegated[:10]:
            label = str(
                task.get("label") or task.get("command") or task.get("taskId") or "task"
            )
            lines.append(f"  - {label} [{task.get('status', 'unknown')}]")
        if len(delegated) > 10:
            lines.append(f"  ... {len(delegated) - 10} more")
    else:
        lines.append("  (none) - /delegation reports capacity in detail")

    lines.append("")
    if slots:
        lines.append(
            f"Background shell tasks: {len(shell_tasks)} tracked | "
            f"slots {slots.get('used_slots', 0)}/{slots.get('max_slots', 0)} used"
        )
    else:
        lines.append(f"Background shell tasks: {len(shell_tasks)} tracked")
    if shell_tasks:
        for task in shell_tasks[:10]:
            label = str(task.get("command") or task.get("label") or "command")
            lines.append(
                f"  - [{task.get('taskId', '?')}] {label} [{task.get('status', 'unknown')}]"
            )
        if len(shell_tasks) > 10:
            lines.append(f"  ... {len(shell_tasks) - 10} more")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Task graphs: {len(graphs)} saved")
    if graphs:
        for graph in graphs:
            state_text = "complete" if graph.get("complete") else "in progress"
            lines.append(
                f"  - {graph.get('id', '?')} [{state_text}] "
                f"{float(graph.get('progress', 0.0) or 0.0):.0f}% "
                f"({graph.get('completed', 0)}/{graph.get('tasks', 0)} tasks)"
            )
    else:
        lines.append("  (none)")

    if not delegated and not shell_tasks and not graphs:
        lines.extend(
            [
                "",
                "No tasks are tracked right now. Background commands and delegated",
                "work appear here once they start.",
            ]
        )
    return "\n".join(lines)


def _handle_security_command(user_input: str, *, cwd: str | None = None) -> str:
    """The ``/security`` surface: what the chain decided, and how it is configured.

    Everything here is local and free -- this is the command someone runs when
    they want to know *why* something was blocked, so it must never itself need a
    model call.  The ``eval`` subcommand runs the heuristic lane only, for the
    same reason.
    """
    from minicode.security_chain import SECURITY_SUBCOMMANDS
    from minicode.security_chain import audit as security_audit
    from minicode.security_chain.gate import get_gate
    from minicode.security_chain.taint import get_registry
    from minicode.security_chain.types import SecurityMode

    argument = user_input[len("/security") :].strip()
    command, _, rest = argument.partition(" ")
    command = command.strip().lower()
    rest = rest.strip()
    gate = get_gate()
    entries = security_audit.load()
    stats = security_audit.AuditStats.from_entries(entries)

    if not command:
        return "\n".join(
            [
                gate.format_status(),
                "",
                f"Audit trail: {security_audit.audit_path()}",
                f"Recorded decisions: {stats.total} "
                f"(denied {stats.denies}, escalated {stats.escalations}, allowed {stats.allows})",
                f"Untrusted sources tracked: {len(get_registry())}",
                "",
                "Subcommands: " + ", ".join(SECURITY_SUBCOMMANDS),
            ]
        )

    if command == "audit":
        return security_audit.format_report()

    if command == "stats":
        import json

        return json.dumps(gate.stats(), ensure_ascii=False, indent=2)

    if command == "taint":
        registry = get_registry()
        if not len(registry):
            return "No untrusted content recorded yet. Taint is marked as tools return data."
        lines = [f"Untrusted sources ({len(registry)}):"]
        for entry in registry.snapshot():
            lines.append(
                f"  - {entry['source']}:{entry['toolName'] or 'unknown'} "
                f"({entry['chars']} chars, {len(entry['indicators'])} indicator(s), "
                f"{entry['fragments']} fragment(s))"
            )
        lines.append("")
        lines.append("A side-effecting action whose arguments contain one of these is escalated.")
        return "\n".join(lines)

    if command == "mode":
        if not rest:
            return f"security mode: {gate.mode.value}"
        try:
            new_mode = SecurityMode(rest.lower())
        except ValueError:
            return "Usage: /security mode <off|heuristic|standard|strict>"
        gate.set_mode(new_mode)
        note = ""
        if new_mode in {SecurityMode.STANDARD, SecurityMode.STRICT}:
            note = "\nThe model channel is consulted from now on (adds latency per review)."
        return f"security mode set to {new_mode.value} for this session.{note}"

    if command == "eval":
        from minicode.security_chain.eval import format_report, run_eval

        report = run_eval(mode=SecurityMode.HEURISTIC, use_model=False)
        return format_report(report, show_misses=3, show_fps=3)

    return (
        f"Unknown /security subcommand '{command}'.\n"
        "Available: " + ", ".join(SECURITY_SUBCOMMANDS)
    )


def _handle_skills_command(user_input: str, *, cwd: str | None = None, tools=None) -> str:
    """Dispatch the ``/skills`` surface across the three capability layers.

    Bare ``/skills`` is the L1 index; the subcommands expose lifecycle, matching,
    statistics, L2 plans and L3 packs, so the whole system is reachable from one
    command instead of five new slash commands (each of which would have had to be
    registered, documented and dispatched).

    Discovery is exception-guarded: a hostile skill directory must produce a
    readable message, never a traceback inside the prompt loop.
    """
    from minicode.skill_system import SKILLS_SUBCOMMANDS
    from minicode.skill_system import examples as skill_examples
    from minicode.skill_system import lifecycle as skill_lifecycle
    from minicode.skill_system import stats as skill_stats
    from minicode.skill_system.composite import (
        discover_workflows,
        format_plan,
        validate_against_registry,
    )
    from minicode.skill_system.domain_pack import discover_packs
    from minicode.skill_system.domain_pack import format_index as format_pack_index
    from minicode.skill_system.domain_pack import format_pack
    from minicode.skill_system.registry import SkillRegistry

    argument = user_input[len("/skills") :].strip()
    workspace = cwd or str(Path.cwd())
    known_tools = None
    if tools is not None:
        try:
            known_tools = {getattr(tool, "name", str(tool)) for tool in tools.list()}
        except Exception:  # noqa: BLE001 - discovery must survive a broken registry
            known_tools = None

    try:
        registry = SkillRegistry.discover(workspace, known_tools=known_tools)
    except Exception as error:  # noqa: BLE001
        return f"skills: discovery failed: {error}"

    command, _, rest = argument.partition(" ")
    command = command.strip().lower()
    rest = rest.strip()

    if not command:
        state = skill_lifecycle.load_state()
        enabled = {name: state.is_enabled(name) for name in registry.names()}
        # Advertise the subcommands here: the index is the first thing anyone
        # types, and nothing else in the UI hints that /skills takes arguments.
        return "\n".join(
            [
                registry.format_index(enabled=enabled),
                "",
                "Subcommands: " + ", ".join(_skills_subcommand_names()),
                "Try /skills doctor for diagnostics, /skills plan <name> for a composite plan,",
                "or /skills drafts to review skills distilled from your own sessions.",
            ]
        )

    if command in {"doctor", "diagnose"}:
        workflows, workflow_issues = discover_workflows(registry)
        lines = [registry.format_doctor(), "", "Composite workflows:"]
        if workflow_issues:
            lines.extend(f"  - {issue}" for issue in workflow_issues)
        elif workflows:
            lines.extend(
                f"  - {workflow.name}: {len(workflow.steps)} step(s)" for workflow in workflows
            )
        else:
            lines.append("  (none discovered)")
        return "\n".join(lines)

    if command in {"show", "inspect"}:
        if not rest:
            return "Usage: /skills show <name>"
        return registry.format_detail(rest)

    if command in {"match", "find"}:
        if not rest:
            return "Usage: /skills match <query>"
        matches = registry.match(rest)
        if not matches:
            return f"No skill matched '{rest}'."
        lines: list[str] = []
        for match in matches:
            lines.append(f"{match.score:>3}  {match.skill.name}  [{match.skill.kind.value}]")
            lines.extend(f"       {reason}" for reason in match.reasons)
            skill_stats.record_match(match.skill.name, rest)
        return "\n".join(lines)

    if command == "stats":
        return skill_stats.format_report()

    if command in {"state", "lifecycle"}:
        return skill_lifecycle.format_state(skill_lifecycle.load_state(), registry)

    if command in {"enable", "disable"}:
        if not rest:
            return f"Usage: /skills {command} <name>"
        skill_lifecycle.set_enabled(rest, command == "enable")
        verb = "Enabled" if command == "enable" else "Disabled"
        warning = (
            ""
            if registry.get(rest)
            else f"\nWarning: no skill named '{rest}' is currently discovered."
        )
        return f"{verb} skill '{rest}'.{warning}"

    if command in {"workflows", "plans"}:
        workflows, workflow_issues = discover_workflows(registry)
        lines = [f"Composite workflows ({len(workflows)}):"]
        if not workflows:
            lines.append("  (none discovered)")
        for workflow in workflows:
            lines.append(f"  - {workflow.name}: {len(workflow.steps)} step(s)")
            lines.extend(f"      {warning}" for warning in validate_against_registry(workflow, registry))
        lines.extend(f"  ! {issue}" for issue in workflow_issues)
        return "\n".join(lines)

    if command == "plan":
        if not rest:
            return "Usage: /skills plan <workflow-name>"
        workflows, workflow_issues = discover_workflows(registry)
        workflow = next((item for item in workflows if item.name == rest), None)
        if workflow is None:
            detail = "\n".join(f"  - {issue}" for issue in workflow_issues)
            return f"No composite workflow named '{rest}'." + (f"\n{detail}" if detail else "")
        lines = [format_plan(workflow)]
        warnings = validate_against_registry(workflow, registry)
        if warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in warnings)
        return "\n".join(lines)

    if command == "packs":
        try:
            packs = discover_packs(workspace)
        except Exception as error:  # noqa: BLE001
            return f"skills: domain pack discovery failed: {error}"
        if not rest:
            return format_pack_index(packs)
        match = next((pack for pack in packs if pack.name == rest), None)
        return format_pack(match) if match is not None else f"Unknown domain pack: {rest}"

    if command in {"examples", "install-examples"}:
        installed = skill_examples.install_examples(workspace, scope="project") if command == "install-examples" else []
        return skill_examples.format_examples(installed)

    if command in {"drafts", "draft", "approve", "reject", "distill"}:
        return _handle_distill_subcommand(command, rest, workspace=workspace)

    return (
        f"Unknown /skills subcommand '{command}'.\n"
        "Available: " + ", ".join(_skills_subcommand_names())
    )


def _handle_distill_subcommand(command: str, rest: str, *, workspace: str) -> str:
    """The self-learning loop, reachable from the skill surface it feeds.

    Drafts are skills, so they live under ``/skills`` rather than behind a new slash
    command: one surface, one registry entry, and the review queue sits next to the
    thing it will become.
    """
    from minicode.distillation.draft import DraftStore
    from minicode.distillation.distiller import Distiller
    from minicode.distillation.miner import mine
    from minicode.distillation.report import (
        format_candidates,
        format_draft_detail,
        format_drafts,
    )
    from minicode.distillation.review import approve_draft, reject_draft
    from minicode.distillation.signals import load_trajectory_refs

    store = DraftStore()

    if command == "drafts":
        return format_drafts(store.list())

    if command == "draft":
        if not rest:
            return "Usage: /skills draft <draft-id>"
        draft = store.get(rest)
        return format_draft_detail(draft) if draft is not None else f"No draft with id '{rest}'."

    if command == "approve":
        if not rest:
            return "Usage: /skills approve <draft-id>"
        return approve_draft(store, rest, cwd=workspace).message

    if command == "reject":
        draft_id, _, reason = rest.partition(" ")
        if not draft_id.strip():
            return "Usage: /skills reject <draft-id> [reason]"
        return reject_draft(store, draft_id.strip(), reason.strip()).message

    # distill
    refs = load_trajectory_refs()
    candidates, rejected = mine(refs)
    if not candidates:
        return format_candidates(candidates, rejected, trajectory_count=len(refs))

    report = Distiller().distill(candidates, store=store)
    lines = [
        f"Distilled {report.produced} draft(s) from {len(candidates)} candidate(s): "
        f"{report.model_calls} model call(s), ${report.spent_usd:.4f}, {report.elapsed_s:.1f}s"
    ]
    if report.budget_exhausted:
        lines.append("Stopped early: the distillation budget for this run was reached.")
    if report.skipped:
        lines.append("")
        lines.append("Skipped:")
        lines.extend(f"  - {signature[:60]!r}: {reason}" for signature, reason in report.skipped[:5])
    if report.failures:
        lines.append("")
        lines.append("Failed:")
        lines.extend(f"  - {signature[:60]!r}: {reason}" for signature, reason in report.failures[:5])
    if report.drafts:
        lines.extend(["", format_drafts(report.drafts)])
    return "\n".join(lines)


def try_handle_local_command(
    user_input: str,
    tools=None,
    cwd: str | None = None,
    session=None,
    app_state=None,
) -> str | None:
    def _product_snapshot() -> dict:
        if session is not None:
            instruction_layers = list(getattr(session, "instruction_layers", []) or [])
            hook_status = dict(getattr(session, "hook_status", {}) or {})
            delegated_tasks = list(getattr(session, "delegated_tasks", []) or [])
            delegation_status = dict(getattr(session, "delegation_status", {}) or {})
            extension_manifests = list(getattr(session, "extension_manifests", []) or [])
            readiness_report = dict(getattr(session, "readiness_report", {}) or {})
            if any(
                [
                    instruction_layers,
                    hook_status,
                    delegated_tasks,
                    delegation_status,
                    extension_manifests,
                    readiness_report,
                ]
            ):
                metadata = getattr(session, "metadata", None)
                return {
                    "instruction_layers": instruction_layers,
                    "instruction_summary": getattr(metadata, "instruction_summary", ""),
                    "hook_status": hook_status,
                    "hook_summary": getattr(metadata, "hook_summary", ""),
                    "delegated_tasks": delegated_tasks,
                    "delegation_status": delegation_status,
                    "delegation_summary": getattr(metadata, "delegation_summary", ""),
                    "extension_manifests": extension_manifests,
                    "extension_summary": getattr(metadata, "extension_summary", ""),
                    "readiness_report": readiness_report,
                    "readiness_summary": getattr(metadata, "readiness_summary", ""),
                }
        if cwd is None:
            return {}
        return build_product_snapshot(cwd)

    def _format_instruction_surface(snapshot: dict) -> str:
        layers = list(snapshot.get("instruction_layers", []) or [])
        lines = [
            "Instruction surface:",
            snapshot.get("instruction_summary", "instructions: unavailable"),
        ]
        if not layers:
            lines.append("No instruction layers discovered for this workspace.")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"Layers ({len(layers)}):")
        for layer in layers:
            scope = str(layer.get("scope") or "unknown")
            kind = str(layer.get("kind") or "unknown")
            exists = "active" if layer.get("exists") else "missing"
            path = str(layer.get("path") or "")
            preview = str(layer.get("preview") or "")
            detail = f"- {scope}/{kind}: {exists}"
            if path:
                detail += f" [{path}]"
            lines.append(detail)
            if preview:
                lines.append(f"  preview: {preview}")
        return "\n".join(lines)

    def _format_hook_surface(snapshot: dict) -> str:
        status = dict(snapshot.get("hook_status", {}) or {})
        lines = [
            "Hook surface:",
            snapshot.get("hook_summary", "hooks: unavailable"),
        ]
        if not status:
            lines.append("No hook telemetry is available.")
            return "\n".join(lines)
        lines.extend(
            [
                "",
                f"Registered hooks: {status.get('enabled_hooks', 0)}/{status.get('total_hooks', 0)} enabled",
                f"Calls: {status.get('total_calls', 0)}",
                f"Duration: {status.get('total_duration_ms', 0)}ms",
            ]
        )
        failure_count = status.get("failure_count")
        last_status = status.get("last_status")
        last_error = status.get("last_error")
        if failure_count is not None:
            lines.append(f"Failures: {failure_count}")
        if last_status:
            lines.append(f"Last status: {last_status}")
        if last_error:
            lines.append(f"Last error: {last_error}")
        return "\n".join(lines)

    def _format_delegation_surface(snapshot: dict) -> str:
        status = dict(snapshot.get("delegation_status", {}) or {})
        tasks = list(snapshot.get("delegated_tasks", []) or [])
        lines = [
            "Delegation surface:",
            snapshot.get("delegation_summary", "delegation: unavailable"),
        ]
        if not status and not tasks:
            lines.append("No delegation state is available.")
            return "\n".join(lines)
        if status:
            lines.extend(
                [
                    "",
                    f"Running tasks: {status.get('running_tasks', 0)}",
                    f"Tracked tasks: {status.get('total_tracked', 0)}",
                    f"Slots: {status.get('available_slots', 0)}/{status.get('max_slots', 0)} free",
                ]
            )
            labels = list(status.get("active_labels", []) or [])
            if labels:
                lines.append(f"Active labels: {', '.join(str(label) for label in labels)}")
        if tasks:
            lines.append("")
            lines.append(f"Tracked task details ({min(len(tasks), 5)} shown):")
            for task in tasks[:5]:
                label = str(task.get("label") or task.get("command") or task.get("taskId") or "task")
                task_status = str(task.get("status") or "unknown")
                lines.append(f"- {label} [{task_status}]")
        return "\n".join(lines)

    def _format_extension_surface(snapshot: dict) -> str:
        manifests = list(snapshot.get("extension_manifests", []) or [])
        lines = [
            "Extension surface:",
            snapshot.get("extension_summary", "extensions: unavailable"),
        ]
        if not manifests:
            lines.append("No extension manifests were discovered.")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"Extensions ({len(manifests)}):")
        for manifest in manifests:
            name = str(manifest.get("name") or "extension")
            scope = str(manifest.get("scope") or "unknown")
            enabled = "enabled" if manifest.get("enabled", True) else "disabled"
            version = str(manifest.get("version") or "").strip()
            detail = f"- {name} [{scope}, {enabled}]"
            if version:
                detail += f" v{version}"
            lines.append(detail)
            description = str(manifest.get("description") or "").strip()
            entrypoint = str(manifest.get("entrypoint") or "").strip()
            if description:
                lines.append(f"  {description}")
            if entrypoint:
                lines.append(f"  entrypoint: {entrypoint}")
        return "\n".join(lines)

    def _format_readiness_surface(snapshot: dict) -> str:
        report = dict(snapshot.get("readiness_report", {}) or {})
        lines = [
            "Readiness surface:",
            snapshot.get("readiness_summary", "readiness: unavailable"),
        ]
        if not report:
            lines.append("No readiness report is available.")
            return "\n".join(lines)
        status = str(report.get("status") or "unknown")
        provider = str(report.get("provider") or "unknown")
        provider_ready = bool(report.get("provider_ready"))
        fallback_ready = bool(report.get("fallback_ready"))
        fallback_candidates = [
            str(candidate)
            for candidate in list(report.get("fallback_candidates", []) or [])
            if str(candidate).strip()
        ]
        viable_fallbacks = [
            str(candidate)
            for candidate in list(report.get("viable_fallbacks", []) or [])
            if str(candidate).strip()
        ]
        lines.extend(
            [
                "",
                f"Status: {status}",
                f"Provider: {provider}",
                f"Provider ready: {'yes' if provider_ready else 'no'}",
                f"Channel: {str(report.get('provider_channel') or 'unknown')}",
                f"Fallback ready: {'yes' if fallback_ready else 'no'}",
                f"Risk scope: {str(report.get('risk_scope') or 'unknown')}",
            ]
        )
        if fallback_candidates:
            lines.append(
                f"Configured fallbacks ({len(viable_fallbacks)}/{len(fallback_candidates)} locally ready):"
            )
            for candidate in fallback_candidates:
                label = "ready" if candidate in viable_fallbacks else "not-ready"
                lines.append(f"- {candidate} [{label}]")
        issues = [str(issue) for issue in list(report.get("issues", []) or []) if str(issue).strip()]
        if issues:
            lines.append("Issues:")
            lines.extend(f"- {issue}" for issue in issues)
        guidance = [
            str(item)
            for item in list(report.get("fallback_guidance", []) or [])
            if str(item).strip()
        ]
        if guidance:
            lines.append("Guidance:")
            lines.extend(f"- {item}" for item in guidance)
        preflight_checks = [
            dict(item)
            for item in list(report.get("preflight_checks", []) or [])
            if isinstance(item, dict)
        ]
        if preflight_checks:
            lines.append("Local preflight:")
            for check in preflight_checks:
                label = str(check.get("label") or "check").strip()
                status_text = str(check.get("status") or "unknown").strip()
                summary = str(check.get("summary") or "").strip()
                action = str(check.get("action") or "").strip()
                detail = f"- {label}: {status_text}"
                if summary:
                    detail += f" - {summary}"
                lines.append(detail)
                if action:
                    lines.append(f"  Action: {action}")
        next_actions = [
            str(item)
            for item in list(report.get("next_actions", []) or [])
            if str(item).strip()
        ]
        if next_actions:
            lines.append("Next actions:")
            lines.extend(f"- {item}" for item in next_actions)
        repair_plan = [
            dict(item)
            for item in list(report.get("repair_plan", []) or [])
            if isinstance(item, dict)
        ]
        if repair_plan:
            lines.append("Repair plan:")
            for item in repair_plan:
                step = str(item.get("step") or "step").strip()
                status_text = str(item.get("status") or "unknown").strip()
                action = str(item.get("action") or "").strip()
                command = str(item.get("command") or "").strip()
                detail = f"- {step}: {status_text}"
                if action:
                    detail += f" - {action}"
                lines.append(detail)
                if command:
                    lines.append(f"  Command: {command}")
        config_examples = [
            dict(item)
            for item in list(report.get("fallback_config_examples", []) or [])
            if isinstance(item, dict)
        ]
        if config_examples:
            lines.append("Config examples:")
            for item in config_examples:
                label = str(item.get("label") or "fallback config").strip()
                path = str(item.get("path") or "").strip()
                settings = item.get("settings", {})
                rendered_settings = json.dumps(settings, ensure_ascii=False, sort_keys=True)
                location = f" [{path}]" if path else ""
                lines.append(f"- {label}{location}: {rendered_settings}")
        return "\n".join(lines)

    def _format_extension_manifest_detail(identifier: str) -> str:
        if cwd is None:
            return "No workspace is available for extension inspection."
        try:
            manifest = resolve_extension_manifest(cwd, identifier)
            payload = extension_manifest_payload(manifest)
        except ValueError as exc:
            return str(exc)
        lines = [
            f"Extension inspect: {manifest.name}",
            f"Scope: {manifest.scope}",
            f"Enabled: {'yes' if manifest.enabled else 'no'}",
            f"Manifest: {manifest.path}",
        ]
        if manifest.version:
            lines.append(f"Version: {manifest.version}")
        if manifest.description:
            lines.append(f"Description: {manifest.description}")
        if manifest.entrypoint:
            entrypoint = Path(manifest.path).parent / manifest.entrypoint
            exists = "yes" if entrypoint.exists() else "no"
            lines.append(f"Entrypoint: {manifest.entrypoint}")
            lines.append(f"Entrypoint path: {entrypoint}")
            lines.append(f"Entrypoint exists: {exists}")
        extra_keys = sorted(
            key for key in payload.keys()
            if key not in {"name", "version", "description", "enabled", "entrypoint"}
        )
        if extra_keys:
            lines.append("Extra manifest keys:")
            lines.extend(f"- {key}" for key in extra_keys)
        return "\n".join(lines)

    def _set_extension_state(identifier: str, enabled: bool) -> str:
        if cwd is None:
            return "No workspace is available for extension changes."
        try:
            manifest = set_extension_enabled(cwd, identifier, enabled)
        except ValueError as exc:
            return str(exc)
        status = "enabled" if enabled else "disabled"
        return (
            f"Extension {manifest.scope}:{manifest.name} is now {status}.\n\n"
            f"{_format_extension_manifest_detail(f'{manifest.scope}:{manifest.name}')}"
        )

    def _format_rewind_result(target_session, restored, prefix: str) -> str:
        restored_preview = ", ".join(
            f"[{item.checkpoint_id[:8]}] {Path(item.file_path).name or item.file_path}"
            for item in restored
        )
        return (
            f"{prefix} {len(restored)} checkpoint(s) for session {target_session.session_id[:8]}.\n"
            f"Restored: {restored_preview}\n\n"
            f"{format_session_resume(target_session)}"
        )

    def _workspace_session(target: str):
        workspace = str(Path(cwd).resolve()) if cwd else None
        return (
            get_latest_session(workspace=workspace)
            if target == "latest"
            else load_session(target)
        )

    if user_input in {"/", "/help"}:
        return format_slash_commands()

    if user_input == "/config-paths":
        return "\n".join(
            [
                f"mini-code settings: {MINI_CODE_SETTINGS_PATH}",
                f"mini-code permissions: {MINI_CODE_PERMISSIONS_PATH}",
                f"mini-code mcp: {MINI_CODE_MCP_PATH}",
                f"compat fallback: {CLAUDE_SETTINGS_PATH}",
            ]
        )

    if user_input == "/permissions":
        return f"permission store: {MINI_CODE_PERMISSIONS_PATH}"

    if user_input == "/sessions":
        workspace = str(Path(cwd).resolve()) if cwd else None
        sessions = list_sessions()
        if workspace is not None:
            sessions = [meta for meta in sessions if meta.workspace == workspace]
        return format_session_list(sessions)

    if user_input == "/instructions":
        return _format_instruction_surface(_product_snapshot())

    if user_input == "/hooks":
        return _format_hook_surface(_product_snapshot())

    if user_input == "/delegation":
        return _format_delegation_surface(_product_snapshot())

    if user_input == "/cost" or user_input.startswith("/cost "):
        flag = user_input[len("/cost") :].strip().lower()
        detailed = flag in {"--detailed", "-d", "detailed"}
        if flag and not detailed:
            return "Usage: /cost [--detailed]"
        store = app_state
        if store is None:
            from minicode.state import get_global_store

            store = get_global_store()
        try:
            snapshot = store.get_state()
        except Exception:  # noqa: BLE001 - a broken store must not kill the prompt loop
            snapshot = None
        try:
            runtime = load_runtime_config()
        except Exception:  # noqa: BLE001
            runtime = None
        return format_cost_report(snapshot, runtime=runtime, detailed=detailed)

    if user_input == "/tasks":
        snapshot = _product_snapshot()
        try:
            from minicode.background_tasks import get_slot_stats, list_background_tasks

            shell_tasks = list(list_background_tasks())
            shell_slots = dict(get_slot_stats())
        except Exception:  # noqa: BLE001
            shell_tasks, shell_slots = [], {}
        graphs = []
        try:
            from minicode.task_graph import list_task_graphs, load_task_graph

            for graph_id in list_task_graphs()[:10]:
                graph = load_task_graph(graph_id)
                if graph is None:
                    continue
                definitions = getattr(graph, "definitions", {}) or {}
                slots = getattr(graph, "slots", {}) or {}
                completed = sum(
                    1
                    for slot in slots.values()
                    if getattr(getattr(slot, "state", None), "value", "") == "completed"
                )
                graphs.append(
                    {
                        "id": graph_id,
                        "progress": graph.get_progress_percentage(),
                        "complete": graph.is_graph_complete(),
                        "tasks": len(definitions),
                        "completed": completed,
                    }
                )
        except Exception:  # noqa: BLE001
            graphs = []
        return format_task_report(
            delegated_tasks=snapshot.get("delegated_tasks", []),
            delegation_status=snapshot.get("delegation_status", {}),
            background_tasks=shell_tasks,
            background_slots=shell_slots,
            task_graphs=graphs,
        )

    if user_input == "/history":
        return format_prompt_history(load_history_entries())

    if user_input == "/extensions":
        return _format_extension_surface(_product_snapshot())

    if user_input.startswith("/extension-inspect "):
        identifier = user_input[len("/extension-inspect ") :].strip()
        if not identifier:
            return "Usage: /extension-inspect <name>"
        return _format_extension_manifest_detail(identifier)

    if user_input.startswith("/extension-enable "):
        identifier = user_input[len("/extension-enable ") :].strip()
        if not identifier:
            return "Usage: /extension-enable <name>"
        return _set_extension_state(identifier, True)

    if user_input.startswith("/extension-disable "):
        identifier = user_input[len("/extension-disable ") :].strip()
        if not identifier:
            return "Usage: /extension-disable <name>"
        return _set_extension_state(identifier, False)

    if user_input == "/readiness":
        return _format_readiness_surface(_product_snapshot())

    if user_input == "/session":
        if session is None:
            return "No active session."
        return format_session_inspect(session)

    if user_input == "/session-replay":
        if session is None:
            return "No active session."
        return format_session_replay(session)

    if user_input == "/checkpoints":
        if session is None:
            return "No active session."
        return format_session_checkpoints(session)

    if user_input.startswith("/session "):
        target = user_input[len("/session ") :].strip()
        if not target:
            return "Usage: /session <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_inspect(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for inspection."
        return format_session_inspect(target_session)

    if user_input.startswith("/session-replay "):
        target = user_input[len("/session-replay ") :].strip()
        if not target:
            return "Usage: /session-replay <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_replay(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for replay."
        return format_session_replay(target_session)

    if user_input.startswith("/checkpoints "):
        target = user_input[len("/checkpoints ") :].strip()
        if not target:
            return "Usage: /checkpoints <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_checkpoints(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for checkpoint inspection."
        return format_session_checkpoints(target_session)

    if user_input == "/rewind-preview" or user_input.startswith("/rewind-preview "):
        if session is None:
            return "No active session."
        target = user_input[len("/rewind-preview") :].strip()
        steps = 1
        checkpoint_id = None
        if target and target != "latest":
            if target.isdigit():
                steps = max(1, int(target))
            else:
                checkpoint_id = target
        return format_rewind_preview(
            session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )

    if user_input == "/rewind" or user_input.startswith("/rewind "):
        if session is None:
            return "No active session."
        target = user_input[len("/rewind") :].strip()
        steps = 1
        checkpoint_id = None
        if target and target != "latest":
            if target.isdigit():
                steps = max(1, int(target))
            else:
                checkpoint_id = target
        restored = rewind_session_data(
            session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )
        if not restored:
            return "No checkpoints available to rewind."
        return _format_rewind_result(session, restored, "Rewound")

    if user_input.startswith("/session-rewind "):
        raw = user_input[len("/session-rewind ") :].strip()
        if not raw:
            return "Usage: /session-rewind <session-id|latest> [latest|steps|checkpoint-id]"
        parts = raw.split(maxsplit=1)
        target = parts[0]
        rewind_arg = parts[1].strip() if len(parts) > 1 else "latest"
        steps = 1
        checkpoint_id = None
        if rewind_arg and rewind_arg != "latest":
            if rewind_arg.isdigit():
                steps = max(1, int(rewind_arg))
            else:
                checkpoint_id = rewind_arg
        if session is not None and target == getattr(session, "session_id", None):
            restored = rewind_session_data(
                session,
                steps=steps,
                checkpoint_id=checkpoint_id,
            )
            if not restored:
                return "No checkpoints available to rewind for that session."
            return _format_rewind_result(session, restored, "Rewound")
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found to rewind."
        rewound_session, restored = rewind_session(
            target_session.session_id,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )
        if rewound_session is None or not restored:
            return "No checkpoints available to rewind for that session."
        return _format_rewind_result(rewound_session, restored, "Rewound")

    if user_input.startswith("/session-rewind-preview "):
        raw = user_input[len("/session-rewind-preview ") :].strip()
        if not raw:
            return "Usage: /session-rewind-preview <session-id|latest> [latest|steps|checkpoint-id]"
        parts = raw.split(maxsplit=1)
        target = parts[0]
        rewind_arg = parts[1].strip() if len(parts) > 1 else "latest"
        steps = 1
        checkpoint_id = None
        if rewind_arg and rewind_arg != "latest":
            if rewind_arg.isdigit():
                steps = max(1, int(rewind_arg))
            else:
                checkpoint_id = rewind_arg
        if session is not None and target == getattr(session, "session_id", None):
            return format_rewind_preview(
                session,
                steps=steps,
                checkpoint_id=checkpoint_id,
            )
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found to preview."
        return format_rewind_preview(
            target_session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )

    if user_input == "/skills" or user_input.startswith("/skills "):
        return _handle_skills_command(user_input, cwd=cwd, tools=tools)

    if user_input == "/security" or user_input.startswith("/security "):
        return _handle_security_command(user_input, cwd=cwd)

    if user_input == "/config":
        from minicode.config import format_config_diagnostic
        return format_config_diagnostic()

    if user_input == "/state":
        try:
            from minicode.state import handle_state_command
            return handle_state_command()
        except ImportError:
            return "State system not available. Please ensure state.py exists."

    if user_input == "/memory":
        # Memory system display
        try:
            from minicode.memory import MemoryManager
            memory_mgr = MemoryManager(project_root=Path(cwd) if cwd else Path.cwd())
            return memory_mgr.format_stats()
        except Exception as e:
            return f"Error loading memory: {e}"

    if user_input == "/context":
        # Context usage display
        try:
            from minicode.context_manager import load_context_state
            ctx_mgr = load_context_state()
            if ctx_mgr:
                return ctx_mgr.format_context_details()
            else:
                return "No context state available. Context tracking starts after first turn."
        except Exception as e:
            return f"Error loading context: {e}"

    if user_input == "/cybernetics":
        return format_cybernetics_status()

    if user_input == "/mcp":
        servers = tools.get_mcp_servers() if tools else []
        if not servers:
            return "No MCP servers configured. Add mcpServers to ~/.mini-code/settings.json, ~/.mini-code/mcp.json, or project .mcp.json."
        lines = []
        for server in servers:
            suffix = f"  error={server['error']}" if server.get("error") else ""
            protocol = f"  protocol={server['protocol']}" if server.get("protocol") else ""
            resources = f"  resources={server['resourceCount']}" if server.get("resourceCount") is not None else ""
            prompts = f"  prompts={server['promptCount']}" if server.get("promptCount") is not None else ""
            lines.append(
                f"{server['name']}  status={server['status']}  tools={server['toolCount']}{resources}{prompts}{protocol}{suffix}"
            )
        return "\n".join(lines)

    if user_input == "/status":
        try:
            runtime = load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        from minicode.model_registry import detect_provider
        provider = detect_provider(runtime["model"], runtime)
        auth_methods = []
        if runtime.get("authToken"):
            auth_methods.append("ANTHROPIC_AUTH_TOKEN")
        if runtime.get("apiKey"):
            auth_methods.append("ANTHROPIC_API_KEY")
        if runtime.get("openaiApiKey"):
            auth_methods.append("OPENAI_API_KEY")
        if runtime.get("openrouterApiKey"):
            auth_methods.append("OPENROUTER_API_KEY")
        if runtime.get("customApiKey"):
            auth_methods.append("CUSTOM_API_KEY")
        return "\n".join(
            [
                f"model: {runtime['model']}",
                f"provider: {provider.value}",
                f"baseUrl: {runtime['baseUrl']}",
                f"auth: {', '.join(auth_methods) or 'none'}",
                f"mcp servers: {len(runtime.get('mcpServers', {}))}",
                runtime["sourceSummary"],
            ]
        )

    if user_input == "/model":
        try:
            runtime = load_runtime_config()
            from minicode.model_registry import format_model_status
            return format_model_status(runtime["model"], runtime)
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"

    if user_input.startswith("/model "):
        arg = user_input[len("/model "):].strip()
        if not arg:
            from minicode.model_registry import format_model_list
            return format_model_list()
        # Subcommands
        if arg in ("status", "info"):
            try:
                runtime = load_runtime_config()
                from minicode.model_registry import format_model_status
                return format_model_status(runtime["model"], runtime)
            except Exception as error:  # noqa: BLE001
                return f"runtime not configured: {error}"
        if arg in ("list", "ls"):
            from minicode.model_registry import format_model_list
            return format_model_list()
        # Provider filter: /model anthropic, /model openrouter, etc.
        from minicode.model_registry import Provider, format_model_list
        for p in Provider:
            if arg.lower() == p.value:
                return format_model_list(provider=p)
        # Otherwise: set model name
        save_mini_code_settings({"model": arg})
        return f"saved model={arg} to {MINI_CODE_SETTINGS_PATH}\nRestart MiniCode for the change to take effect."

    if user_input == "/user" or user_input.startswith("/user "):
        from minicode.user_profile import handle_user_command
        args = user_input[len("/user"):].strip()
        return handle_user_command(args)

    return None


def format_cybernetics_status() -> str:
    """Format cybernetic controller inventory and persisted state hints."""
    from minicode.cybernetic_supervisor import CyberneticSupervisor, load_supervisor_report
    from minicode.context_manager import load_context_state

    controllers = [
        ("ContextCyberneticsOrchestrator", "context pressure PID + prediction"),
        ("CostControlLoop", "budget PID for tool-result persistence"),
        ("VerificationController", "risk-adaptive verification planning"),
        ("ToolSchedulerController", "error/latency-aware concurrency control"),
        ("MemoryInjectionController", "context-aware memory injection"),
        ("ModelSelectionController", "cost/latency/failure-aware model routing"),
        ("ProgressController", "health/stall task progress control"),
        ("CyberneticSupervisor", "global health and risk aggregation"),
    ]

    ctx = load_context_state()
    snapshots = []
    if ctx:
        stats = ctx.get_stats()
        usage = stats.usage_percentage / 100.0
        snapshots.append(CyberneticSupervisor().snapshot_from_context({
            "sensor": {"current_usage": usage},
            "predictor": {"urgency": 0.0},
        }))
    persisted_report = load_supervisor_report()
    report = persisted_report or CyberneticSupervisor().report(snapshots)

    lines = [
        "Cybernetic Control System",
        "=" * 50,
        f"overall_health: {report.overall_health:.2f}",
        f"risk_level: {report.risk_level.value}",
        f"source: {'latest agent-loop report' if persisted_report else 'current persisted context'}",
        "",
        "Controllers:",
    ]
    for name, desc in controllers:
        lines.append(f"  - {name}: {desc}")
    lines.extend([
        "",
        "Runtime aggregation:",
        "  - pipeline outputs: progress_control + verification_plan + cybernetic_supervisor",
        "  - agent loop logs: context + cost + tool scheduling supervisor report",
    ])
    if report.recommended_actions:
        lines.append("")
        lines.append("Current actions:")
        for action in report.recommended_actions[:5]:
            lines.append(f"  - {action}")
    return "\n".join(lines)
