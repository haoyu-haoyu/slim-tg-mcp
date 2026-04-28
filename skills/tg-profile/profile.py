#!/usr/bin/env python3
"""tg-profile Skill dispatcher: update / username / photo / status."""

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


def cmd_update(args, c):
    if args.first_name is None and args.last_name is None and args.about is None:
        raise SystemExit(
            "error: pass at least one of --first-name / --last-name / --about"
        )
    return c.profile_update(
        first_name=args.first_name,
        last_name=args.last_name,
        about=args.about,
    )


def cmd_username(args, c):
    if args.clear and args.new:
        raise SystemExit("error: --clear and --new are mutually exclusive")
    if not args.clear and args.new is None:
        raise SystemExit("error: pass --new <name> or --clear")
    return c.profile_username("" if args.clear else args.new)


def cmd_photo(args, c):
    abs_path = os.path.abspath(args.file)
    if not os.path.exists(abs_path):
        raise SystemExit(f"error: file not found: {abs_path}")
    return c.profile_photo(abs_path)


def cmd_photo_delete(args, c):
    return c.profile_photo_delete()


def cmd_online(args, c):
    return c.profile_status(True)


def cmd_offline(args, c):
    return c.profile_status(False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="profile.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    upd = sub.add_parser("update")
    upd.add_argument("--first-name", default=None)
    upd.add_argument("--last-name", default=None)
    upd.add_argument("--about", default=None)

    un = sub.add_parser("username")
    g = un.add_mutually_exclusive_group()
    g.add_argument("--new", default=None)
    g.add_argument("--clear", action="store_true")

    ph = sub.add_parser("photo")
    ph.add_argument("--file", required=True)

    sub.add_parser("photo-delete")
    sub.add_parser("online")
    sub.add_parser("offline")

    return p


HANDLERS = {
    "update": cmd_update,
    "username": cmd_username,
    "photo": cmd_photo,
    "photo-delete": cmd_photo_delete,
    "online": cmd_online,
    "offline": cmd_offline,
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
