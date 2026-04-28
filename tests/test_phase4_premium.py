"""Phase 4 Batch 5: premium reactions + emoji status."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgmcp.daemon import server


# ---------- react: extended schema ----------


def test_react_emoji_only():
    server.ReactReq(chat="@x", msg_id=1, emoji="👍")


def test_react_custom_emoji_only():
    server.ReactReq(chat="@x", msg_id=1, custom_emoji_id=12345)


def test_react_clear():
    server.ReactReq(chat="@x", msg_id=1)


def test_react_emoji_xor_custom():
    """Round-1 invariant: emoji and custom_emoji_id are mutually exclusive."""
    with pytest.raises(ValueError, match="not both"):
        server.ReactReq(chat="@x", msg_id=1, emoji="👍", custom_emoji_id=42)


def test_react_big_flag_round_trips():
    req = server.ReactReq(chat="@x", msg_id=1, emoji="👍", big=True)
    assert req.big is True


# ---------- emoji status schema ----------


def test_emoji_status_clear():
    req = server.SetEmojiStatusReq(document_id=None)
    assert req.document_id is None
    assert req.until is None


def test_emoji_status_set_simple():
    server.SetEmojiStatusReq(document_id=12345)


def test_emoji_status_until_requires_document():
    """Setting `until` without a document_id is meaningless — must reject."""
    fut = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(ValueError, match="non-null"):
        server.SetEmojiStatusReq(document_id=None, until=fut)


def test_emoji_status_until_must_be_tz_aware():
    naive = datetime(2030, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        server.SetEmojiStatusReq(document_id=12345, until=naive)


def test_emoji_status_until_must_be_future():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with pytest.raises(ValueError, match="future"):
        server.SetEmojiStatusReq(document_id=12345, until=past)


def test_emoji_status_set_with_until():
    fut = datetime.now(timezone.utc) + timedelta(hours=2)
    server.SetEmojiStatusReq(document_id=12345, until=fut)


def test_emoji_status_route_registered():
    paths = {r.path for r in server.app.routes}
    assert "/profile/emoji_status" in paths


# ---------- session-layer: react extended ----------


def test_session_react_rejects_both_emoji_and_custom():
    """The TGSession-layer guard echoes the schema invariant."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="not both"):
        asyncio.run(
            s.react("@x", 1, "👍", custom_emoji_id=42)
        )


# ---------- session-layer: set_emoji_status branches ----------


def test_set_emoji_status_clear_uses_empty_type():
    """document_id=None must produce an EmojiStatusEmpty TL object."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured = {}

    class FakeClient:
        def __call__(self, req):
            captured["req"] = req
            async def _coro():
                return SimpleNamespace()
            return _coro()

    s.client = FakeClient()
    asyncio.run(s.set_emoji_status(None))
    assert captured["req"].emoji_status.__class__.__name__ == "EmojiStatusEmpty"


def test_set_emoji_status_set_uses_emoji_status_type():
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured = {}

    class FakeClient:
        def __call__(self, req):
            captured["req"] = req
            async def _coro():
                return SimpleNamespace()
            return _coro()

    s.client = FakeClient()
    asyncio.run(s.set_emoji_status(98765))
    es = captured["req"].emoji_status
    assert es.__class__.__name__ == "EmojiStatus"
    assert es.document_id == 98765


# ---------- skill: tg-messaging react extension ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_messaging_react_requires_exactly_one_kind():
    mod = _load_skill("tg-messaging", "act.py")
    parser = mod.build_parser()

    # No flag at all
    args = parser.parse_args(
        ["react", "--chat", "@x", "--msg-id", "1"]
    )
    with pytest.raises(SystemExit, match="exactly one"):
        mod.cmd_react(args, c=None)

    # Two flags simultaneously
    args = parser.parse_args(
        [
            "react", "--chat", "@x", "--msg-id", "1",
            "--emoji", "👍", "--custom-emoji-id", "42",
        ]
    )
    with pytest.raises(SystemExit, match="exactly one"):
        mod.cmd_react(args, c=None)


def test_messaging_react_passes_big_flag():
    mod = _load_skill("tg-messaging", "act.py")
    parser = mod.build_parser()

    captured = {}

    class FakeClient:
        def react(self, chat, msg_id, emoji, *, custom_emoji_id, big):
            captured.update(
                chat=chat, msg_id=msg_id, emoji=emoji,
                custom_emoji_id=custom_emoji_id, big=big
            )
            return {"ok": True}

    args = parser.parse_args(
        ["react", "--chat", "@x", "--msg-id", "5", "--emoji", "🔥", "--big"]
    )
    mod.cmd_react(args, FakeClient())
    assert captured["emoji"] == "🔥"
    assert captured["big"] is True


def test_messaging_react_passes_custom_emoji():
    mod = _load_skill("tg-messaging", "act.py")
    parser = mod.build_parser()

    captured = {}

    class FakeClient:
        def react(self, chat, msg_id, emoji, *, custom_emoji_id, big):
            captured.update(
                chat=chat, msg_id=msg_id, emoji=emoji,
                custom_emoji_id=custom_emoji_id, big=big
            )
            return {"ok": True}

    args = parser.parse_args(
        [
            "react", "--chat", "@x", "--msg-id", "5",
            "--custom-emoji-id", "1234567890",
        ]
    )
    mod.cmd_react(args, FakeClient())
    assert captured["custom_emoji_id"] == 1234567890
    assert captured["emoji"] is None


# ---------- skill: tg-profile emoji-status ----------


def test_profile_emoji_status_handler_registered():
    mod = _load_skill("tg-profile", "profile.py")
    assert "emoji-status" in mod.HANDLERS


def test_profile_emoji_status_requires_one_action():
    mod = _load_skill("tg-profile", "profile.py")
    parser = mod.build_parser()
    args = parser.parse_args(["emoji-status"])
    with pytest.raises(SystemExit, match="--document-id"):
        mod.cmd_emoji_status(args, c=None)


def test_profile_emoji_status_clear_via_skill():
    mod = _load_skill("tg-profile", "profile.py")

    captured = {}

    class FakeClient:
        def profile_emoji_status(self, document_id, *, until_iso):
            captured.update(document_id=document_id, until_iso=until_iso)
            return {"ok": True}

    args = mod.build_parser().parse_args(["emoji-status", "--clear"])
    mod.cmd_emoji_status(args, FakeClient())
    assert captured == {"document_id": None, "until_iso": None}


def test_profile_emoji_status_set_via_skill():
    mod = _load_skill("tg-profile", "profile.py")

    captured = {}

    class FakeClient:
        def profile_emoji_status(self, document_id, *, until_iso):
            captured.update(document_id=document_id, until_iso=until_iso)
            return {"ok": True}

    args = mod.build_parser().parse_args(
        [
            "emoji-status",
            "--document-id", "123",
            "--until", "2030-01-01T00:00:00+00:00",
        ]
    )
    mod.cmd_emoji_status(args, FakeClient())
    assert captured == {
        "document_id": 123,
        "until_iso": "2030-01-01T00:00:00+00:00",
    }


# ---------- 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_react_400_when_emoji_and_custom_both():
    c = _client()
    r = c.post(
        "/react",
        json={"chat": "@x", "msg_id": 1, "emoji": "👍", "custom_emoji_id": 42},
    )
    assert r.status_code == 400, r.text


def test_react_skill_treats_zero_as_present():
    """Round-1 MAJOR fix: --custom-emoji-id 0 is a valid (if unusual) value.
    The dispatcher must NOT silently treat it as 'absent' just because 0 is
    falsy in Python."""
    mod = _load_skill("tg-messaging", "act.py")
    parser = mod.build_parser()
    args = parser.parse_args(
        [
            "react", "--chat", "@x", "--msg-id", "1",
            "--emoji", "👍", "--custom-emoji-id", "0",
        ]
    )
    with pytest.raises(SystemExit, match="exactly one"):
        mod.cmd_react(args, c=None)


def test_telethon_errors_module_maps_to_502():
    """Round-1 MAJOR fix: PremiumAccountRequiredError lives in
    telethon.errors.* but doesn't end in RPCError. The exception handler
    must classify it as upstream (502), not 500."""
    from fastapi.testclient import TestClient

    from tgmcp.daemon import server as srv

    @srv.app.post("/_premium_test_only")
    async def _premium_test_only() -> dict:
        # Synthesize an exception with the right class shape: name doesn't
        # end in RPCError, but module path starts with telethon.errors.
        E = type(
            "PremiumAccountRequiredError",
            (Exception,),
            {"__module__": "telethon.errors.rpcerrorlist"},
        )
        raise E("CHANNELS_TOO_MUCH or PREMIUM_REQUIRED")

    c = TestClient(srv.app, raise_server_exceptions=False)
    r = c.post("/_premium_test_only", json={})
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["error"] == "PremiumAccountRequiredError"


def test_emoji_status_400_when_until_without_document():
    c = _client()
    r = c.post(
        "/profile/emoji_status",
        json={"document_id": None, "until": "2030-01-01T00:00:00+00:00"},
    )
    assert r.status_code == 400, r.text
