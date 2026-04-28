"""Phase 3 Batch 2: channel admin endpoints (participants / signatures /
slow_mode / discussion / admin_log) — schema + route + dispatcher tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/chat/participants",
        "/chat/signatures",
        "/chat/slow_mode",
        "/chat/discussion",
        "/chat/admin_log",
    ):
        assert p in paths, f"route {p} not registered"


# ---------- participants ----------


def test_participants_filter_kind_validator():
    server.GetParticipantsReq(chat="@x", filter_kind="admins")
    with pytest.raises(ValueError, match="filter_kind"):
        server.GetParticipantsReq(chat="@x", filter_kind="bogus")


def test_participants_limit_bounds():
    server.GetParticipantsReq(chat="@x", limit=1)
    server.GetParticipantsReq(chat="@x", limit=1000)
    with pytest.raises(ValueError):
        server.GetParticipantsReq(chat="@x", limit=0)
    with pytest.raises(ValueError):
        server.GetParticipantsReq(chat="@x", limit=1001)


# ---------- slow mode ----------


def test_slow_mode_seconds_must_be_in_allowed_set():
    """Telegram only accepts specific values: 0/10/30/60/300/900/3600."""
    for v in (0, 10, 30, 60, 300, 900, 3600):
        server.SlowModeReq(chat="@x", seconds=v)
    for v in (5, 15, 120, 1000, 7200):
        with pytest.raises(ValueError, match="seconds must be"):
            server.SlowModeReq(chat="@x", seconds=v)


# ---------- discussion ----------


def test_discussion_group_optional():
    """group=None means unbind."""
    req = server.DiscussionReq(broadcast="@chan")
    assert req.group is None
    server.DiscussionReq(broadcast="@chan", group="@discuss")


# ---------- admin log ----------


def test_admin_log_limit_bounds():
    server.AdminLogReq(chat="@x", limit=1)
    server.AdminLogReq(chat="@x", limit=500)
    with pytest.raises(ValueError):
        server.AdminLogReq(chat="@x", limit=0)
    with pytest.raises(ValueError):
        server.AdminLogReq(chat="@x", limit=501)


# ---------- TGSession + Client ----------


def test_session_has_channel_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "get_participants",
        "channel_set_signatures",
        "channel_set_slow_mode",
        "channel_set_discussion",
        "channel_admin_log",
    ):
        assert hasattr(TGSession, name), f"missing {name!r}"


def test_get_participants_validates_filter_kind_in_session():
    """The session method enforces filter_kind itself, in addition to
    the schema layer."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="filter_kind"):
        asyncio.run(s.get_participants("@x", filter_kind="bogus"))


def test_channel_set_slow_mode_validates_in_session():
    """Session-level seconds validation (defense in depth)."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="slow_mode seconds"):
        asyncio.run(s.channel_set_slow_mode("@x", 7))


def test_client_has_channel_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "chat_participants",
        "chat_signatures",
        "chat_slow_mode",
        "chat_discussion",
        "chat_admin_log",
    ):
        assert hasattr(DaemonClient, name)


# ---------- skill dispatcher ----------


def _load_admin_skill():
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-group-admin"
        / "admin.py"
    )
    spec = importlib.util.spec_from_file_location("admin_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_admin_skill_has_new_subcommands():
    mod = _load_admin_skill()
    for name in ("participants", "signatures", "slow-mode", "discussion", "admin-log"):
        assert name in mod.HANDLERS, f"dispatcher missing {name!r}"


def test_signatures_requires_on_or_off():
    mod = _load_admin_skill()
    args = mod.build_parser().parse_args(["signatures", "--chat", "@x"])
    with pytest.raises(SystemExit, match="--on or --off"):
        mod.cmd_signatures(args, c=None)


def test_signatures_rejects_both_on_and_off_via_argparse():
    """argparse mutex group enforces this."""
    mod = _load_admin_skill()
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["signatures", "--chat", "@x", "--on", "--off"]
        )


def test_discussion_requires_group_or_unbind():
    mod = _load_admin_skill()
    with pytest.raises(SystemExit):
        # mutex group is required → no flag at all is rejected
        mod.build_parser().parse_args(["discussion", "--broadcast", "@x"])


# ---------- route-level: 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_invalid_filter_kind_returns_400():
    c = _client()
    r = c.post("/chat/participants", json={"chat": "@x", "filter_kind": "bogus"})
    assert r.status_code == 400, r.text


def test_invalid_slow_mode_seconds_returns_400():
    c = _client()
    r = c.post("/chat/slow_mode", json={"chat": "@x", "seconds": 5})
    assert r.status_code == 400, r.text


def test_signatures_uses_correct_telethon_kwarg():
    """Round-4 BLOCKER: ToggleSignaturesRequest's keyword is
    `signatures_enabled`, not `enabled`. Passing the wrong name
    `TypeError`s before any RPC. Pin the fix via getsource."""
    import inspect

    from tgmcp.daemon.telegram import TGSession

    src = inspect.getsource(TGSession.channel_set_signatures)
    assert "signatures_enabled=" in src
    # The buggy `enabled=enabled` keyword call must be gone.
    assert "ToggleSignaturesRequest(channel=entity, enabled=" not in src


def test_get_participants_recognizes_basic_group_admins():
    """Round-4 MAJOR: is_admin used type-name string equality and only
    listed Channel* classes, so basic-group ChatParticipantAdmin /
    ChatParticipantCreator were always reported as non-admin. Verify
    the fix uses isinstance against both Channel* and Chat* types."""
    import asyncio
    from types import SimpleNamespace

    from telethon.tl.types import ChatParticipantAdmin, ChatParticipantCreator

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_admin = ChatParticipantAdmin.__new__(ChatParticipantAdmin)
    fake_creator = ChatParticipantCreator.__new__(ChatParticipantCreator)

    fake_users = [
        SimpleNamespace(
            id=1, username="alice", first_name="Alice", last_name=None,
            bot=False, participant=fake_admin,
        ),
        SimpleNamespace(
            id=2, username="bob", first_name="Bob", last_name=None,
            bot=False, participant=fake_creator,
        ),
        SimpleNamespace(
            id=3, username="carol", first_name="Carol", last_name=None,
            bot=False, participant=None,
        ),
    ]

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        def iter_participants(self, *a, **kw):
            async def _it():
                for u in fake_users:
                    yield u
            return _it()

    s.client = FakeClient()
    res = asyncio.run(s.get_participants("@x"))
    by_id = {u["id"]: u for u in res["users"]}
    assert by_id[1]["is_admin"] is True, "ChatParticipantAdmin must be flagged"
    assert by_id[2]["is_admin"] is True, "ChatParticipantCreator must be flagged"
    assert by_id[3]["is_admin"] is False, "non-participant must be False"
