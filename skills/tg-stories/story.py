#!/usr/bin/env python3
"""tg-stories Skill dispatcher: read/mark/delete stories."""

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


def cmd_active(args, c):
    return c.stories_active(args.peer)


def cmd_pinned(args, c):
    return c.stories_pinned(args.peer, limit=args.limit, offset_id=args.offset_id)


def cmd_mark_read(args, c):
    if not args.ack:
        raise SystemExit(
            "error: --ack required. Marking stories as read sends a "
            "viewed receipt to the peer — the action is observable. "
            "If the user asked you to summarize stories silently "
            "(\"lurk\"), do NOT pass --ack."
        )
    return c.stories_mark_read(args.peer, args.max_id)


def cmd_delete(args, c):
    if not args.id:
        raise SystemExit("error: pass at least one --id")
    if not args.yes:
        raise SystemExit(
            "error: --yes required (deletes your own stories permanently)"
        )
    return c.stories_delete(list(args.id))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="story.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("active", help="List a peer's active (unexpired) stories")
    a.add_argument("--peer", required=True)

    pn = sub.add_parser("pinned", help="List a peer's pinned stories (paginated)")
    pn.add_argument("--peer", required=True)
    pn.add_argument("--limit", type=int, default=50)
    pn.add_argument("--offset-id", type=int, default=0)

    mr = sub.add_parser(
        "mark-read",
        help="Mark a peer's stories ≤ max-id as viewed (sends viewed receipt)",
    )
    mr.add_argument("--peer", required=True)
    mr.add_argument("--max-id", type=int, required=True)
    mr.add_argument(
        "--ack",
        action="store_true",
        help="Required: explicitly opt in to sending a viewed receipt to "
        "the peer. Without this flag, the skill refuses (lurk-safe default).",
    )

    rm = sub.add_parser("delete", help="Delete your own stories (irreversible)")
    rm.add_argument("--id", action="append", type=int, required=False)
    rm.add_argument("--yes", action="store_true")

    return p


HANDLERS = {
    "active": cmd_active,
    "pinned": cmd_pinned,
    "mark-read": cmd_mark_read,
    "delete": cmd_delete,
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
