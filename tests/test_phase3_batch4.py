"""Phase 3 Batch 4: edit_poll / edit_scheduled / 2fa enhancements."""

from __future__ import annotations

import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgmcp.daemon import server


# ---------- routes ----------


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in ("/poll/edit", "/scheduled/edit", "/profile/2fa"):
        assert p in paths


# ---------- edit_poll ----------


def test_edit_poll_at_least_one_change():
    with pytest.raises(ValueError, match="question and/or options"):
        server.EditPollReq(chat="@x", msg_id=1)
    server.EditPollReq(chat="@x", msg_id=1, question="new?")
    server.EditPollReq(chat="@x", msg_id=1, options=["a", "b"])


def test_edit_poll_options_each_in_bounds():
    with pytest.raises(ValueError, match="empty"):
        server.EditPollReq(chat="@x", msg_id=1, options=["a", "  "])
    with pytest.raises(ValueError, match="100 chars"):
        server.EditPollReq(chat="@x", msg_id=1, options=["a", "x" * 101])


def test_edit_poll_session_requires_matching_option_count():
    """The TGSession helper preserves opaque option bytes — a different
    option count would invalidate every existing vote."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_poll = SimpleNamespace(
        question="?",
        answers=[
            SimpleNamespace(text="A", option=b"\x00"),
            SimpleNamespace(text="B", option=b"\x01"),
        ],
        closed=False,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )
    fake_msg = SimpleNamespace(
        poll=SimpleNamespace(poll=fake_poll, results=None)
    )

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg

        def __call__(self, _req):
            async def _coro():
                return SimpleNamespace()
            return _coro()

    s.client = FakeClient()
    with pytest.raises(ValueError, match="option count must match"):
        asyncio.run(
            s.edit_poll("@x", 1, options=["only_one"])
        )


# ---------- edit_scheduled ----------


def test_edit_scheduled_at_least_one_change():
    with pytest.raises(ValueError, match="text and/or schedule_date"):
        server.EditScheduledReq(chat="@x", msg_id=1)
    server.EditScheduledReq(chat="@x", msg_id=1, text="hi")


def test_edit_scheduled_naive_date_rejected():
    naive = datetime(2030, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        server.EditScheduledReq(chat="@x", msg_id=1, schedule_date=naive)


def test_edit_scheduled_past_rejected():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    with pytest.raises(ValueError, match="future"):
        server.EditScheduledReq(chat="@x", msg_id=1, schedule_date=past)


def test_edit_scheduled_session_needs_at_least_one():
    """Defense in depth at the session layer."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(s.edit_scheduled("@x", 1))


# ---------- 2fa ----------


def test_change_2fa_at_least_one_password():
    with pytest.raises(ValueError, match="at least one"):
        server.Change2faReq()


def test_change_2fa_new_password_min_8():
    with pytest.raises(ValueError):
        server.Change2faReq(new_password="short")
    server.Change2faReq(new_password="x" * 8)


def test_change_2fa_audit_records_only_transition_kind():
    """Audit must NOT include any password material — just the
    transition kind (set / change / remove)."""
    src = inspect.getsource(server.profile_2fa)
    audit_idx = src.find("audit.log(")
    block = src[audit_idx:]
    # Forbidden: any password-shaped variable in audit
    assert "current_password" not in block
    assert "new_password" not in block
    assert "req.hint" not in block
    # Required: transition kind
    assert "transition" in block


def test_session_change_2fa_wipes_local_refs_in_finally():
    """The session helper must drop its local password references in
    a try/finally so they don't outlive the call."""
    from tgmcp.daemon.telegram import TGSession

    src = inspect.getsource(TGSession.change_2fa_password)
    # Pattern: try / finally with explicit None reassignment
    assert "current_password = None" in src
    assert "new_password = None" in src
    assert "finally:" in src


# ---------- skill dispatchers ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_polls_skill_has_edit():
    mod = _load_skill("tg-polls", "poll.py")
    assert "edit" in mod.HANDLERS


def test_polls_skill_edit_requires_question_or_options():
    mod = _load_skill("tg-polls", "poll.py")
    args = mod.build_parser().parse_args(
        ["edit", "--chat", "@x", "--msg-id", "1"]
    )
    with pytest.raises(SystemExit, match="--question"):
        mod.cmd_edit(args, c=None)


def test_scheduling_skill_has_edit():
    mod = _load_skill("tg-scheduling", "schedule.py")
    assert "edit" in mod.HANDLERS


def test_scheduling_skill_edit_requires_change():
    mod = _load_skill("tg-scheduling", "schedule.py")
    args = mod.build_parser().parse_args(
        ["edit", "--chat", "@x", "--msg-id", "1"]
    )
    with pytest.raises(SystemExit, match="--text"):
        mod.cmd_edit(args, c=None)


def test_profile_skill_has_2fa():
    mod = _load_skill("tg-profile", "profile.py")
    assert "2fa" in mod.HANDLERS


def test_profile_skill_2fa_does_not_take_passwords_on_argv():
    """The 2fa subcommand must NOT have --password / --new-password
    flags — passwords go through getpass, never argv."""
    mod = _load_skill("tg-profile", "profile.py")
    # We check the source of cmd_set_2fa uses getpass and never reads
    # passwords from argparse.
    src = inspect.getsource(mod.cmd_set_2fa)
    assert "getpass" in src
    assert "args.password" not in src
    assert "args.new_password" not in src


# ---------- route-level 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_edit_poll_400_when_neither_field():
    c = _client()
    r = c.post("/poll/edit", json={"chat": "@x", "msg_id": 1})
    assert r.status_code == 400, r.text


def test_edit_scheduled_400_when_naive_date():
    c = _client()
    r = c.post(
        "/scheduled/edit",
        json={"chat": "@x", "msg_id": 1, "schedule_date": "2030-01-01T00:00:00"},
    )
    assert r.status_code == 400, r.text


def test_change_2fa_400_when_no_passwords():
    c = _client()
    r = c.post("/profile/2fa", json={})
    assert r.status_code == 400, r.text


# ---------- round-11 regression fixes ----------


def test_edit_poll_refuses_quiz_polls():
    """Round-11 MAJOR: editing quiz polls would silently drop
    correct_answers/solution metadata (those are on InputMediaPoll,
    not on Poll, and Telegram doesn't echo them back via GetMessages).
    Refuse with a clear message rather than risk losing quiz semantics."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    quiz_poll = SimpleNamespace(
        question="Capital of France?",
        answers=[
            SimpleNamespace(text="Berlin", option=b"\x00"),
            SimpleNamespace(text="Paris", option=b"\x01"),
        ],
        closed=False,
        public_voters=False,
        multiple_choice=False,
        quiz=True,
    )
    fake_msg = SimpleNamespace(poll=SimpleNamespace(poll=quiz_poll, results=None))

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg

    s.client = FakeClient()
    with pytest.raises(ValueError, match="quiz polls is not supported"):
        asyncio.run(s.edit_poll("@x", 1, question="new?"))


def test_edit_scheduled_rejects_more_than_365_days():
    """Round-11 MAJOR: EditScheduledReq lacked the 365-day upper bound
    that SendScheduledReq has. Telegram's max schedule horizon is
    ~1 year; anything further would fail upstream."""
    far = datetime.now(timezone.utc) + timedelta(days=400)
    with pytest.raises(ValueError, match="365 days"):
        server.EditScheduledReq(chat="@x", msg_id=1, schedule_date=far)


@pytest.mark.asyncio
async def test_scheduled_edit_handler_rechecks_window(monkeypatch):
    """Round-11 MAJOR: the handler must re-validate schedule_date right
    before send_scheduled, just like /scheduled/send does. Forge time
    to simulate in-process delay."""
    from fastapi import HTTPException

    req = server.EditScheduledReq(
        chat="@x",
        msg_id=1,
        schedule_date=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    class FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            # Simulate request having sat in process for 6 minutes —
            # schedule_date is now in the past.
            return req.schedule_date - timedelta(seconds=2)

    monkeypatch.setattr(server, "datetime", FakeDatetime)

    with pytest.raises(HTTPException) as ei:
        await server.scheduled_edit(req)
    assert ei.value.status_code == 400


def test_2fa_skill_refuses_non_tty():
    """Round-11 MINOR: the 2fa skill must refuse to run when stdin is
    not a TTY — `getpass.getpass` silently degrades to plain reads."""
    mod = _load_skill("tg-profile", "profile.py")
    src = inspect.getsource(mod.cmd_set_2fa)
    assert "isatty" in src, "2fa skill must check stdin.isatty()"
    assert "interactive TTY" in src or "non-tty" in src.lower()
