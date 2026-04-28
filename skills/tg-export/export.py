#!/usr/bin/env python3
"""tg-export Skill dispatcher: export a chat history to local disk."""

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


def cmd_run(args, c):
    """Enforce the SKILL.md confirmation gate at the dispatcher.

    Two equivalent ways to pass the gate:
      1. `--yes` (caller has already confirmed with the human).
      2. `--confirm-chat <X> --confirm-out-dir <Y>` where X / Y must
         echo back the exact `--chat` and `--out-dir` arguments.
    """
    if not args.yes:
        if args.confirm_chat is None or args.confirm_out_dir is None:
            raise SystemExit(
                "error: refusing to export without confirmation. Pass --yes "
                "(after asking the human) OR pass --confirm-chat and "
                "--confirm-out-dir echoing the exact target."
            )
        if (
            args.confirm_chat != args.chat
            or args.confirm_out_dir != args.out_dir
        ):
            raise SystemExit(
                "error: --confirm-chat/--confirm-out-dir do not match "
                "--chat/--out-dir; refusing to export."
            )

    abs_dir = os.path.abspath(args.out_dir)
    if not os.path.exists(abs_dir):
        raise SystemExit(
            f"error: {abs_dir} does not exist. Create it first — the daemon "
            "will not auto-mkdir arbitrary directories."
        )

    return c.export_chat(
        _coerce(args.chat),
        abs_dir,
        limit=args.limit,
        include_media=args.include_media,
        since_date=args.since,
        until_date=args.until,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="export.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run")
    run.add_argument("--chat", required=True)
    run.add_argument("--out-dir", required=True)
    run.add_argument("--limit", type=int, default=1000)
    run.add_argument("--include-media", action="store_true")
    run.add_argument("--since", default=None,
                     help="ISO-8601 with timezone; only export messages on/after this")
    run.add_argument("--until", default=None,
                     help="ISO-8601 with timezone; only export messages before this")
    run.add_argument("--yes", action="store_true",
                     help="Caller has confirmed with the human; bypasses double-keystroke")
    run.add_argument("--confirm-chat", default=None,
                     help="Must echo --chat. See --confirm-out-dir.")
    run.add_argument("--confirm-out-dir", default=None,
                     help="Must echo --out-dir. Pair with --confirm-chat for double-keystroke.")
    return p


HANDLERS = {"run": cmd_run}


def main() -> int:
    args = build_parser().parse_args()
    try:
        with DaemonClient(timeout=600.0) as c:  # exports can be slow
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
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **(res if isinstance(res, dict) else {"result": res})},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
