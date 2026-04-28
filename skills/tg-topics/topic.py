#!/usr/bin/env python3
"""tg-topics Skill dispatcher: forum topic CRUD."""

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


def cmd_list(args, c):
    return c.topics_list(args.chat, limit=args.limit, query=args.query)


def cmd_create(args, c):
    return c.topics_create(
        args.chat,
        args.title,
        icon_color=args.icon_color,
        icon_emoji_id=args.icon_emoji_id,
    )


def cmd_edit(args, c):
    if all(
        x is None for x in (args.title, args.icon_emoji_id, args.closed, args.hidden)
    ):
        raise SystemExit(
            "error: pass at least one of --title / --icon-emoji-id / "
            "--closed / --reopen / --hidden / --visible"
        )
    return c.topics_edit(
        args.chat,
        args.topic_id,
        title=args.title,
        icon_emoji_id=args.icon_emoji_id,
        closed=args.closed,
        hidden=args.hidden,
    )


def cmd_delete(args, c):
    if not args.yes:
        raise SystemExit(
            "error: --yes required (delete removes the topic AND its "
            "history; this is irreversible)"
        )
    return c.topics_delete(args.chat, args.topic_id)


def cmd_pin(args, c):
    return c.topics_pin(args.chat, args.topic_id, args.pinned)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="topic.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="List topics in a forum supergroup")
    ls.add_argument("--chat", required=True)
    ls.add_argument("--limit", type=int, default=100)
    ls.add_argument("--query", default=None, help="Optional title substring filter")

    cr = sub.add_parser("create", help="Create a new topic")
    cr.add_argument("--chat", required=True)
    cr.add_argument("--title", required=True)
    cr.add_argument("--icon-color", type=int, default=None)
    cr.add_argument("--icon-emoji-id", type=int, default=None)

    ed = sub.add_parser("edit", help="Edit an existing topic")
    ed.add_argument("--chat", required=True)
    ed.add_argument("--topic-id", type=int, required=True)
    ed.add_argument("--title", default=None)
    ed.add_argument("--icon-emoji-id", type=int, default=None)
    g = ed.add_mutually_exclusive_group()
    g.add_argument("--closed", dest="closed", action="store_const", const=True)
    g.add_argument("--reopen", dest="closed", action="store_const", const=False)
    h = ed.add_mutually_exclusive_group()
    h.add_argument("--hidden", dest="hidden", action="store_const", const=True)
    h.add_argument("--visible", dest="hidden", action="store_const", const=False)
    ed.set_defaults(closed=None, hidden=None)

    rm = sub.add_parser("delete", help="Delete a topic (irreversible)")
    rm.add_argument("--chat", required=True)
    rm.add_argument("--topic-id", type=int, required=True)
    rm.add_argument("--yes", action="store_true", required=False)

    pn = sub.add_parser("pin", help="Pin or unpin a topic")
    pn.add_argument("--chat", required=True)
    pn.add_argument("--topic-id", type=int, required=True)
    pg = pn.add_mutually_exclusive_group(required=True)
    pg.add_argument("--pinned", dest="pinned", action="store_const", const=True)
    pg.add_argument("--unpinned", dest="pinned", action="store_const", const=False)

    return p


HANDLERS = {
    "list": cmd_list,
    "create": cmd_create,
    "edit": cmd_edit,
    "delete": cmd_delete,
    "pin": cmd_pin,
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
