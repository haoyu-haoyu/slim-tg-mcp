"""tg-profile: schema validators, route registration, audit redaction."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from tgmcp.daemon import server


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    for p in (
        "/profile/update",
        "/profile/username",
        "/profile/photo",
        "/profile/photo_delete",
        "/profile/status",
    ):
        assert p in paths, f"route {p} not registered"


def test_update_profile_all_optional():
    """All three fields can be None — the helper just won't change anything."""
    req = server.UpdateProfileReq()
    assert req.first_name is None
    assert req.last_name is None
    assert req.about is None


def test_update_profile_first_name_min_length_1():
    """first_name=None is fine (skip), but '' is meaningless."""
    with pytest.raises(ValueError):
        server.UpdateProfileReq(first_name="")


def test_update_profile_about_max_140():
    """Premium-aware cap; 70 is the regular cap but Telegram allows up to 140."""
    server.UpdateProfileReq(about="x" * 140)
    with pytest.raises(ValueError):
        server.UpdateProfileReq(about="x" * 141)


def test_update_profile_first_name_max_64():
    server.UpdateProfileReq(first_name="x" * 64)
    with pytest.raises(ValueError):
        server.UpdateProfileReq(first_name="x" * 65)


def test_username_requires_5_to_32_chars_and_letter_first():
    # Valid
    server.UpdateUsernameReq(username="alice5")
    server.UpdateUsernameReq(username="A1_b2_c3")
    server.UpdateUsernameReq(username="x" * 32)
    # Invalid: too short
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="abcd")
    # Invalid: starts with digit
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="9alice")
    # Invalid: bad character
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="alice-7")
    # Invalid: too long (33 chars triggers max_length first)
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="x" * 33)


def test_username_rejects_trailing_underscore():
    """Round-16 MAJOR: Telegram rejects usernames ending in `_`. The local
    validator must catch that before we issue the upstream request."""
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="alice_")
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="bob123_")
    # Underscores in the MIDDLE are still fine.
    server.UpdateUsernameReq(username="al_ice")


def test_username_rejects_leading_underscore():
    """The first char must be a letter — already enforced, but
    explicitly pin the leading-underscore case here too."""
    with pytest.raises(ValueError):
        server.UpdateUsernameReq(username="_alice")


def test_username_empty_string_clears():
    """Empty username explicitly = "clear my @username"."""
    req = server.UpdateUsernameReq(username="")
    assert req.username == ""


def test_session_has_profile_methods():
    from tgmcp.daemon.telegram import TGSession

    for name in (
        "update_profile",
        "update_username",
        "set_profile_photo",
        "delete_current_profile_photo",
        "set_online_status",
    ):
        assert hasattr(TGSession, name), f"missing {name!r}"


def test_client_has_profile_methods():
    from tgmcp.client import DaemonClient

    for name in (
        "profile_update",
        "profile_username",
        "profile_photo",
        "profile_photo_delete",
        "profile_status",
    ):
        assert hasattr(DaemonClient, name)


def test_audit_logs_field_lengths_via_is_not_none_check():
    """Round-16 MINOR: explicit `req.last_name=""` must be logged as
    length-0, not None — distinguishing a clear from a missing field.
    The handler must use `is not None` (not truthiness) to guard the
    len() call."""
    src = inspect.getsource(server.profile_update)
    audit_idx = src.find("audit.log(")
    block = src[audit_idx:]
    # We forbid the bare-truthiness pattern that conflates "" with None:
    assert "if req.first_name else None" not in block
    assert "if req.last_name else None" not in block
    assert "if req.about else None" not in block
    # And require the explicit-None check is present:
    assert "is not None" in block


def test_delete_profile_photo_uses_get_profile_photos_and_input_photo():
    """Round-16 BLOCKER: Telethon's TelegramClient does not expose a
    `get_full_user` shortcut, and DeletePhotosRequest needs InputPhoto
    (not bare Photo). The fixed implementation uses
    client.get_profile_photos('me') and telethon.utils.get_input_photo."""
    from tgmcp.daemon.telegram import TGSession

    src = inspect.getsource(TGSession.delete_current_profile_photo)
    assert "get_profile_photos" in src
    assert "get_input_photo" in src
    # And the buggy call must be gone.
    assert "client.get_full_user" not in src


def test_update_profile_reads_about_via_get_full_user():
    """Round-16 MAJOR: about/bio is on UserFull, not the bare User from
    get_me(). Returning it from the post-update response requires
    GetFullUserRequest."""
    from tgmcp.daemon.telegram import TGSession

    src = inspect.getsource(TGSession.update_profile)
    assert "GetFullUserRequest" in src, (
        "update_profile must read `about` from full_user, not from get_me()"
    )


def test_audit_does_not_log_profile_field_values():
    """Display names and bios can be very personal (real name, contact info,
    employer). Audit must record what fields were touched and their length,
    not the values themselves."""
    src = inspect.getsource(server.profile_update)
    audit_idx = src.find("audit.log(")
    block = src[audit_idx:]
    # Forbidden: passing the raw values into audit
    assert "first_name=req.first_name" not in block
    assert "last_name=req.last_name" not in block
    assert "about=req.about" not in block


def test_audit_logs_username_value_since_public():
    """Username IS public — logging it is fine and is useful for tracing
    'who am I now reachable as' history."""
    src = inspect.getsource(server.profile_username)
    assert "new_username" in src
    assert "audit.log" in src


def test_photo_endpoint_reuses_open_validated_upload():
    """Profile photos must go through the same TOCTOU-hardened pipeline
    as media uploads — symlink/runtime-dir/FIFO/inode-swap defenses."""
    src = inspect.getsource(server.profile_photo)
    assert "_open_validated_upload" in src, (
        "profile/photo must reuse the hardened upload pipeline"
    )
    assert "_audit_path_redacted" in src


def _load_skill():
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-profile"
        / "profile.py"
    )
    spec = importlib.util.spec_from_file_location("profile_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_skill_handlers_registered():
    mod = _load_skill()
    expected = {"update", "username", "photo", "photo-delete", "online", "offline"}
    assert set(mod.HANDLERS.keys()) == expected


def test_skill_update_requires_at_least_one_field():
    mod = _load_skill()
    args = mod.build_parser().parse_args(["update"])
    with pytest.raises(SystemExit, match="at least one"):
        mod.cmd_update(args, c=None)


def test_skill_username_requires_new_or_clear():
    mod = _load_skill()
    args = mod.build_parser().parse_args(["username"])
    with pytest.raises(SystemExit, match="--new"):
        mod.cmd_username(args, c=None)


def test_skill_username_rejects_both_new_and_clear():
    """argparse mutually_exclusive_group already enforces this."""
    mod = _load_skill()
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["username", "--new", "abc", "--clear"]
        )
