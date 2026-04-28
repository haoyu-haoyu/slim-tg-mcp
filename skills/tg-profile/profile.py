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


def cmd_emoji_status(args, c):
    """Set / clear an emoji status (Telegram Premium feature).

    Pass --document-id to set; pass --clear to remove. --until expects
    an ISO 8601 timezone-aware timestamp (e.g. 2026-12-31T23:59:00+00:00)
    and only applies when setting.
    """
    if args.clear and args.document_id is not None:
        raise SystemExit("error: --clear and --document-id are mutually exclusive")
    if not args.clear and args.document_id is None:
        raise SystemExit("error: pass --document-id <id> or --clear")
    until_iso = args.until if not args.clear else None
    return c.profile_emoji_status(
        document_id=None if args.clear else args.document_id,
        until_iso=until_iso,
    )


def cmd_set_2fa(args, c):
    """Enable / change / remove cloud-password (two-factor auth).

    The skill prompts for passwords interactively; they never go on
    argv. Pass --remove to disable 2FA (asks only for the current
    password). Pass --enable when there's no current password yet.
    Default = change existing password (asks for both).

    `getpass.getpass` requires a TTY for the prompt to actually hide
    keystrokes; in non-interactive contexts (CI, piped stdin) it
    degrades to plain stdin reads. For a secret-input command that's
    too easy to misuse, so we fail fast with a clear message instead.
    """
    import getpass
    import sys as _sys

    if not _sys.stdin.isatty():
        raise SystemExit(
            "error: 2fa requires an interactive TTY for password prompts; "
            "refusing to read passwords from a piped/non-tty stdin. Run "
            "this command directly in a terminal."
        )

    current = None
    new = None
    if args.remove:
        current = getpass.getpass("Current 2FA password: ")
        # new stays None → disables
    elif args.enable:
        new = getpass.getpass("New 2FA password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if new != confirm:
            raise SystemExit("error: passwords do not match")
    else:
        current = getpass.getpass("Current 2FA password: ")
        new = getpass.getpass("New 2FA password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if new != confirm:
            raise SystemExit("error: passwords do not match")
    return c.profile_2fa(
        current_password=current,
        new_password=new,
        hint=args.hint or "",
        email=args.email,
    )


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

    em = sub.add_parser(
        "emoji-status",
        help="Set or clear emoji status (Telegram Premium feature)",
    )
    g = em.add_mutually_exclusive_group()
    g.add_argument(
        "--document-id",
        type=int,
        default=None,
        help="Custom-emoji document id from Telegram",
    )
    g.add_argument(
        "--clear",
        action="store_true",
        help="Remove the current emoji status (no Premium needed for clear)",
    )
    em.add_argument(
        "--until",
        default=None,
        help="ISO 8601 timezone-aware timestamp for auto-removal "
        "(only with --document-id)",
    )

    fa = sub.add_parser("2fa")
    g = fa.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true",
                   help="Enable 2FA on an account that has none (asks for new password only)")
    g.add_argument("--remove", action="store_true",
                   help="Disable 2FA (asks for current password only)")
    fa.add_argument("--hint", default=None,
                    help="Optional password hint stored alongside the new password")
    fa.add_argument("--email", default=None,
                    help="Optional recovery email")

    return p


HANDLERS = {
    "update": cmd_update,
    "username": cmd_username,
    "photo": cmd_photo,
    "photo-delete": cmd_photo_delete,
    "online": cmd_online,
    "offline": cmd_offline,
    "2fa": cmd_set_2fa,
    "emoji-status": cmd_emoji_status,
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
