"""`daemon stop` must wait for the daemon to actually exit (lock release)
before unlinking artifacts. Cleaning while the daemon is still flushing
state would yank its socket out from under it."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tgmcp.cli import main as cli_main


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOG_PATH", tmp_path / "log")
    yield


def test_stop_waits_for_lock_release_before_cleaning():
    """SIGTERM is sent, then the loop polls is_daemon_locked until it's False.

    After exit, /health must stop responding too — so the post-loop
    re-inspect classifies as STALE and cleanup proceeds.
    """
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    # First probe (inside inspect_daemon at start of stop): daemon is RUNNING.
    # Subsequent probes (after SIGTERM): None (daemon exited).
    probe_seq = iter([{"ok": True, "pid": my_pid}, None, None, None])

    def fake_probe():
        return next(probe_seq, None)

    locked_seq = iter([(True, my_pid), (True, my_pid), (False, None), (False, None)])

    def fake_locked():
        return next(locked_seq, (False, None))

    with patch.object(cli_main, "_probe_existing_daemon", side_effect=fake_probe):
        with patch("tgmcp.daemon.server.is_daemon_locked", side_effect=fake_locked):
            with patch("tgmcp.cli.main.os.kill"):
                with patch("tgmcp.cli.main.time.sleep") as sleep_mock:
                    res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    assert sleep_mock.called, "stop must poll-wait for lock release"
    # After re-inspect saw STALE → cleanup ran.
    assert not cli_main.PID_PATH.exists()


def test_stop_refuses_to_clean_when_lock_never_released():
    """If our daemon never releases the lock AND a daemon (same pid) still
    holds it after timeout, we must NOT clean — the live daemon would lose
    its socket. Operator must investigate."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    # Probe: initial RUNNING; after timeout, still says RUNNING (or None).
    # We model: post-timeout probe returns None → re-inspect sees lock still
    # held with same pid → UNREACHABLE → refuse to clean.
    probe_seq = iter([{"ok": True, "pid": my_pid}, None, None])

    with patch.object(cli_main, "_probe_existing_daemon", side_effect=lambda: next(probe_seq, None)):
        # Lock never releases, holder remains the same pid.
        with patch("tgmcp.daemon.server.is_daemon_locked", return_value=(True, my_pid)):
            with patch("tgmcp.cli.main.os.kill"):
                t = [0.0]

                def fake_time():
                    return t[0]

                def fake_sleep(_s):
                    t[0] += 11.0

                with patch("tgmcp.cli.main.time.time", side_effect=fake_time):
                    with patch("tgmcp.cli.main.time.sleep", side_effect=fake_sleep):
                        res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    # We do NOT clean live artifacts; the live daemon must keep its socket.
    assert cli_main.PID_PATH.exists() or "refusing" in res.output.lower() \
        or "still holds" in res.output.lower()


def test_stop_aborts_cleanup_when_replacement_daemon_takes_over():
    """If a different daemon (different pid) takes over the lock during our
    wait loop, we must immediately abort cleanup — its artifacts are not ours."""
    my_pid = os.getpid()
    other_pid = my_pid + 1
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": my_pid}
    ):
        # Within the wait loop, lock is now held by a DIFFERENT pid.
        with patch("tgmcp.daemon.server.is_daemon_locked", return_value=(True, other_pid)):
            with patch("tgmcp.cli.main.os.kill"):
                with patch("tgmcp.cli.main.time.sleep"):
                    res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    # Replacement-daemon path → no cleanup, mention different pid.
    assert "different daemon" in res.output.lower() or str(other_pid) in res.output
    assert cli_main.SOCKET_PATH.exists()
