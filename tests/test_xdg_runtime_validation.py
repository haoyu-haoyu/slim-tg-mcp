"""Round-13 MINOR-37: XDG_RUNTIME_DIR itself must be validated, not just
the `.../tgmcp` leaf inside it. An attacker who can set the env var (e.g.
through a poisoned shell init) could otherwise redirect the entire
runtime tree."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

from tgmcp.daemon import paths as paths_mod  # noqa: E402


def test_safe_xdg_returned_when_owned_and_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    os.chmod(tmp_path, 0o700)
    assert paths_mod._xdg_runtime_dir_safe() == tmp_path


def test_xdg_with_wide_perms_is_rejected(tmp_path, monkeypatch):
    """Per XDG spec, runtime dir MUST be 0700. Anything wider is unsafe."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    os.chmod(tmp_path, 0o755)
    assert paths_mod._xdg_runtime_dir_safe() is None


def test_xdg_symlink_is_rejected(tmp_path, monkeypatch):
    decoy = tmp_path / "real"
    decoy.mkdir()
    os.chmod(decoy, 0o700)
    link = tmp_path / "link"
    link.symlink_to(decoy)

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(link))
    assert paths_mod._xdg_runtime_dir_safe() is None


def test_xdg_unset_returns_none(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert paths_mod._xdg_runtime_dir_safe() is None


def test_xdg_nonexistent_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    assert paths_mod._xdg_runtime_dir_safe() is None


def test_xdg_wrong_owner_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    os.chmod(tmp_path, 0o700)

    real_lstat = os.lstat

    def fake_lstat(p):
        st = real_lstat(p)
        if Path(str(p)) == tmp_path:
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                 os.getuid() + 9999, st.st_gid, st.st_size, st.st_atime,
                 st.st_mtime, st.st_ctime)
            )
        return st

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)
    assert paths_mod._xdg_runtime_dir_safe() is None
