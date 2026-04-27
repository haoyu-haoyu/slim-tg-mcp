"""Encrypted Telegram session storage using OS keychain + AES-GCM.

A session string grants full account access. We store it encrypted at rest:
    1. Generate a random 32-byte data key (DK) per account.
    2. Encrypt the session string with AES-GCM(DK).
    3. Store DK in the OS keychain (macOS Keychain / libsecret / Windows DPAPI)
       via the `keyring` library, scoped to (service="slim-tg-mcp", username=<label>).
    4. Write the ciphertext + nonce to ~/.config/tgmcp/sessions/<label>.enc.

If the keychain is unavailable, fall back to a passphrase-derived key
(scrypt) prompted interactively. Both paths produce the same envelope.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import paths as _paths

KEYRING_SERVICE = "slim-tg-mcp"
CONFIG_DIR = _paths.CONFIG_DIR
SESSIONS_DIR = _paths.SESSIONS_DIR
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Envelope:
    nonce: str
    ct: str
    kdf: str  # "keychain" or "scrypt"
    salt: Optional[str] = None  # only for scrypt

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "Envelope":
        return cls(**json.loads(s))


def _ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    os.chmod(SESSIONS_DIR, 0o700)


def _session_path(label: str) -> Path:
    if not _LABEL_RE.fullmatch(label):
        raise ValueError(f"invalid label {label!r}; must match {_LABEL_RE.pattern}")
    return SESSIONS_DIR / f"{label}.enc"


class KeychainUnavailable(RuntimeError):
    """Raised when the OS keychain backend cannot be used.

    Callers should catch this and either fall back to passphrase mode or
    surface a clear instruction to the user (instead of crashing).
    """


def _keychain_get_or_create(label: str) -> bytes:
    try:
        existing = keyring.get_password(KEYRING_SERVICE, label)
    except keyring.errors.KeyringError as exc:
        raise KeychainUnavailable(
            f"keyring backend failed: {exc!r}. "
            "Use --passphrase to encrypt with a passphrase instead."
        ) from exc
    if existing:
        return base64.b64decode(existing)
    dk = secrets.token_bytes(32)
    try:
        keyring.set_password(KEYRING_SERVICE, label, base64.b64encode(dk).decode())
    except keyring.errors.KeyringError as exc:
        raise KeychainUnavailable(
            f"keyring backend rejected store: {exc!r}. "
            "Use --passphrase to encrypt with a passphrase instead."
        ) from exc
    return dk


def _keychain_get(label: str) -> bytes:
    try:
        existing = keyring.get_password(KEYRING_SERVICE, label)
    except keyring.errors.KeyringError as exc:
        raise KeychainUnavailable(
            f"keyring backend failed: {exc!r}. "
            "Re-run `tgmcp init` and choose --passphrase mode."
        ) from exc
    if not existing:
        raise FileNotFoundError(f"no keychain entry for label={label}")
    return base64.b64decode(existing)


def _scrypt_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def save_session(
    label: str,
    session_string: str,
    *,
    passphrase: Optional[str] = None,
) -> Path:
    """Encrypt and persist the session string. Returns the file path."""
    _ensure_dirs()
    nonce = secrets.token_bytes(12)

    if passphrase is None:
        dk = _keychain_get_or_create(label)
        kdf = "keychain"
        salt: Optional[bytes] = None
    else:
        salt = secrets.token_bytes(16)
        dk = _scrypt_key(passphrase, salt)
        kdf = "scrypt"

    aes = AESGCM(dk)
    ct = aes.encrypt(nonce, session_string.encode("utf-8"), label.encode("utf-8"))

    env = Envelope(
        nonce=base64.b64encode(nonce).decode(),
        ct=base64.b64encode(ct).decode(),
        kdf=kdf,
        salt=base64.b64encode(salt).decode() if salt else None,
    )

    path = _session_path(label)
    path.write_text(env.to_json())
    os.chmod(path, 0o600)
    return path


def load_session(label: str, *, passphrase: Optional[str] = None) -> str:
    path = _session_path(label)
    env = Envelope.from_json(path.read_text())

    if env.kdf == "keychain":
        dk = _keychain_get(label)
    elif env.kdf == "scrypt":
        if passphrase is None:
            raise ValueError(f"label={label} requires a passphrase")
        if env.salt is None:
            raise ValueError("scrypt envelope missing salt")
        dk = _scrypt_key(passphrase, base64.b64decode(env.salt))
    else:
        raise ValueError(f"unknown kdf: {env.kdf}")

    aes = AESGCM(dk)
    pt = aes.decrypt(
        base64.b64decode(env.nonce),
        base64.b64decode(env.ct),
        label.encode("utf-8"),
    )
    return pt.decode("utf-8")


def list_accounts() -> list[str]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(p.stem for p in SESSIONS_DIR.glob("*.enc"))


def delete_account(label: str) -> bool:
    path = _session_path(label)
    if not path.exists():
        return False
    path.unlink()
    try:
        keyring.delete_password(KEYRING_SERVICE, label)
    except (keyring.errors.PasswordDeleteError, keyring.errors.KeyringError):
        pass
    return True
