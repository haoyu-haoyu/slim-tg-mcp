#!/usr/bin/env python3
"""Skill helper: send a Telegram message via the local slim-tg-mcp daemon.

Designed to be invoked by Claude Code skills. Prints a JSON result line on
success and exits non-zero on failure with a human-readable message on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running directly via `python send.py` without installing the package.
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tgmcp.client import DaemonClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send a Telegram message via the daemon.")
    p.add_argument("--chat", required=True, help="Chat ID or @username")
    p.add_argument("--text", help="Message text (use --stdin for multi-line)")
    p.add_argument("--stdin", action="store_true", help="Read text from stdin")
    p.add_argument("--reply-to", type=int, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.stdin:
        text = sys.stdin.read().rstrip("\n")
    elif args.text is not None:
        text = args.text
    else:
        print("error: provide --text or --stdin", file=sys.stderr)
        return 2

    if not text.strip():
        print("error: refusing to send empty message", file=sys.stderr)
        return 2

    chat: str | int = args.chat
    if isinstance(chat, str) and chat.lstrip("-").isdigit():
        chat = int(chat)

    import httpx

    try:
        with DaemonClient() as c:
            res = c.send(chat, text, reply_to=args.reply_to)
    except (httpx.ConnectError, FileNotFoundError, ConnectionRefusedError, OSError) as e:
        # httpx raises ConnectError/OSError when the unix socket is missing or
        # not yet listening; FileNotFoundError can also surface depending on
        # platform. All three mean: daemon isn't up.
        print(
            f"error: cannot reach daemon ({type(e).__name__}: {e}). "
            f"Start it with `tgmcp daemon start`.",
            file=sys.stderr,
        )
        return 3
    except httpx.HTTPStatusError as e:
        # Structured error from the daemon — surface its kind/detail.
        try:
            body = e.response.json()
            kind, detail = body.get("error"), body.get("detail")
            print(f"error: daemon returned {kind}: {detail}", file=sys.stderr)
        except Exception:
            print(f"error: HTTP {e.response.status_code}: {e.response.text}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, **res}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
