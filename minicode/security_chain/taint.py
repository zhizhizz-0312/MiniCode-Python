"""Taint tracking: remember what the agent read, so an action can be traced to it.

The kill chain for indirect injection is *untrusted content -> decision -> side
effect*.  Layer L1/L2/L3 can judge an action in isolation, but only the taint
registry can answer the question that actually matters:

    did this parameter come from something the agent read, or from the user?

Same command, different provenance, different verdict.  ``rm -rf build`` typed by
the user is routine; the same string lifted out of a poisoned README is an attack.

Two detection mechanisms, because poisoned payloads come in two shapes:

* **Indicators** -- URLs, paths and command-looking tokens *extracted* from
  untrusted content.  Catches "the payload named a target, and the agent is now
  touching that target".
* **Fragments** -- distinctive long literal substrings of the content.  Catches
  "the payload contained the whole command, and the agent is running it".

Everything is session-scoped and capped: the registry is a memory aid, not an
archive, and it must stay cheap enough to consult on every tool call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

__all__ = [
    "MAX_ENTRIES",
    "MIN_FRAGMENT_CHARS",
    "TaintEntry",
    "TaintHit",
    "TaintRegistry",
    "get_registry",
    "reset_registry",
]

MAX_ENTRIES = 64
MIN_FRAGMENT_CHARS = 16
_MAX_FRAGMENTS_PER_ENTRY = 6
_MAX_INDICATORS_PER_ENTRY = 24

# Zero-width and bidi characters are a favourite way to hide a payload from a
# human reviewer while keeping it intact for the model.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD], None
)
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://[^\s<>'\"\)\]]+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s<>'\"\|]+")
_POSIX_PATH = re.compile(r"(?<![\w.-])/(?:[\w.-]+/)*[\w.-]+")
_COMMAND_ISH = re.compile(
    r"\b(?:curl|wget|bash|sh|powershell|pwsh|python3?|node|npm|pip|rm|del|git|scp|nc|netcat)\b"
    r"[^\n;|&]{0,120}",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return _WS.sub(" ", text.translate(_INVISIBLE)).strip()


@dataclass(slots=True)
class TaintEntry:
    """One piece of untrusted content the agent has seen."""

    key: str
    source: str
    tool_name: str
    indicators: tuple[str, ...] = ()
    fragments: tuple[str, ...] = ()
    chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "toolName": self.tool_name,
            "chars": self.chars,
            "indicators": list(self.indicators),
            "fragments": len(self.fragments),
        }


@dataclass(frozen=True, slots=True)
class TaintHit:
    """Why a piece of text looked tainted."""

    matched: str
    source: str
    tool_name: str
    kind: str  # "indicator" | "fragment"

    def describe(self) -> str:
        return f"{self.kind} {self.matched[:80]!r} from {self.source}:{self.tool_name or 'unknown'}"


def _extract_indicators(text: str) -> tuple[str, ...]:
    """Harvest the concrete targets an attacker would have to name."""
    found: list[str] = []
    for pattern in (_URL, _WINDOWS_PATH, _POSIX_PATH, _COMMAND_ISH):
        for match in pattern.findall(text):
            candidate = _normalize(match)
            if len(candidate) < 6:
                continue
            if candidate not in found:
                found.append(candidate)
            if len(found) >= _MAX_INDICATORS_PER_ENTRY:
                return tuple(found)
    return tuple(found)


def _extract_fragments(text: str) -> tuple[str, ...]:
    """Longest distinctive literal lines, for whole-command reuse detection."""
    candidates: list[str] = []
    for raw_line in text.splitlines():
        line = _normalize(raw_line)
        if len(line) >= MIN_FRAGMENT_CHARS:
            candidates.append(line)
    # Also consider long runs without newlines (e.g. minified HTML with a payload).
    if not candidates:
        collapsed = _normalize(text)
        if len(collapsed) >= MIN_FRAGMENT_CHARS:
            candidates.append(collapsed[:400])
    candidates.sort(key=len, reverse=True)
    return tuple(candidates[:_MAX_FRAGMENTS_PER_ENTRY])


class TaintRegistry:
    """Session-scoped set of untrusted content, queryable by the action boundary."""

    def __init__(self, *, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: dict[str, TaintEntry] = {}
        self._max_entries = max_entries

    # -- recording (entry boundary) ---------------------------------------

    def mark(self, text: str, *, source: str = "unknown", tool_name: str = "") -> TaintEntry | None:
        """Record untrusted content. Returns ``None`` for empty/trivial input."""
        if not text or not text.strip():
            return None
        normalized = _normalize(text)
        if len(normalized) < MIN_FRAGMENT_CHARS:
            return None
        import hashlib

        key = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:24]
        existing = self._entries.get(key)
        if existing is not None:
            return existing
        entry = TaintEntry(
            key=key,
            source=source,
            tool_name=tool_name,
            indicators=_extract_indicators(normalized),
            fragments=_extract_fragments(normalized),
            chars=len(normalized),
        )
        self._entries[key] = entry
        self._evict()
        return entry

    def _evict(self) -> None:
        while len(self._entries) > self._max_entries:
            # dict preserves insertion order: drop the oldest observation.
            self._entries.pop(next(iter(self._entries)))

    # -- querying (exit boundary) -----------------------------------------

    def inspect(self, text: str) -> TaintHit | None:
        """Return the first reason ``text`` looks derived from untrusted content."""
        if not text:
            return None
        normalized = _normalize(text)
        for entry in self._entries.values():
            for indicator in entry.indicators:
                if indicator in normalized:
                    return TaintHit(indicator, entry.source, entry.tool_name, "indicator")
            for fragment in entry.fragments:
                if fragment in normalized:
                    return TaintHit(fragment, entry.source, entry.tool_name, "fragment")
        return None

    def is_tainted(self, text: str) -> bool:
        return self.inspect(text) is not None

    def hits(self, text: str, *, limit: int = 5) -> list[TaintHit]:
        """All matches, for audit detail."""
        if not text:
            return []
        normalized = _normalize(text)
        found: list[TaintHit] = []
        for entry in self._entries.values():
            for indicator in entry.indicators:
                if indicator in normalized:
                    found.append(TaintHit(indicator, entry.source, entry.tool_name, "indicator"))
                    break
            else:
                for fragment in entry.fragments:
                    if fragment in normalized:
                        found.append(TaintHit(fragment, entry.source, entry.tool_name, "fragment"))
                        break
            if len(found) >= limit:
                break
        return found

    # -- housekeeping ------------------------------------------------------

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[TaintEntry]:
        return iter(self._entries.values())

    def snapshot(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._entries.values()]

    def sources(self) -> list[str]:
        return sorted({entry.source for entry in self._entries.values()})


_DEFAULT_REGISTRY: TaintRegistry | None = None


def get_registry() -> TaintRegistry:
    """Process-wide registry used by the tool chokepoint."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = TaintRegistry()
    return _DEFAULT_REGISTRY


def reset_registry() -> None:
    """Drop the process-wide registry (session end, tests)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
