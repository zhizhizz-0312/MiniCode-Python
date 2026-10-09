"""One hand-written page, no framework and no external resources.

Two constraints shape this file.  The repository has no runtime dependencies, so the
page is inline HTML/CSS/JS served from the standard library -- no CDN, no build step.
And the text it displays is **untrusted**: draft bodies and skill descriptions are
content that came from documents and sessions, which is exactly what P2 exists to
distrust.  Everything is escaped, and nothing shown here is ever fed back to the model,
so the panel cannot become an injection channel of its own.

The actions are a handful of `fetch` calls carrying the URL's token.  There is no state
in the page: it re-reads the same artefacts the CLI reads.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

__all__ = ["escape", "page", "render_draft", "render_overview", "render_runs", "render_skills"]

_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       margin: 0; padding: 24px; max-width: 1100px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 24px 0 8px; text-transform: uppercase; letter-spacing: .04em; opacity: .7; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #8884; vertical-align: top; }
.badge { padding: 1px 6px; border: 1px solid #8886; border-radius: 10px; font-size: 12px; }
.done { color: #2e7d32; } .deny, .failed { color: #c62828; } .esc { color: #ef6c00; }
.muted { opacity: .65; } .card { border: 1px solid #8884; border-radius: 8px; padding: 12px; margin: 8px 0; }
pre { white-space: pre-wrap; word-break: break-word; background: #8881; padding: 12px; border-radius: 6px; }
button { font: inherit; padding: 3px 10px; cursor: pointer; }
nav a { margin-right: 12px; }
"""

_SCRIPT = """
function act(path, body, note) {
  const url = path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json',
             'X-MiniCode-Token': TOKEN}, body: JSON.stringify(body || {})})
    .then(r => r.json())
    .then(d => { document.getElementById('note').textContent = (note || '') + ' ' + (d.message || JSON.stringify(d));
                 if (d.ok) { setTimeout(() => location.reload(), 800); } })
    .catch(e => { document.getElementById('note').textContent = 'request failed: ' + e; });
}
"""


def _js_literal(value: str) -> str:
    """A JavaScript string literal that is safe to inline inside ``<script>``.

    ``json.dumps`` gets the quoting right, and escaping the angle brackets stops data
    containing ``</script>`` from ending the block early.  HTML-escaping here would be
    *wrong*: entities are not decoded inside a script element, so ``&#x27;abc&#x27;``
    would be a syntax error rather than a string -- which would have left every action
    button on the page dead.
    """
    import json

    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def page(title: str, body: str, *, token: str = "") -> str:
    """Wrap content in the single page shell.

    The token is embedded so the inline script can send it.  It goes in as a JS literal
    (see :func:`_js_literal`) rather than as escaped HTML, and it is the only piece of
    data the script is given.
    """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{_STYLE}</style></head>
<body>
<h1>MiniCode &mdash; {escape(title)}</h1>
<div class="muted">local only &middot; read-only by default &middot; actions go through the same gates as the CLI</div>
<nav><a href="/?token={escape(token)}">overview</a><a href="/skills?token={escape(token)}">skills</a>
<a href="/security?token={escape(token)}">security</a><a href="/learning?token={escape(token)}">learning</a>
<a href="/runs?token={escape(token)}">orchestration</a></nav>
<div id="note" class="muted"></div>
{body}
<script>const TOKEN = {_js_literal(token)};{_SCRIPT}</script>
</body></html>
"""


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    head = "".join(f"<th>{escape(str(item))}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_overview(payload: dict[str, Any], drafts: list[dict[str, Any]], runs: list[dict[str, Any]]) -> str:
    skills = payload.get("skills", {})
    security = payload.get("security", {})
    learning = payload.get("learning", {})
    orchestration = payload.get("orchestration", {})
    parts = [
        '<h2>stages</h2>',
        _table(
            ["stage", "state"],
            [
                ("skills (P1)", f"{skills.get('total', 0)} total, {skills.get('composites', 0)} composite(s)"),
                ("security (P2)", f"mode {escape(str(security.get('mode')))} &middot; {security.get('decisions', 0)} decision(s), {security.get('denied', 0)} denied"),
                ("learning (P3)", f"{learning.get('drafts', 0)} draft(s), {learning.get('pending', 0)} awaiting review, ${learning.get('spentUsd', 0):.4f} spent"),
                ("orchestration (P4)", f"{orchestration.get('runs', 0)} run(s)" + (f", latest {escape(str(orchestration.get('latest')))}" if orchestration.get("latest") else "")),
            ],
        ),
    ]
    if drafts:
        parts.append("<h2>drafts awaiting review</h2>")
        parts.append(
            _table(
                ["draft", "confidence", "from", "action"],
                [
                    (
                        f"<a href=\"/draft?id={escape(d['id'])}&token={escape(payload.get('token', ''))}\">{escape(d['name'])}</a>",
                        f"{d['confidence']:.2f} <span class=\"muted\">(evidence {d['priorConfidence']:.2f})</span>",
                        f"{d['sources']} session(s)",
                        f"<button onclick=\"act('/api/drafts/{escape(d['id'])}/approve', {{}}, 'approve:')\">approve</button> "
                        f"<button onclick=\"act('/api/drafts/{escape(d['id'])}/reject', {{reason: 'rejected from the panel'}}, 'reject:')\">reject</button>",
                    )
                    for d in drafts
                ],
            )
        )
    if runs:
        parts.append("<h2>orchestrations</h2>")
        parts.append(render_runs(runs))
    return "\n".join(parts)


def render_skills(rows: list[dict[str, Any]], *, token: str = "") -> str:
    return _table(
        ["name", "kind", "uses", "outcome", "state", "action"],
        [
            (
                escape(row["name"]),
                escape(row["kind"]),
                str(row["matches"]),
                f"{row['successes']} ok / {row['failures']} failed",
                "enabled" if row["enabled"] else "disabled",
                (
                    f"<button onclick=\"act('/api/skills/{escape(row['name'])}/disable', {{}}, 'disable:')\">disable</button>"
                    if row["enabled"]
                    else f"<button onclick=\"act('/api/skills/{escape(row['name'])}/enable', {{}}, 'enable:')\">enable</button>"
                ),
            )
            for row in rows
        ],
    )


def render_draft(detail: dict[str, Any]) -> str:
    issues = "".join(f"<li>{escape(str(item))}</li>" for item in detail.get("issues") or ())
    return "\n".join(
        [
            f"<h2>{escape(str(detail.get('name', '')))}</h2>",
            '<div class="card">',
            f"<div>status <span class=\"badge\">{escape(str(detail.get('status', '')))}</span> "
            f"confidence {detail.get('confidence', 0):.2f} "
            f"<span class=\"muted\">(evidence prior {detail.get('priorConfidence', 0):.2f})</span></div>",
            f"<div class=\"muted\">{escape(str(detail.get('description', '')))}</div>",
            f"<div>triggers: {escape(', '.join(detail.get('triggers') or ()))}</div>",
            f"<div>tools: {escape(', '.join(detail.get('tools') or ()))}</div>",
            f"<ul>{issues}</ul>" if issues else "",
            "</div>",
            "<h2>skill text (shown as data, never fed back to the model)</h2>",
            f"<pre>{escape(str(detail.get('skillMarkdown', '')))}</pre>",
        ]
    )


def render_runs(rows: list[dict[str, Any]]) -> str:
    return _table(
        ["run", "status", "children", "cost", "events", "trail"],
        [
            (
                escape(row["id"]),
                f"<span class=\"{escape(row['status'])}\">{escape(row['status'])}</span>",
                str(row["children"]),
                f"${row['costUsd']:.4f}",
                str(row["events"]),
                f"<code>python -m minicode.orchestration replay {escape(row['id'])}</code>",
            )
            for row in rows
        ],
    )
