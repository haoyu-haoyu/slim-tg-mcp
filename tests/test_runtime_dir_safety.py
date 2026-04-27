"""Round-12 BLOCKER: runtime dir squat / symlink attacks.

If another local user can pre-create `/tmp/tgmcp-<uid>` (or symlink it
elsewhere), they can position themselves to read our daemon socket, lock,
and pid files. We MUST refuse to use such a directory.
"""

from __future__ import annotations

import os
import sys

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

from tgmcp.daemon import paths as paths_mod  # noqa: E402


def test_fresh_dir_creation_succeeds(tmp_path, monkeypatch):
    """Happy path: dir doesn't exist → mkdir 0700 → safe."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    result = paths_mod._resolve_runtime_dir(fallback_base=tmp_path)
    expected = tmp_path / f"tgmcp-{os.getuid()}"
    assert result == expected
    assert expected.is_dir()
    mode = os.stat(expected).st_mode & 0o777
    assert mode == 0o700


def test_symlink_squat_is_rejected(tmp_path, monkeypatch):
    """Attacker pre-creates the target as a symlink to their own dir.
    We must lstat and refuse."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    target = tmp_path / f"tgmcp-{os.getuid()}"
    decoy = tmp_path / "attacker_dir"
    decoy.mkdir()
    target.symlink_to(decoy)

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="symlink"):
        paths_mod._resolve_runtime_dir(fallback_base=tmp_path)


def test_world_writable_dir_is_rejected(tmp_path, monkeypatch):
    """Attacker pre-creates the dir with 0777. We must refuse."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    target = tmp_path / f"tgmcp-{os.getuid()}"
    target.mkdir()
    os.chmod(target, 0o777)

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="0700"):
        paths_mod._resolve_runtime_dir(fallback_base=tmp_path)


def test_existing_safe_dir_is_accepted(tmp_path, monkeypatch):
    """If the dir already exists with correct owner+mode, we use it."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    target = tmp_path / f"tgmcp-{os.getuid()}"
    target.mkdir()
    os.chmod(target, 0o700)

    result = paths_mod._resolve_runtime_dir(fallback_base=tmp_path)
    assert result == target


def test_file_at_runtime_path_is_rejected(tmp_path, monkeypatch):
    """If the path exists but is a regular file, refuse."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    target = tmp_path / f"tgmcp-{os.getuid()}"
    target.write_text("squatting")

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="not a directory"):
        paths_mod._resolve_runtime_dir(fallback_base=tmp_path)


def test_validate_rejects_wrong_owner(tmp_path, monkeypatch):
    """We can't easily chown in tests, so we forge ownership via os.lstat."""
    target = tmp_path / "wrongowner"
    target.mkdir()
    os.chmod(target, 0o700)

    real_lstat = os.lstat

    def fake_lstat(p):
        st = real_lstat(p)
        # Forge a different uid than ours.
        return os.stat_result(
            (st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
             os.getuid() + 9999, st.st_gid, st.st_size, st.st_atime,
             st.st_mtime, st.st_ctime)
        )

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="squat|owned by uid"):
        paths_mod._validate_runtime_dir(target)
