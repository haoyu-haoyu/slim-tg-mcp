"""Round-12 MAJOR: TOCTOU between /health probe and SIGTERM.

If the daemon exits in the gap between the initial /health probe (which
returns its pid) and our os.kill, the pid can be recycled by the kernel
and we'd kill an unrelated process. The fix: re-check the lock holder
immediately before SIGTERM."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX-only", allow_module_level=True)

from tgmcp.cli import main as cli_main  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_main, "PID_PATH", tmp_path / "pid")
    monkeypatch.setattr(cli_main, "SOCKET_PATH", tmp_path / "sock")
    monkeypatch.setattr(cli_main, "LOCK_PATH", tmp_path / "lock")
    yield


def test_stop_aborts_sigterm_when_lock_holder_changes_before_signal():
    """Lock initially held by our pid → /health probe sees it. By the time
    we go to SIGTERM, a different pid holds the lock (daemon exited and
    fast-restarted). Must NOT signal."""
    my_pid = os.getpid()
    other_pid = my_pid + 1
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": my_pid}
    ):
        # Pre-SIGTERM recheck reports a different holder.
        with patch(
            "tgmcp.daemon.server.is_daemon_locked",
            return_value=(True, other_pid),
        ):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], "must NOT SIGTERM when lock holder has changed"
    assert "lock holder changed" in res.output.lower() or "refusing" in res.output.lower()


def test_stop_skips_sigterm_when_daemon_already_exited():
    """If lock is free at recheck, the daemon already exited between our probe
    and the kill — don't signal a possibly-recycled pid."""
    my_pid = os.getpid()
    cli_main.PID_PATH.write_text(str(my_pid))
    cli_main.SOCKET_PATH.touch()

    with patch.object(
        cli_main, "_probe_existing_daemon", return_value={"ok": True, "pid": my_pid}
    ):
        with patch("tgmcp.daemon.server.is_daemon_locked", return_value=(False, None)):
            with patch("tgmcp.cli.main.os.kill") as kill_mock:
                res = CliRunner().invoke(cli_main.cli, ["daemon", "stop"])

    assert res.exit_code == 0, res.output
    import signal as _sig

    sigterm_calls = [c for c in kill_mock.call_args_list if c.args[1] == _sig.SIGTERM]
    assert sigterm_calls == [], "must NOT signal a possibly-recycled pid"
    assert "already exited" in res.output.lower() or "nothing to signal" in res.output.lower()
