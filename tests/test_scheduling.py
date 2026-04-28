"""tg-scheduling: schema validators, route registration, dispatcher behavior."""

from __future__ import annotations

import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/scheduled/send",
        "/scheduled/list",
        "/scheduled/delete",
        "/draft/save",
        "/draft/get",
        "/draft/clear",
    ):
        assert p in paths, f"route {p} not registered"


def test_schedule_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        server.SendScheduledReq(
            chat="@x",
            text="hi",
            schedule_date=datetime(2030, 1, 1, 12, 0, 0),  # naive
        )


def test_schedule_rejects_past():
    with pytest.raises(ValueError, match="future"):
        server.SendScheduledReq(
            chat="@x",
            text="hi",
            schedule_date=datetime.now(timezone.utc) - timedelta(seconds=60),
        )


def test_schedule_rejects_too_close_to_now():
    """Telegram needs at least a few seconds; we enforce 10s minimum."""
    with pytest.raises(ValueError, match="future"):
        server.SendScheduledReq(
            chat="@x",
            text="hi",
            schedule_date=datetime.now(timezone.utc) + timedelta(seconds=2),
        )


def test_schedule_rejects_too_far_out():
    with pytest.raises(ValueError, match="365 days"):
        server.SendScheduledReq(
            chat="@x",
            text="hi",
            schedule_date=datetime.now(timezone.utc) + timedelta(days=400),
        )


def test_schedule_accepts_valid_window():
    s = server.SendScheduledReq(
        chat="@x",
        text="hi",
        schedule_date=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert s.text == "hi"


def test_schedule_text_length_capped():
    with pytest.raises(ValueError):
        server.SendScheduledReq(
            chat="@x",
            text="x" * 4097,
            schedule_date=datetime.now(timezone.utc) + timedelta(minutes=5),
        )


def test_delete_scheduled_min_one():
    with pytest.raises(ValueError):
        server.DeleteScheduledReq(chat="@x", msg_ids=[])


def test_delete_scheduled_max_100():
    with pytest.raises(ValueError):
        server.DeleteScheduledReq(chat="@x", msg_ids=list(range(101)))


def test_session_has_scheduling_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "send_scheduled",
        "list_scheduled",
        "delete_scheduled",
        "save_draft",
        "get_draft",
        "clear_draft",
    ):
        assert hasattr(TGSession, name), f"missing {name!r}"


def test_client_has_scheduling_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "scheduled_send",
        "scheduled_list",
        "scheduled_delete",
        "draft_save",
        "draft_get",
        "draft_clear",
    ):
        assert hasattr(DaemonClient, name)


def test_audit_logs_no_message_text():
    """Scheduled and draft text can be sensitive (e.g. private notes the
    user is drafting). Audit should record metadata only — len, timestamp,
    chat — not the body."""
    for fn in (server.scheduled_send, server.draft_save):
        src = inspect.getsource(fn)
        audit_idx = src.find("audit.log(")
        assert audit_idx >= 0
        block = src[audit_idx:]
        assert "text=req.text" not in block, (
            f"{fn.__name__} must not log message body in audit"
        )


def _load_skill():
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-scheduling"
        / "schedule.py"
    )
    spec = importlib.util.spec_from_file_location("schedule_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_skill_handlers_registered():
    mod = _load_skill()
    expected = {"send", "list", "cancel", "edit", "draft-save", "draft-get", "draft-clear"}
    assert set(mod.HANDLERS.keys()) == expected


def test_skill_resolve_when_rejects_naive():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--text", "hi", "--when", "2030-01-01T00:00:00"]
    )
    with pytest.raises(SystemExit, match="timezone"):
        mod._resolve_when(args)


def test_skill_resolve_when_rejects_both_when_and_in_seconds():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        [
            "send",
            "--chat", "@x",
            "--text", "hi",
            "--when", "2030-01-01T00:00:00+00:00",
            "--in-seconds", "60",
        ]
    )
    with pytest.raises(SystemExit, match="not both"):
        mod._resolve_when(args)


def test_skill_resolve_when_rejects_short_relative_offset():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--text", "hi", "--in-seconds", "5"]
    )
    with pytest.raises(SystemExit, match="at least 10"):
        mod._resolve_when(args)


def test_skill_resolve_when_accepts_iso_with_tz():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--text", "hi", "--when", "2030-01-01T00:00:00+00:00"]
    )
    iso = mod._resolve_when(args)
    # Round-tripped to UTC ISO.
    assert iso.endswith("+00:00")
    assert "2030-01-01" in iso


def test_save_draft_rejects_empty_text():
    """Round-14 MINOR: empty drafts must go through /draft/clear so the
    API has one obvious path per intent. /draft/save with text='' would
    otherwise leave Telegram's behaviour ambiguous."""
    with pytest.raises(ValueError):
        server.SaveDraftReq(chat="@x", text="")


@pytest.mark.asyncio
async def test_scheduled_send_rechecks_window_before_send(monkeypatch):
    """Round-14 MINOR: the @field_validator runs at request parse time. If
    the request sits in process for >10s the schedule could fall below
    the minimum by the time we send. The handler must re-check and
    return 400, not let it fail late as an upstream 502."""
    from fastapi import HTTPException

    # Build a request that's valid at parse time (5 minutes out)...
    req = server.SendScheduledReq(
        chat="@x",
        text="hi",
        schedule_date=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    # ...then forge "now" to be just after the schedule, simulating delay.
    fake_now = req.schedule_date - timedelta(seconds=2)

    real_dt = server.datetime

    class FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(server, "datetime", FakeDatetime)

    with pytest.raises(HTTPException) as ei:
        await server.scheduled_send(req)
    assert ei.value.status_code == 400
    assert "schedule_date" in ei.value.detail.lower() or "future" in ei.value.detail.lower()

    monkeypatch.setattr(server, "datetime", real_dt)


@pytest.mark.asyncio
async def test_get_draft_treats_empty_placeholder_as_none():
    """Round-14 MINOR: iter_drafts yields placeholder DraftMessage for
    chats the user merely opened (empty text, no reply_to). Those are NOT
    real drafts and must return None, not a {"text": "", "reply_to_msg_id":
    None} object that callers might treat as a saved draft."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.me_id = 1

    async def fake_iter():
        yield SimpleNamespace(
            entity=SimpleNamespace(),
            text="",
            date=None,
            reply_to_msg_id=None,
        )

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_peer_id(self, _e):
            return 42

        def iter_drafts(self):
            return fake_iter()

    s.client = FakeClient()
    res = await s.get_draft("@x")
    assert res is None


@pytest.mark.asyncio
async def test_get_draft_returns_real_saved_draft():
    """Counterpart: a real draft (non-empty text OR a reply_to) must be
    returned as a populated dict."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    target_entity = SimpleNamespace()

    async def fake_iter():
        yield SimpleNamespace(
            entity=SimpleNamespace(),  # different peer
            text="other-chat-draft",
            date=None,
            reply_to_msg_id=None,
        )
        yield SimpleNamespace(
            entity=target_entity,
            text="real saved text",
            date=None,
            reply_to_msg_id=999,
        )

    class FakeClient:
        async def get_entity(self, _q):
            return target_entity

        async def get_peer_id(self, e):
            return 42 if e is target_entity else 99

        def iter_drafts(self):
            return fake_iter()

    s.client = FakeClient()
    res = await s.get_draft("@x")
    assert res is not None
    assert res["text"] == "real saved text"
    assert res["reply_to_msg_id"] == 999


def test_skill_cancel_empty_ids_rejected():
    mod = _load_skill()
    args = mod.build_parser().parse_args(
        ["cancel", "--chat", "@x", "--msg-ids", ", , "]
    )
    with pytest.raises(SystemExit, match="empty"):
        mod.cmd_cancel(args, c=None)
