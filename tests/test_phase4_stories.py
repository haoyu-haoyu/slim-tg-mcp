"""Phase 4 Batch 3: stories (read/mark/delete)."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgmcp.daemon import server


def test_stories_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/stories/active",
        "/stories/pinned",
        "/stories/mark_read",
        "/stories/delete",
    ):
        assert p in paths


# ---------- schema ----------


def test_stories_pinned_limits():
    server.StoriesPinnedReq(peer="@x", limit=1)
    server.StoriesPinnedReq(peer="@x", limit=100)
    with pytest.raises(ValueError):
        server.StoriesPinnedReq(peer="@x", limit=0)
    with pytest.raises(ValueError):
        server.StoriesPinnedReq(peer="@x", limit=101)


def test_stories_pinned_offset_non_negative():
    server.StoriesPinnedReq(peer="@x", offset_id=0)
    with pytest.raises(ValueError):
        server.StoriesPinnedReq(peer="@x", offset_id=-1)


def test_stories_mark_read_max_id_positive():
    server.StoriesReadReq(peer="@x", max_id=1)
    with pytest.raises(ValueError):
        server.StoriesReadReq(peer="@x", max_id=0)


def test_stories_delete_ids_required():
    with pytest.raises(ValueError):
        server.StoriesDeleteReq(ids=[])


def test_stories_delete_ids_positive():
    server.StoriesDeleteReq(ids=[1, 2, 3])
    with pytest.raises(ValueError, match="≥1"):
        server.StoriesDeleteReq(ids=[1, 0, 3])


def test_stories_delete_ids_capped_at_100():
    server.StoriesDeleteReq(ids=list(range(1, 101)))
    with pytest.raises(ValueError):
        server.StoriesDeleteReq(ids=list(range(1, 102)))


# ---------- session-layer ----------


def test_format_story_handles_live_variant():
    from tgmcp.daemon.telegram import TGSession

    class StoryItem:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    s = StoryItem(
        id=42,
        date=None,
        expire_date=None,
        caption="hi",
        pinned=True,
        public=False,
        close_friends=True,
        contacts=False,
        selected_contacts=False,
        noforwards=False,
        edited=True,
        media=SimpleNamespace(),
    )
    out = TGSession._format_story(s)
    assert out["id"] == 42
    assert out["caption"] == "hi"
    assert out["pinned"] is True
    assert out["close_friends"] is True
    assert out["edited"] is True
    assert out["has_media"] is True


def test_format_story_handles_deleted_variant():
    from tgmcp.daemon.telegram import TGSession

    class StoryItemDeleted:
        pass
    obj = StoryItemDeleted()
    obj.id = 99
    out = TGSession._format_story(obj)
    assert out == {"kind": "StoryItemDeleted", "id": 99}


def test_format_story_handles_skipped_variant():
    from tgmcp.daemon.telegram import TGSession

    class StoryItemSkipped:
        pass
    obj = StoryItemSkipped()
    obj.id = 7
    obj.close_friends = True
    out = TGSession._format_story(obj)
    assert out == {"kind": "StoryItemSkipped", "id": 7, "close_friends": True}


def test_delete_own_stories_requires_at_least_one():
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(s.delete_own_stories([]))


def test_delete_own_stories_hardcodes_self_peer():
    """Delete must always use peer='me' to enforce 'own only'."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    captured = {}

    class FakeClient:
        async def get_input_entity(self, peer):
            captured["peer"] = peer
            return SimpleNamespace()

        def __call__(self, req):
            captured["req"] = req
            async def _coro():
                return SimpleNamespace()
            return _coro()

    s.client = FakeClient()
    asyncio.run(s.delete_own_stories([1, 2]))
    assert captured["peer"] == "me"


# ---------- skill dispatcher ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_stories_skill_handlers_registered():
    mod = _load_skill("tg-stories", "story.py")
    assert set(mod.HANDLERS.keys()) == {"active", "pinned", "mark-read", "delete"}


def test_stories_skill_delete_requires_at_least_one_id():
    mod = _load_skill("tg-stories", "story.py")
    args = mod.build_parser().parse_args(["delete"])
    with pytest.raises(SystemExit, match="--id"):
        mod.cmd_delete(args, c=None)


def test_stories_skill_delete_requires_yes():
    mod = _load_skill("tg-stories", "story.py")
    args = mod.build_parser().parse_args(["delete", "--id", "5"])
    with pytest.raises(SystemExit, match="--yes"):
        mod.cmd_delete(args, c=None)


def test_stories_skill_active_minimal_args():
    mod = _load_skill("tg-stories", "story.py")
    args = mod.build_parser().parse_args(["active", "--peer", "@x"])
    assert args.peer == "@x"


def test_stories_skill_mark_read_requires_ack():
    """Round-1 MINOR fix: mark-read sends a viewed receipt (observable
    side effect). The dispatcher must refuse without --ack."""
    mod = _load_skill("tg-stories", "story.py")
    args = mod.build_parser().parse_args(
        ["mark-read", "--peer", "@x", "--max-id", "5"]
    )
    with pytest.raises(SystemExit, match="--ack"):
        mod.cmd_mark_read(args, c=None)


def test_stories_skill_mark_read_with_ack_calls_through():
    """With --ack, mark-read should reach the daemon client."""
    mod = _load_skill("tg-stories", "story.py")

    captured = {}

    class FakeClient:
        def stories_mark_read(self, peer, max_id):
            captured["peer"] = peer
            captured["max_id"] = max_id
            return {"ok": True}

    args = mod.build_parser().parse_args(
        ["mark-read", "--peer", "@x", "--max-id", "5", "--ack"]
    )
    res = mod.cmd_mark_read(args, FakeClient())
    assert captured == {"peer": "@x", "max_id": 5}
    assert res == {"ok": True}


# ---------- 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_stories_delete_400_when_empty():
    c = _client()
    r = c.post("/stories/delete", json={"ids": []})
    assert r.status_code == 400, r.text


def test_stories_mark_read_400_when_zero_max_id():
    c = _client()
    r = c.post("/stories/mark_read", json={"peer": "@x", "max_id": 0})
    assert r.status_code == 400, r.text
