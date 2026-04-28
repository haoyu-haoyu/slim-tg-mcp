#!/usr/bin/env python3
"""End-to-end smoke test against a real Telegram account.

Touches a live Telegram account, so it CANNOT run in CI. Run it
locally after `tgmcp init` to confirm the entire stack works against
real MTProto.

Test groups (each can be enabled/disabled independently):

    --core            v0.1 / v0.2 — list / search / send / edit / delete
                      (the original e2e — always run)
    --topics          v0.5 — forum topics CRUD on a forum supergroup you
                      own. Skipped unless --topics-chat is provided.
    --stories         v0.5 — read your own active stories + your pinned
                      stories list. Read-only — safe.
    --location        v0.5 — send a static location pin to Saved
                      Messages and immediately delete it.
    --privacy         v0.4 — read each of the 10 privacy keys.
                      Read-only.
    --folders         v0.4 — list folders. Read-only.
    --metrics         v0.5 — confirm /metrics endpoint serves something
                      Prometheus-shaped.
    --bot             v0.5 — bot-mode RPCs against a SEPARATE label
                      (TGMCP_E2E_BOT_LABEL, default "e2e-bot"). Requires
                      that label to have been initialized with
                      `tgmcp init --bot-token-stdin --label e2e-bot`.

Default with no flags: --core + --metrics + --privacy + --folders +
--stories. (Read-only or self-only — the safest set.)

It refuses to run unless `TGMCP_E2E_CONFIRM=yes` is set, so you don't
fire it accidentally.
"""

from __future__ import annotations

import argparse
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
BOT_LABEL = os.environ.get("TGMCP_E2E_BOT_LABEL", "e2e-bot")


def fail(msg: str, code: int = 1) -> None:
    print(f"\033[31mFAIL: {msg}\033[0m", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"\033[33mWARN: {msg}\033[0m", file=sys.stderr)


def step(name: str) -> None:
    print(f"\n\033[36m==> {name}\033[0m")


def ok(msg: str) -> None:
    print(f"    \033[32m✓\033[0m {msg}")


# ---------- core (v0.1 + v0.2) ----------


def run_core(c: DaemonClient, me_id: int) -> None:
    step("core: list_dialogs (read-side)")
    d = c.list_dialogs(limit=5)
    for dialog in d.get("dialogs", []):
        print(
            f"    {dialog.get('type'):8s} {dialog.get('id'):>15} "
            f"{dialog.get('title')!r}"
        )
    ok(f"got {len(d.get('dialogs', []))} dialogs")

    step("core: send to Saved Messages")
    text = f"slim-tg-mcp e2e ping {int(time.time())}"
    res = c.send(me_id, text)
    sent_id = res["msg_id"]
    ok(f"sent msg_id={sent_id}")

    step("core: search to confirm")
    found = c.search_in_chat(me_id, query=text, limit=5)
    ids = [m["id"] for m in found.get("messages", [])]
    if sent_id not in ids:
        fail(f"sent msg {sent_id} not found by search; got {ids}")
    ok(f"search found it (ids={ids})")

    step("core: edit")
    c.edit(me_id, sent_id, text + " [edited]")
    ok("edited")

    step("core: delete and verify via readback")
    c.delete(me_id, [sent_id])
    after = c.search_in_chat(me_id, query=text, limit=5)
    after_ids = [m["id"] for m in after.get("messages", [])]
    if sent_id in after_ids:
        fail(f"delete didn't take effect: msg {sent_id} still searchable")
    ok(f"deletion confirmed (msg {sent_id} no longer searchable)")


# ---------- privacy (v0.4 read-only) ----------


def run_privacy(c: DaemonClient) -> None:
    step("privacy: read all 10 privacy keys")
    keys = [
        "status",
        "photo",
        "calls",
        "forwards",
        "chat_invite",
        "phone",
        "added_by_phone",
        "voice",
        "about",
        "p2p",
    ]
    for key in keys:
        try:
            res = c.privacy_get(key)
            rules = res.get("rules", [])
            ok(f"{key}: {len(rules)} rule(s)")
        except Exception as e:
            warn(f"{key}: {type(e).__name__}: {e}")


# ---------- folders (v0.4 read-only) ----------


def run_folders(c: DaemonClient) -> None:
    step("folders: list")
    res = c.folders_list()
    folders = res.get("folders", [])
    for f in folders:
        ok(f"id={f.get('id')} title={f.get('title')!r}")
    if not folders:
        ok("(no chat folders configured — that's fine)")


# ---------- stories (v0.5 read-only) ----------


def run_stories(c: DaemonClient, me_id: int) -> None:
    step("stories: list your own active stories")
    try:
        res = c.stories_active("me")
        stories = res.get("stories", [])
        ok(f"found {len(stories)} active stories on your own account")
        for s in stories[:3]:
            print(f"    kind={s.get('kind')} id={s.get('id')}")
    except Exception as e:
        warn(f"active stories failed: {type(e).__name__}: {e}")

    step("stories: list your pinned stories")
    try:
        res = c.stories_pinned("me", limit=10)
        stories = res.get("stories", [])
        ok(f"found {len(stories)} pinned stories")
    except Exception as e:
        warn(f"pinned stories failed: {type(e).__name__}: {e}")


# ---------- location (v0.5 — sends to Saved Messages, then deletes) ----------


def run_location(c: DaemonClient, me_id: int) -> None:
    step("location: send static pin to Saved Messages")
    res = c.location_send(me_id, lat=37.7749, lng=-122.4194, accuracy=50)
    msg_id = res["msg_id"]
    ok(f"sent location msg_id={msg_id}")

    step("location: delete the pin we just sent")
    c.delete(me_id, [msg_id])
    ok("deleted")


# ---------- topics (v0.5 — destructive; needs --topics-chat) ----------


def run_topics(c: DaemonClient, chat: str) -> None:
    # In-group failures use RuntimeError so the top-level group wrapper
    # (which catches Exception, not BaseException) can isolate them
    # without aborting the whole script. `fail()` is reserved for the
    # script-level prerequisites (missing env, missing session).
    step(f"topics: list in {chat}")
    try:
        res = c.topics_list(chat, limit=10)
        topics = res.get("topics", [])
        ok(f"found {len(topics)} topic(s)")
        for t in topics[:3]:
            print(f"    id={t.get('id')} title={t.get('title')!r}")
    except Exception as e:
        raise RuntimeError(f"topics_list failed: {type(e).__name__}: {e}") from e

    step(f"topics: create test topic in {chat}")
    title = f"e2e-test-{int(time.time())}"
    try:
        res = c.topics_create(chat, title)
        topic_id = res.get("topic_id")
        if not topic_id:
            warn(
                "topic_create returned topic_id=0 — Telethon may not have "
                "echoed the new topic id. Skip edit/delete."
            )
            return
        ok(f"created topic_id={topic_id}")
    except Exception as e:
        raise RuntimeError(
            f"topics_create failed: {type(e).__name__}: {e}"
        ) from e

    step(f"topics: edit test topic title")
    try:
        c.topics_edit(chat, topic_id, title=title + " [edited]")
        ok("edited")
    except Exception as e:
        warn(f"topic edit failed (may need admin perms): {type(e).__name__}: {e}")

    step(f"topics: delete test topic (irreversible)")
    try:
        c.topics_delete(chat, topic_id)
        ok("deleted")
    except Exception as e:
        warn(
            f"topic delete failed: {type(e).__name__}: {e}\n"
            f"    NOTE: topic id={topic_id} may still exist in {chat}; "
            f"clean up manually."
        )


# ---------- metrics (v0.5) ----------


def run_metrics() -> None:
    step("metrics: GET /metrics")
    with DaemonClient(timeout=5.0) as c:
        body = c.get_metrics_text()
    if "tgmcp_rpc_requests_total" not in body:
        raise RuntimeError("metrics endpoint missing tgmcp_rpc_requests_total")
    if "tgmcp_daemon_up" not in body:
        raise RuntimeError("metrics endpoint missing tgmcp_daemon_up")
    ok("metrics endpoint serving prometheus-shaped output")


# ---------- bot (v0.5 — separate label) ----------


def run_bot() -> None:
    step(f"bot: switch active account to {BOT_LABEL!r}")
    if BOT_LABEL not in auth.list_accounts():
        warn(
            f"no on-disk session for label={BOT_LABEL!r}; skipping bot tests.\n"
            f"    To enable: TGMCP_E2E_BOT_TOKEN=... or run\n"
            f"    `printf %s '<bot-token>' | tgmcp init --label {BOT_LABEL} "
            "--bot-token-stdin`"
        )
        return
    with DaemonClient(timeout=10.0) as c:
        try:
            c.switch_account(BOT_LABEL)
        except Exception as e:
            raise RuntimeError(
                f"switch_account({BOT_LABEL!r}) failed: {type(e).__name__}: {e}"
            ) from e
        h = c.health()
        if not h.get("is_bot"):
            # Best-effort restore so a failure here doesn't strand the
            # daemon on the wrong account.
            try:
                c.switch_account(LABEL)
            except Exception:
                pass
            raise RuntimeError(
                f"after switch, is_bot={h.get('is_bot')}; expected True"
            )
        ok(f"active bot account: {h.get('account')} (me_id={h.get('me_id')})")

        step("bot: poll callbacks (non-blocking, expect empty)")
        bot_failure: Exception | None = None
        try:
            res = c.bot_poll_callbacks(timeout=0, limit=10)
            ok(f"queue had {res.get('count', 0)} callback(s)")
        except Exception as e:
            bot_failure = e

        step(f"bot: switch back to user account {LABEL!r}")
        try:
            c.switch_account(LABEL)
            ok("switched back")
        except Exception as e:
            warn(
                f"switch back failed: {type(e).__name__}: {e}\n"
                f"    The daemon is still on bot label {BOT_LABEL!r}. "
                f"Restart with `tgmcp daemon stop && tgmcp daemon start "
                f"--account {LABEL}` if you want the user session back."
            )

        if bot_failure is not None:
            raise RuntimeError(
                f"bot_poll_callbacks failed: "
                f"{type(bot_failure).__name__}: {bot_failure}"
            ) from bot_failure


# ---------- main ----------


def main() -> int:
    if os.environ.get("TGMCP_E2E_CONFIRM") != "yes":
        fail(
            "This script touches a real Telegram account.\n"
            "  Set TGMCP_E2E_CONFIRM=yes to acknowledge, then re-run.\n"
            "  Recommended: use a burner account, not your main one."
        )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", action="store_true", help="v0.1+v0.2 core flow")
    parser.add_argument("--privacy", action="store_true", help="read 10 privacy keys")
    parser.add_argument("--folders", action="store_true", help="list chat folders")
    parser.add_argument("--stories", action="store_true", help="read own active+pinned stories")
    parser.add_argument(
        "--location",
        action="store_true",
        help="send a static location pin to Saved Messages and delete it",
    )
    parser.add_argument(
        "--topics-chat",
        default=None,
        help="forum supergroup id/username to test against (skipped if absent)",
    )
    parser.add_argument("--metrics", action="store_true", help="probe /metrics endpoint")
    parser.add_argument(
        "--bot",
        action="store_true",
        help=f"validate bot mode using a separate label (default: {BOT_LABEL!r})",
    )
    parser.add_argument("--all", action="store_true", help="enable every group above")
    args = parser.parse_args()

    # Default: read-only + self-only safe set
    if not any(
        [
            args.core,
            args.privacy,
            args.folders,
            args.stories,
            args.location,
            args.topics_chat,
            args.metrics,
            args.bot,
            args.all,
        ]
    ):
        args.core = True
        args.metrics = True
        args.privacy = True
        args.folders = True
        args.stories = True

    if args.all:
        args.core = True
        args.privacy = True
        args.folders = True
        args.stories = True
        args.location = True
        args.metrics = True
        args.bot = True

    step("0. Check env")
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        fail("TG_API_ID and TG_API_HASH must be set (https://my.telegram.org).")
    ok("TG_API_ID / TG_API_HASH present")

    step("1. Check session for label=e2e")
    if LABEL not in auth.list_accounts():
        fail(
            f"No session for label={LABEL!r}.\n"
            f"Run: tgmcp init --label {LABEL}\n"
            "(use your burner account for the phone number prompt)"
        )
    ok(f"found encrypted session for {LABEL!r}")

    step("2. Start daemon")
    env = os.environ.copy()
    env["TGMCP_ACCOUNT"] = LABEL
    daemon = subprocess.Popen(
        [sys.executable, "-m", "tgmcp.cli.main", "daemon", "start", "--account", LABEL],
        env=env,
    )
    if daemon.wait(timeout=15) != 0:
        fail("daemon start exited non-zero")
    ok("daemon up")

    failures: list[str] = []
    try:
        with DaemonClient(timeout=15.0) as c:
            step("3. /health")
            h = c.health()
            if not h.get("ok"):
                fail(f"/health not ok: {h}")
            me_id = h.get("me_id")
            if not me_id:
                fail("daemon did not report me_id; session not authorized?")
            ok(f"pid={h.get('pid')} account={h.get('account')} me_id={me_id}")

            if args.core:
                try:
                    run_core(c, me_id)
                except SystemExit:
                    raise
                except Exception as e:
                    failures.append(f"core: {type(e).__name__}: {e}")

            if args.privacy:
                try:
                    run_privacy(c)
                except Exception as e:
                    failures.append(f"privacy: {type(e).__name__}: {e}")

            if args.folders:
                try:
                    run_folders(c)
                except Exception as e:
                    failures.append(f"folders: {type(e).__name__}: {e}")

            if args.stories:
                try:
                    run_stories(c, me_id)
                except Exception as e:
                    failures.append(f"stories: {type(e).__name__}: {e}")

            if args.location:
                try:
                    run_location(c, me_id)
                except Exception as e:
                    failures.append(f"location: {type(e).__name__}: {e}")

            if args.topics_chat:
                try:
                    run_topics(c, args.topics_chat)
                except Exception as e:
                    failures.append(f"topics: {type(e).__name__}: {e}")

        if args.metrics:
            try:
                run_metrics()
            except Exception as e:
                failures.append(f"metrics: {type(e).__name__}: {e}")

        if args.bot:
            try:
                run_bot()
            except Exception as e:
                failures.append(f"bot: {type(e).__name__}: {e}")

    finally:
        step("Stop daemon")
        try:
            subprocess.run(
                [sys.executable, "-m", "tgmcp.cli.main", "daemon", "stop"],
                env=env,
                timeout=15,
                check=False,
            )
        except Exception as e:
            warn(f"stop failed: {e}")

    if failures:
        print("\n\033[31m╔══ E2E FAILURES ══╗\033[0m")
        for f in failures:
            print(f"\033[31m  • {f}\033[0m")
        print(f"\n\033[31m{len(failures)} group(s) failed; see above.\033[0m")
        return 1

    print("\n\033[32m╔══ ALL E2E STEPS PASSED ══╗\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
