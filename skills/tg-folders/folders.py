#!/usr/bin/env python3
"""tg-folders Skill dispatcher."""

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


def _split_peers(raw: str | None) -> list[str | int]:
    if not raw:
        return []
    out: list[str | int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
        else:
            out.append(chunk)
    return out


def cmd_list(args, c):
    return c.folders_list()


def cmd_update(args, c):
    return c.folders_update(
        args.id,
        title=args.title,
        include_peers=_split_peers(args.include),
        exclude_peers=_split_peers(args.exclude),
        contacts=args.contacts,
        non_contacts=args.non_contacts,
        groups=args.groups,
        broadcasts=args.broadcasts,
        bots=args.bots,
    )


def cmd_delete(args, c):
    return c.folders_delete(args.id)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="folders.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    u = sub.add_parser("update")
    u.add_argument("--id", type=int, required=True)
    u.add_argument("--title", required=True)
    u.add_argument("--include", default=None, help="Comma-separated peers")
    u.add_argument("--exclude", default=None, help="Comma-separated peers")
    u.add_argument("--contacts", action="store_true")
    u.add_argument("--non-contacts", action="store_true")
    u.add_argument("--groups", action="store_true")
    u.add_argument("--broadcasts", action="store_true")
    u.add_argument("--bots", action="store_true")

    d = sub.add_parser("delete")
    d.add_argument("--id", type=int, required=True)

    return p


HANDLERS = {"list": cmd_list, "update": cmd_update, "delete": cmd_delete}


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
