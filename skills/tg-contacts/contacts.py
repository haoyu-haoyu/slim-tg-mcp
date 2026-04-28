#!/usr/bin/env python3
"""tg-contacts Skill dispatcher.

Subcommands map onto daemon /contacts/* endpoints.
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


def cmd_add(args, c):
    if not args.phone.startswith("+"):
        raise SystemExit("error: --phone must be E.164 (start with +)")
    return c.contact_add(args.phone, args.first_name, args.last_name or "")


def cmd_delete(args, c):
    return c.contact_delete(_coerce(args.user))


def cmd_block(args, c):
    return c.contact_block(_coerce(args.user))


def cmd_unblock(args, c):
    return c.contact_unblock(_coerce(args.user))


def cmd_search(args, c):
    return c.contact_search(args.query, limit=args.limit)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="contacts.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--phone", required=True)
    a.add_argument("--first-name", required=True)
    a.add_argument("--last-name", default="")

    for name in ("delete", "block", "unblock"):
        s = sub.add_parser(name)
        s.add_argument("--user", required=True)

    se = sub.add_parser("search")
    se.add_argument("--query", required=True)
    se.add_argument("--limit", type=int, default=20)

    return p


HANDLERS = {
    "add": cmd_add,
    "delete": cmd_delete,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "search": cmd_search,
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
    print(
        json.dumps(
            {"ok": True, **(res if isinstance(res, dict) else {"result": res})},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
