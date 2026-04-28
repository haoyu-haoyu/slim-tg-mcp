#!/usr/bin/env python3
"""tg-scheduling Skill dispatcher.

Subcommands:
    send / list / cancel              — scheduled messages
    draft-save / draft-get / draft-clear  — drafts
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
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


def _resolve_when(args) -> str:
    """Convert --when / --in-seconds to an ISO-8601 UTC string."""
    if args.when and args.in_seconds is not None:
        raise SystemExit("error: pass --when OR --in-seconds, not both")
    if args.when:
        try:
            dt = datetime.fromisoformat(args.when)
        except ValueError as e:
            raise SystemExit(f"error: invalid --when ({e}); use ISO-8601 with timezone") from e
        if dt.tzinfo is None:
            raise SystemExit(
                "error: --when must include a timezone (e.g. ...+00:00 or ...Z)"
            )
        return dt.astimezone(timezone.utc).isoformat()
    if args.in_seconds is not None:
        if args.in_seconds < 10:
            raise SystemExit("error: --in-seconds must be at least 10")
        return (datetime.now(timezone.utc) + timedelta(seconds=args.in_seconds)).isoformat()
    raise SystemExit("error: pass --when or --in-seconds")


def cmd_send(args, c):
    when_iso = _resolve_when(args)
    return c.scheduled_send(
        _coerce(args.chat), args.text, when_iso, reply_to=args.reply_to
    )


def cmd_list(args, c):
    return c.scheduled_list(_coerce(args.chat), limit=args.limit)


def cmd_cancel(args, c):
    ids = [int(s) for s in args.msg_ids.split(",") if s.strip()]
    if not ids:
        raise SystemExit("error: --msg-ids is empty")
    return c.scheduled_delete(_coerce(args.chat), ids)


def cmd_edit(args, c):
    if args.text is None and args.when is None and args.in_seconds is None:
        raise SystemExit("error: pass --text and/or --when / --in-seconds")
    when_iso = None
    if args.when or args.in_seconds is not None:
        when_iso = _resolve_when(args)
    return c.scheduled_edit(
        _coerce(args.chat),
        args.msg_id,
        text=args.text,
        schedule_date=when_iso,
    )


def cmd_draft_save(args, c):
    return c.draft_save(_coerce(args.chat), args.text, reply_to=args.reply_to)


def cmd_draft_get(args, c):
    return c.draft_get(_coerce(args.chat))


def cmd_draft_clear(args, c):
    return c.draft_clear(_coerce(args.chat))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="schedule.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("--chat", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--when", help="ISO-8601 datetime with timezone")
    s.add_argument("--in-seconds", type=int, help="Schedule N seconds from now")
    s.add_argument("--reply-to", type=int, default=None)

    ls = sub.add_parser("list")
    ls.add_argument("--chat", required=True)
    ls.add_argument("--limit", type=int, default=100)

    cn = sub.add_parser("cancel")
    cn.add_argument("--chat", required=True)
    cn.add_argument("--msg-ids", required=True, help="Comma-separated msg ids")

    ed = sub.add_parser("edit")
    ed.add_argument("--chat", required=True)
    ed.add_argument("--msg-id", type=int, required=True)
    ed.add_argument("--text", default=None)
    ed.add_argument("--when", default=None,
                    help="ISO-8601 datetime with timezone")
    ed.add_argument("--in-seconds", type=int, default=None)

    ds = sub.add_parser("draft-save")
    ds.add_argument("--chat", required=True)
    ds.add_argument("--text", required=True)
    ds.add_argument("--reply-to", type=int, default=None)

    for name in ("draft-get", "draft-clear"):
        x = sub.add_parser(name)
        x.add_argument("--chat", required=True)

    return p


HANDLERS = {
    "send": cmd_send,
    "list": cmd_list,
    "cancel": cmd_cancel,
    "edit": cmd_edit,
    "draft-save": cmd_draft_save,
    "draft-get": cmd_draft_get,
    "draft-clear": cmd_draft_clear,
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
