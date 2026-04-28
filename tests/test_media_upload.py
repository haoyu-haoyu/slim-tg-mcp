"""tg-media-upload: schema, route, path-validation regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from tgmcp.daemon import server


def test_send_media_route_registered():
    paths = {r.path for r in server.app.routes}
    assert "/send_media" in paths


def test_send_media_required_fields():
    fields = {n for n, f in server.SendMediaReq.model_fields.items() if f.is_required()}
    assert fields == {"chat", "file_path"}


def test_validate_upload_path_rejects_symlink(tmp_path: Path):
    real = tmp_path / "real.txt"
    real.write_text("ok")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(link))
    assert ei.value.status_code == 400
    assert "symlink" in ei.value.detail.lower()


def test_validate_upload_path_rejects_directory(tmp_path: Path):
    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(tmp_path))
    assert ei.value.status_code == 400
    assert "regular file" in ei.value.detail.lower()


def test_validate_upload_path_rejects_missing(tmp_path: Path):
    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(tmp_path / "ghost.bin"))
    assert ei.value.status_code == 404


def test_validate_upload_path_rejects_oversize(tmp_path: Path, monkeypatch):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x")
    # Force the cap below the actual file size to trigger 413 without
    # writing a real 2 GB file.
    monkeypatch.setattr(server, "MAX_UPLOAD_SIZE", 0)
    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(f))
    assert ei.value.status_code == 413


def test_validate_upload_path_rejects_runtime_dir(tmp_path: Path, monkeypatch):
    """Refuse to upload from the daemon's own runtime tree (lock/sock/pid)."""
    from tgmcp.daemon import paths as paths_mod

    fake_runtime = tmp_path / "fake-runtime"
    fake_runtime.mkdir()
    monkeypatch.setattr(paths_mod, "RUNTIME_DIR", fake_runtime)

    inside = fake_runtime / "secret.txt"
    inside.write_text("hi")

    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(inside))
    assert ei.value.status_code == 400
    assert "runtime directory" in ei.value.detail.lower()


def test_validate_upload_path_returns_absolute(tmp_path: Path):
    f = tmp_path / "ok.txt"
    f.write_text("hi")
    abs_path = server._validate_upload_path(str(f))
    assert os.path.isabs(abs_path)
    assert abs_path == str(f)


def test_session_has_send_media_method():
    from tgmcp.daemon.telegram import TGSession

    assert hasattr(TGSession, "send_media")


def test_client_has_send_media_method():
    from tgmcp.client import DaemonClient

    assert hasattr(DaemonClient, "send_media")


def test_skill_dispatcher_loads():
    import importlib.util

    skill = Path(__file__).resolve().parents[1] / "skills" / "tg-media-upload" / "media.py"
    spec = importlib.util.spec_from_file_location("media", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert "send" in mod.HANDLERS


def _load_media_skill():
    import importlib.util

    skill = Path(__file__).resolve().parents[1] / "skills" / "tg-media-upload" / "media.py"
    spec = importlib.util.spec_from_file_location("media_skill", skill)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_validate_rejects_symlink_in_parent_chain(tmp_path: Path):
    """Round-8 BLOCKER: a symlink in a PARENT component bypassed the leaf
    lstat. Pin it: link a parent dir, place a real file inside, validate
    the path → must be rejected."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "secret.txt"
    real_file.write_text("hi")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    via_link = link_dir / "secret.txt"
    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(via_link))
    assert ei.value.status_code == 400
    assert "symlink" in ei.value.detail.lower()


def test_validate_rejects_runtime_dir_via_symlink(tmp_path: Path, monkeypatch):
    """Containment check must use realpath on BOTH sides — a path that
    resolves into RUNTIME_DIR via a symlink shortcut must still be blocked."""
    from tgmcp.daemon import paths as paths_mod

    fake_runtime = tmp_path / "rt"
    fake_runtime.mkdir()
    target = fake_runtime / "leak.txt"
    target.write_text("x")
    monkeypatch.setattr(paths_mod, "RUNTIME_DIR", fake_runtime)

    link_into_runtime = tmp_path / "shortcut"
    link_into_runtime.symlink_to(fake_runtime)
    via = link_into_runtime / "leak.txt"

    with pytest.raises(HTTPException) as ei:
        server._validate_upload_path(str(via))
    assert ei.value.status_code == 400


def test_open_validated_upload_returns_fd_and_size(tmp_path: Path):
    """Validate-then-open pipeline returns an opened fd and accurate size."""
    f = tmp_path / "ok.bin"
    f.write_bytes(b"hello")
    abs_path, size, fd = server._open_validated_upload(str(f))
    try:
        assert size == 5
        # We got a real fd, not the path.
        assert isinstance(fd, int) and fd >= 0
        assert os.read(fd, 5) == b"hello"
    finally:
        os.close(fd)


def test_open_validated_upload_o_nofollow_flag(tmp_path: Path, monkeypatch):
    """O_NOFOLLOW must be in the open flags so a symlink swap raises ELOOP
    instead of being silently followed."""
    f = tmp_path / "ok.bin"
    f.write_bytes(b"hi")

    seen_flags: list[int] = []
    real_open = os.open

    def spy(p, flags, *a, **kw):
        seen_flags.append(flags)
        return real_open(p, flags, *a, **kw)

    monkeypatch.setattr(server.os, "open", spy)
    abs_path, size, fd = server._open_validated_upload(str(f))
    os.close(fd)

    assert len(seen_flags) == 1
    assert seen_flags[0] & os.O_NOFOLLOW, "must include O_NOFOLLOW"


def test_open_validated_upload_rejects_regular_file_replacement(
    tmp_path: Path, monkeypatch
):
    """Round-9 BLOCKER: O_NOFOLLOW alone does NOT defend against a
    regular-file → regular-file swap (mv attacker.bin victim). The
    daemon must compare lstat (pre-open) to fstat (post-open) by
    (st_dev, st_ino) and refuse on mismatch.

    We simulate the race by patching `os.open` to atomically replace
    the validated file with a different regular file *just before*
    the real open returns its fd."""
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"AAAA")
    decoy = tmp_path / "decoy.bin"
    decoy.write_bytes(b"DECOY-CONTENT-WAY-DIFFERENT")

    real_open = os.open

    def race_open(path, flags, *a, **kw):
        # Right before the real open, swap victim's inode with decoy's
        # via os.replace. After this, the path "victim" still exists but
        # points at decoy's inode.
        if str(path) == str(victim):
            os.replace(str(decoy), str(victim))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(server.os, "open", race_open)

    with pytest.raises(HTTPException) as ei:
        server._open_validated_upload(str(victim))
    assert ei.value.status_code == 400
    detail = ei.value.detail.lower()
    assert (
        "replaced between validation and open" in detail
        or "ino=" in detail
    ), f"expected dev/ino mismatch error, got: {ei.value.detail!r}"


def test_open_validated_upload_does_not_hang_on_fifo_swap(
    tmp_path: Path, monkeypatch
):
    """Round-10 BLOCKER: opening a FIFO read-side without O_NONBLOCK blocks
    indefinitely until a writer arrives. An attacker who can swap the
    validated path for a FIFO could DoS the daemon. The open must include
    O_NONBLOCK so we return fast and reject via fstat checks.

    The defense-in-depth cascade (any of these is sufficient):
      1. O_NONBLOCK → open returns fast
      2. fstat dev/ino mismatch → swap detected
      3. S_ISREG check → non-regular fd rejected
    """
    import threading

    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"AAAA")
    fifo = tmp_path / "trap.fifo"
    os.mkfifo(str(fifo))

    real_open = os.open

    def race_open(path, flags, *a, **kw):
        if str(path) == str(victim):
            os.unlink(str(victim))
            os.replace(str(fifo), str(victim))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(server.os, "open", race_open)

    # Watchdog: the test's primary assertion is "doesn't hang". Without
    # O_NONBLOCK in the flags, the FIFO open would block until a writer
    # appeared (i.e. forever). With it, we should complete in micros.
    result: dict = {}

    def runner():
        try:
            server._open_validated_upload(str(victim))
        except HTTPException as e:
            result["err"] = e
        except Exception as e:
            result["other"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), (
        "open hung past 5s — O_NONBLOCK is missing; daemon would be DoS'd "
        "by a FIFO swap"
    )
    err = result.get("err")
    assert err is not None, f"expected HTTPException, got {result}"
    assert err.status_code == 400
    detail = err.detail.lower()
    # Any of the cascade defenses triggering is acceptable:
    assert (
        "non-regular" in detail
        or "fifo" in detail
        or "replaced between validation and open" in detail
        or "ino=" in detail
    ), f"expected swap/non-regular rejection, got: {err.detail!r}"


def test_open_validated_upload_uses_o_nonblock_in_flags(
    tmp_path: Path, monkeypatch
):
    """Pin O_NONBLOCK explicitly — the only thing standing between us and a
    FIFO-DoS. Without this flag, `os.open` of a FIFO read-side blocks
    indefinitely waiting for a writer."""
    f = tmp_path / "ok.bin"
    f.write_bytes(b"hi")

    seen_flags: list[int] = []
    real_open = os.open

    def spy(p, flags, *a, **kw):
        seen_flags.append(flags)
        return real_open(p, flags, *a, **kw)

    monkeypatch.setattr(server.os, "open", spy)
    abs_path, size, fd = server._open_validated_upload(str(f))
    os.close(fd)

    assert len(seen_flags) == 1
    assert seen_flags[0] & os.O_NONBLOCK, (
        "must include O_NONBLOCK so a late FIFO swap can't DoS us"
    )


def test_open_validated_upload_accepts_unmodified_file(tmp_path: Path):
    """Sanity: when nothing races, the same-inode check passes and we get
    a usable fd back."""
    f = tmp_path / "ok.bin"
    f.write_bytes(b"hello")
    abs_path, size, fd = server._open_validated_upload(str(f))
    try:
        assert size == 5
        assert os.read(fd, 5) == b"hello"
    finally:
        os.close(fd)


def test_audit_path_redacted_only_logs_basename_and_hash():
    """Round-8 MAJOR: audit must NOT log absolute paths."""
    out = server._audit_path_redacted("/Users/wanghaoyu/secret/dossier.pdf")
    assert out["name"] == "dossier.pdf"
    assert out["parent_hash"] and len(out["parent_hash"]) == 8
    # Critical: the original directory must not appear anywhere in the dict.
    flat = repr(out)
    assert "/Users/wanghaoyu" not in flat
    assert "/secret" not in flat


def test_caption_too_long_rejected_at_schema():
    """Telegram cap is 1024 chars; we should bounce at the boundary."""
    with pytest.raises(ValueError):
        server.SendMediaReq(chat="@x", file_path="/tmp/x", caption="a" * 1025)
    # 1024 exactly is allowed.
    ok = server.SendMediaReq(chat="@x", file_path="/tmp/x", caption="a" * 1024)
    assert ok.caption == "a" * 1024


def test_skill_refuses_send_without_any_confirmation(tmp_path: Path):
    """Round-8 MAJOR: dispatcher must enforce the confirm-before-send
    contract at the skill, not just in docs."""
    mod = _load_media_skill()
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x")
    args = mod.build_parser().parse_args(["send", "--chat", "@x", "--file", str(f)])
    with pytest.raises(SystemExit, match="confirmation"):
        mod.cmd_send(args, c=None)


def test_skill_refuses_send_when_confirm_strings_dont_match(tmp_path: Path):
    mod = _load_media_skill()
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x")
    args = mod.build_parser().parse_args(
        [
            "send",
            "--chat", "@alice",
            "--file", str(f),
            "--confirm-chat", "@bob",  # mismatched
            "--confirm-file", str(f),
        ]
    )
    with pytest.raises(SystemExit, match="do not match"):
        mod.cmd_send(args, c=None)


def test_skill_refuses_send_when_only_yes_flag_implicit(tmp_path: Path):
    """The --yes flag is the explicit caller-confirms-with-human path. Without
    it, --confirm-chat AND --confirm-file are required (asymmetry test)."""
    mod = _load_media_skill()
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x")
    args = mod.build_parser().parse_args(
        ["send", "--chat", "@x", "--file", str(f), "--confirm-chat", "@x"]
        # missing --confirm-file
    )
    with pytest.raises(SystemExit, match="confirmation"):
        mod.cmd_send(args, c=None)


def test_voice_extension_rejected_when_not_audio(tmp_path: Path):
    """as_voice=True with a non-audio extension is a clear configuration
    error — bounce at the daemon boundary, not after upload starts."""
    f = tmp_path / "vid.mp4"
    f.write_bytes(b"x")

    # We exercise the route function directly with a fake session.
    import asyncio

    async def run():
        return await server.send_media(
            server.SendMediaReq(
                chat="@x", file_path=str(f), as_voice=True
            )
        )

    with pytest.raises(HTTPException) as ei:
        asyncio.run(run())
    assert ei.value.status_code == 400
    assert "audio extension" in ei.value.detail.lower()
