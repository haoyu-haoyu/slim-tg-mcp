#!/usr/bin/env python3
"""Skill helper: dispatch any tg-messaging write action through the daemon.

Subcommands map 1:1 onto daemon endpoints. Each prints a JSON result on
success and exits non-zero on failure with a human-readable stderr message.

    act.py send    --chat X --text Y [--reply-to ID] [--stdin]
    act.py edit    --chat X --msg-id ID --text Y [--stdin]
    act.py delete  --chat X --msg-ids 1,2,3 [--no-revoke]
    act.py forward --from-chat A --to-chat B --msg-ids 1,2,3
    act.py pin     --chat X --msg-id ID [--silent]
    act.py unpin   --chat X [--msg-id ID]
    act.py react   --chat X --msg-id ID [--emoji "👍" | --clear]
    act.py read    --chat X
"""

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


def _coerce_chat(value: str) -> str | int:
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _read_text(args: argparse.Namespace) -> str:
    if getattr(args, "stdin", False):
        return sys.stdin.read().rstrip("\n")
    return args.text or ""


def cmd_send(args: argparse.Namespace, c: DaemonClient) -> dict:
    text = _read_text(args)
    if not text.strip():
        raise SystemExit("error: refusing to send empty message")
    return c.send(_coerce_chat(args.chat), text, reply_to=args.reply_to)


def cmd_edit(args: argparse.Namespace, c: DaemonClient) -> dict:
    text = _read_text(args)
    if not text.strip():
        raise SystemExit("error: refusing to set empty text")
    return c.edit(_coerce_chat(args.chat), args.msg_id, text)


def cmd_delete(args: argparse.Namespace, c: DaemonClient) -> dict:
    ids = [int(s) for s in args.msg_ids.split(",") if s.strip()]
    if not ids:
        raise SystemExit("error: --msg-ids is empty")
    return c.delete(_coerce_chat(args.chat), ids, revoke=not args.no_revoke)


def cmd_forward(args: argparse.Namespace, c: DaemonClient) -> dict:
    ids = [int(s) for s in args.msg_ids.split(",") if s.strip()]
    if not ids:
        raise SystemExit("error: --msg-ids is empty")
    return c.forward(
        _coerce_chat(args.from_chat),
        _coerce_chat(args.to_chat),
        ids,
    )


def cmd_pin(args: argparse.Namespace, c: DaemonClient) -> dict:
    return c.pin(_coerce_chat(args.chat), args.msg_id, notify=not args.silent)


def cmd_unpin(args: argparse.Namespace, c: DaemonClient) -> dict:
    return c.unpin(_coerce_chat(args.chat), args.msg_id)


def cmd_react(args: argparse.Namespace, c: DaemonClient) -> dict:
    if args.clear and args.emoji:
        raise SystemExit("error: cannot use --clear and --emoji together")
    emoji = None if args.clear else args.emoji
    if not args.clear and not emoji:
        raise SystemExit("error: provide --emoji or --clear")
    return c.react(_coerce_chat(args.chat), args.msg_id, emoji)


def cmd_read(args: argparse.Namespace, c: DaemonClient) -> dict:
    return c.mark_read(_coerce_chat(args.chat))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="act.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("--chat", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--stdin", action="store_true")
    s.add_argument("--reply-to", type=int, default=None)

    e = sub.add_parser("edit")
    e.add_argument("--chat", required=True)
    e.add_argument("--msg-id", type=int, required=True)
    g = e.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--stdin", action="store_true")

    d = sub.add_parser("delete")
    d.add_argument("--chat", required=True)
    d.add_argument("--msg-ids", required=True, help="Comma-separated msg ids")
    d.add_argument(
        "--no-revoke",
        action="store_true",
        help="Delete only for self; leave copies in others' chats",
    )

    f = sub.add_parser("forward")
    f.add_argument("--from-chat", required=True)
    f.add_argument("--to-chat", required=True)
    f.add_argument("--msg-ids", required=True)

    pn = sub.add_parser("pin")
    pn.add_argument("--chat", required=True)
    pn.add_argument("--msg-id", type=int, required=True)
    pn.add_argument("--silent", action="store_true")

    u = sub.add_parser("unpin")
    u.add_argument("--chat", required=True)
    u.add_argument("--msg-id", type=int, default=None, help="Omit to unpin all")

    r = sub.add_parser("react")
    r.add_argument("--chat", required=True)
    r.add_argument("--msg-id", type=int, required=True)
    r.add_argument("--emoji")
    r.add_argument("--clear", action="store_true")

    rd = sub.add_parser("read")
    rd.add_argument("--chat", required=True)

    return p


HANDLERS = {
    "send": cmd_send,
    "edit": cmd_edit,
    "delete": cmd_delete,
    "forward": cmd_forward,
    "pin": cmd_pin,
    "unpin": cmd_unpin,
    "react": cmd_react,
    "read": cmd_read,
}


def main() -> int:
    args = build_parser().parse_args()
    handler = HANDLERS[args.cmd]
    try:
        with DaemonClient() as c:
            res = handler(args, c)
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
            print(
                f"error: HTTP {e.response.status_code}: {e.response.text}",
                file=sys.stderr,
            )
        return 1
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **res}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
