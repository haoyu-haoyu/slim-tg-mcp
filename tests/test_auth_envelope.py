"""Round-trip test for the encrypted session envelope (passphrase mode).

Keychain mode is exercised in integration tests since it depends on the OS.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tgmcp.daemon import auth


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path / "sessions")
    yield


def test_passphrase_round_trip():
    label = "test_acct"
    secret = "1ABC" * 64  # fake telethon session string
    passphrase = "correct horse battery staple"

    path = auth.save_session(label, secret, passphrase=passphrase)
    assert path.exists()

    decrypted = auth.load_session(label, passphrase=passphrase)
    assert decrypted == secret


def test_wrong_passphrase_fails():
    label = "test_acct2"
    auth.save_session(label, "secret-data", passphrase="right")
    with pytest.raises(Exception):
        auth.load_session(label, passphrase="wrong")


def test_label_sanitization_rejects_path_traversal():
    with pytest.raises(ValueError):
        auth._session_path("../etc/passwd")
    with pytest.raises(ValueError):
        auth._session_path("")


def test_list_accounts_lists_only_enc():
    auth.save_session("alpha", "x" * 64, passphrase="p")
    auth.save_session("beta", "y" * 64, passphrase="p")
    accounts = auth.list_accounts()
    assert set(accounts) == {"alpha", "beta"}


def test_delete_account_removes_file():
    auth.save_session("gone", "x" * 64, passphrase="p")
    assert auth.delete_account("gone") is True
    assert auth.delete_account("gone") is False
    assert "gone" not in auth.list_accounts()


def test_file_permissions_locked_down():
    label = "perm_test"
    path = auth.save_session(label, "x" * 64, passphrase="p")
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600
