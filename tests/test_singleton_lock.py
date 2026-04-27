"""Verify the daemon's singleton flock prevents two daemons from racing on
the same socket — the round-4 MAJOR scenario.

The daemon is POSIX-only by design (Unix domain sockets + fcntl). On
non-POSIX runners this whole module skips."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":  # pragma: no cover - module-level skip
    pytest.skip("POSIX-only: fcntl/flock", allow_module_level=True)

fcntl = pytest.importorskip("fcntl")

from tgmcp.daemon import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "LOCK_PATH", tmp_path / "test.lock")
    monkeypatch.setattr(server, "SOCKET_PATH", tmp_path / "test.sock")
    yield


def test_acquire_returns_fd_and_writes_pid():
    fd = server._acquire_singleton_lock()
    try:
        # Lock file exists and contains our pid.
        content = server.LOCK_PATH.read_text().strip()
        assert content == str(os.getpid())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_second_acquire_fails_fast():
    """A second attempt while the first lock is held must exit, not block."""
    fd1 = server._acquire_singleton_lock()
    try:
        with pytest.raises(SystemExit) as ei:
            server._acquire_singleton_lock()
        assert ei.value.code == 1
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


def test_lock_releases_on_close():
    """Once the holder closes the fd, a fresh acquire must succeed."""
    fd1 = server._acquire_singleton_lock()
    fcntl.flock(fd1, fcntl.LOCK_UN)
    os.close(fd1)

    fd2 = server._acquire_singleton_lock()
    try:
        assert fd2 >= 0
    finally:
        fcntl.flock(fd2, fcntl.LOCK_UN)
        os.close(fd2)


def test_release_socket_cleans_up_socket_file(tmp_path):
    fd = server._acquire_singleton_lock()
    # Simulate uvicorn having created the socket file.
    server.SOCKET_PATH.touch()
    server._release_socket(fd)
    assert not server.SOCKET_PATH.exists()


def test_unexpected_oserror_exits_distinctly(monkeypatch, capsys):
    """A non-EWOULDBLOCK OSError from flock must take the *real-fault* branch,
    not the lock-held branch. We distinguish by stderr message."""
    import errno

    def boom(*args, **kwargs):
        raise OSError(errno.EIO, "fake I/O error")

    monkeypatch.setattr(fcntl, "flock", boom)
    with pytest.raises(SystemExit) as ei:
        server._acquire_singleton_lock()
    assert ei.value.code == 1

    err = capsys.readouterr().err
    # Real-fault branch emits "unexpected error" — distinct from the
    # "another daemon is running" message of the lock-held branch.
    assert "unexpected error" in err
    assert "another daemon is running" not in err


def test_lock_held_branch_emits_distinct_message(capsys):
    """Sibling test: the lock-held branch must emit its own distinct stderr
    message, so the two branches are observationally distinguishable."""
    fd1 = server._acquire_singleton_lock()
    try:
        with pytest.raises(SystemExit):
            server._acquire_singleton_lock()
        err = capsys.readouterr().err
        assert "another daemon is running" in err
        assert "unexpected error" not in err
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


def test_windows_path_exits_cleanly(monkeypatch):
    """On Windows the module helper should refuse cleanly, not crash with
    ModuleNotFoundError. We simulate by patching sys.platform."""
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as ei:
        server._import_fcntl()
    assert ei.value.code == 1
