#!/usr/bin/env python3
"""tg-location Skill dispatcher: static + live location."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import httpx  # noqa: E402

from tgmcp.client import DaemonClient  # noqa: E402


def cmd_send(args, c):
    return c.location_send(
        args.chat,
        args.lat,
        args.lng,
        accuracy=args.accuracy,
        reply_to=args.reply_to,
    )


_PERIOD_INDEFINITE = 0x7FFFFFFF


def cmd_send_live(args, c):
    if args.period == _PERIOD_INDEFINITE and not args.confirm_indefinite:
        raise SystemExit(
            "error: --confirm-indefinite required when period equals "
            f"{_PERIOD_INDEFINITE} (0x7FFFFFFF). Indefinite sharing leaks "
            "real-time location until you manually stop it; the dispatcher "
            "refuses without an explicit opt-in."
        )
    return c.location_send_live(
        args.chat,
        args.lat,
        args.lng,
        args.period,
        accuracy=args.accuracy,
        heading=args.heading,
        proximity=args.proximity,
        reply_to=args.reply_to,
    )


def cmd_edit_live(args, c):
    return c.location_edit_live(
        args.chat,
        args.msg_id,
        args.lat,
        args.lng,
        accuracy=args.accuracy,
        heading=args.heading,
        proximity=args.proximity,
    )


def cmd_stop_live(args, c):
    return c.location_stop_live(args.chat, args.msg_id)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="location.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="Send a static location pin")
    s.add_argument("--chat", required=True)
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lng", type=float, required=True)
    s.add_argument("--accuracy", type=int, default=None)
    s.add_argument("--reply-to", type=int, default=None)

    sl = sub.add_parser("send-live", help="Send a live (live-tracked) location")
    sl.add_argument("--chat", required=True)
    sl.add_argument("--lat", type=float, required=True)
    sl.add_argument("--lng", type=float, required=True)
    sl.add_argument(
        "--period",
        type=int,
        required=True,
        help="Duration in seconds. Any value in [60, 86400], or 2147483647 "
        "(0x7FFFFFFF) for indefinite (requires --confirm-indefinite).",
    )
    sl.add_argument("--accuracy", type=int, default=None)
    sl.add_argument("--heading", type=int, default=None, help="1..360 degrees")
    sl.add_argument("--proximity", type=int, default=None, help="meters")
    sl.add_argument("--reply-to", type=int, default=None)
    sl.add_argument(
        "--confirm-indefinite",
        action="store_true",
        help="Required ONLY when --period=2147483647 (0x7FFFFFFF, "
        "indefinite). Acknowledge the open-ended location leak.",
    )

    el = sub.add_parser("edit-live", help="Update an active live location")
    el.add_argument("--chat", required=True)
    el.add_argument("--msg-id", type=int, required=True)
    el.add_argument("--lat", type=float, required=True)
    el.add_argument("--lng", type=float, required=True)
    el.add_argument("--accuracy", type=int, default=None)
    el.add_argument("--heading", type=int, default=None)
    el.add_argument("--proximity", type=int, default=None)

    st = sub.add_parser("stop-live", help="Stop sharing a live location")
    st.add_argument("--chat", required=True)
    st.add_argument("--msg-id", type=int, required=True)

    return p


HANDLERS = {
    "send": cmd_send,
    "send-live": cmd_send_live,
    "edit-live": cmd_edit_live,
    "stop-live": cmd_stop_live,
}


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
