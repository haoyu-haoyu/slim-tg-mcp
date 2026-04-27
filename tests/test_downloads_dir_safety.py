"""Round-13 MAJOR: DOWNLOADS_DIR must be validated like RUNTIME_DIR.

If `<RUNTIME_DIR>/downloads` is pre-created as a symlink to an attacker-
controlled directory, our server-generated filenames (and Telegram media
content) get written into the attacker's path. The extension filter on
the filename does NOT prevent this — only path validation does.
"""

from __future__ import annotations

import os
import sys

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

from tgmcp.daemon import paths as paths_mod  # noqa: E402


def test_ensure_safe_subdir_creates_with_0700(tmp_path):
    d = paths_mod.ensure_safe_subdir(tmp_path, "downloads")
    assert d == tmp_path / "downloads"
    assert d.is_dir()
    mode = os.stat(d).st_mode & 0o777
    assert mode == 0o700


def test_ensure_safe_subdir_rejects_symlink(tmp_path):
    decoy = tmp_path / "attacker"
    decoy.mkdir()
    target = tmp_path / "downloads"
    target.symlink_to(decoy)

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="symlink"):
        paths_mod.ensure_safe_subdir(tmp_path, "downloads")


def test_ensure_safe_subdir_rejects_wide_perms(tmp_path):
    target = tmp_path / "downloads"
    target.mkdir()
    os.chmod(target, 0o755)

    with pytest.raises(paths_mod.UnsafeRuntimeDir, match="0700"):
        paths_mod.ensure_safe_subdir(tmp_path, "downloads")


def test_ensure_safe_subdir_accepts_existing_safe_dir(tmp_path):
    target = tmp_path / "downloads"
    target.mkdir()
    os.chmod(target, 0o700)

    # No exception — already-safe dirs pass through.
    result = paths_mod.ensure_safe_subdir(tmp_path, "downloads")
    assert result == target
