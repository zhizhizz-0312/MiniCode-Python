"""``python -m minicode.web_panel`` -- a local window onto the four stages.

Prints the URL once, including the token, and holds the process until interrupted.
Ctrl+C is the only way to stop it, which is deliberate: a window that closes itself
while someone is reading it is worse than no window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from minicode.web_panel.server import LOOPBACK, serve

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m minicode.web_panel",
        description="Serve a read-only-by-default panel for skills, security, learning and runs.",
    )
    parser.add_argument("--cwd", default=".", help="workspace whose skills to show")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="refuse the two write actions (approve/reject drafts, enable/disable skills)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    panel = serve(
        cwd=str(Path(args.cwd).resolve()),
        port=args.port,
        allow_actions=not args.read_only,
    )
    # flush=True matters more than it looks: stdout is block-buffered when this is not a
    # terminal, so the one line carrying the token -- the only way into the panel -- stayed
    # invisible until the process exited.  That is why the panel appeared not to exist.
    print(f"MiniCode panel: {panel.url}", flush=True)
    print(f"  workspace: {panel.cwd}", flush=True)
    print(f"  bound to : {LOOPBACK} only, token required", flush=True)
    print(f"  actions  : {'disabled (read-only)' if args.read_only else 'approve/reject drafts, enable/disable skills'}", flush=True)
    # ...and written down, so a lost scrollback does not mean restarting the panel.
    try:
        from pathlib import Path as _Path

        url_file = _Path.home() / ".mini-code" / "panel-url.txt"
        url_file.parent.mkdir(parents=True, exist_ok=True)
        url_file.write_text(panel.url + "\n", encoding="utf-8")
        print(f"  url saved: {url_file}", flush=True)
    except OSError as error:
        print(f"  url not saved: {error}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            import time

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        panel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
