#!/usr/bin/env python3
"""tg-group-admin Skill dispatcher: group/channel admin write actions.

All actions go through the local slim-tg-mcp daemon. Each prints a JSON
result on success and exits non-zero on failure with stderr context.
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


def _coerce(value: str) -> str | int:
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _split_users(raw: str) -> list[str | int]:
    return [_coerce(s.strip()) for s in raw.split(",") if s.strip()]


def cmd_create(args, c):
    return c.chat_create(
        args.title,
        _split_users(args.users) if args.users else [],
        megagroup=args.megagroup,
        broadcast=args.broadcast,
        about=args.about or "",
    )


def cmd_add(args, c):
    return c.chat_add_member(_coerce(args.chat), _coerce(args.user))


def cmd_kick(args, c):
    return c.chat_kick_member(_coerce(args.chat), _coerce(args.user))


def cmd_ban(args, c):
    return c.chat_ban_member(_coerce(args.chat), _coerce(args.user))


def cmd_unban(args, c):
    return c.chat_unban_member(_coerce(args.chat), _coerce(args.user))


def cmd_invite(args, c):
    return c.chat_invite_link(
        _coerce(args.chat),
        expire_seconds=args.expire_seconds,
        usage_limit=args.usage_limit,
    )


def cmd_rename(args, c):
    return c.chat_set_title(_coerce(args.chat), args.title)


def cmd_leave(args, c):
    return c.chat_leave(_coerce(args.chat))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="admin.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    cr = sub.add_parser("create")
    cr.add_argument("--title", required=True)
    cr.add_argument("--users", default="")
    cr.add_argument("--megagroup", action="store_true")
    cr.add_argument("--broadcast", action="store_true")
    cr.add_argument("--about", default="")

    for name in ("add", "kick", "ban", "unban"):
        s = sub.add_parser(name)
        s.add_argument("--chat", required=True)
        s.add_argument("--user", required=True)

    inv = sub.add_parser("invite")
    inv.add_argument("--chat", required=True)
    inv.add_argument("--expire-seconds", type=int, default=None)
    inv.add_argument("--usage-limit", type=int, default=None)

    rn = sub.add_parser("rename")
    rn.add_argument("--chat", required=True)
    rn.add_argument("--title", required=True)

    lv = sub.add_parser("leave")
    lv.add_argument("--chat", required=True)

    return p


HANDLERS = {
    "create": cmd_create,
    "add": cmd_add,
    "kick": cmd_kick,
    "ban": cmd_ban,
    "unban": cmd_unban,
    "invite": cmd_invite,
    "rename": cmd_rename,
    "leave": cmd_leave,
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
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **(res if isinstance(res, dict) else {"result": res})},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
