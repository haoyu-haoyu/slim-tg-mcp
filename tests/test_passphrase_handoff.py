"""Verify that passphrases reach the daemon WITHOUT going through process
environment variables that same-user processes could read."""

from __future__ import annotations

import os

import pytest

from tgmcp.daemon import server


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    server.set_passphrase_override(None)
    monkeypatch.delenv("TGMCP_PASSPHRASE_FD", raising=False)
    monkeypatch.delenv("TGMCP_PASSPHRASE", raising=False)
    yield
    server.set_passphrase_override(None)


def test_module_override_consumed_and_cleared():
    server.set_passphrase_override("hunter2")
    assert server._consume_passphrase() == "hunter2"
    # Second call returns None — the secret is single-use.
    assert server._consume_passphrase() is None


def test_pipe_fd_passphrase_consumed_and_env_cleared():
    r, w = os.pipe()
    os.write(w, b"my-secret\n")
    os.close(w)
    os.environ["TGMCP_PASSPHRASE_FD"] = str(r)

    got = server._consume_passphrase()

    assert got == "my-secret"
    # FD env var must be removed so a re-import / second consume can't reuse.
    assert "TGMCP_PASSPHRASE_FD" not in os.environ
    # FD must be closed (read returned EOF; further use raises).
    with pytest.raises(OSError):
        os.read(r, 1)


def test_legacy_env_passphrase_is_popped():
    os.environ["TGMCP_PASSPHRASE"] = "leaky"
    got = server._consume_passphrase()
    assert got == "leaky"
    # Critical: the env var is removed so /proc/<pid>/environ on subsequent
    # reads (and `os.environ` introspection from this process) is clean.
    assert "TGMCP_PASSPHRASE" not in os.environ


def test_priority_module_over_fd():
    """Module override wins. If both are present, fd is left for next caller —
    but in practice this state shouldn't occur."""
    server.set_passphrase_override("from-module")
    r, w = os.pipe()
    os.write(w, b"from-fd")
    os.close(w)
    os.environ["TGMCP_PASSPHRASE_FD"] = str(r)
    try:
        assert server._consume_passphrase() == "from-module"
    finally:
        try:
            os.close(r)
        except OSError:
            pass
        os.environ.pop("TGMCP_PASSPHRASE_FD", None)


def test_no_passphrase_returns_none():
    assert server._consume_passphrase() is None
