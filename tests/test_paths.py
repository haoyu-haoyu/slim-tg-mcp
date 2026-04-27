"""Runtime artifacts MUST live on a guaranteed-local filesystem.

Background: Unix sockets and `flock` are unreliable on NFS/SMB mounts.
If `~/.config/tgmcp` happens to live on a network home, anchoring the
socket+lock there breaks our "lock is authoritative" liveness invariant.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def fresh_paths(monkeypatch):
    """Reload the paths module with a clean environment so its module-level
    `_resolve_runtime_dir()` re-evaluates."""
    import importlib

    def _load(env: dict[str, str | None]) -> object:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        # Ensure the module exists in sys.modules first, then reload.
        mod = importlib.import_module("tgmcp.daemon.paths")
        return importlib.reload(mod)

    return _load


def test_runtime_dir_uses_xdg_when_set(fresh_paths, tmp_path):
    paths = fresh_paths({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert paths.RUNTIME_DIR == tmp_path / "tgmcp"
    assert paths.RUNTIME_DIR.is_dir()


def test_runtime_dir_falls_back_to_fixed_tmp_when_xdg_unset(fresh_paths):
    paths = fresh_paths({"XDG_RUNTIME_DIR": None})
    expected = Path("/tmp") / f"tgmcp-{os.getuid()}"
    assert paths.RUNTIME_DIR == expected
    assert paths.RUNTIME_DIR.is_dir()


def test_runtime_dir_ignores_tmpdir_env_override(fresh_paths, tmp_path):
    """Critical: `tempfile.gettempdir()` honors $TMPDIR which can point at
    NFS, defeating the whole point of the runtime split. We must hardcode /tmp."""
    paths = fresh_paths(
        {
            "XDG_RUNTIME_DIR": None,
            "TMPDIR": str(tmp_path),  # e.g. some weird user-set tempdir
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        }
    )
    # Even with TMPDIR pointing at tmp_path, our runtime dir must be /tmp/...
    assert paths.RUNTIME_DIR == Path("/tmp") / f"tgmcp-{os.getuid()}"
    assert tmp_path not in paths.RUNTIME_DIR.parents


def test_socket_lock_pid_all_under_runtime_dir(fresh_paths, tmp_path):
    paths = fresh_paths({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert paths.SOCKET_PATH.parent == paths.RUNTIME_DIR
    assert paths.LOCK_PATH.parent == paths.RUNTIME_DIR
    assert paths.PID_PATH.parent == paths.RUNTIME_DIR
    # The three runtime artifacts must NOT live under CONFIG_DIR.
    assert paths.CONFIG_DIR not in paths.SOCKET_PATH.parents
    assert paths.CONFIG_DIR not in paths.LOCK_PATH.parents


def test_persistent_paths_remain_in_config_dir(fresh_paths, tmp_path):
    paths = fresh_paths({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert paths.AUDIT_LOG_PATH.parent == paths.CONFIG_DIR
    assert paths.LOG_PATH.parent == paths.CONFIG_DIR
    assert paths.SESSIONS_DIR.parent == paths.CONFIG_DIR


def test_runtime_dir_permissions_are_locked_down(fresh_paths, tmp_path):
    paths = fresh_paths({"XDG_RUNTIME_DIR": str(tmp_path)})
    mode = os.stat(paths.RUNTIME_DIR).st_mode & 0o777
    # Must be at most 0o700 — no group/world access to our runtime artifacts.
    assert mode & 0o077 == 0, f"runtime dir is too permissive: {oct(mode)}"
