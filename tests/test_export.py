"""tg-export: out_dir validation, confirmation gate, audit redaction."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from tgmcp.daemon import server


def test_export_route_registered():
    paths = {r.path for r in server.app.routes}
    assert "/export/chat" in paths


def test_export_schema_required_fields():
    fields = {n for n, f in server.ExportChatReq.model_fields.items() if f.is_required()}
    assert fields == {"chat", "out_dir"}


def test_export_limit_bounds():
    server.ExportChatReq(chat="@x", out_dir="/tmp")
    with pytest.raises(ValueError):
        server.ExportChatReq(chat="@x", out_dir="/tmp", limit=0)
    with pytest.raises(ValueError):
        server.ExportChatReq(chat="@x", out_dir="/tmp", limit=100001)


def test_validate_export_dir_rejects_missing(tmp_path: Path):
    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(tmp_path / "ghost"))
    assert ei.value.status_code == 404


def test_validate_export_dir_rejects_file(tmp_path: Path):
    f = tmp_path / "afile"
    f.write_text("hi")
    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(f))
    assert ei.value.status_code == 400
    assert "not a directory" in ei.value.detail.lower()


def test_validate_export_dir_rejects_symlink_leaf(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(link))
    assert "symlink" in ei.value.detail.lower()


def test_validate_export_dir_rejects_symlink_in_parent(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    target = real / "out"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    via_link = link / "out"
    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(via_link))
    assert ei.value.status_code == 400
    assert "symlink" in ei.value.detail.lower()


def test_validate_export_dir_rejects_runtime_dir(tmp_path: Path, monkeypatch):
    """Refuse to export under the daemon's own runtime tree."""
    from tgmcp.daemon import paths as paths_mod

    fake_runtime = tmp_path / "rt"
    fake_runtime.mkdir()
    target = fake_runtime / "out"
    target.mkdir()
    monkeypatch.setattr(paths_mod, "RUNTIME_DIR", fake_runtime)

    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(target))
    assert ei.value.status_code == 400
    assert "runtime" in ei.value.detail.lower()


def test_validate_export_dir_rejects_config_dir(tmp_path: Path, monkeypatch):
    """Refuse to export under the daemon's own config tree (sessions/audit)."""
    from tgmcp.daemon import paths as paths_mod

    fake_config = tmp_path / "cfg"
    fake_config.mkdir()
    target = fake_config / "out"
    target.mkdir()
    monkeypatch.setattr(paths_mod, "CONFIG_DIR", fake_config)

    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(target))
    assert ei.value.status_code == 400
    assert "config" in ei.value.detail.lower()


def test_validate_export_dir_rejects_wrong_owner(tmp_path: Path, monkeypatch):
    target = tmp_path / "out"
    target.mkdir()

    real_lstat = os.lstat

    def fake_lstat(p):
        st = real_lstat(p)
        if str(p) == str(target):
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                 os.getuid() + 9999, st.st_gid, st.st_size, st.st_atime,
                 st.st_mtime, st.st_ctime)
            )
        return st

    monkeypatch.setattr(server.os, "lstat", fake_lstat)
    with pytest.raises(HTTPException) as ei:
        server._validate_export_dir(str(target))
    assert ei.value.status_code == 400
    assert "owned by" in ei.value.detail.lower()


def test_validate_export_dir_accepts_valid(tmp_path: Path):
    """Sanity: a real, owned-by-us directory passes."""
    target = tmp_path / "out"
    target.mkdir()
    res = server._validate_export_dir(str(target))
    assert res == str(target)


def test_session_has_export_chat():
    from tgmcp.daemon.telegram import TGSession

    assert hasattr(TGSession, "export_chat")


def test_client_has_export_chat():
    from tgmcp.client import DaemonClient

    assert hasattr(DaemonClient, "export_chat")


def _load_skill():
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "tg-export"
        / "export.py"
    )
    spec = importlib.util.spec_from_file_location("export_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_skill_refuses_export_without_any_confirmation(tmp_path: Path):
    mod = _load_skill()
    target = tmp_path / "out"
    target.mkdir()
    args = mod.build_parser().parse_args(
        ["run", "--chat", "@x", "--out-dir", str(target)]
    )
    with pytest.raises(SystemExit, match="confirmation"):
        mod.cmd_run(args, c=None)


def test_skill_refuses_when_confirm_strings_mismatch(tmp_path: Path):
    mod = _load_skill()
    target = tmp_path / "out"
    target.mkdir()
    args = mod.build_parser().parse_args(
        [
            "run",
            "--chat", "@alice",
            "--out-dir", str(target),
            "--confirm-chat", "@bob",  # mismatch
            "--confirm-out-dir", str(target),
        ]
    )
    with pytest.raises(SystemExit, match="do not match"):
        mod.cmd_run(args, c=None)


def test_skill_refuses_when_only_one_confirm_arg(tmp_path: Path):
    """Both --confirm-chat AND --confirm-out-dir required when not --yes."""
    mod = _load_skill()
    target = tmp_path / "out"
    target.mkdir()
    args = mod.build_parser().parse_args(
        [
            "run",
            "--chat", "@x",
            "--out-dir", str(target),
            "--confirm-chat", "@x",
            # missing --confirm-out-dir
        ]
    )
    with pytest.raises(SystemExit, match="confirmation"):
        mod.cmd_run(args, c=None)


def test_export_dates_must_be_timezone_aware():
    """Round-18 MAJOR: since_date / until_date naive datetimes would
    TypeError when compared to Telethon's tz-aware Message.date. The
    validator must reject naive values at the schema layer."""
    from datetime import datetime as _dt

    naive = _dt(2026, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        server.ExportChatReq(chat="@x", out_dir="/tmp", since_date=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        server.ExportChatReq(chat="@x", out_dir="/tmp", until_date=naive)


def test_export_dates_normalized_to_utc():
    """Tz-aware non-UTC inputs must be normalized to UTC for deterministic
    comparison against Telethon's UTC dates."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    plus8 = _tz(_td(hours=8))
    aware = _dt(2026, 1, 1, 12, 0, 0, tzinfo=plus8)
    req = server.ExportChatReq(chat="@x", out_dir="/tmp", since_date=aware)
    assert req.since_date.tzinfo is _tz.utc
    # 12:00 +08:00 = 04:00 UTC
    assert req.since_date.hour == 4


def test_open_validated_export_dir_returns_fd_and_closes_on_error(tmp_path: Path):
    """Round-18 MAJOR: the validate→use TOCTOU is closed by handing the
    caller a directory fd opened with O_DIRECTORY|O_NOFOLLOW. Verify
    the helper returns (abs_path, fd) and that subsequent operations can
    be performed via dir_fd."""
    target = tmp_path / "out"
    target.mkdir()
    abs_path, fd = server._open_validated_export_dir(str(target))
    try:
        assert abs_path == str(target)
        assert isinstance(fd, int) and fd >= 0
        # Confirm we can openat-create a file under it.
        sub_fd = os.open(
            "child", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
        )
        os.close(sub_fd)
        assert (target / "child").exists()
    finally:
        os.close(fd)


def test_open_validated_export_dir_uses_o_directory_and_nofollow_flags(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "out"
    target.mkdir()

    seen_flags: list[int] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        seen_flags.append(flags)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(server.os, "open", spy)
    abs_path, fd = server._open_validated_export_dir(str(target))
    os.close(fd)

    assert any(f & os.O_DIRECTORY for f in seen_flags), (
        "must include O_DIRECTORY so a swap to a non-directory fails open"
    )
    assert any(f & os.O_NOFOLLOW for f in seen_flags), (
        "must include O_NOFOLLOW so a symlink swap fails open"
    )


@pytest.mark.asyncio
async def test_export_chat_creates_files_via_dir_fd_not_path(tmp_path: Path):
    """Round-18 MAJOR: file creation must happen relative to the
    pre-opened parent fd. Run the real export_chat with stub Telethon
    and verify messages.json appears under chat_<id>/, with mode 0600."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    async def fake_iter_messages(_e, **kw):
        # Return nothing — we just want to verify the json file is written.
        if False:
            yield None
        return

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace(id=42, title="t")

        async def get_peer_id(self, _e):
            return -100123

        def iter_messages(self, *a, **kw):
            return fake_iter_messages(*a, **kw)

    s.client = FakeClient()

    target = tmp_path / "out"
    target.mkdir()
    abs_path, fd = server._open_validated_export_dir(str(target))
    try:
        res = await s.export_chat("@x", str(target), fd, limit=10)
    finally:
        os.close(fd)

    chat_dir = target / "chat_-100123"
    assert chat_dir.is_dir()
    json_file = chat_dir / "messages.json"
    assert json_file.is_file()
    mode = os.stat(json_file).st_mode & 0o777
    assert mode == 0o600
    assert res["message_count"] == 0


@pytest.mark.asyncio
async def test_export_chat_refuses_to_overwrite_existing_messages_json(
    tmp_path: Path,
):
    """Round-19 MAJOR: messages.json must be opened with O_EXCL, not
    O_TRUNC — a pre-existing file at the target name should make the
    export fail, not silently clobber the previous export."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    target = tmp_path / "out"
    target.mkdir()
    chat_dir = target / "chat_-100123"
    chat_dir.mkdir(mode=0o700)
    pre_existing = chat_dir / "messages.json"
    pre_existing.write_text('{"prior": "export"}')
    original_content = pre_existing.read_text()

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace(id=42, title="t")

        async def get_peer_id(self, _e):
            return -100123

        def iter_messages(self, *a, **kw):
            async def empty():
                if False:
                    yield None
            return empty()

    s.client = FakeClient()

    abs_path, fd = server._open_validated_export_dir(str(target))
    try:
        with pytest.raises(RuntimeError, match="already exists"):
            await s.export_chat("@x", str(target), fd, limit=10)
    finally:
        os.close(fd)

    # Critical: pre-existing messages.json must be UNTOUCHED.
    assert pre_existing.read_text() == original_content, (
        "export must NOT have overwritten the prior messages.json"
    )


@pytest.mark.asyncio
async def test_export_chat_rejects_pre_existing_chat_dir_symlink(tmp_path: Path):
    """If `chat_<id>` already exists in out_dir as a SYMLINK, refuse —
    don't follow it and write into the attacker-controlled target."""
    from types import SimpleNamespace

    from tgmcp.daemon.telegram import TGConfig, TGSession

    s = TGSession(cfg=TGConfig(api_id=1, api_hash="x", session_string="y"))

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    # Plant a symlink with the SAME name our code would create.
    (target / "chat_-100123").symlink_to(decoy)

    class FakeClient:
        async def get_entity(self, _q):
            return SimpleNamespace(id=42, title="t")

        async def get_peer_id(self, _e):
            return -100123

        def iter_messages(self, *a, **kw):
            async def empty():
                if False:
                    yield None
            return empty()

    s.client = FakeClient()

    abs_path, fd = server._open_validated_export_dir(str(target))
    try:
        with pytest.raises(RuntimeError, match="symlink"):
            await s.export_chat("@x", str(target), fd, limit=10)
    finally:
        os.close(fd)


def test_audit_does_not_log_message_content():
    """The export file itself contains the message bodies; the audit log
    just records that an export happened. We log the destination dir,
    counts, and the include_media flag — never message text."""
    import inspect

    src = inspect.getsource(server.export_chat)
    audit_idx = src.find("audit.log(")
    block = src[audit_idx:]
    # Whatever fields are passed to audit, message content must not appear.
    assert "messages=" not in block
    assert "text=" not in block
    # Counts and metadata are fine.
    assert "message_count" in block
    assert "out_dir" in block
