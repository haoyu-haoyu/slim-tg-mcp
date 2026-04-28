#!/usr/bin/env python3
"""tg-polls Skill dispatcher: create / close / results."""

from __future__ import annotations

import argparse
import json
import re
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


def _split_options(raw: str) -> list[str]:
    """Split comma-separated options, allowing `\\,` to embed a literal comma."""
    # Replace escaped commas with a placeholder, split, restore.
    SENTINEL = "\x00"
    swapped = raw.replace(r"\,", SENTINEL)
    parts = [p.strip().replace(SENTINEL, ",") for p in swapped.split(",")]
    return [p for p in parts if p]


def cmd_create(args, c):
    options = _split_options(args.options)
    if args.quiz and args.multiple:
        raise SystemExit("error: --quiz and --multiple are mutually exclusive")
    if args.quiz and args.correct_option is None:
        raise SystemExit("error: --quiz requires --correct-option")
    return c.poll_create(
        _coerce(args.chat),
        args.question,
        options,
        anonymous=not args.public,
        multiple_choice=args.multiple,
        quiz=args.quiz,
        correct_option=args.correct_option,
        explanation=args.explanation or "",
    )


def cmd_close(args, c):
    return c.poll_close(_coerce(args.chat), args.msg_id)


def cmd_edit(args, c):
    if args.question is None and not args.options:
        raise SystemExit("error: pass --question and/or --options")
    options = _split_options(args.options) if args.options else None
    return c.poll_edit(
        _coerce(args.chat), args.msg_id, question=args.question, options=options
    )


def cmd_results(args, c):
    return c.poll_results(_coerce(args.chat), args.msg_id)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poll.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    cr = sub.add_parser("create")
    cr.add_argument("--chat", required=True)
    cr.add_argument("--question", required=True)
    cr.add_argument("--options", required=True, help="Comma-separated; '\\,' escapes a literal comma")
    cr.add_argument("--public", action="store_true",
                    help="Non-anonymous poll (voters' identities visible)")
    cr.add_argument("--multiple", action="store_true",
                    help="Voters can choose multiple options")
    cr.add_argument("--quiz", action="store_true",
                    help="Quiz mode (one correct answer)")
    cr.add_argument("--correct-option", type=int, default=None,
                    help="0-based index, required with --quiz")
    cr.add_argument("--explanation", default="",
                    help="Shown after a quiz answer is selected (max 200 chars)")

    for name in ("close", "results"):
        s = sub.add_parser(name)
        s.add_argument("--chat", required=True)
        s.add_argument("--msg-id", type=int, required=True)

    e = sub.add_parser("edit")
    e.add_argument("--chat", required=True)
    e.add_argument("--msg-id", type=int, required=True)
    e.add_argument("--question", default=None)
    e.add_argument("--options", default=None,
                   help="Comma-separated; option count must match the original")

    return p


HANDLERS = {
    "create": cmd_create,
    "edit": cmd_edit,
    "close": cmd_close,
    "results": cmd_results,
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
            print(f"error: daemon returned {body.get('error')}: {body.get('detail')}",
                  file=sys.stderr)
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
