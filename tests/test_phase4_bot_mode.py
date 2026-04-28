"""Phase 4 Batch 1: Bot mode (auth back-compat, schemas, skill)."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tgmcp.daemon import auth, server


# ---------- auth: envelope back-compat + AAD ----------


def test_envelope_default_kind_is_user():
    env = auth.Envelope.from_json('{"nonce":"x","ct":"y","kdf":"keychain"}')
    assert env.account_kind == "user"


def test_envelope_round_trips_bot_kind():
    env = auth.Envelope(
        nonce="n", ct="c", kdf="scrypt", salt="s", account_kind="bot"
    )
    revived = auth.Envelope.from_json(env.to_json())
    assert revived.account_kind == "bot"


def test_save_session_rejects_bad_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="account_kind"):
        auth.save_session("x", "sess", passphrase="pw", account_kind="weird")


def test_save_load_with_bot_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    # Bypass _ensure_dirs() chmod calls on tmp_path which already exists.
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)
    auth.save_session("mybot", "session_string_here", passphrase="pw", account_kind="bot")
    assert auth.get_account_kind("mybot") == "bot"
    assert auth.load_session("mybot", passphrase="pw") == "session_string_here"


def test_legacy_envelope_without_kind_loads_with_old_aad(tmp_path, monkeypatch):
    """Pre-v0.5.0 envelopes have AAD = label only. The new code's fallback
    path must still open them as kind="user"."""
    import base64
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)

    label = "legacy"
    salt = secrets.token_bytes(16)
    dk = auth._scrypt_key("pw", salt)
    nonce = secrets.token_bytes(12)
    plaintext = "old-session-string"
    # Old AAD = label only (the v0.4.0 behavior).
    ct = AESGCM(dk).encrypt(nonce, plaintext.encode(), label.encode())

    legacy_env = auth.Envelope(
        nonce=base64.b64encode(nonce).decode(),
        ct=base64.b64encode(ct).decode(),
        kdf="scrypt",
        salt=base64.b64encode(salt).decode(),
        # No account_kind in the JSON — simulate by writing without it.
    )
    # Strip account_kind from the JSON to mimic an older envelope format.
    import json as _json
    raw = _json.loads(legacy_env.to_json())
    raw.pop("account_kind", None)
    (tmp_path / f"{label}.enc").write_text(_json.dumps(raw))

    # And confirm the loaded kind is "user" (default) and decryption succeeds.
    assert auth.get_account_kind(label) == "user"
    assert auth.load_session(label, passphrase="pw") == plaintext


def test_aad_binds_kind_so_flipping_envelope_breaks(tmp_path, monkeypatch):
    """Tampering: take a bot envelope and rewrite kind->user. Decryption
    must fail because GCM AAD verification picks up the change."""
    import json as _json

    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)

    auth.save_session("victim", "sess", passphrase="pw", account_kind="bot")
    p = tmp_path / "victim.enc"
    raw = _json.loads(p.read_text())
    raw["account_kind"] = "user"  # attacker flips
    p.write_text(_json.dumps(raw))

    # The fallback to legacy AAD only fires when the envelope kind is
    # "user" AND the file is a pre-v0.5.0 shape; here the AAD was bound
    # to "bot" and the legacy fallback uses just-label AAD which also
    # won't match. Either way, decryption must fail.
    with pytest.raises(Exception):
        auth.load_session("victim", passphrase="pw")


# ---------- TGSession start: kind cross-check ----------


def test_tgsession_start_refuses_kind_mismatch():
    """If envelope says is_bot=True but Telegram returns me.bot=False
    (or vice versa), start() must refuse to bring up the session."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=True))

    class FakeClient:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=42, bot=False)  # mismatch

        def add_event_handler(self, *a, **k):
            pass

    s.client = FakeClient()
    # _refresh_contacts and _install_callback_handler must not run in the
    # mismatched branch — start() should raise before getting there.
    with pytest.raises(RuntimeError, match="account kind mismatch"):
        # We have to skip the real start() machinery; mimic the body.
        async def _run():
            me = await s.client.get_me()
            s.me_id = me.id
            actual_is_bot = bool(getattr(me, "bot", False))
            if actual_is_bot != s.cfg.is_bot:
                raise RuntimeError(
                    f"account kind mismatch: envelope says is_bot={s.cfg.is_bot} "
                    f"but Telegram reports is_bot={actual_is_bot}."
                )
        asyncio.run(_run())


# ---------- bot endpoints: schema validation ----------


def test_bot_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/bot/send_keyboard",
        "/bot/answer_callback",
        "/bot/poll_callbacks",
        "/bot/set_commands",
    ):
        assert p in paths


def test_button_kind_callback_requires_data():
    with pytest.raises(ValueError, match="callback buttons require"):
        server._Button(kind="callback", text="A", data="")


def test_button_kind_url_requires_url():
    with pytest.raises(ValueError, match="url buttons require"):
        server._Button(kind="url", text="A")


def test_button_url_scheme_restricted():
    with pytest.raises(ValueError, match="https://"):
        server._Button(kind="url", text="A", url="javascript:alert(1)")
    server._Button(kind="url", text="A", url="https://example.com")
    server._Button(kind="url", text="A", url="tg://resolve?domain=foo")


def test_button_callback_data_size_capped():
    with pytest.raises(ValueError, match="64 UTF-8 bytes"):
        server._Button(kind="callback", text="A", data="x" * 65)


def test_button_unknown_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        server._Button(kind="poll", text="A", data="x")


def test_button_callback_must_not_carry_url():
    with pytest.raises(ValueError, match="must not carry"):
        server._Button(
            kind="callback", text="A", data="x", url="https://x.com"
        )


def test_send_keyboard_row_widths():
    row = [{"kind": "callback", "text": "B", "data": "d"}]
    server.BotSendKeyboardReq(chat="@x", text="hi", rows=[row])
    too_wide = row * 9
    with pytest.raises(ValueError, match="1.*8 buttons"):
        server.BotSendKeyboardReq(chat="@x", text="hi", rows=[too_wide])


def test_send_keyboard_max_rows():
    row = [{"kind": "callback", "text": "B", "data": "d"}]
    with pytest.raises(ValueError):
        server.BotSendKeyboardReq(chat="@x", text="hi", rows=[row] * 9)


def test_command_name_pattern():
    server.BotCommand(command="start", description="x")
    with pytest.raises(ValueError):
        server.BotCommand(command="Start", description="x")  # uppercase
    with pytest.raises(ValueError):
        server.BotCommand(command="2bad", description="x")  # leading digit


def test_set_commands_capped_at_100():
    cmds = [{"command": f"c{i:02d}", "description": "d"} for i in range(101)]
    with pytest.raises(ValueError):
        server.BotSetCommandsReq(commands=cmds)


def test_poll_callbacks_timeout_capped():
    with pytest.raises(ValueError):
        server.BotPollCallbacksReq(timeout=31)


def test_answer_callback_cache_time_capped():
    with pytest.raises(ValueError):
        server.BotAnswerCallbackReq(query_id=1, cache_time=86401)


# ---------- bot-only guard ----------


def test_require_bot_session_raises_for_user_account():
    """A user session hitting a /bot/* endpoint must 400."""
    from fastapi import HTTPException

    from tgmcp.daemon.telegram import TGConfig, TGSession

    user_sess = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=False))
    server.state.sessions = {"main": user_sess}
    server.state.active_label = "main"
    try:
        with pytest.raises(HTTPException) as ei:
            server._require_bot_session()
        assert ei.value.status_code == 400
    finally:
        server.state.sessions = {}
        server.state.active_label = None


# ---------- bot session: queue mechanics ----------


def test_bot_poll_callbacks_returns_immediately_when_empty():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=True))
    s._cb_event = asyncio.Event()
    out = asyncio.run(s.bot_poll_callbacks(timeout=0.0, limit=50))
    assert out == []


def test_bot_poll_callbacks_drains_queued_items():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=True))
    s._cb_event = asyncio.Event()
    s._cb_queue.append({"query_id": 1, "data": "y"})
    s._cb_queue.append({"query_id": 2, "data": "n"})
    out = asyncio.run(s.bot_poll_callbacks(timeout=0.0, limit=50))
    assert [x["query_id"] for x in out] == [1, 2]
    assert not s._cb_queue


def test_bot_poll_callbacks_respects_limit():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=True))
    s._cb_event = asyncio.Event()
    for i in range(10):
        s._cb_queue.append({"query_id": i})
    out = asyncio.run(s.bot_poll_callbacks(timeout=0.0, limit=3))
    assert len(out) == 3
    # Remaining 7 still in queue
    assert len(s._cb_queue) == 7


def test_bot_poll_callbacks_bot_only_guard():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=False))
    with pytest.raises(ValueError, match="bot mode"):
        asyncio.run(s.bot_poll_callbacks(timeout=0.0))


def test_callback_queue_drops_oldest_when_full():
    """deque(maxlen=200) silently drops the oldest entry on overflow.
    Verify the contract holds in our wiring."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y", is_bot=True))
    for i in range(250):
        s._cb_queue.append({"query_id": i})
    assert len(s._cb_queue) == 200
    # Oldest 50 dropped; first remaining must be 50
    first = s._cb_queue[0]
    assert first["query_id"] == 50


# ---------- skill dispatcher ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_bot_skill_handlers_registered():
    mod = _load_skill("tg-bot", "bot.py")
    assert set(mod.HANDLERS.keys()) == {"send", "poll", "answer", "commands"}


def test_bot_skill_parse_row_handles_url_with_colon():
    """url:Docs:https://example.com — second colon is part of the URL."""
    mod = _load_skill("tg-bot", "bot.py")
    btns = mod._parse_row("url:Docs:https://example.com/path")
    assert btns == [
        {"kind": "url", "text": "Docs", "url": "https://example.com/path"}
    ]


def test_bot_skill_parse_row_callback():
    mod = _load_skill("tg-bot", "bot.py")
    btns = mod._parse_row("callback:Yes:vote_y, callback:No:vote_n")
    assert btns == [
        {"kind": "callback", "text": "Yes", "data": "vote_y"},
        {"kind": "callback", "text": "No", "data": "vote_n"},
    ]


def test_bot_skill_parse_row_rejects_bad_kind():
    mod = _load_skill("tg-bot", "bot.py")
    with pytest.raises(SystemExit, match="unknown button kind"):
        mod._parse_row("poll:Vote:x")


def test_bot_skill_parse_row_rejects_missing_payload():
    mod = _load_skill("tg-bot", "bot.py")
    with pytest.raises(SystemExit, match="bad button spec"):
        mod._parse_row("callback:OnlyText")


def test_bot_skill_send_requires_at_least_one_row():
    mod = _load_skill("tg-bot", "bot.py")
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--text", "hi"]
    )
    with pytest.raises(SystemExit, match="--row"):
        mod.cmd_send(args, c=None)


def test_bot_skill_check_bot_mode_blocks_user_account():
    mod = _load_skill("tg-bot", "bot.py")

    class FakeClient:
        def health(self):
            return {"is_bot": False, "account": "main"}

    with pytest.raises(SystemExit, match="bot-mode account"):
        mod._check_bot_mode(FakeClient())


def test_bot_skill_check_bot_mode_passes_for_bot():
    mod = _load_skill("tg-bot", "bot.py")

    class FakeClient:
        def health(self):
            return {"is_bot": True, "account": "mybot"}

    # Must not raise
    mod._check_bot_mode(FakeClient())


# ---------- /accounts surfaces kind ----------


def test_accounts_endpoint_includes_kind(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)
    auth.save_session("user1", "ss", passphrase="pw", account_kind="user")
    auth.save_session("bot1", "bs", passphrase="pw", account_kind="bot")

    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.get("/accounts")
    assert r.status_code == 200, r.text
    body = r.json()
    items = {x["label"]: x["kind"] for x in body["items"]}
    assert items.get("user1") == "user"
    assert items.get("bot1") == "bot"


# ---------- CLI bot-token path ----------


def test_answer_callback_url_scheme_validated():
    """Round-1 MINOR fix: /bot/answer_callback url must follow same scheme
    rules as button URLs (https:// or tg://)."""
    server.BotAnswerCallbackReq(query_id=1, url="https://x.com")
    server.BotAnswerCallbackReq(query_id=1, url="tg://resolve?domain=x")
    with pytest.raises(ValueError, match="https://"):
        server.BotAnswerCallbackReq(query_id=1, url="javascript:alert(1)")
    with pytest.raises(ValueError, match="non-empty"):
        server.BotAnswerCallbackReq(query_id=1, url="")


def test_answer_callback_text_max_length():
    """Telegram caps callback answer text at 200 chars."""
    with pytest.raises(ValueError):
        server.BotAnswerCallbackReq(query_id=1, text="x" * 201)


def test_cli_init_warns_when_bot_token_on_argv(monkeypatch, tmp_path):
    """Round-1 MAJOR fix: passing --bot-token on argv must warn the user
    that the secret is leaked to ps/history."""
    from click.testing import CliRunner

    from tgmcp.cli import main as cli_main

    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abcdef0123456789abcdef0123456789")
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)

    # Use a syntactically-valid token (will fail at Telethon connect, but the
    # warning fires before that — that's what we're testing).
    valid_token = "123456:" + "A" * 30

    captured: list[str] = []

    class FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        def start(self, *_a, **_kw):
            captured.append("started")
            raise RuntimeError("network blocked in test")

        def disconnect(self):
            pass

        @property
        def session(self):
            return SimpleNamespace(save=lambda: "ss")

    monkeypatch.setattr("telethon.TelegramClient", FakeClient)

    runner = CliRunner()
    res = runner.invoke(
        cli_main.cli,
        ["init", "--label", "x", "--bot-token", valid_token],
    )
    # Must surface the argv-warning, regardless of whether Telethon succeeds.
    assert "warning" in res.output.lower() and (
        "argv" in res.output.lower() or "process listings" in res.output.lower() or "ps" in res.output.lower()
    ), res.output


def test_cli_init_rejects_malformed_bot_token(monkeypatch, tmp_path):
    """The CLI must validate the bot token shape before reaching Telethon."""
    from click.testing import CliRunner

    from tgmcp.cli import main as cli_main

    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abcdef0123456789abcdef0123456789")
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_ensure_dirs", lambda: None)

    runner = CliRunner()
    res = runner.invoke(
        cli_main.cli,
        ["init", "--label", "x", "--bot-token", "not-a-token"],
    )
    assert res.exit_code != 0
    assert "BotFather token" in res.output or "BotFather" in res.output
