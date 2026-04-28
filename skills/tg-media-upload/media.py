#!/usr/bin/env python3
"""tg-media-upload Skill dispatcher: upload local files to a Telegram chat.

Subcommand:
    media.py send --chat X --file /abs/path [--caption ...] [--reply-to ID]
                  [--as-voice] [--force-document]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx  # noqa: E402

from tgmcp.client import DaemonClient  # noqa: E402


def _coerce(value: str) -> str | int:
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def cmd_send(args, c):
    """Enforce the SKILL.md confirm-before-send contract at the dispatcher.

    Two equivalent ways to pass the gate:
      1. `--yes` (caller has already confirmed with the human).
      2. `--confirm-chat <X> --confirm-file <Y>` where X / Y must
         echo back the exact `--chat` and `--file` arguments. This makes
         a "double-keystroke" pattern that prevents an LLM from sending
         to one chat while the human thought they were confirming a
         different one.
    """
    if not args.yes:
        if args.confirm_chat is None or args.confirm_file is None:
            raise SystemExit(
                "error: refusing to send without confirmation. Pass --yes "
                "(after asking the human) OR pass --confirm-chat and "
                "--confirm-file echoing the exact target."
            )
        if args.confirm_chat != args.chat or args.confirm_file != args.file:
            raise SystemExit(
                "error: --confirm-chat/--confirm-file do not match "
                "--chat/--file; refusing to send."
            )

    abs_path = os.path.abspath(args.file)
    if not os.path.exists(abs_path):
        raise SystemExit(f"error: file not found: {abs_path}")
    return c.send_media(
        _coerce(args.chat),
        abs_path,
        caption=args.caption or "",
        reply_to=args.reply_to,
        as_voice=args.as_voice,
        force_document=args.force_document,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--chat", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--caption", default="")
    s.add_argument("--reply-to", type=int, default=None)
    s.add_argument("--as-voice", action="store_true")
    s.add_argument("--force-document", action="store_true")
    s.add_argument(
        "--yes",
        action="store_true",
        help="Caller has already confirmed with the human. Bypasses the "
        "double-keystroke check.",
    )
    s.add_argument(
        "--confirm-chat",
        default=None,
        help="Must echo the value of --chat. Pass with --confirm-file "
        "instead of --yes for an extra typo-resistant safeguard.",
    )
    s.add_argument(
        "--confirm-file",
        default=None,
        help="Must echo the value of --file. See --confirm-chat.",
    )
    return p


HANDLERS = {"send": cmd_send}


def main() -> int:
    args = build_parser().parse_args()
    try:
        with DaemonClient() as c:
            res = HANDLERS[args.cmd](args, c)
    except (httpx.ConnectError, FileNotFoundError, ConnectionRefusedError, OSError) as e:
        print(
            f"error: cannot reach daemon ({type(e).__name__}: {e}). "
            "Start it with `tgmcp daemon start`.",
            file=sys.stderr,
        )
        return 3
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
            print(
                f"error: daemon returned {body.get('error')}: {body.get('detail')}",
                file=sys.stderr,
            )
        except Exception:
            print(f"error: HTTP {e.response.status_code}: {e.response.text}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **(res if isinstance(res, dict) else {"result": res})},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
