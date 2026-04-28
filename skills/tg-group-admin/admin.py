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


def cmd_participants(args, c):
    return c.chat_participants(
        _coerce(args.chat),
        limit=args.limit,
        offset=args.offset,
        search=args.search or "",
        filter_kind=args.filter,
    )


def cmd_signatures(args, c):
    if args.on and args.off:
        raise SystemExit("error: --on and --off are mutually exclusive")
    if not (args.on or args.off):
        raise SystemExit("error: pass --on or --off")
    return c.chat_signatures(_coerce(args.chat), enabled=args.on)


def cmd_slow_mode(args, c):
    return c.chat_slow_mode(_coerce(args.chat), args.seconds)


def cmd_discussion(args, c):
    if args.unbind and args.group:
        raise SystemExit("error: --group and --unbind are mutually exclusive")
    return c.chat_discussion(
        _coerce(args.broadcast),
        None if args.unbind else _coerce(args.group),
    )


def cmd_admin_log(args, c):
    return c.chat_admin_log(
        _coerce(args.chat), limit=args.limit, search=args.search or ""
    )


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

    pp = sub.add_parser("participants")
    pp.add_argument("--chat", required=True)
    pp.add_argument("--limit", type=int, default=100)
    pp.add_argument("--offset", type=int, default=0)
    pp.add_argument("--search", default=None)
    pp.add_argument(
        "--filter",
        default="all",
        choices=["all", "admins", "kicked", "banned", "bots", "search"],
    )

    sig = sub.add_parser("signatures")
    sig.add_argument("--chat", required=True)
    g = sig.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")

    sm = sub.add_parser("slow-mode")
    sm.add_argument("--chat", required=True)
    sm.add_argument(
        "--seconds",
        type=int,
        required=True,
        help="0 disables; non-zero ∈ {10,30,60,300,900,3600}",
    )

    ds = sub.add_parser("discussion")
    ds.add_argument("--broadcast", required=True)
    grp = ds.add_mutually_exclusive_group(required=True)
    grp.add_argument("--group", default=None)
    grp.add_argument("--unbind", action="store_true")

    al = sub.add_parser("admin-log")
    al.add_argument("--chat", required=True)
    al.add_argument("--limit", type=int, default=50)
    al.add_argument("--search", default=None)

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
    "participants": cmd_participants,
    "signatures": cmd_signatures,
    "slow-mode": cmd_slow_mode,
    "discussion": cmd_discussion,
    "admin-log": cmd_admin_log,
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
