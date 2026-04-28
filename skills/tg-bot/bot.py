#!/usr/bin/env python3
"""tg-bot Skill dispatcher: inline keyboards + callbacks + commands."""

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


def _parse_row(spec: str) -> list[dict[str, str]]:
    """Parse one --row argument: comma-separated buttons.

    Each button: 'callback:<text>:<data>' or 'url:<text>:<url>'.
    """
    out: list[dict[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        # Split on the FIRST two colons only — the third part may itself
        # contain a colon (e.g., 'url:Docs:https://example.com').
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise SystemExit(
                f"error: bad button spec {item!r}; expected "
                "'callback:<text>:<data>' or 'url:<text>:<url>'"
            )
        kind, text, payload = parts
        text = text.strip()
        payload = payload.strip()
        if kind == "callback":
            out.append({"kind": "callback", "text": text, "data": payload})
        elif kind == "url":
            out.append({"kind": "url", "text": text, "url": payload})
        else:
            raise SystemExit(
                f"error: unknown button kind {kind!r}; use 'callback' or 'url'"
            )
    if not out:
        raise SystemExit("error: empty --row")
    return out


def _check_bot_mode(c: DaemonClient) -> None:
    """Bail before issuing any /bot/* request if the active account isn't
    a bot. The daemon would 400 anyway, but we want a friendlier message.
    """
    try:
        h = c.health()
    except Exception:
        # If health fails, let the next call surface the real error.
        return
    if not h.get("is_bot"):
        raise SystemExit(
            "error: this skill requires a bot-mode account. The active "
            f"account is {h.get('account')!r} (is_bot={h.get('is_bot')}). "
            "Switch to a bot account with `tgmcp account use <bot-label>` "
            "or create one with `tgmcp init --bot-token <token>`."
        )


def cmd_send(args, c: DaemonClient):
    if not args.row:
        raise SystemExit("error: pass at least one --row")
    rows = [_parse_row(r) for r in args.row]
    _check_bot_mode(c)
    return c.bot_send_keyboard(args.chat, args.text, rows, reply_to=args.reply_to)


def cmd_poll(args, c: DaemonClient):
    _check_bot_mode(c)
    return c.bot_poll_callbacks(timeout=args.timeout, limit=args.limit)


def cmd_answer(args, c: DaemonClient):
    _check_bot_mode(c)
    return c.bot_answer_callback(
        args.query_id,
        text=args.text or "",
        alert=args.alert,
        url=args.url,
        cache_time=args.cache_time,
    )


def cmd_commands(args, c: DaemonClient):
    cmds: list[dict[str, str]] = []
    for spec in args.command or []:
        if ":" not in spec:
            raise SystemExit(
                f"error: --command {spec!r} must be 'name:description'"
            )
        name, desc = spec.split(":", 1)
        cmds.append({"command": name.strip(), "description": desc.strip()})
    _check_bot_mode(c)
    return c.bot_set_commands(cmds, language_code=args.language_code or "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bot.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="Send a message with an inline keyboard")
    s.add_argument("--chat", required=True)
    s.add_argument("--text", required=True)
    s.add_argument(
        "--row",
        action="append",
        help="A comma-separated list of buttons: "
        "'callback:<text>:<data>' or 'url:<text>:<url>'. Pass multiple "
        "times for multi-row keyboards.",
    )
    s.add_argument("--reply-to", type=int, default=None)

    pl = sub.add_parser("poll", help="Drain pending callback queries")
    pl.add_argument("--timeout", type=float, default=0.0)
    pl.add_argument("--limit", type=int, default=50)

    a = sub.add_parser("answer", help="Acknowledge a callback query")
    a.add_argument("--query-id", type=int, required=True)
    a.add_argument("--text", default="")
    a.add_argument("--alert", action="store_true")
    a.add_argument("--url", default=None)
    a.add_argument("--cache-time", type=int, default=0)

    c = sub.add_parser("commands", help="Set the bot's slash-command list")
    c.add_argument(
        "--command",
        action="append",
        help="One command, formatted 'name:description'. Pass multiple times.",
    )
    c.add_argument("--language-code", default="")

    return p


HANDLERS = {
    "send": cmd_send,
    "poll": cmd_poll,
    "answer": cmd_answer,
    "commands": cmd_commands,
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
