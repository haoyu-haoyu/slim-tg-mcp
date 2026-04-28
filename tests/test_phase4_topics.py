"""Phase 4 Batch 2: forum topics."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgmcp.daemon import server


def test_topics_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/topics/list",
        "/topics/create",
        "/topics/edit",
        "/topics/delete",
        "/topics/pin",
    ):
        assert p in paths


# ---------- schema ----------


def test_create_topic_title_length():
    server.CreateTopicReq(chat="@x", title="x")
    server.CreateTopicReq(chat="@x", title="x" * 128)
    with pytest.raises(ValueError):
        server.CreateTopicReq(chat="@x", title="")
    with pytest.raises(ValueError):
        server.CreateTopicReq(chat="@x", title="x" * 129)


def test_create_topic_icon_color_bounded():
    server.CreateTopicReq(chat="@x", title="t", icon_color=0)
    server.CreateTopicReq(chat="@x", title="t", icon_color=0xFFFFFF)
    with pytest.raises(ValueError):
        server.CreateTopicReq(chat="@x", title="t", icon_color=-1)
    with pytest.raises(ValueError):
        server.CreateTopicReq(chat="@x", title="t", icon_color=0x1000000)


def test_edit_topic_requires_topic_id_positive():
    with pytest.raises(ValueError):
        server.EditTopicReq(chat="@x", topic_id=0, title="t")
    with pytest.raises(ValueError):
        server.EditTopicReq(chat="@x", topic_id=-1, title="t")


def test_edit_topic_at_least_one_field():
    with pytest.raises(ValueError, match="at least one"):
        server.EditTopicReq(chat="@x", topic_id=42)
    server.EditTopicReq(chat="@x", topic_id=42, title="new")
    server.EditTopicReq(chat="@x", topic_id=42, closed=True)
    server.EditTopicReq(chat="@x", topic_id=42, hidden=False)


def test_pin_topic_schema():
    req = server.PinTopicReq(chat="@x", topic_id=5, pinned=True)
    assert req.pinned is True


def test_topic_req_validates_id():
    server.TopicReq(chat="@x", topic_id=1)
    with pytest.raises(ValueError):
        server.TopicReq(chat="@x", topic_id=0)


def test_list_topics_query_capped():
    server.ListTopicsReq(chat="@x", query="x" * 64)
    with pytest.raises(ValueError):
        server.ListTopicsReq(chat="@x", query="x" * 65)


# ---------- session-layer ----------


def test_edit_topic_session_at_least_one():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(s.edit_topic("@x", 1))


# ---------- skill dispatcher ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_topics_skill_handlers_registered():
    mod = _load_skill("tg-topics", "topic.py")
    assert set(mod.HANDLERS.keys()) == {"list", "create", "edit", "delete", "pin"}


def test_topics_skill_delete_requires_yes():
    mod = _load_skill("tg-topics", "topic.py")
    args = mod.build_parser().parse_args(
        ["delete", "--chat", "@x", "--topic-id", "1"]
    )
    with pytest.raises(SystemExit, match="--yes"):
        mod.cmd_delete(args, c=None)


def test_topics_skill_edit_requires_change():
    mod = _load_skill("tg-topics", "topic.py")
    args = mod.build_parser().parse_args(
        ["edit", "--chat", "@x", "--topic-id", "1"]
    )
    with pytest.raises(SystemExit, match="title"):
        mod.cmd_edit(args, c=None)


def test_topics_skill_edit_closed_reopen_mutually_exclusive():
    """argparse should error when both --closed and --reopen are passed."""
    mod = _load_skill("tg-topics", "topic.py")
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["edit", "--chat", "@x", "--topic-id", "1", "--closed", "--reopen"]
        )


def test_topics_skill_pin_requires_explicit_state():
    mod = _load_skill("tg-topics", "topic.py")
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["pin", "--chat", "@x", "--topic-id", "1"]
        )


def test_topics_skill_list_default_limit():
    mod = _load_skill("tg-topics", "topic.py")
    args = mod.build_parser().parse_args(["list", "--chat", "@x"])
    assert args.limit == 100
    assert args.query is None


# ---------- 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_create_topic_400_when_empty_title():
    c = _client()
    r = c.post("/topics/create", json={"chat": "@x", "title": ""})
    assert r.status_code == 400, r.text


def test_edit_topic_400_when_no_fields():
    c = _client()
    r = c.post("/topics/edit", json={"chat": "@x", "topic_id": 1})
    assert r.status_code == 400, r.text


def test_pin_topic_400_when_missing_pinned():
    c = _client()
    r = c.post("/topics/pin", json={"chat": "@x", "topic_id": 1})
    assert r.status_code == 400, r.text


# ---------- list_topics: ForumTopicDeleted skipping ----------


@pytest.mark.asyncio
async def test_list_topics_skips_deleted_variant():
    """ForumTopicDeleted has no `title` etc. — must be skipped."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    # Build fake response. The session-layer code dispatches on
    # `__class__.__name__ == "ForumTopicDeleted"`, so we use real classes
    # named appropriately.
    class ForumTopic:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class ForumTopicDeleted:
        pass

    live_obj = ForumTopic(
        id=1,
        title="Live",
        icon_color=0x123456,
        icon_emoji_id=None,
        closed=False,
        pinned=False,
        hidden=False,
        from_id=None,
        top_message=42,
        unread_count=3,
    )
    deleted_obj = ForumTopicDeleted()

    fake_result = SimpleNamespace(topics=[live_obj, deleted_obj])

    class FakeClient:
        async def get_input_entity(self, _q):
            return SimpleNamespace()

        def __call__(self, _req):
            async def _coro():
                return fake_result
            return _coro()

    s.client = FakeClient()
    out = await s.list_topics("@x", limit=10)
    assert len(out) == 1
    assert out[0]["title"] == "Live"
