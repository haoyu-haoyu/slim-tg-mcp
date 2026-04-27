"""Smoke test: the DaemonClient can be constructed and gracefully fails when
no daemon is running. Full round-trip is in integration tests (requires a
real Telegram account)."""

from __future__ import annotations

import httpx
import pytest

from tgmcp.client import DaemonClient


def test_client_construct_and_close(tmp_path):
    sock = tmp_path / "missing.sock"
    c = DaemonClient(socket_path=sock)
    c.close()


def test_client_raises_when_socket_missing(tmp_path):
    sock = tmp_path / "missing.sock"
    with DaemonClient(socket_path=sock, timeout=1.0) as c:
        with pytest.raises((httpx.ConnectError, FileNotFoundError, OSError)):
            c.health()
