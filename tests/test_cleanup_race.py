"""TOCTOU regression: when `_clean_stale_artifacts` runs, a concurrent daemon
must not lose its live socket/pid.

Scenario from round-11 review:
  1. CLI runs `inspect_daemon()` → sees lock-free → decides to clean.
  2. Before unlink, a fresh daemon starts and acquires `daemon.lock`.
  3. Without atomic cleanup, our unlink would delete the new daemon's socket.

The fix: `_clean_stale_artifacts` itself takes the same flock before
unlinking. If the lock is held (= a daemon is alive), it aborts cleanup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

fcntl = pytest.importorskip("fcntl")

import os  # noqa: E402

from tgmcp.cli import main as cli_main  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOCK_PATH", tmp_path / "lock")
    yield


def test_cleanup_skipped_when_lock_is_held():
    """If another process holds daemon.lock at cleanup time, _clean_stale_artifacts
    must NOT unlink — those files belong to the live daemon."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()

    # Simulate a concurrent daemon: open the lock file and hold the flock.
    cli_main.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    holder_fd = os.open(str(cli_main.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        cli_main._clean_stale_artifacts()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # Files MUST still exist — we refused to clean while lock was held.
    assert cli_main.PID_PATH.exists(), "live pid file must not be deleted"
    assert cli_main.SOCKET_PATH.exists(), "live socket must not be deleted"


def test_cleanup_proceeds_when_lock_is_free():
    """When the lock is free, cleanup must proceed normally."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()
    # Lock file exists but nobody is holding it.

    cli_main._clean_stale_artifacts()

    assert not cli_main.PID_PATH.exists()
    assert not cli_main.SOCKET_PATH.exists()


def test_cleanup_creates_lock_file_if_missing():
    """If the lock file doesn't exist at all, cleanup must still work
    (acquire-and-release on the freshly-created file)."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()
    # Note: LOCK_PATH does NOT exist here.
    assert not cli_main.LOCK_PATH.exists()

    cli_main._clean_stale_artifacts()

    assert not cli_main.PID_PATH.exists()
    assert not cli_main.SOCKET_PATH.exists()


def test_cleanup_releases_lock_after_unlink():
    """After cleanup, the lock must be released so the next daemon can take it."""
    cli_main.PID_PATH.write_text("12345")
    cli_main.SOCKET_PATH.touch()

    cli_main._clean_stale_artifacts()

    # We should now be able to acquire the lock from a fresh fd.
    fd = os.open(str(cli_main.LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got it — cleanup released properly.
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
