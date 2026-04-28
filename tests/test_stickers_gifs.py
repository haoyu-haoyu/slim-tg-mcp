"""Phase 3 Batch 3: tg-stickers-gifs schema, route, dispatcher tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/gif/saved",
        "/gif/send",
        "/sticker/saved",
        "/sticker/set",
        "/sticker/send",
    ):
        assert p in paths


def test_no_gif_search_route_telegram_doesnt_expose():
    """Direct GIF search isn't in the user API; verify we didn't
    accidentally re-add a /gif/search endpoint claiming to do it."""
    paths = {r.path for r in server.app.routes}
    assert "/gif/search" not in paths


def test_send_doc_by_ref_validates_hex():
    """file_reference_hex must be valid hex — non-hex must 400 at the
    schema layer rather than late TypeError-ing inside Telethon."""
    server.SendDocByRefReq(
        chat="@x", doc_id=1, access_hash=2, file_reference_hex="aabbccdd"
    )
    with pytest.raises(ValueError, match="hex"):
        server.SendDocByRefReq(
            chat="@x", doc_id=1, access_hash=2, file_reference_hex="not-hex"
        )


def test_send_doc_by_ref_hex_max_length():
    """File reference is bounded to keep request bodies small."""
    long_hex = "ab" * 257  # 514 chars > 512 cap
    with pytest.raises(ValueError):
        server.SendDocByRefReq(
            chat="@x", doc_id=1, access_hash=2, file_reference_hex=long_hex
        )


def test_session_has_sg_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "get_saved_gifs",
        "send_gif",
        "get_saved_stickers",
        "get_sticker_set",
        "send_sticker",
    ):
        assert hasattr(TGSession, name)


def test_send_gif_validates_hex_in_session():
    """Session-level hex validation (defense in depth)."""
    import asyncio
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))
    s.client = SimpleNamespace()
    with pytest.raises(ValueError, match="not valid hex"):
        asyncio.run(s.send_gif("@x", 1, 2, "not-hex"))


def test_client_has_sg_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "gif_saved",
        "gif_send",
        "sticker_saved",
        "sticker_set",
        "sticker_send",
    ):
        assert hasattr(DaemonClient, name)


def _load_skill():
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-stickers-gifs"
        / "sg.py"
    )
    spec = importlib.util.spec_from_file_location("sg_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_skill_handlers_registered():
    mod = _load_skill()
    expected = {
        "gif-saved", "gif-send",
        "sticker-saved", "sticker-set", "sticker-send",
    }
    assert set(mod.HANDLERS.keys()) == expected


def _fake_doc(doc_id: int, access_hash: int, ref: bytes, mime: str = "image/gif"):
    """Telethon Document-shaped namespace good enough for our pickers."""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=doc_id, access_hash=access_hash, file_reference=ref, mime_type=mime
    )


def _make_session_with_call(rpc_response):
    """Build a TGSession whose only client capability is invoking RPCs."""
    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    class FakeClient:
        def __call__(self, _req):
            async def _coro():
                return rpc_response
            return _coro()

    s.client = FakeClient()
    return s


def test_get_saved_gifs_returns_full_triple():
    """Round-6 MAJOR (now behavioral, not source-grep): the list output
    must include doc_id + access_hash + file_reference_hex so the entry
    can feed send_gif unchanged."""
    import asyncio
    from types import SimpleNamespace

    docs = [_fake_doc(11, 22, b"\xab\xcd"), _fake_doc(33, 44, b"\xde\xad\xbe\xef")]
    response = SimpleNamespace(gifs=docs)
    s = _make_session_with_call(response)

    res = asyncio.run(s.get_saved_gifs())
    assert len(res) == 2
    assert res[0] == {
        "doc_id": 11, "access_hash": 22,
        "file_reference_hex": "abcd", "mime_type": "image/gif",
    }
    assert res[1]["file_reference_hex"] == "deadbeef"


def test_search_gifs_method_intentionally_absent():
    """Round-7: Telegram's user API doesn't expose a SearchGifs RPC
    (it goes through inline bots). We dropped the method rather than
    fake one that would 404-equivalent at the upstream layer."""
    from tgmcp.daemon.telegram import TGSession

    assert not hasattr(TGSession, "search_gifs"), (
        "search_gifs should not exist — Telegram's user API doesn't "
        "support direct GIF search. Use inline bots (@gif) instead."
    )


def test_get_sticker_set_returns_full_send_triples():
    """Round-6 MAJOR (now behavioral): /sticker/set must hand back
    each sticker's full sendable triple."""
    import asyncio
    from types import SimpleNamespace

    docs = [_fake_doc(100, 200, b"\xaa\xbb"), _fake_doc(101, 201, b"\xcc")]
    response = SimpleNamespace(documents=docs)
    s = _make_session_with_call(response)

    res = asyncio.run(s.get_sticker_set(set_id=42, access_hash=999))
    assert len(res) == 2
    for entry in res:
        # Every required key for send_sticker must be present and non-None.
        for required in ("doc_id", "access_hash", "file_reference_hex"):
            assert entry.get(required) is not None, (
                f"send-critical field {required} missing or None: {entry!r}"
            )
    assert res[0]["file_reference_hex"] == "aabb"


def test_sticker_set_endpoint_registered():
    paths = {r.path for r in server.app.routes}
    assert "/sticker/set" in paths


def test_get_saved_stickers_uses_set_id_field_for_consistency():
    """Round-6: get_saved_stickers returns PACK descriptors. The id
    field should be named `set_id` (not just `id`) so it pairs cleanly
    with /sticker/set's request schema (`set_id`)."""
    import asyncio
    from types import SimpleNamespace

    sets = [
        SimpleNamespace(
            id=1234, access_hash=5678, title="Pack A",
            short_name="packa", count=10,
        ),
    ]
    response = SimpleNamespace(sets=sets)
    s = _make_session_with_call(response)
    res = asyncio.run(s.get_saved_stickers())
    assert res[0].get("set_id") == 1234
    assert res[0].get("access_hash") == 5678


def test_route_400_for_invalid_hex():
    """End-to-end: malformed hex in /gif/send body → 400 (not 422)."""
    from fastapi.testclient import TestClient

    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post(
        "/gif/send",
        json={
            "chat": "@x",
            "doc_id": 1,
            "access_hash": 2,
            "file_reference_hex": "GG",  # not hex
        },
    )
    assert r.status_code == 400, r.text
