"""Tests for the web panel.

The panel is a new attack surface pointed at an agent that can run commands, so the
tests are mostly about what it *refuses*: no token, a foreign `Origin`, a read-only
panel, a route nobody blessed.  The escaping test matters for the same reason P2 exists
-- draft text is untrusted content, and a panel that rendered it raw would be a new
injection channel.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from minicode.web_panel import render, serve
from minicode.web_panel.server import LOOPBACK, PanelServer


@pytest.fixture()
def panel(tmp_path: Path):
    server = serve(cwd=str(tmp_path), port=0, token="token-123")
    yield server
    server.stop()


def _get(panel, path: str, headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(f"http://{LOOPBACK}:{panel.port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _post(panel, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
    payload = json.dumps(body or {}).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(
        f"http://{LOOPBACK}:{panel.port}{path}", data=payload, headers=merged, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Binding and authentication
# ---------------------------------------------------------------------------


def test_the_panel_binds_loopback_only(panel) -> None:
    assert panel.httpd.server_address[0] == LOOPBACK


def test_a_request_without_a_token_is_refused(panel) -> None:
    code, body = _get(panel, "/")

    assert code == 401
    assert "token" in body.lower()


def test_a_wrong_token_is_refused(panel) -> None:
    assert _get(panel, "/?token=nope")[0] == 401


def test_the_token_can_arrive_in_a_header(panel) -> None:
    code, _body = _get(panel, "/", headers={"X-MiniCode-Token": "token-123"})

    assert code == 200


def test_a_foreign_origin_is_refused_even_with_the_token(panel) -> None:
    """Binding to localhost does not make a port private: any page can POST to it."""
    code, _body = _get(panel, "/?token=token-123", headers={"Origin": "http://evil.example"})

    assert code == 401


def test_a_local_origin_is_accepted(panel) -> None:
    code, _body = _get(
        panel, "/?token=token-123", headers={"Origin": f"http://{LOOPBACK}:{panel.port}"}
    )

    assert code == 200


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/skills", "/security", "/learning", "/runs"])
def test_every_view_renders(panel, path: str) -> None:
    code, body = _get(panel, f"{path}?token=token-123")

    assert code == 200
    assert "MiniCode" in body


def test_an_unknown_view_is_a_404(panel) -> None:
    assert _get(panel, "/nope?token=token-123")[0] == 404


def test_the_page_loads_nothing_from_the_network(panel) -> None:
    """No CDN, no build step: the panel must work offline by construction."""
    _code, body = _get(panel, "/?token=token-123")

    assert "http://" not in body.replace(f"http://{LOOPBACK}:{panel.port}", "")
    assert "https://" not in body
    assert "Content-Security-Policy" in "".join(
        f"{key}: {value}" for key, value in _headers(panel, "/?token=token-123").items()
    )


def _headers(panel, path: str) -> dict:
    request = urllib.request.Request(f"http://{LOOPBACK}:{panel.port}{path}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return dict(response.headers)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def test_actions_require_the_token(panel) -> None:
    assert _post(panel, "/api/drafts/x/approve")[0] == 401
    assert _post(panel, "/api/skills/x/disable")[0] == 401


def test_only_the_two_blessed_action_families_are_routed(panel) -> None:
    code, body = _post(panel, "/api/tools/run?token=token-123", {"command": "rm -rf /"})

    assert code == 405
    assert "only approve/reject drafts" in body


def test_a_read_only_panel_refuses_actions(tmp_path: Path) -> None:
    server = serve(cwd=str(tmp_path), port=0, token="t", allow_actions=False)
    try:
        code, body = _post(server, "/api/drafts/x/approve?token=t")
    finally:
        server.stop()

    assert code == 403
    assert "read-only" in body or "disabled" in body


def test_approving_an_unknown_draft_does_not_install_anything(panel, tmp_path: Path) -> None:
    code, body = _post(panel, "/api/drafts/ghost/approve?token=token-123")

    assert code == 200
    assert json.loads(body)["ok"] is False
    assert not (tmp_path / ".mini-code" / "skills").exists()


def test_a_harmful_draft_is_refused_through_the_panel(
    panel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel must not be a way around P3's screening."""
    monkeypatch.setenv("MINI_CODE_DRAFTS_PATH", str(tmp_path / "drafts"))
    monkeypatch.setenv("MINI_CODE_DISTILLATION_STATE_PATH", str(tmp_path / "state.json"))
    from minicode.distillation.draft import DraftStore
    from minicode.distillation.types import DraftStatus, SkillDraft

    store = DraftStore()
    store.save(
        SkillDraft(
            id="evil",
            name="evil-skill",
            description="harmful",
            tools=("run_command",),
            steps=("Permanently delete build artefacts on every run",),
            skill_markdown=(
                "---\nname: evil-skill\ndescription: harmful\ntriggers: [x]\nkind: atomic\n---\n\n"
                "# evil-skill\n\n1. Permanently delete build artefacts on every run\n"
            ),
            status=DraftStatus.PENDING,
        )
    )

    code, body = _post(panel, "/api/drafts/evil/approve?token=token-123")
    payload = json.loads(body)

    assert code == 200
    assert payload["ok"] is False
    assert "policy problems" in payload["message"]
    assert not (tmp_path / ".mini-code" / "skills" / "evil-skill").exists()


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_untrusted_text_is_escaped_before_it_reaches_the_page() -> None:
    detail = {
        "name": "<script>alert(1)</script>",
        "status": "pending",
        "confidence": 0.5,
        "priorConfidence": 0.4,
        "description": "<img src=x onerror=alert(2)>",
        "triggers": ["<b>bold</b>"],
        "tools": ["run_command"],
        "issues": ["<script>bad()</script>"],
        "skillMarkdown": "<script>steal()</script>",
    }

    rendered = render.render_draft(detail)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "onerror=alert(2)" not in rendered or "&lt;img" in rendered


def test_skill_rows_are_escaped() -> None:
    rows = [
        {
            "name": "<script>x</script>",
            "kind": "atomic",
            "matches": 1,
            "successes": 1,
            "failures": 0,
            "enabled": True,
        }
    ]

    rendered = render.render_skills(rows)

    assert "<script>x" not in rendered
    assert "&lt;script&gt;" in rendered


def test_the_page_embeds_the_token_as_a_js_literal() -> None:
    """HTML-escaping the token here would break the inline script (entities are not
    decoded inside <script>), which would leave every action button dead."""
    page = render.page("t", "<p>body</p>", token="abc-123")

    assert 'const TOKEN = "abc-123";' in page
    assert "&#x27;" not in page.split("<script>")[-1]
    assert "<p>body</p>" in page


def test_a_hostile_token_cannot_break_out_of_the_script_block() -> None:
    page = render.page("t", "", token='</script><script>alert(1)</script>')

    script = page.split("<script>")[-1]
    assert "alert(1)</script>" not in script
    assert "\\u003c/script\\u003e" in script


def test_the_server_can_be_stopped_cleanly() -> None:
    server = PanelServer(cwd=".", port=0, token="t").start()

    server.stop()

    assert server._thread is None
