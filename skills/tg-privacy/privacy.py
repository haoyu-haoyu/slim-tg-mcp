#!/usr/bin/env python3
"""tg-privacy Skill dispatcher: get / set privacy rules."""

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

KIND_NEEDS_USERS = {"allow_users", "disallow_users"}


def cmd_get(args, c):
    return c.privacy_get(args.key)


def _parse_rules(raw_kinds, raw_users):
    """Pair --rule entries with their --users list (if applicable).

    `--users` is a single comma-separated list shared across all
    *_users rules in this invocation. For finer-grained control,
    issue multiple `set` calls.
    """
    user_ids: list[int] = []
    if raw_users:
        for chunk in raw_users.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                user_ids.append(int(chunk))
            except ValueError as e:
                raise SystemExit(
                    f"error: --users must be numeric ids; got {chunk!r}"
                ) from e

    rules = []
    for kind in raw_kinds:
        rule = {"kind": kind}
        if kind in KIND_NEEDS_USERS:
            if not user_ids:
                raise SystemExit(
                    f"error: rule {kind!r} requires --users <id1,id2,...>"
                )
            rule["user_ids"] = user_ids
        rules.append(rule)
    return rules


def cmd_set(args, c):
    if not args.rule:
        raise SystemExit("error: pass at least one --rule")
    rules = _parse_rules(args.rule, args.users)
    return c.privacy_set(args.key, rules)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="privacy.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("--key", required=True)

    s = sub.add_parser("set")
    s.add_argument("--key", required=True)
    s.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Repeat to chain rules (evaluated in order).",
    )
    s.add_argument(
        "--users",
        default=None,
        help="Comma-separated numeric user ids (required when a rule is "
        "allow_users or disallow_users).",
    )

    return p


HANDLERS = {"get": cmd_get, "set": cmd_set}


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
