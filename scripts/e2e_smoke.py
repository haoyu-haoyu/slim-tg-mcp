#!/usr/bin/env python3
"""End-to-end smoke test against a real Telegram account.

This script touches a live Telegram account, so it CANNOT run in CI. Run it
once locally after `tgmcp init` to confirm the entire Phase 1 + Phase 2
plumbing works against the real MTProto API.

What it does:
  1. Reads TG_API_ID / TG_API_HASH from env (you must export them).
  2. Verifies a session for label `e2e` exists (or guides you to init it).
  3. Starts the daemon as a child process.
  4. Probes /health.
  5. Lists the first 5 dialogs (read-side test).
  6. Sends a message to "Saved Messages" (write to yourself, harmless).
  7. Reads it back via search.
  8. Edits it.
  9. Deletes it.
 10. Stops the daemon.

It refuses to run unless `TGMCP_E2E_CONFIRM=yes` is set, so you don't run
it accidentally.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tgmcp.client import DaemonClient  # noqa: E402
from tgmcp.daemon import auth  # noqa: E402

LABEL = "e2e"


def fail(msg: str, code: int = 1) -> None:
    print(f"\033[31mFAIL: {msg}\033[0m", file=sys.stderr)
    sys.exit(code)


def step(name: str) -> None:
    print(f"\n\033[36m==> {name}\033[0m")


def main() -> int:
    if os.environ.get("TGMCP_E2E_CONFIRM") != "yes":
        fail(
            "This script touches a real Telegram account.\n"
            "  Set TGMCP_E2E_CONFIRM=yes to acknowledge, then re-run.\n"
            "  Recommended: use a burner account, not your main one."
        )

    step("1. Check env")
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        fail("TG_API_ID and TG_API_HASH must be set (https://my.telegram.org).")
    print("    TG_API_ID / TG_API_HASH present")

    step("2. Check session for label=e2e")
    if LABEL not in auth.list_accounts():
        fail(
            f"No session for label={LABEL!r}.\n"
            f"Run: TGMCP_ACCOUNT={LABEL} tgmcp init --label {LABEL}\n"
            "(use your burner account for the phone number prompt)"
        )
    print(f"    found encrypted session for {LABEL!r}")

    step("3. Start daemon (foreground = false, so we can kill it cleanly)")
    env = os.environ.copy()
    env["TGMCP_ACCOUNT"] = LABEL
    daemon = subprocess.Popen(
        [sys.executable, "-m", "tgmcp.cli.main", "daemon", "start", "--account", LABEL],
        env=env,
    )
    if daemon.wait(timeout=15) != 0:
        fail("daemon start exited non-zero")
    print("    daemon up")

    try:
        with DaemonClient(timeout=10.0) as c:
            step("4. /health")
            h = c.health()
            print(f"    pid={h.get('pid')} account={h.get('account')} "
                  f"me_id={h.get('me_id')}")
            if not h.get("ok"):
                fail(f"/health not ok: {h}")
            me_id = h.get("me_id")
            if not me_id:
                fail("daemon did not report me_id; session not authorized?")

            step("5. list_dialogs (read-side)")
            d = c.list_dialogs(limit=5)
            for dialog in d.get("dialogs", []):
                print(f"    {dialog.get('type'):8s} {dialog.get('id'):>15} "
                      f"{dialog.get('title')!r}")

            step("6. Send to Saved Messages (chat=me_id)")
            text = f"slim-tg-mcp e2e ping {int(time.time())}"
            res = c.send(me_id, text)
            sent_id = res["msg_id"]
            print(f"    sent msg_id={sent_id}")

            step("7. Search to confirm it landed")
            found = c.search_in_chat(me_id, query=text, limit=5)
            ids = [m["id"] for m in found.get("messages", [])]
            if sent_id not in ids:
                fail(f"sent msg {sent_id} not found by search; got {ids}")
            print(f"    search hit (ids={ids})")

            step("8. Edit it")
            c.edit(me_id, sent_id, text + " [edited]")
            print("    edited")

            step("9. Delete it (and verify by readback)")
            c.delete(me_id, [sent_id])
            # Verify deletion via search rather than trusting any "count" the
            # daemon reports — Telethon does not return a reliable per-id
            # delete count; only ground truth is "the message is no longer
            # there".
            after = c.search_in_chat(me_id, query=text, limit=5)
            after_ids = [m["id"] for m in after.get("messages", [])]
            if sent_id in after_ids:
                fail(f"delete didn't take effect: msg {sent_id} still searchable")
            print(f"    deletion confirmed (msg {sent_id} no longer searchable)")

    finally:
        step("10. Stop daemon")
        try:
            subprocess.run(
                [sys.executable, "-m", "tgmcp.cli.main", "daemon", "stop"],
                env=env,
                timeout=15,
                check=False,
            )
        except Exception as e:
            print(f"    warning: stop failed: {e}", file=sys.stderr)

    print("\n\033[32mALL E2E STEPS PASSED\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
