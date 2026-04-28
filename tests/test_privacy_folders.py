"""Batch 1: tg-privacy + tg-folders schema, route, dispatcher tests."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from tgmcp.daemon import server


# ---------- privacy ----------


def test_privacy_routes_registered():
    paths = {r.path for r in server.app.routes}
    assert "/privacy/get" in paths
    assert "/privacy/set" in paths


def test_privacy_key_validator():
    server.GetPrivacyReq(key="status")
    with pytest.raises(ValueError, match="unknown privacy key"):
        server.GetPrivacyReq(key="nonexistent")


def test_privacy_rule_kind_validator():
    server.PrivacyRule(kind="allow_all")
    server.PrivacyRule(kind="allow_users", user_ids=[1, 2])
    with pytest.raises(ValueError, match="unknown rule kind"):
        server.PrivacyRule(kind="bogus")


def test_set_privacy_min_one_rule():
    with pytest.raises(ValueError):
        server.SetPrivacyReq(key="status", rules=[])


def test_session_has_privacy_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in ("get_privacy", "set_privacy"):
        assert hasattr(TGSession, name)


def test_client_has_privacy_methods():
    from tgmcp.client import DaemonClient

    for name in ("privacy_get", "privacy_set"):
        assert hasattr(DaemonClient, name)


def test_audit_does_not_log_user_ids():
    """privacy_set audit must NOT include the raw user-id allowlists —
    those reveal who the user has chosen to block / allow."""
    src = inspect.getsource(server.privacy_set)
    audit_idx = src.find("audit.log(")
    block = src[audit_idx:]
    assert "user_ids" not in block
    assert "rules=req.rules" not in block
    # Counts/keys are fine.
    assert "rule_count" in block


# ---------- folders ----------


def test_folder_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in ("/folders/list", "/folders/update", "/folders/delete"):
        assert p in paths


def test_folder_id_must_be_2_to_255():
    with pytest.raises(ValueError):
        server.FolderIdReq(folder_id=0)
    with pytest.raises(ValueError):
        server.FolderIdReq(folder_id=1)
    with pytest.raises(ValueError):
        server.FolderIdReq(folder_id=256)
    server.FolderIdReq(folder_id=2)
    server.FolderIdReq(folder_id=255)


def test_folder_title_length():
    """Telegram's dialogFilter.title is capped at 12 UTF-8 chars."""
    with pytest.raises(ValueError):
        server.FolderPeerSpec(folder_id=2, title="", contacts=True)
    with pytest.raises(ValueError):
        # 13 chars exceeds Telegram's real limit
        server.FolderPeerSpec(folder_id=2, title="x" * 13, contacts=True)
    server.FolderPeerSpec(folder_id=2, title="x" * 12, contacts=True)


def test_privacy_rule_allow_users_requires_user_ids():
    """Round-1 MAJOR: allow_users / disallow_users without a non-empty
    user_ids list must be a 400 at the schema layer, not a late
    Telethon failure."""
    with pytest.raises(ValueError, match="non-empty user_ids"):
        server.PrivacyRule(kind="allow_users")
    with pytest.raises(ValueError, match="non-empty user_ids"):
        server.PrivacyRule(kind="disallow_users", user_ids=[])
    # Valid:
    server.PrivacyRule(kind="allow_users", user_ids=[1, 2])


def test_privacy_rule_allow_all_rejects_user_ids():
    """The non-users kinds must NOT silently accept user_ids — that
    would surprise the caller (the field is dropped upstream)."""
    with pytest.raises(ValueError, match="does not accept user_ids"):
        server.PrivacyRule(kind="allow_all", user_ids=[1, 2])
    with pytest.raises(ValueError, match="does not accept user_ids"):
        server.PrivacyRule(kind="allow_contacts", user_ids=[1])
    # Valid empty:
    server.PrivacyRule(kind="allow_all")
    server.PrivacyRule(kind="allow_contacts")


def test_folder_must_have_at_least_one_inclusion():
    """Round-1 MAJOR: empty folder definition (no peers, no kind flags)
    would FILTER_INCLUDE_EMPTY upstream. Bounce at the schema layer."""
    with pytest.raises(ValueError, match="at least one inclusion"):
        server.FolderPeerSpec(folder_id=2, title="W")
    # Any single inclusion source is enough:
    server.FolderPeerSpec(folder_id=2, title="W", include_peers=["@x"])
    server.FolderPeerSpec(folder_id=2, title="W", contacts=True)
    server.FolderPeerSpec(folder_id=2, title="W", non_contacts=True)
    server.FolderPeerSpec(folder_id=2, title="W", groups=True)
    server.FolderPeerSpec(folder_id=2, title="W", broadcasts=True)
    server.FolderPeerSpec(folder_id=2, title="W", bots=True)


def test_session_has_folder_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in ("list_folders", "update_folder", "delete_folder"):
        assert hasattr(TGSession, name)


def test_client_has_folder_methods():
    from tgmcp.client import DaemonClient

    for name in ("folders_list", "folders_update", "folders_delete"):
        assert hasattr(DaemonClient, name)


# ---------- skill dispatchers ----------


def _load_skill(name, file):
    skill = Path(__file__).resolve().parents[1] / "skills" / name / file
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_privacy_dispatcher_handlers():
    mod = _load_skill("tg-privacy", "privacy.py")
    assert set(mod.HANDLERS.keys()) == {"get", "set"}


def test_privacy_dispatcher_rejects_users_rule_without_users_arg():
    mod = _load_skill("tg-privacy", "privacy.py")
    args = mod.build_parser().parse_args(
        ["set", "--key", "status", "--rule", "allow_users"]
    )
    with pytest.raises(SystemExit, match="--users"):
        mod.cmd_set(args, c=None)


def test_privacy_dispatcher_rejects_non_numeric_users():
    mod = _load_skill("tg-privacy", "privacy.py")
    args = mod.build_parser().parse_args(
        ["set", "--key", "status",
         "--rule", "allow_users",
         "--users", "alice,123"],
    )
    with pytest.raises(SystemExit, match="numeric"):
        mod.cmd_set(args, c=None)


def test_privacy_dispatcher_set_with_no_rules_rejected():
    mod = _load_skill("tg-privacy", "privacy.py")
    args = mod.build_parser().parse_args(["set", "--key", "status"])
    with pytest.raises(SystemExit, match="--rule"):
        mod.cmd_set(args, c=None)


def test_folders_dispatcher_handlers():
    mod = _load_skill("tg-folders", "folders.py")
    assert set(mod.HANDLERS.keys()) == {"list", "update", "delete"}


def test_folders_split_peers_handles_mixed():
    mod = _load_skill("tg-folders", "folders.py")
    out = mod._split_peers("@alice, 123, , -100456")
    assert out == ["@alice", 123, -100456]


# ---------- route-level validation: schema errors → 400, not 422 ----------


def _client():
    """In-process TestClient that bypasses lifespan (we just want to
    exercise the request validators, not bring up Telethon)."""
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def test_privacy_set_invalid_rule_kind_returns_400():
    c = _client()
    r = c.post(
            "/privacy/set",
            json={"key": "status", "rules": [{"kind": "bogus"}]},
        )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body.get("error") == "ValidationError"


def test_privacy_set_allow_users_without_user_ids_returns_400():
    """The MAJOR-1 fix: invalid PrivacyRule combo at the route layer
    must surface as a 400, not FastAPI's default 422."""
    c = _client()
    r = c.post(
            "/privacy/set",
            json={
                "key": "status",
                "rules": [{"kind": "allow_users", "user_ids": []}],
            },
        )
    assert r.status_code == 400, r.text


def test_privacy_set_allow_all_with_user_ids_returns_400():
    c = _client()
    r = c.post(
            "/privacy/set",
            json={
                "key": "status",
                "rules": [{"kind": "allow_all", "user_ids": [1, 2]}],
            },
        )
    assert r.status_code == 400, r.text


def test_privacy_set_unknown_key_returns_400():
    c = _client()
    r = c.post(
            "/privacy/set",
            json={"key": "bogus", "rules": [{"kind": "allow_all"}]},
        )
    assert r.status_code == 400, r.text


def test_folder_update_with_no_inclusion_returns_400():
    """The MAJOR-2 fix: empty folder definition rejected at the route."""
    c = _client()
    r = c.post(
            "/folders/update",
            json={"folder_id": 2, "title": "W"},
        )
    assert r.status_code == 400, r.text


def test_folder_update_with_oversize_title_returns_400():
    """The MINOR fix: title >12 chars rejected at the route."""
    c = _client()
    r = c.post(
            "/folders/update",
            json={"folder_id": 2, "title": "x" * 13, "contacts": True},
        )
    assert r.status_code == 400, r.text


def test_folder_delete_with_reserved_id_returns_400():
    c = _client()
    r = c.post("/folders/delete", json={"folder_id": 1})
    assert r.status_code == 400, r.text
