"""The panel's HTTP surface: localhost only, token-gated, read-only by default.

Three constraints, each with a reason rather than a preference.

**It binds 127.0.0.1.**  A panel that reaches the network is a remote control for an
agent that can run commands, and nothing in this project needs that.

**It requires a token, and rejects a foreign `Origin`.**  Binding to localhost does not
make a port private: any page in any browser on this machine can POST to it.  The token
(printed once at startup) is what turns "reachable" into "mine", and the `Origin` check
closes the browser-based path.

**Writes are two families, and they are the gates' writes, not the panel's.**  Approving
a draft calls P3's `review`, so the screening that is not switchable off still runs;
enabling a skill calls P1's lifecycle.  Every other method or path is refused, which
means there is no way to reach the agent's tools from a browser.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from minicode.web_panel import api, render

__all__ = ["PanelServer", "build_handler", "serve"]

LOOPBACK = "127.0.0.1"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _action_routes() -> dict[tuple[str, str], Callable[..., dict[str, Any]]]:
    return {
        ("POST", "/approve"): api.approve_draft,
        ("POST", "/reject"): api.reject_draft,
        ("POST", "/enable"): api.enable_skill,
        ("POST", "/disable"): api.disable_skill,
    }


def build_handler(*, token: str, cwd: str, allow_actions: bool = True):
    """Build the request handler class (a class, because ``http.server`` wants one)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniCodePanel/1.0"

        # -- helpers -------------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            # Quiet by default: the panel is a window, not a log sink.
            return

        def _authorised(self) -> bool:
            parsed = urlparse(self.path)
            supplied = (parse_qs(parsed.query).get("token") or [""])[0]
            header = self.headers.get("X-MiniCode-Token", "")
            origin = self.headers.get("Origin", "")
            if origin:
                host = urlparse(origin).hostname or ""
                if host not in _LOCAL_HOSTS:
                    return False
            return bool(token) and supplied == token or header == token

        def _send(self, status: int, body: str, *, content_type: str = "text/html; charset=utf-8") -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            # No external resources, so nothing needs to be allowed to load.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False), content_type="application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        # -- routing -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            if not self._authorised():
                self._send(401, render.page("unauthorised", "<p>Missing or wrong token.</p>"))
                return
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            page_token = (query.get("token") or [""])[0]

            if route == "/":
                payload = api.overview(cwd)
                payload["token"] = page_token
                self._send(200, render.page("overview", render.render_overview(payload, api.drafts(), api.runs()), token=page_token))
            elif route == "/skills":
                self._send(200, render.page("skills", render.render_skills(api.skills_view(cwd), token=page_token), token=page_token))
            elif route == "/security":
                view = api.security_view()
                rows = [(render.escape(r["decision"]), render.escape(r["risk"]), render.escape(r["ruleId"]), render.escape(r["tool"]), render.escape(r["reason"][:90])) for r in view["recent"]]
                body = f"<p>mode <b>{render.escape(view['mode'])}</b> &middot; {view['total']} decision(s) in <code>{render.escape(view['auditPath'])}</code></p>"
                self._send(200, render.page("security", body + render._table(["decision", "risk", "rule", "tool", "reason"], rows), token=page_token))
            elif route == "/learning":
                view = api.trajectories_view()
                body = render._table(
                    ["corpus", "value"],
                    [
                        ("turns recorded", str(view["turns"])),
                        ("synthetic", str(view["synthetic"])),
                        ("corrections detected", str(view["corrections"])),
                        ("failures", str(view["failures"])),
                    ],
                )
                self._send(200, render.page("learning", body, token=page_token))
            elif route == "/runs":
                self._send(200, render.page("orchestration", render.render_runs(api.runs()), token=page_token))
            elif route == "/draft":
                detail = api.draft_detail((query.get("id") or [""])[0])
                if detail is None:
                    self._send(404, render.page("not found", "<p>No such draft.</p>", token=page_token))
                else:
                    self._send(200, render.page(f"draft {detail['name']}", render.render_draft(detail), token=page_token))
            elif route == "/api/overview":
                self._json(200, api.overview(cwd))
            else:
                self._send(404, render.page("not found", "<p>No such view.</p>", token=page_token))

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            if not self._authorised():
                self._json(401, {"ok": False, "message": "missing or wrong token"})
                return
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/")
            if not allow_actions:
                self._json(403, {"ok": False, "message": "actions are disabled for this panel"})
                return
            body = self._body()

            if route.startswith("/api/drafts/") and route.count("/") == 4:
                _, _, _, draft_id, verb = route.split("/")
                if verb == "approve":
                    self._json(200, api.approve_draft(draft_id, cwd=cwd))
                    return
                if verb == "reject":
                    self._json(200, api.reject_draft(draft_id, str(body.get("reason") or "")))
                    return
            if route.startswith("/api/skills/") and route.count("/") == 4:
                _, _, _, name, verb = route.split("/")
                if verb == "enable":
                    self._json(200, api.enable_skill(name, enabled=True))
                    return
                if verb == "disable":
                    self._json(200, api.disable_skill(name))
                    return
            self._json(405, {"ok": False, "message": "only approve/reject drafts and enable/disable skills are allowed"})

    return Handler


class PanelServer:
    """A running panel: the server, the URL to open, and a clean shutdown."""

    def __init__(self, *, cwd: str = ".", port: int = 0, token: str = "", allow_actions: bool = True) -> None:
        self.token = token or secrets.token_urlsafe(16)
        self.cwd = str(Path(cwd).resolve())
        self.allow_actions = allow_actions
        handler = build_handler(token=self.token, cwd=self.cwd, allow_actions=allow_actions)
        self.httpd = ThreadingHTTPServer((LOOPBACK, port), handler)
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}/?token={self.token}"

    def start(self) -> "PanelServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def serve(*, cwd: str = ".", port: int = 0, token: str = "", allow_actions: bool = True) -> PanelServer:
    """Start a panel and return it (the caller decides when to stop it)."""
    return PanelServer(cwd=cwd, port=port, token=token, allow_actions=allow_actions).start()
