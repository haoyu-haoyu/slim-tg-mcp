"""Keychain backend failure must surface a controlled KeychainUnavailable
exception with actionable instructions, not crash with a backend-specific
traceback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import keyring.errors
import pytest

from tgmcp.daemon import auth


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "SESSIONS_DIR", tmp_path / "sessions")
    yield


def test_keychain_get_raises_controlled_error_on_backend_failure():
    with patch.object(
        auth.keyring,
        "get_password",
        side_effect=keyring.errors.KeyringError("backend down"),
    ):
        with pytest.raises(auth.KeychainUnavailable) as ei:
            auth._keychain_get("any")
        assert "passphrase" in str(ei.value).lower()


def test_keychain_set_raises_controlled_error_on_backend_failure():
    with patch.object(auth.keyring, "get_password", return_value=None):
        with patch.object(
            auth.keyring,
            "set_password",
            side_effect=keyring.errors.KeyringError("locked"),
        ):
            with pytest.raises(auth.KeychainUnavailable):
                auth._keychain_get_or_create("any")


def test_save_session_with_passphrase_works_when_keychain_broken():
    """The whole point of the fallback: if keychain is unusable, --passphrase
    must still work end-to-end."""
    with patch.object(
        auth.keyring,
        "get_password",
        side_effect=keyring.errors.KeyringError("dead"),
    ):
        auth.save_session("acct", "secret-string", passphrase="hunter2")
        assert auth.load_session("acct", passphrase="hunter2") == "secret-string"


def test_delete_account_swallows_keychain_errors():
    auth.save_session("doomed", "x" * 64, passphrase="p")
    with patch.object(
        auth.keyring,
        "delete_password",
        side_effect=keyring.errors.KeyringError("nope"),
    ):
        # Must succeed (return True) even if keychain delete blows up,
        # since the on-disk envelope was the only recoverable artifact.
        assert auth.delete_account("doomed") is True
