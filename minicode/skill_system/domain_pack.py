"""L3 domain packs: knowledge + entity labels + few-shot examples, injected on demand.

A domain pack is the third layer of the capability system.  Where an L1 skill
says *how to do something*, a pack carries the *background knowledge* a task
needs -- and it is deliberately not resident in the prompt.  A repository with
twenty packs would otherwise pay for all twenty on every turn.

Layout::

    <root>/<pack-name>/
        pack.json        # {"name", "version", "description", "labels": [...]}
        knowledge.md     # the prose that gets injected when the pack matches
        examples.jsonl   # optional few-shot examples, one JSON object per line

Discovery roots mirror skills (``<cwd>/.mini-code/packs`` then
``~/.mini-code/packs``), with optional extra roots at the lowest precedence.
Injection goes through :func:`inject_context`, which appends at most one pack to
an existing memory-context string and is exception-safe: a pack that fails to
load degrades to "no injection", never to a broken turn.

Selection reuses the L1 matcher's text pipeline (``text_tokens``) so a query
scores against pack labels exactly the way it scores against skill triggers --
one matching story for the whole capability system rather than two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from minicode.skill_system.registry import normalize_text, text_tokens

__all__ = [
    "MANIFEST_FILENAME",
    "KNOWLEDGE_FILENAME",
    "EXAMPLES_FILENAME",
    "DEFAULT_MAX_INJECTION_CHARS",
    "PackExample",
    "DomainPack",
    "PackMatch",
    "pack_roots",
    "parse_pack",
    "discover_packs",
    "select_packs",
    "render_injection",
    "inject_context",
    "format_index",
    "format_pack",
]

MANIFEST_FILENAME = "pack.json"
KNOWLEDGE_FILENAME = "knowledge.md"
EXAMPLES_FILENAME = "examples.jsonl"
DEFAULT_MAX_INJECTION_CHARS = 1200
_MAX_EXAMPLES_INJECTED = 3


@dataclass(slots=True)
class PackExample:
    """One few-shot example from ``examples.jsonl``."""

    input: str = ""
    output: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"input": self.input, "output": self.output}
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PackExample":
        return cls(
            input=str(payload.get("input") or ""),
            output=str(payload.get("output") or ""),
            note=str(payload.get("note") or ""),
        )


@dataclass
class DomainPack:
    """A validated pack: metadata, knowledge body, labels and examples."""

    name: str
    description: str = ""
    version: str = "0.0.0"
    labels: tuple[str, ...] = ()
    knowledge: str = ""
    examples: list[PackExample] = field(default_factory=list)
    path: str = ""
    source: str = ""
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "labels": list(self.labels),
            "path": self.path,
            "source": self.source,
            "knowledgeChars": len(self.knowledge),
            "examples": len(self.examples),
            "issues": list(self.issues),
        }


@dataclass(slots=True)
class PackMatch:
    """A scored pack selection."""

    pack: DomainPack
    score: int
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def pack_roots(
    cwd: str | Path,
    extra_roots: Sequence[tuple[Path, str]] | None = None,
) -> list[tuple[Path, str]]:
    """``[(root, source)]`` in precedence order (project beats user)."""
    base = Path(cwd)
    home = Path.home()
    roots: list[tuple[Path, str]] = [
        (base / ".mini-code" / "packs", "project"),
        (home / ".mini-code" / "packs", "user"),
    ]
    if extra_roots:
        roots.extend(extra_roots)
    return roots


def _as_text_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _load_examples(path: Path, issues: list[str]) -> list[PackExample]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(f"{EXAMPLES_FILENAME} could not be read: {exc}")
        return []
    examples: list[PackExample] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            issues.append(f"{EXAMPLES_FILENAME} line {number} is not valid JSON; skipped")
            continue
        if isinstance(payload, dict):
            examples.append(PackExample.from_dict(payload))
        else:
            issues.append(f"{EXAMPLES_FILENAME} line {number} is not a JSON object; skipped")
    return examples


def parse_pack(pack_dir: Path, source: str) -> DomainPack:
    """Read one pack directory, collecting diagnostics instead of raising."""
    issues: list[str] = []
    manifest_path = pack_dir / MANIFEST_FILENAME
    payload: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            issues.append(f"{MANIFEST_FILENAME} could not be read: {exc}")
            raw = None
        if isinstance(raw, dict):
            payload = raw
        elif raw is not None:
            issues.append(f"{MANIFEST_FILENAME} must contain a JSON object")
    else:
        issues.append(f"no {MANIFEST_FILENAME}; using the directory name and defaults")

    knowledge_path = pack_dir / KNOWLEDGE_FILENAME
    if knowledge_path.exists():
        try:
            knowledge = knowledge_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            knowledge = ""
            issues.append(f"{KNOWLEDGE_FILENAME} could not be read: {exc}")
    else:
        knowledge = ""
        issues.append(f"no {KNOWLEDGE_FILENAME}; this pack has nothing to inject")

    labels = _as_text_list(payload.get("labels"))
    if not labels:
        issues.append("no labels declared; only name/description matching will hit")

    name = str(payload.get("name") or pack_dir.name).strip() or pack_dir.name
    description = str(payload.get("description") or "").strip()
    if not description:
        issues.append("no description declared")

    return DomainPack(
        name=name,
        description=description,
        version=str(payload.get("version") or "0.0.0").strip() or "0.0.0",
        labels=labels,
        knowledge=knowledge,
        examples=_load_examples(pack_dir / EXAMPLES_FILENAME, issues),
        path=str(pack_dir),
        source=source,
        issues=tuple(issues),
    )


def discover_packs(
    cwd: str | Path,
    *,
    extra_roots: Sequence[tuple[Path, str]] | None = None,
    roots: Sequence[tuple[Path, str]] | None = None,
) -> list[DomainPack]:
    """Discover packs; the first root to declare a name owns it."""
    found: dict[str, DomainPack] = {}
    for root, source in (roots if roots is not None else pack_roots(cwd, extra_roots)):
        root_path = Path(root)
        if not root_path.exists():
            continue
        try:
            entries = sorted(root_path.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            pack = parse_pack(entry, source)
            found.setdefault(pack.name, pack)
    return [found[name] for name in sorted(found)]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_packs(
    packs: Iterable[DomainPack],
    query: str,
    *,
    limit: int = 1,
    min_score: int = 2,
) -> list[PackMatch]:
    """Rank packs for a query: label phrase 4, label token 2, name 3, description 1/token.

    Same shape and thresholds as ``SkillRegistry.match`` on purpose -- a user who
    understands why a skill was suggested should not have to learn a second
    scoring story for packs.
    """
    normalized = normalize_text(query)
    if not normalized:
        return []
    query_tokens = text_tokens(normalized)
    query_text = f" {normalized} "

    matches: list[PackMatch] = []
    for pack in packs:
        score = 0
        reasons: list[str] = []
        for label in pack.labels:
            label_norm = normalize_text(label)
            if not label_norm:
                continue
            if f" {label_norm} " in query_text or label_norm == normalized:
                score += 4
                reasons.append(f"label phrase {label!r}")
                continue
            overlap = query_tokens & text_tokens(label_norm)
            if overlap:
                score += 2 * len(overlap)
                reasons.append(f"label tokens {', '.join(sorted(overlap))}")

        name_norm = normalize_text(pack.name.replace("-", " ").replace("_", " "))
        if name_norm and name_norm in query_text:
            score += 3
            reasons.append(f"name {pack.name!r}")

        description_overlap = query_tokens & text_tokens(normalize_text(pack.description))
        if description_overlap:
            points = min(len(description_overlap), 3)
            score += points
            reasons.append(f"description tokens {', '.join(sorted(description_overlap))}")

        if score >= min_score:
            matches.append(PackMatch(pack=pack, score=score, reasons=tuple(reasons)))

    matches.sort(key=lambda item: (-item.score, item.pack.name))
    return matches[:limit]


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def render_injection(pack: DomainPack, *, max_chars: int = DEFAULT_MAX_INJECTION_CHARS) -> str:
    """Render a pack as a prompt-ready markdown block, bounded by ``max_chars``.

    The knowledge body is clipped at the budget; examples are only added while
    room remains, so a small budget yields knowledge and a generous one adds
    few-shot guidance.
    """
    header = [f"## Domain knowledge: {pack.name}"]
    if pack.description:
        header.append(pack.description)
    if pack.labels:
        header.append(f"Labels: {', '.join(pack.labels)}")
    header.append("")

    budget = max(0, max_chars)
    body = pack.knowledge.strip()
    if len(body) > budget:
        body = body[:budget].rstrip() + "\n[knowledge truncated]"

    lines = [*header, body] if body else list(header)
    used = sum(len(line) + 1 for line in lines)

    for example in pack.examples[:_MAX_EXAMPLES_INJECTED]:
        block = [f"- Example: {example.input}", f"  -> {example.output}"]
        if example.note:
            block.append(f"  note: {example.note}")
        cost = sum(len(line) + 1 for line in block)
        if used + cost > max_chars and body:
            break
        lines.extend(block)
        used += cost

    return "\n".join(lines).strip()


def inject_context(
    cwd: str | Path,
    query: str,
    base: str = "",
    *,
    limit: int = 1,
    min_score: int = 2,
    max_chars: int = DEFAULT_MAX_INJECTION_CHARS,
    extra_roots: Sequence[tuple[Path, str]] | None = None,
) -> str:
    """Append the best-matching domain pack to ``base``; never raises.

    Returns ``base`` unchanged when nothing matches or when loading fails, so a
    caller can splice this straight into an existing memory-context string.
    """
    try:
        packs = discover_packs(cwd, extra_roots=extra_roots)
        if not packs:
            return base
        matches = select_packs(packs, query, limit=limit, min_score=min_score)
        if not matches:
            return base
        blocks = [render_injection(match.pack, max_chars=max_chars) for match in matches]
        blocks = [block for block in blocks if block]
        if not blocks:
            return base
        return (base.rstrip() + "\n\n" if base.strip() else "") + "\n\n".join(blocks)
    except Exception:  # noqa: BLE001 - injection must never break a turn
        return base


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_index(packs: Iterable[DomainPack]) -> str:
    """``/skills packs``-style listing."""
    ordered = sorted(packs, key=lambda pack: pack.name)
    if not ordered:
        return (
            "No domain packs discovered. Add packs under "
            ".mini-code/packs/<name>/ or ~/.mini-code/packs/<name>/ "
            f"with {MANIFEST_FILENAME} + {KNOWLEDGE_FILENAME}."
        )
    lines = [f"Domain packs ({len(ordered)} discovered):", ""]
    for pack in ordered:
        flags = f"  {{{len(pack.issues)} issue(s)}}" if pack.issues else ""
        lines.append(f"- {pack.name} v{pack.version} ({pack.source}){flags}")
        lines.append(f"    {pack.description or '(no description)'}")
        if pack.labels:
            lines.append(f"    labels: {', '.join(pack.labels)}")
        lines.append(f"    knowledge: {len(pack.knowledge)} chars, {len(pack.examples)} example(s)")
    return "\n".join(lines)


def format_pack(pack: DomainPack) -> str:
    """One pack in full, diagnostics included."""
    lines = [
        f"Domain pack: {pack.name}",
        f"Version: {pack.version}",
        f"Source: {pack.source}",
        f"Path: {pack.path}",
        f"Description: {pack.description or '(none)'}",
        f"Labels: {', '.join(pack.labels) if pack.labels else '(none)'}",
        f"Knowledge: {len(pack.knowledge)} chars",
        f"Examples: {len(pack.examples)}",
        "",
        "Issues:" if pack.issues else "Issues: none",
    ]
    lines.extend(f"  - {issue}" for issue in pack.issues)
    if pack.examples:
        lines.append("")
        lines.append("Example previews:")
        lines.extend(f"  - {example.input[:80]}" for example in pack.examples[:3])
    return "\n".join(lines)
