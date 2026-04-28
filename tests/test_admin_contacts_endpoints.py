"""Phase 2: tg-group-admin and tg-contacts surface regressions.

Mirrors the pattern from test_messaging_endpoints.py: assert schemas,
route registration, client wrappers, session methods, and skill
dispatcher subcommands all line up. We can't hit live Telegram in unit
tests, but we CAN guarantee the local plumbing is consistent.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from tgmcp.daemon import server


# ---------- group admin ----------


def test_group_admin_schemas_required_fields():
    cases = {
        server.CreateGroupReq: {"title"},
        server.ChatMemberReq: {"chat", "user"},
        server.InviteLinkReq: {"chat"},
        server.SetTitleReq: {"chat", "title"},
        server.LeaveReq: {"chat"},
    }
    for cls, expected in cases.items():
        actual = {n for n, f in cls.model_fields.items() if f.is_required()}
        assert actual == expected, f"{cls.__name__}: required mismatch ({actual} vs {expected})"


def test_group_admin_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/chat/create",
        "/chat/add_member",
        "/chat/kick_member",
        "/chat/ban_member",
        "/chat/unban_member",
        "/chat/invite_link",
        "/chat/set_title",
        "/chat/leave",
    ):
        assert p in paths, f"route {p} not registered"


def test_session_has_group_admin_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "create_group",
        "add_chat_member",
        "kick_chat_member",
        "ban_chat_member",
        "unban_chat_member",
        "create_invite_link",
        "set_chat_title",
        "leave_chat",
    ):
        assert hasattr(TGSession, name), f"TGSession missing {name!r}"


def test_create_group_rejects_megagroup_and_broadcast_simultaneously():
    """megagroup=True + broadcast=True is meaningless — the helper must reject."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="mutually exclusive"):
        asyncio.run(s.create_group("X", [], megagroup=True, broadcast=True))


def test_client_has_group_admin_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "chat_create",
        "chat_add_member",
        "chat_kick_member",
        "chat_ban_member",
        "chat_unban_member",
        "chat_invite_link",
        "chat_set_title",
        "chat_leave",
    ):
        assert hasattr(DaemonClient, name), f"DaemonClient missing {name!r}"


def _load_skill(name: str, file: str):
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / name
        / file
    )
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_group_admin_skill_dispatcher_has_all_subcommands():
    mod = _load_skill("tg-group-admin", "admin.py")
    expected = {"create", "add", "kick", "ban", "unban", "invite", "rename", "leave"}
    assert set(mod.HANDLERS.keys()) == expected
    parser = mod.build_parser()
    for sub in expected:
        try:
            parser.parse_args([sub, "--help"])
        except SystemExit as e:
            assert e.code == 0, f"--help failed for {sub}"


# ---------- contacts ----------


def test_contacts_schemas_required_fields():
    assert {n for n, f in server.AddContactReq.model_fields.items() if f.is_required()} == {
        "phone",
        "first_name",
    }
    assert {n for n, f in server.ContactUserReq.model_fields.items() if f.is_required()} == {"user"}
    assert {n for n, f in server.SearchContactsReq.model_fields.items() if f.is_required()} == {
        "query"
    }


def test_contacts_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/contacts/add",
        "/contacts/delete",
        "/contacts/block",
        "/contacts/unblock",
        "/contacts/search",
    ):
        assert p in paths


def test_session_has_contact_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "add_contact",
        "delete_contact",
        "block_user",
        "unblock_user",
        "search_contacts",
    ):
        assert hasattr(TGSession, name), f"TGSession missing {name!r}"


def test_client_has_contact_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "contact_add",
        "contact_delete",
        "contact_block",
        "contact_unblock",
        "contact_search",
    ):
        assert hasattr(DaemonClient, name), f"DaemonClient missing {name!r}"


def test_contacts_skill_dispatcher_has_all_subcommands():
    mod = _load_skill("tg-contacts", "contacts.py")
    expected = {"add", "delete", "block", "unblock", "search"}
    assert set(mod.HANDLERS.keys()) == expected


def test_contacts_skill_rejects_non_e164_phone():
    mod = _load_skill("tg-contacts", "contacts.py")
    args = mod.build_parser().parse_args(
        ["add", "--phone", "4155552671", "--first-name", "x"]
    )
    with pytest.raises(SystemExit, match="E.164"):
        mod.cmd_add(args, c=None)


def _make_session_with_fake_create_channel():
    """Helper: TGSession whose .client returns a stub channel for any
    CreateChannelRequest. Reusable across megagroup/broadcast tests."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    class FakeClient:
        async def get_entity(self, _u):
            raise RuntimeError("unresolved")

        async def get_peer_id(self, _e):
            return -100123

        def __call__(self, _req):
            async def _coro():
                return SimpleNamespace(chats=[SimpleNamespace(id=1, title="X")])
            return _coro()

    s.client = FakeClient()
    return s


def test_create_basic_group_rejects_no_resolved_invitees():
    """Phase-2 review caught: messages.createChat needs invitees, so an
    empty/all-unresolved list must be rejected pre-RPC instead of leaking
    UsersTooFewError."""
    import asyncio

    s = _make_session_with_fake_create_channel()
    with pytest.raises(ValueError, match="basic group"):
        asyncio.run(s.create_group("X", ["@nobody"]))


def test_create_megagroup_with_no_invitees_allowed():
    """megagroup creation doesn't require invitees (members can be added later)."""
    import asyncio

    s = _make_session_with_fake_create_channel()
    res = asyncio.run(s.create_group("X", [], megagroup=True))
    assert res["kind"] == "group"


def test_create_broadcast_channel_with_no_invitees_allowed():
    """Broadcast (one-way) channel creation also has no invitee requirement.
    Round-6 review noted this branch was not exercised — without it a
    broadcast-only regression would slip through."""
    import asyncio

    s = _make_session_with_fake_create_channel()
    res = asyncio.run(s.create_group("Announcements", [], broadcast=True))
    assert res["kind"] == "channel"


def test_search_contacts_filters_to_actual_contacts_and_excludes_bots():
    """Phase-2 review: contacts.SearchRequest returns global users + bots.
    Under /contacts/search we must filter to the user's saved contacts and
    exclude bots so prompt-injected callers can't enumerate strangers."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.contact_ids = {100, 200}

    fake_users = [
        SimpleNamespace(id=100, username="alice", first_name="Alice", last_name=None, bot=False),
        SimpleNamespace(id=999, username="stranger", first_name="X", last_name=None, bot=False),
        SimpleNamespace(id=200, username="bot1", first_name="Bot", last_name=None, bot=True),
    ]

    class FakeClient:
        def __call__(self, _req):
            async def _coro():
                return SimpleNamespace(users=fake_users)
            return _coro()

    s.client = FakeClient()

    out = asyncio.run(s.search_contacts("x"))
    ids = {u["id"] for u in out}
    assert ids == {100}, f"expected only contact-and-non-bot id 100, got {ids}"


def test_set_chat_title_runs_without_typeerror_and_issues_correct_rpc():
    """Phase-2 review caught: an `await self.client.edit_admin` line was
    awaiting a bound method object — every call would TypeError at runtime.
    Pin the fix BEHAVIORALLY: actually invoke set_chat_title with a fake
    client and assert no TypeError + the right Telethon request type goes
    out for both Channel and basic-Chat entities."""
    import asyncio
    from types import SimpleNamespace

    from telethon.tl.functions.channels import EditTitleRequest as ChEditTitle
    from telethon.tl.functions.messages import EditChatTitleRequest as MsgEditTitle
    from telethon.tl.types import Channel, Chat

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    issued: list = []

    class FakeClient:
        def __init__(self, entity):
            self._entity = entity

        async def get_entity(self, _q):
            return self._entity

        def __call__(self, req):
            issued.append(req)

            async def _coro():
                return SimpleNamespace(updates=[])

            return _coro()

    # 1) Channel path → ChEditTitle. Construct via __new__ to sidestep
    # Telethon's heavyweight required-args constructor.
    channel = Channel.__new__(Channel)
    channel.id = 100
    s.client = FakeClient(channel)
    asyncio.run(s.set_chat_title(100, "New Title"))
    assert len(issued) == 1
    assert isinstance(issued[0], ChEditTitle), (
        f"channel branch should issue ChEditTitle, got {type(issued[0])}"
    )
    assert issued[0].title == "New Title"

    # 2) Basic chat path → MsgEditTitle
    issued.clear()
    chat = Chat.__new__(Chat)
    chat.id = 42
    s.client = FakeClient(chat)
    asyncio.run(s.set_chat_title(42, "Hello"))
    assert len(issued) == 1
    assert isinstance(issued[0], MsgEditTitle), (
        f"chat branch should issue MsgEditTitle, got {type(issued[0])}"
    )
    assert issued[0].title == "Hello"


def test_add_contact_pydantic_rejects_non_e164():
    """Phase-2 review: E.164 enforcement was only in the skill — direct
    HTTP/client callers could send arbitrary garbage. Validator at the
    schema layer is the right defense."""
    with pytest.raises(ValueError, match="E.164"):
        server.AddContactReq(phone="4155552671", first_name="A")  # missing +
    with pytest.raises(ValueError, match="E.164"):
        server.AddContactReq(phone="+0", first_name="A")  # too short
    # Valid passes:
    ok = server.AddContactReq(phone="+14155552671", first_name="A")
    assert ok.phone == "+14155552671"


def test_audit_log_phone_only_records_suffix():
    """The /contacts/add audit log must NOT include the full phone — only
    the last 4 digits — to avoid logging PII in plaintext."""
    src = inspect.getsource(server.contacts_add)
    assert "phone_suffix" in src
    # We never pass req.phone directly to audit.log
    audit_call_idx = src.find("audit.log(")
    assert audit_call_idx >= 0
    audit_block = src[audit_call_idx:]
    # Look for a bare 'phone=' (no suffix) in the audit call only.
    assert "phone=req.phone" not in audit_block
    assert ", phone=" not in audit_block
