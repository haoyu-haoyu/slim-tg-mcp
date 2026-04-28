"""Phase 4 Batch 4: location / live location."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_location_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/location/send",
        "/location/send_live",
        "/location/edit_live",
        "/location/stop_live",
    ):
        assert p in paths


# ---------- schema: lat/lng bounds ----------


def test_lat_lng_bounded():
    server.SendLocationReq(chat="@x", lat=0, lng=0)
    server.SendLocationReq(chat="@x", lat=90, lng=180)
    server.SendLocationReq(chat="@x", lat=-90, lng=-180)
    with pytest.raises(ValueError):
        server.SendLocationReq(chat="@x", lat=91, lng=0)
    with pytest.raises(ValueError):
        server.SendLocationReq(chat="@x", lat=0, lng=181)
    with pytest.raises(ValueError):
        server.SendLocationReq(chat="@x", lat=-91, lng=0)


def test_accuracy_bounds():
    server.SendLocationReq(chat="@x", lat=0, lng=0, accuracy=0)
    server.SendLocationReq(chat="@x", lat=0, lng=0, accuracy=1500)
    with pytest.raises(ValueError):
        server.SendLocationReq(chat="@x", lat=0, lng=0, accuracy=-1)
    with pytest.raises(ValueError):
        server.SendLocationReq(chat="@x", lat=0, lng=0, accuracy=1501)


# ---------- schema: live period range + indefinite sentinel ----------


def test_live_period_accepts_full_range():
    """Round-1 BLOCKER fix: accept ANY value in [60, 86400], plus
    0x7FFFFFFF for indefinite. The previous whitelist of 5 magic values
    was wrong per Telegram Bot API docs."""
    for p in (60, 120, 900, 1800, 3600, 7200, 28800, 86400, 0x7FFFFFFF):
        server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=p)


def test_live_period_rejects_out_of_range():
    for bad in (0, 30, 59, 86401, -1, 0xFFFFFFFF):
        with pytest.raises(ValueError, match="period"):
            server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=bad)


def test_live_period_rejects_old_wrong_indefinite_sentinel():
    """Round-1 BLOCKER fix: 0xFFFFFFFF was the old (wrong) sentinel —
    Telegram's actual indefinite value is 0x7FFFFFFF."""
    with pytest.raises(ValueError):
        server.SendLiveLocationReq(
            chat="@x", lat=0, lng=0, period=0xFFFFFFFF
        )


def test_live_heading_bounds():
    server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=60, heading=1)
    server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=60, heading=360)
    with pytest.raises(ValueError):
        server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=60, heading=0)
    with pytest.raises(ValueError):
        server.SendLiveLocationReq(chat="@x", lat=0, lng=0, period=60, heading=361)


def test_live_proximity_bounds():
    server.SendLiveLocationReq(
        chat="@x", lat=0, lng=0, period=60, proximity=0
    )
    server.SendLiveLocationReq(
        chat="@x", lat=0, lng=0, period=60, proximity=100000
    )
    with pytest.raises(ValueError):
        server.SendLiveLocationReq(
            chat="@x", lat=0, lng=0, period=60, proximity=100001
        )


def test_edit_live_msg_id_positive():
    server.EditLiveLocationReq(chat="@x", msg_id=1, lat=0, lng=0)
    with pytest.raises(ValueError):
        server.EditLiveLocationReq(chat="@x", msg_id=0, lat=0, lng=0)


def test_stop_live_msg_id_positive():
    server.StopLiveLocationReq(chat="@x", msg_id=5)
    with pytest.raises(ValueError):
        server.StopLiveLocationReq(chat="@x", msg_id=0)


# ---------- skill dispatcher ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_location_skill_handlers_registered():
    mod = _load_skill("tg-location", "location.py")
    assert set(mod.HANDLERS.keys()) == {"send", "send-live", "edit-live", "stop-live"}


def test_location_skill_send_minimal_args():
    mod = _load_skill("tg-location", "location.py")
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--lat", "37.7", "--lng", "-122.4"]
    )
    assert args.lat == 37.7
    assert args.lng == -122.4


def test_location_skill_send_live_requires_period():
    mod = _load_skill("tg-location", "location.py")
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["send-live", "--chat", "@x", "--lat", "0", "--lng", "0"]
        )


def test_location_skill_edit_live_requires_msg_id():
    mod = _load_skill("tg-location", "location.py")
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["edit-live", "--chat", "@x", "--lat", "0", "--lng", "0"]
        )


# ---------- 400 surface ----------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_send_live_400_when_bad_period():
    """600 is INSIDE [60, 86400] — used to be rejected by the old
    whitelist, but is now valid. We use a clearly-out-of-range value
    instead."""
    c = _client()
    r = c.post(
        "/location/send_live",
        json={"chat": "@x", "lat": 0, "lng": 0, "period": 99999},
    )
    assert r.status_code == 400, r.text


def test_send_400_when_lat_out_of_range():
    c = _client()
    r = c.post(
        "/location/send",
        json={"chat": "@x", "lat": 999, "lng": 0},
    )
    assert r.status_code == 400, r.text


# ---------- round 1 BLOCKER: stop_live reuses last-known coordinates ----------


def test_stop_live_reuses_last_known_geo():
    """The stop edit must NOT send (0, 0) — that would re-anchor the
    marker to Null Island on some clients before showing 'stopped'.
    Instead it must read the existing message's geo and reuse those
    coordinates."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_geo = SimpleNamespace(lat=37.7749, long=-122.4194)
    fake_msg = SimpleNamespace(media=SimpleNamespace(geo=fake_geo))

    captured = {}

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            captured["ids"] = ids
            return fake_msg

        def __call__(self, req):
            captured["req"] = req
            async def _coro():
                return SimpleNamespace()
            return _coro()

    s.client = FakeClient()
    asyncio.run(s.stop_live_location("@x", 99))

    geo_pt = captured["req"].media.geo_point
    assert abs(geo_pt.lat - 37.7749) < 1e-6
    assert abs(geo_pt.long - (-122.4194)) < 1e-6
    assert captured["req"].media.stopped is True


def test_stop_live_refuses_when_msg_has_no_geo():
    """Round-1 BLOCKER fix corollary: don't fall back to (0,0) silently
    when the message turned out not to be a live location."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    fake_msg_no_geo = SimpleNamespace(
        media=SimpleNamespace()  # no .geo attribute
    )

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace()

        async def get_messages(self, _e, ids):
            return fake_msg_no_geo

    s.client = FakeClient()
    with pytest.raises(ValueError, match="not a live-location"):
        asyncio.run(s.stop_live_location("@x", 99))


# ---------- round 1 MAJOR: --confirm-indefinite gate ----------


def test_send_live_indefinite_requires_confirm_flag():
    mod = _load_skill("tg-location", "location.py")
    args = mod.build_parser().parse_args(
        [
            "send-live", "--chat", "@x", "--lat", "0", "--lng", "0",
            "--period", "2147483647",
        ]
    )
    with pytest.raises(SystemExit, match="confirm-indefinite"):
        mod.cmd_send_live(args, c=None)


def test_send_live_indefinite_with_confirm_calls_through():
    mod = _load_skill("tg-location", "location.py")

    captured = {}

    class FakeClient:
        def location_send_live(self, chat, lat, lng, period, **kw):
            captured.update(
                chat=chat, lat=lat, lng=lng, period=period, **kw
            )
            return {"ok": True}

    args = mod.build_parser().parse_args(
        [
            "send-live", "--chat", "@x", "--lat", "0", "--lng", "0",
            "--period", "2147483647", "--confirm-indefinite",
        ]
    )
    res = mod.cmd_send_live(args, FakeClient())
    assert captured["period"] == 0x7FFFFFFF
    assert res == {"ok": True}


def test_send_live_normal_period_doesnt_need_confirm():
    """The gate must fire ONLY for indefinite — normal periods just go
    through."""
    mod = _load_skill("tg-location", "location.py")

    class FakeClient:
        def location_send_live(self, *a, **kw):
            return {"ok": True}

    args = mod.build_parser().parse_args(
        [
            "send-live", "--chat", "@x", "--lat", "0", "--lng", "0",
            "--period", "900",
        ]
    )
    # Must not raise.
    mod.cmd_send_live(args, FakeClient())
