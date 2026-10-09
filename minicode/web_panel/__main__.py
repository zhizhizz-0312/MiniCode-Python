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
    print(f"MiniCode panel: {panel.url}")
    print(f"  workspace: {panel.cwd}")
    print(f"  bound to : {LOOPBACK} only, token required")
    print(f"  actions  : {'disabled (read-only)' if args.read_only else 'approve/reject drafts, enable/disable skills'}")
    print("Press Ctrl+C to stop.")
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
