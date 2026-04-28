#!/usr/bin/env python3
"""tg-stickers-gifs Skill dispatcher."""

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


def _coerce(v: str) -> str | int:
    if v.lstrip("-").isdigit():
        return int(v)
    return v


def cmd_gif_saved(args, c):
    return c.gif_saved()


def cmd_gif_send(args, c):
    return c.gif_send(
        _coerce(args.chat), args.doc_id, args.access_hash, args.file_ref_hex
    )


def cmd_sticker_saved(args, c):
    return c.sticker_saved()


def cmd_sticker_set(args, c):
    return c.sticker_set(args.set_id, args.access_hash)


def cmd_sticker_send(args, c):
    return c.sticker_send(
        _coerce(args.chat), args.doc_id, args.access_hash, args.file_ref_hex
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sg.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gif-saved")

    for name, parser_var in [("gif-send", "gn"), ("sticker-send", "ss")]:
        s = sub.add_parser(name)
        s.add_argument("--chat", required=True)
        s.add_argument("--doc-id", type=int, required=True)
        s.add_argument("--access-hash", type=int, required=True)
        s.add_argument("--file-ref-hex", required=True)

    sub.add_parser("sticker-saved")

    ssset = sub.add_parser("sticker-set")
    ssset.add_argument("--set-id", type=int, required=True)
    ssset.add_argument("--access-hash", type=int, required=True)

    return p


HANDLERS = {
    "gif-saved": cmd_gif_saved,
    "gif-send": cmd_gif_send,
    "sticker-saved": cmd_sticker_saved,
    "sticker-set": cmd_sticker_set,
    "sticker-send": cmd_sticker_send,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        with DaemonClient() as c:
            res = HANDLERS[args.cmd](args, c)
    except (httpx.ConnectError, FileNotFoundError, ConnectionRefusedError, OSError) as e:
        print(f"error: cannot reach daemon ({type(e).__name__}: {e}). "
              "Start it with `tgmcp daemon start`.", file=sys.stderr)
        return 3
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
            print(f"error: daemon returned {body.get('error')}: {body.get('detail')}",
                  file=sys.stderr)
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
