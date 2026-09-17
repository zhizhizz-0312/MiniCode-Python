"""Skill hit-rate and success-rate statistics.

This is the observability half of the L1 layer, and the demand signal the P3
distillation loop is supposed to learn from: which skills actually get matched
for real queries, which of those get used, and which ones work when used.

Counters live in ``~/.mini-code/skill-stats.json`` and are written with the same
tolerant-read discipline as the lifecycle state -- a corrupt statistics file must
never interrupt a session.  Recording is deliberately cheap and silent: these
functions are called from the matching path, so they swallow every error.

Retirement is *reported*, never automatic.  ``retire_candidates`` names the
skills that look wasteful and says why; actually disabling one stays an explicit
``/skills disable`` decision, because silently deleting a capability the user may
still need is worse than carrying an idle one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from minicode.config import MINI_CODE_DIR

__all__ = [
    "STATS_FILENAME",
    "DISABLE_ENV",
    "SkillStat",
    "SkillStatsBook",
    "stats_path",
    "load_stats",
    "record_match",
    "record_run",
    "format_report",
    "retire_candidates",
]

STATS_FILENAME = "skill-stats.json"
DISABLE_ENV = "MINI_CODE_SKILL_STATS_DISABLED"
_MAX_QUERY_CHARS = 120


def stats_path(path: str | Path | None = None) -> Path:
    """Resolve the statistics file location (``~/.mini-code/skill-stats.json``)."""
    return Path(path) if path is not None else MINI_CODE_DIR / STATS_FILENAME


@dataclass
class SkillStat:
    """Counters for one skill."""

    matches: int = 0
    runs: int = 0
    successes: int = 0
    failures: int = 0
    last_matched_at: float = 0.0
    last_run_at: float = 0.0
    last_query: str = ""

    @property
    def hit_rate(self) -> float:
        """Fraction of runs that succeeded (``0.0`` when never run)."""
        return self.successes / self.runs if self.runs else 0.0

    @property
    def conversion(self) -> float:
        """Fraction of matches that turned into an actual run."""
        return self.runs / self.matches if self.matches else 0.0

    def to_dict(self) -> dict:
        return {
            "matches": self.matches,
            "runs": self.runs,
            "successes": self.successes,
            "failures": self.failures,
            "lastMatchedAt": self.last_matched_at,
            "lastRunAt": self.last_run_at,
            "lastQuery": self.last_query,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SkillStat":
        if not isinstance(payload, dict):
            return cls()

        def _count(key: str) -> int:
            value = payload.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        def _stamp(key: str) -> float:
            value = payload.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        return cls(
            matches=_count("matches"),
            runs=_count("runs"),
            successes=_count("successes"),
            failures=_count("failures"),
            last_matched_at=_stamp("lastMatchedAt"),
            last_run_at=_stamp("lastRunAt"),
            last_query=str(payload.get("lastQuery") or ""),
        )


@dataclass
class SkillStatsBook:
    """The whole statistics file: per-skill counters plus a schema version."""

    skills: dict[str, SkillStat] = field(default_factory=dict)
    updated_at: float = 0.0

    def stat_for(self, name: str) -> SkillStat:
        """Get (creating on demand) the counters for ``name``."""
        return self.skills.setdefault(name, SkillStat())

    def to_dict(self) -> dict:
        return {
            "schemaVersion": 1,
            "updatedAt": self.updated_at,
            "skills": {name: stat.to_dict() for name, stat in sorted(self.skills.items())},
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SkillStatsBook":
        if not isinstance(payload, dict):
            return cls()
        raw_skills = payload.get("skills")
        skills: dict[str, SkillStat] = {}
        if isinstance(raw_skills, dict):
            for name, stat in raw_skills.items():
                skills[str(name)] = SkillStat.from_dict(stat)
        raw_updated = payload.get("updatedAt")
        updated_at = float(raw_updated) if isinstance(raw_updated, (int, float)) else 0.0
        return cls(skills=skills, updated_at=updated_at)

    # -- mutation ----------------------------------------------------------

    def record_match(self, name: str, query: str = "") -> SkillStat:
        stat = self.stat_for(name.strip())
        stat.matches += 1
        stat.last_matched_at = time.time()
        stat.last_query = query.strip()[:_MAX_QUERY_CHARS]
        return stat

    def record_run(self, name: str, success: bool) -> SkillStat:
        stat = self.stat_for(name.strip())
        stat.runs += 1
        if success:
            stat.successes += 1
        else:
            stat.failures += 1
        stat.last_run_at = time.time()
        return stat

    # -- io ----------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        target = stats_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time.time()
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SkillStatsBook":
        target = stats_path(path)
        if not target.exists():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return cls()
        return cls.from_dict(payload)


# ---------------------------------------------------------------------------
# Convenience API used from the matching path (never raises)
# ---------------------------------------------------------------------------


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in {"", "0", "false", "False"}


def load_stats(path: str | Path | None = None) -> SkillStatsBook:
    return SkillStatsBook.load(path)


def record_match(name: str, query: str = "", *, path: str | Path | None = None) -> None:
    """Record that ``name`` was surfaced for ``query``; never raises."""
    if _disabled() or not name.strip():
        return
    try:
        book = SkillStatsBook.load(path)
        book.record_match(name, query)
        book.save(path)
    except Exception:  # noqa: BLE001 - statistics must not break a session
        return


def record_run(name: str, success: bool, *, path: str | Path | None = None) -> None:
    """Record that ``name`` was used and whether it worked; never raises."""
    if _disabled() or not name.strip():
        return
    try:
        book = SkillStatsBook.load(path)
        book.record_run(name, success)
        book.save(path)
    except Exception:  # noqa: BLE001
        return


def format_report(
    book: SkillStatsBook | None = None,
    *,
    path: str | Path | None = None,
) -> str:
    """The hit-rate report promised by the design document."""
    stats = book if book is not None else SkillStatsBook.load(path)
    if not stats.skills:
        return (
            "Skill statistics\n"
            + "=" * 60
            + "\n"
            + "No skill has been matched or run yet.\n"
            + f"Counters are written to {stats_path(path)}."
        )

    totals = {
        "matches": sum(stat.matches for stat in stats.skills.values()),
        "runs": sum(stat.runs for stat in stats.skills.values()),
        "successes": sum(stat.successes for stat in stats.skills.values()),
        "failures": sum(stat.failures for stat in stats.skills.values()),
    }
    lines = [
        "Skill statistics",
        "=" * 60,
        f"Tracked skills: {len(stats.skills)}",
        f"Totals: {totals['matches']} match(es), {totals['runs']} run(s), "
        f"{totals['successes']} success(es), {totals['failures']} failure(s)",
        "",
    ]
    ordered = sorted(
        stats.skills.items(),
        key=lambda item: (-item[1].matches, -item[1].runs, item[0]),
    )
    for name, stat in ordered:
        parts = [
            f"- {name}: matches={stat.matches}",
            f"runs={stat.runs}",
            f"success={stat.successes}",
            f"failure={stat.failures}",
        ]
        if stat.runs:
            parts.append(f"success_rate={stat.hit_rate:.0%}")
        if stat.matches:
            parts.append(f"conversion={stat.conversion:.0%}")
        lines.append(" ".join(parts))
        if stat.last_query:
            lines.append(f"    last query: {stat.last_query}")

    candidates = retire_candidates(stats)
    lines.append("")
    if candidates:
        lines.append("Retire candidates:")
        for candidate in candidates:
            lines.append(f"  - {candidate['name']}: {candidate['reason']}")
    else:
        lines.append("Retire candidates: none")
    return "\n".join(lines)


def retire_candidates(
    book: SkillStatsBook | None = None,
    *,
    path: str | Path | None = None,
    min_matches: int = 5,
    max_conversion: float = 0.1,
    min_runs: int = 3,
    max_success_rate: float = 0.5,
) -> list[dict[str, object]]:
    """Name skills that look wasteful, with the evidence.

    Two independent signals: a skill surfaced repeatedly but almost never used is
    dead weight; a skill that is used but usually fails is actively unhelpful.
    Thresholds are arguments so the report can be tuned without editing code.
    """
    stats = book if book is not None else SkillStatsBook.load(path)
    candidates: list[dict[str, object]] = []
    for name, stat in sorted(stats.skills.items()):
        if stat.matches >= min_matches and stat.conversion <= max_conversion:
            candidates.append(
                {
                    "name": name,
                    "reason": (
                        f"matched {stat.matches}x but used {stat.runs}x "
                        f"(conversion {stat.conversion:.0%}); triggers may be too broad"
                    ),
                    "signal": "unconverted",
                }
            )
            continue
        if stat.runs >= min_runs and stat.hit_rate < max_success_rate:
            candidates.append(
                {
                    "name": name,
                    "reason": (
                        f"ran {stat.runs}x with {stat.hit_rate:.0%} success "
                        f"({stat.failures} failure(s)); consider revising or disabling"
                    ),
                    "signal": "unreliable",
                }
            )
    return candidates
