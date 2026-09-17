"""L1 atomic skill registry: frontmatter-validated discovery, indexing, matching.

This is the upgrade path for the legacy ``minicode.skills`` loader.  The legacy
module scanned four roots, de-duplicated by directory name, and derived a
description with a markdown heuristic.  The registry keeps all of that working
while adding the L1 contract from the改造 design:

* a validated ``frontmatter`` block (``name`` / ``version`` / ``description`` /
  ``triggers`` / ``tools`` / ``risk_level`` / ``kind``),
* a startup index that can be matched **by trigger** instead of by name alone,
* per-skill diagnostics so ``skill doctor`` can explain a broken skill instead
  of silently dropping it.

Compatibility is a hard requirement: a ``SKILL.md`` with no frontmatter at all
still loads (that is what ``tests/test_skills.py`` and the repository's own
``.claude/skills/minicode/SKILL.md`` exercise).  In that case every L1 field
takes its default and the description comes from the legacy heuristic.

Zero runtime dependencies: the frontmatter reader is a deliberately small
YAML-subset parser rather than a PyYAML import.  Structured orchestrations
(the L2 DAG) live in a sibling ``workflow.json`` instead of nested YAML, which
keeps this parser flat and predictable -- see ``composite.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from minicode.skills import extract_description

__all__ = [
    "DEFAULT_VERSION",
    "RiskLevel",
    "SkillKind",
    "SkillMatch",
    "SkillRegistry",
    "SkillSpec",
    "normalize_text",
    "parse_frontmatter",
    "parse_skill_file",
    "skill_roots",
    "text_tokens",
    "extract_description",
]


DEFAULT_VERSION = "0.0.0"

_FRONTMATTER_OPEN = re.compile(r"\A---[ \t]*\r?\n")
_FRONTMATTER_CLOSE = re.compile(r"^---[ \t]*$", re.MULTILINE)
_KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:[ \t]*(.*)$")
_LIST_ITEM = re.compile(r"^[ \t]+-[ \t]*(.*)$")
_SEMVER = re.compile(r"^\d+\.\d+(\.\d+)?([-.+][0-9A-Za-z.-]+)?$")


class RiskLevel(str, Enum):
    """How much damage a skill can do if it goes wrong."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SkillKind(str, Enum):
    """Which layer of the three-layer capability system a skill belongs to."""

    ATOMIC = "atomic"
    COMPOSITE = "composite"
    DOMAIN_PACK = "domain_pack"


@dataclass(slots=True)
class SkillSpec:
    """One discovered skill, L1 metadata plus the raw markdown body."""

    name: str
    description: str
    path: str
    source: str
    version: str = DEFAULT_VERSION
    triggers: tuple[str, ...] = ()
    tools: tuple[str, ...] | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    kind: SkillKind = SkillKind.ATOMIC
    content: str = ""
    body: str = ""
    has_frontmatter: bool = False
    issues: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        """``None`` means unrestricted; an empty tuple means "no tools allowed"."""
        return self.tools

    def summary(self) -> dict[str, str]:
        """Legacy ``ToolRegistry.get_skills()`` shape (name/description/source)."""
        return {"name": self.name, "description": self.description, "source": self.source}


@dataclass(slots=True)
class SkillMatch:
    """A scored match produced by :meth:`SkillRegistry.match`."""

    skill: SkillSpec
    score: int
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Frontmatter parsing (stdlib-only YAML subset)
# ---------------------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_comment(value: str) -> str:
    """Drop a trailing ``#`` comment, but keep ``#`` inside meaning-bearing text.

    A comment must be preceded by whitespace (``key: v  # note``) so that values
    such as ``C#`` or ``#1234`` survive intact.
    """
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _parse_value(raw: str) -> str:
    """Parse one scalar value: honour quotes first, then strip a trailing comment.

    Order matters.  ``description: "C# and #1234 stay"`` must keep its ``#1234``
    because the quote wins; the unquoted form is a YAML comment and is trimmed.
    """
    text = raw.strip()
    if text[:1] in {"'", '"'}:
        quote = text[0]
        end = text.find(quote, 1)
        if end != -1:
            return _strip_quotes(text[: end + 1])
    return _strip_quotes(_strip_comment(text).strip())


def _split_inline_list(value: str) -> list[str]:
    """Split ``[a, "b, c", d]`` on commas that are outside quotes."""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ",":
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    return [item for item in (_strip_quotes(item) for item in items) if item]


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str, bool, list[str]]:
    """Split ``markdown`` into (metadata, body, has_frontmatter, issues).

    The parser is intentionally forgiving.  If the leading ``---`` block does not
    actually contain ``key: value`` lines -- for example a document that opens
    with a horizontal rule -- it is treated as ordinary body text so legacy
    skills are never silently truncated.
    """
    issues: list[str] = []
    text = markdown.lstrip("\ufeff")
    normalized = text.replace("\r\n", "\n")
    opening = _FRONTMATTER_OPEN.match(normalized)
    if opening is None:
        return {}, normalized, False, issues

    remainder = normalized[opening.end() :]
    close = _FRONTMATTER_CLOSE.search(remainder)
    if close is None:
        issues.append("frontmatter opened with '---' but never closed; treated as body text")
        return {}, normalized, False, issues

    block = remainder[: close.start()]
    # The closing fence match ends before its newline, so drop the blank run
    # that follows it; leading blank lines carry no meaning in a skill body.
    body = remainder[close.end() :].lstrip("\n")

    metadata: dict[str, Any] = {}
    pending_key: str | None = None
    pending_items: list[str] = []
    saw_key = False

    def _flush_pending() -> None:
        nonlocal pending_key, pending_items
        if pending_key is not None:
            metadata[pending_key] = list(pending_items)
        pending_key, pending_items = None, []

    for raw_line in block.split("\n"):
        if not raw_line.strip():
            continue
        if raw_line.lstrip().startswith("#"):
            continue
        item = _LIST_ITEM.match(raw_line)
        if item is not None:
            if pending_key is None:
                issues.append(f"list item without a key: {item.group(1).strip()!r}")
                continue
            value = _parse_value(item.group(1))
            if value:
                pending_items.append(value)
            continue
        match = _KEY_VALUE.match(raw_line.strip())
        if match is None:
            issues.append(f"unparsable frontmatter line: {raw_line.strip()!r}")
            continue
        saw_key = True
        _flush_pending()
        key, raw_value = match.group(1), match.group(2).strip()
        value = _parse_value(raw_value)
        if not value:
            pending_key, pending_items = key, []
        elif value.startswith("["):
            metadata[key] = _split_inline_list(value)
        else:
            metadata[key] = _strip_quotes(value)
    _flush_pending()

    if not saw_key:
        issues.append("leading '---' block has no key/value pairs; treated as body text")
        return {}, normalized, False, issues

    return metadata, body, True, issues


def _as_text_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def parse_skill_file(
    path: Path,
    *,
    name: str,
    source: str,
    known_tools: Iterable[str] | None = None,
) -> SkillSpec:
    """Read one ``SKILL.md`` into a validated :class:`SkillSpec`."""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        content = ""
        unreadable_issue = f"SKILL.md is not valid UTF-8 and could not be read: {exc}"
    else:
        unreadable_issue = ""

    metadata, body, has_frontmatter, issues = parse_frontmatter(content)
    if unreadable_issue:
        issues.insert(0, unreadable_issue)

    declared_name = str(metadata.get("name") or "").strip()
    # The directory name is the lookup key everywhere else in the product
    # (``load_skill`` resolves ``<root>/<name>/SKILL.md``), so it wins when the
    # frontmatter disagrees -- and the disagreement is reported.
    skill_name = name or declared_name
    if declared_name and declared_name != name:
        issues.append(
            f"frontmatter name {declared_name!r} differs from directory name {name!r}; "
            "the directory name wins for lookup"
        )

    description = str(metadata.get("description") or "").strip()
    if not description:
        description = extract_description(body)
        if has_frontmatter:
            issues.append("frontmatter has no 'description'; fell back to the body heuristic")

    version = str(metadata.get("version") or "").strip() or DEFAULT_VERSION
    if version != DEFAULT_VERSION and not _SEMVER.match(version):
        issues.append(f"version {version!r} is not semver-like; keeping it as declared")

    triggers = _as_text_list(metadata.get("triggers"))
    if not triggers:
        issues.append("no triggers declared; only name/description matching will hit")

    if "tools" in metadata:
        tools: tuple[str, ...] | None = _as_text_list(metadata.get("tools"))
        allowed = set(known_tools or ())
        if not tools:
            # An empty whitelist fails closed on purpose: a skill that declares
            # `tools:` and lists nothing gets no tools at all.  It is called out
            # so `skill doctor` shows the author why the skill cannot act.
            issues.append("tools whitelist is empty; this skill is allowed no tools (omit 'tools' for unrestricted)")
        elif allowed:
            unknown = sorted(set(tools) - allowed)
            if unknown:
                issues.append(f"tools whitelist references unknown tool(s): {', '.join(unknown)}")
    else:
        tools = None

    raw_risk = str(metadata.get("risk_level") or "").strip().lower()
    if raw_risk:
        try:
            risk_level = RiskLevel(raw_risk)
        except ValueError:
            issues.append(f"unknown risk_level {raw_risk!r}; defaulting to low")
            risk_level = RiskLevel.LOW
    else:
        risk_level = RiskLevel.LOW

    raw_kind = str(metadata.get("kind") or "").strip().lower()
    if raw_kind:
        try:
            kind = SkillKind(raw_kind)
        except ValueError:
            issues.append(f"unknown kind {raw_kind!r}; defaulting to atomic")
            kind = SkillKind.ATOMIC
    else:
        kind = SkillKind.ATOMIC

    recognized = {
        "name",
        "version",
        "description",
        "triggers",
        "tools",
        "risk_level",
        "kind",
    }
    extra = {key: value for key, value in metadata.items() if key not in recognized}
    if extra:
        for key in sorted(extra):
            issues.append(f"unrecognized frontmatter key {key!r} kept in metadata")

    return SkillSpec(
        name=skill_name,
        description=description,
        path=str(path),
        source=source,
        version=version,
        triggers=triggers,
        tools=tools,
        risk_level=risk_level,
        kind=kind,
        content=content,
        body=body,
        has_frontmatter=has_frontmatter,
        issues=tuple(issues),
        metadata=extra,
    )


# ---------------------------------------------------------------------------
# Discovery roots
# ---------------------------------------------------------------------------


def skill_roots(cwd: str | Path, extra_roots: Sequence[tuple[Path, str]] | None = None) -> list[tuple[Path, str]]:
    """Return ``[(root, source)]`` in precedence order (first match wins).

    Mirrors ``minicode.skills._skill_roots`` so the legacy and registry views
    agree, with optional extra roots appended at the lowest precedence.
    """
    base = Path(cwd)
    home = Path.home()
    roots: list[tuple[Path, str]] = [
        (base / ".mini-code" / "skills", "project"),
        (home / ".mini-code" / "skills", "user"),
        (base / ".claude" / "skills", "compat_project"),
        (home / ".claude" / "skills", "compat_user"),
    ]
    if extra_roots:
        roots.extend(extra_roots)
    return roots


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """An index over discovered skills with trigger-aware matching."""

    def __init__(self, skills: Iterable[SkillSpec] = ()) -> None:
        self._skills: dict[str, SkillSpec] = {}
        for skill in skills:
            self._skills.setdefault(skill.name, skill)

    # -- construction ------------------------------------------------------

    @classmethod
    def discover(
        cls,
        cwd: str | Path,
        *,
        extra_roots: Sequence[tuple[Path, str]] | None = None,
        known_tools: Iterable[str] | None = None,
        roots: Sequence[tuple[Path, str]] | None = None,
    ) -> "SkillRegistry":
        """Build the index from the discovery roots (project beats user)."""
        discovered: list[SkillSpec] = []
        for root, source in (roots if roots is not None else skill_roots(cwd, extra_roots)):
            discovered.extend(cls._load_root(Path(root), source, known_tools))
        return cls(discovered)

    @staticmethod
    def _load_root(root: Path, source: str, known_tools: Iterable[str] | None) -> list[SkillSpec]:
        if not root.exists():
            return []
        found: list[SkillSpec] = []
        for entry in sorted(root.iterdir(), key=lambda path: path.name):
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                # Windows: broken symlinks, untrusted mounts.
                continue
            skill_path = entry / "SKILL.md"
            if not skill_path.exists():
                continue
            try:
                found.append(
                    parse_skill_file(skill_path, name=entry.name, source=source, known_tools=known_tools)
                )
            except OSError:
                continue
        return found

    # -- access ------------------------------------------------------------

    def all(self) -> list[SkillSpec]:
        return list(self._skills.values())

    def get(self, name: str) -> SkillSpec | None:
        return self._skills.get(name.strip())

    def names(self) -> list[str]:
        return sorted(self._skills)

    def by_kind(self, kind: SkillKind) -> list[SkillSpec]:
        return [skill for skill in self._skills.values() if skill.kind is kind]

    def summaries(self) -> list[dict[str, str]]:
        """Legacy ``get_skills()`` payload for the prompt bundle and ``/skills``."""
        return [skill.summary() for skill in self._skills.values()]

    def issues(self) -> list[tuple[str, str]]:
        """``[(skill_name, issue)]`` for everything the parser complained about."""
        return [
            (skill.name, issue)
            for skill in self._skills.values()
            for issue in skill.issues
        ]

    # -- matching ----------------------------------------------------------

    def match(self, query: str, *, limit: int = 3, min_score: int = 2) -> list[SkillMatch]:
        """Rank skills for ``query`` by trigger/name/description evidence.

        Scoring is deterministic and explainable (no embeddings, no network):
        a trigger phrase found in the query is worth 4, a trigger token overlap
        2, the skill name appearing in the query 3, and description token
        overlap 1 per token (capped at 3).  Every point is reported in
        ``reasons`` so ``/skills match`` can justify a hit.
        """
        normalized = _normalize(query)
        if not normalized:
            return []
        query_tokens = _tokens(normalized)
        query_text = f" {normalized} "

        matches: list[SkillMatch] = []
        for skill in self._skills.values():
            score = 0
            reasons: list[str] = []

            for trigger in skill.triggers:
                trigger_norm = _normalize(trigger)
                if not trigger_norm:
                    continue
                if f" {trigger_norm} " in query_text or trigger_norm == normalized:
                    score += 4
                    reasons.append(f"trigger phrase {trigger!r}")
                    continue
                overlap = query_tokens & _tokens(trigger_norm)
                if overlap:
                    score += 2 * len(overlap)
                    reasons.append(f"trigger tokens {', '.join(sorted(overlap))}")

            name_norm = _normalize(skill.name.replace("-", " ").replace("_", " "))
            if name_norm and name_norm in query_text:
                score += 3
                reasons.append(f"name {skill.name!r}")
            else:
                name_overlap = query_tokens & _tokens(name_norm)
                if name_overlap:
                    score += len(name_overlap)
                    reasons.append(f"name tokens {', '.join(sorted(name_overlap))}")

            description_overlap = query_tokens & _tokens(_normalize(skill.description))
            if description_overlap:
                points = min(len(description_overlap), 3)
                score += points
                reasons.append(f"description tokens {', '.join(sorted(description_overlap))}")

            if score >= min_score:
                matches.append(SkillMatch(skill=skill, score=score, reasons=tuple(reasons)))

        matches.sort(key=lambda item: (-item.score, item.skill.name))
        return matches[:limit]

    # -- rendering ---------------------------------------------------------

    def format_index(self, *, enabled: dict[str, bool] | None = None) -> str:
        """The ``/skills`` listing: name, version, risk, kind, triggers, source."""
        skills = sorted(self._skills.values(), key=lambda skill: (skill.kind.value, skill.name))
        if not skills:
            return (
                "No skills discovered. Add skills under "
                ".mini-code/skills/<name>/SKILL.md, ~/.mini-code/skills/<name>/SKILL.md, "
                ".claude/skills/<name>/SKILL.md or ~/.claude/skills/<name>/SKILL.md."
            )
        lines = [f"Skills ({len(skills)} discovered):", ""]
        for skill in skills:
            state = ""
            if enabled is not None:
                state = " [enabled]" if enabled.get(skill.name, True) else " [disabled]"
            flags = []
            if not skill.has_frontmatter:
                flags.append("legacy (no frontmatter)")
            if skill.issues:
                flags.append(f"{len(skill.issues)} issue(s)")
            suffix = f"  {{{'; '.join(flags)}}}" if flags else ""
            lines.append(
                f"- {skill.name} v{skill.version} [{skill.kind.value}, risk={skill.risk_level.value}]"
                f" ({skill.source}){state}{suffix}"
            )
            lines.append(f"    {skill.description}")
            if skill.triggers:
                lines.append(f"    triggers: {', '.join(skill.triggers)}")
            if skill.tools is not None:
                whitelist = ", ".join(skill.tools) if skill.tools else "none"
                lines.append(f"    tools: {whitelist}")
        return "\n".join(lines)

    def format_detail(self, name: str) -> str:
        """One skill in full, including its diagnostics."""
        skill = self.get(name)
        if skill is None:
            return f"Unknown skill: {name}"
        lines = [
            f"Skill: {skill.name}",
            f"Version: {skill.version}",
            f"Kind: {skill.kind.value}",
            f"Risk level: {skill.risk_level.value}",
            f"Source: {skill.source}",
            f"Path: {skill.path}",
            f"Frontmatter: {'yes' if skill.has_frontmatter else 'no (legacy skill)'}",
            f"Description: {skill.description}",
        ]
        lines.append(f"Triggers: {', '.join(skill.triggers) if skill.triggers else '(none)'}")
        if skill.tools is None:
            lines.append("Tools: unrestricted (no whitelist declared)")
        else:
            lines.append(f"Tools: {', '.join(skill.tools) if skill.tools else 'none allowed'}")
        if skill.metadata:
            lines.append(
                "Extra metadata: "
                + ", ".join(f"{key}={value!r}" for key, value in sorted(skill.metadata.items()))
            )
        lines.append("")
        lines.append("Issues:" if skill.issues else "Issues: none")
        lines.extend(f"  - {issue}" for issue in skill.issues)
        return "\n".join(lines)

    def format_doctor(self) -> str:
        """``skill doctor``: every diagnostic across the registry in one report."""
        skills = sorted(self._skills.values(), key=lambda skill: skill.name)
        if not skills:
            return "skill doctor: no skills discovered."
        clean = [skill for skill in skills if not skill.issues]
        dirty = [skill for skill in skills if skill.issues]
        lines = [
            "skill doctor",
            "=" * 60,
            f"Discovered: {len(skills)}",
            f"Clean: {len(clean)}",
            f"With issues: {len(dirty)}",
            f"Legacy (no frontmatter): {sum(1 for skill in skills if not skill.has_frontmatter)}",
            "",
        ]
        if not dirty:
            lines.append("No issues found.")
            return "\n".join(lines)
        for skill in dirty:
            lines.append(f"{skill.name} ({skill.path})")
            lines.extend(f"  - {issue}" for issue in skill.issues)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_PUNCTUATION = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "for", "with", "to", "of", "in", "on",
        "is", "are", "be", "it", "this", "that", "my", "me", "you", "your",
        "how", "do", "i", "can", "please", "help",
    }
)


def _normalize(text: str) -> str:
    return _PUNCTUATION.sub(" ", text.strip().lower()).strip()


def _tokens(text: str) -> set[str]:
    """Token set for scoring.

    Latin runs are split on whitespace/punctuation and stopword-filtered; CJK
    has no word delimiters, so each run is also emitted as character bigrams
    (``发版流程`` -> ``发版``, ``版流``, ``流程``) which is what makes the
    Chinese triggers in this project's skills match at all.
    """
    tokens: set[str] = set()
    for token in text.split():
        if len(token) == 1 and not _CJK.match(token):
            continue
        if token in _STOPWORDS:
            continue
        if _CJK.search(token):
            if len(token) >= 2:
                tokens.add(token)
            for index in range(len(token) - 1):
                tokens.add(token[index : index + 2])
        elif len(token) > 1:
            tokens.add(token)
    return tokens


# Public aliases: the L3 domain-pack selector scores its candidates with exactly
# the same text pipeline, so these are shared rather than reimplemented.
normalize_text = _normalize
text_tokens = _tokens
