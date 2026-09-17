"""Tests for L3 domain packs (``minicode/skill_system/domain_pack.py``).

Domain packs exist to be injected on demand, so the two properties under test are
*selection* (the right pack for a query, with explainable reasons) and
*boundedness* (an injection never exceeds its character budget, and a broken pack
degrades to no injection rather than a broken turn).
"""

from __future__ import annotations

import json
from pathlib import Path

from minicode.skill_system.domain_pack import (
    KNOWLEDGE_FILENAME,
    MANIFEST_FILENAME,
    discover_packs,
    format_index,
    format_pack,
    inject_context,
    parse_pack,
    render_injection,
    select_packs,
)


def _write_pack(
    root: Path,
    name: str,
    *,
    description: str = "Knowledge about a topic",
    labels: list[str] | None = None,
    knowledge: str = "# Knowledge\n\nThe details live here.\n",
    examples: list[dict] | None = None,
    manifest: dict | None = None,
    source_dir: str = ".mini-code/packs",
) -> Path:
    pack_dir = root / source_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload = manifest if manifest is not None else {
        "name": name,
        "version": "1.0.0",
        "description": description,
        "labels": labels if labels is not None else ["topic"],
    }
    (pack_dir / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    if knowledge is not None:
        (pack_dir / KNOWLEDGE_FILENAME).write_text(knowledge, encoding="utf-8")
    if examples:
        (pack_dir / "examples.jsonl").write_text(
            "\n".join(json.dumps(example) for example in examples) + "\n", encoding="utf-8"
        )
    return pack_dir


def _roots(tmp_path: Path) -> list[tuple[Path, str]]:
    return [
        (tmp_path / ".mini-code" / "packs", "project"),
        (tmp_path / "home" / ".mini-code" / "packs", "user"),
    ]


# ---------------------------------------------------------------------------
# Discovery and parsing
# ---------------------------------------------------------------------------


def test_pack_is_parsed_with_metadata_knowledge_and_examples(tmp_path: Path) -> None:
    pack_dir = _write_pack(
        tmp_path,
        "python-packaging",
        description="Cutting a release",
        labels=["release", "发版"],
        examples=[{"input": "how do I tag?", "output": "git tag -a v1.0.0", "note": "signed"}],
    )

    pack = parse_pack(pack_dir, "project")

    assert pack.name == "python-packaging"
    assert pack.version == "1.0.0"
    assert pack.labels == ("release", "发版")
    assert pack.knowledge.startswith("# Knowledge")
    assert len(pack.examples) == 1
    assert pack.examples[0].output == "git tag -a v1.0.0"
    assert pack.issues == ()


def test_missing_manifest_and_knowledge_are_reported_not_fatal(tmp_path: Path) -> None:
    pack_dir = tmp_path / "bare"
    pack_dir.mkdir()

    pack = parse_pack(pack_dir, "project")

    assert pack.name == "bare"
    assert pack.knowledge == ""
    assert any(MANIFEST_FILENAME in issue for issue in pack.issues)
    assert any(KNOWLEDGE_FILENAME in issue for issue in pack.issues)
    assert any("no description" in issue for issue in pack.issues)


def test_malformed_manifest_is_reported(tmp_path: Path) -> None:
    pack_dir = tmp_path / ".mini-code" / "packs" / "broken"
    pack_dir.mkdir(parents=True)
    (pack_dir / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")

    pack = parse_pack(pack_dir, "project")

    assert any("could not be read" in issue for issue in pack.issues)
    assert pack.name == "broken"


def test_non_object_manifest_is_reported(tmp_path: Path) -> None:
    pack_dir = tmp_path / ".mini-code" / "packs" / "listy"
    pack_dir.mkdir(parents=True)
    (pack_dir / MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

    pack = parse_pack(pack_dir, "project")

    assert any("must contain a JSON object" in issue for issue in pack.issues)


def test_bad_example_lines_are_skipped_and_reported(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path, "examples", examples=[{"input": "a", "output": "b"}])
    (pack_dir / "examples.jsonl").write_text(
        '{"input": "ok", "output": "fine"}\nnot json\n["not", "an", "object"]\n',
        encoding="utf-8",
    )

    pack = parse_pack(pack_dir, "project")

    assert len(pack.examples) == 1
    assert any("line 2" in issue for issue in pack.issues)
    assert any("line 3" in issue for issue in pack.issues)


def test_missing_examples_file_is_fine(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path, "no-examples")

    assert parse_pack(pack_dir, "project").examples == []


def test_discovery_prefers_project_and_sorts_by_name(tmp_path: Path) -> None:
    _write_pack(tmp_path, "zeta", source_dir=".mini-code/packs")
    _write_pack(tmp_path, "alpha", source_dir=".mini-code/packs")
    _write_pack(tmp_path, "alpha", description="User copy", source_dir="home/.mini-code/packs")

    packs = discover_packs(tmp_path, roots=_roots(tmp_path))

    assert [pack.name for pack in packs] == ["alpha", "zeta"]
    assert packs[0].description != "User copy"
    assert packs[0].source == "project"


def test_discovery_tolerates_absent_roots(tmp_path: Path) -> None:
    assert discover_packs(tmp_path, roots=[(tmp_path / "nope", "project")]) == []


def test_files_in_the_root_are_ignored(tmp_path: Path) -> None:
    (tmp_path / ".mini-code" / "packs").mkdir(parents=True)
    (tmp_path / ".mini-code" / "packs" / "README.md").write_text("not a pack", encoding="utf-8")

    assert discover_packs(tmp_path, roots=_roots(tmp_path)) == []


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _packs(tmp_path: Path):
    _write_pack(
        tmp_path,
        "release-pack",
        description="Publish a Python package",
        labels=["release", "publish package", "发版流程"],
    )
    _write_pack(
        tmp_path,
        "review-pack",
        description="Review a diff",
        labels=["code review", "代码评审"],
    )
    return discover_packs(tmp_path, roots=_roots(tmp_path))


def test_label_phrase_wins(tmp_path: Path) -> None:
    matches = select_packs(_packs(tmp_path), "I want to publish package today")

    assert matches
    assert matches[0].pack.name == "release-pack"
    assert any("label phrase" in reason for reason in matches[0].reasons)


def test_chinese_labels_match_by_bigram(tmp_path: Path) -> None:
    matches = select_packs(_packs(tmp_path), "帮我做代码评审")

    assert matches
    assert matches[0].pack.name == "review-pack"


def test_unrelated_query_selects_nothing(tmp_path: Path) -> None:
    assert select_packs(_packs(tmp_path), "what is the weather") == []
    assert select_packs(_packs(tmp_path), "") == []


def test_selection_respects_limit(tmp_path: Path) -> None:
    matches = select_packs(_packs(tmp_path), "code review and release", limit=1)

    assert len(matches) == 1


def test_selection_is_deterministic(tmp_path: Path) -> None:
    packs = _packs(tmp_path)

    first = [match.pack.name for match in select_packs(packs, "review")]
    second = [match.pack.name for match in select_packs(packs, "review")]

    assert first == second


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def test_render_injection_contains_labels_and_knowledge(tmp_path: Path) -> None:
    pack = parse_pack(_write_pack(tmp_path, "p", labels=["alpha", "beta"]), "project")

    rendered = render_injection(pack)

    assert "## Domain knowledge: p" in rendered
    assert "Labels: alpha, beta" in rendered
    assert "The details live here." in rendered


def test_render_injection_fits_the_budget(tmp_path: Path) -> None:
    pack = parse_pack(
        _write_pack(tmp_path, "big", knowledge="x" * 5000, examples=[{"input": "i", "output": "o"}]),
        "project",
    )

    rendered = render_injection(pack, max_chars=400)

    assert "[knowledge truncated]" in rendered
    assert len(rendered) < 600


def test_render_injection_includes_examples_when_there_is_room(tmp_path: Path) -> None:
    pack = parse_pack(
        _write_pack(
            tmp_path,
            "with-examples",
            knowledge="short",
            examples=[{"input": "how?", "output": "like this", "note": "n"}],
        ),
        "project",
    )

    rendered = render_injection(pack, max_chars=800)

    assert "- Example: how?" in rendered
    assert "-> like this" in rendered
    assert "note: n" in rendered


def test_pack_without_knowledge_renders_header_only(tmp_path: Path) -> None:
    pack_dir = tmp_path / "empty-pack"
    pack_dir.mkdir()
    (pack_dir / MANIFEST_FILENAME).write_text(json.dumps({"name": "empty-pack", "labels": ["x"]}), encoding="utf-8")

    rendered = render_injection(parse_pack(pack_dir, "project"))

    assert rendered.startswith("## Domain knowledge: empty-pack")
    assert "Example" not in rendered


def test_inject_context_appends_the_matching_pack(tmp_path: Path) -> None:
    _packs(tmp_path)

    combined = inject_context(
        tmp_path, "publish package", "## Relevant Context from Memory\n\n1. something",
        extra_roots=None,
    )

    assert combined.startswith("## Relevant Context from Memory")
    assert "## Domain knowledge: release-pack" in combined


def test_inject_context_returns_base_unchanged_without_a_match(tmp_path: Path) -> None:
    _packs(tmp_path)

    assert inject_context(tmp_path, "unrelated question", "BASE") == "BASE"


def test_inject_context_handles_empty_base(tmp_path: Path) -> None:
    _packs(tmp_path)

    combined = inject_context(tmp_path, "code review", "")

    assert combined.startswith("## Domain knowledge: review-pack")


def test_inject_context_survives_a_broken_pack_directory(tmp_path: Path) -> None:
    pack_dir = tmp_path / ".mini-code" / "packs" / "broken"
    pack_dir.mkdir(parents=True)
    (pack_dir / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    (pack_dir / KNOWLEDGE_FILENAME).mkdir()  # a directory where a file is expected

    # Must not raise; the broken pack simply contributes nothing injectable.
    result = inject_context(tmp_path, "broken", "BASE")

    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_format_index_lists_packs(tmp_path: Path) -> None:
    rendered = format_index(_packs(tmp_path))

    assert "Domain packs (2 discovered)" in rendered
    assert "- release-pack v1.0.0 (project)" in rendered
    assert "labels: release, publish package, 发版流程" in rendered


def test_format_index_handles_no_packs() -> None:
    assert "No domain packs discovered" in format_index([])


def test_format_pack_reports_details_and_examples(tmp_path: Path) -> None:
    pack = parse_pack(
        _write_pack(tmp_path, "p", examples=[{"input": "q", "output": "a"}]), "project"
    )

    rendered = format_pack(pack)

    assert "Domain pack: p" in rendered
    assert "Examples: 1" in rendered
    assert "Issues: none" in rendered
    assert "- q" in rendered
