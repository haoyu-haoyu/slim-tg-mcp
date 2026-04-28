"""FastAPI daemon. Listens on a Unix domain socket so only local users
on the same machine can talk to it.

Endpoints (all POST except where noted):
    GET  /health
    GET  /accounts
    POST /search/global   {query, limit}
    POST /search/in_chat  {chat, query, limit, from_user?, min_date?, max_date?}
    POST /list_dialogs    {limit}
    POST /get_messages    {chat, limit, offset_id?}
    POST /get_context     {chat, msg_id, before, after}
    POST /resolve         {query}
    POST /chat_info       {chat}
    POST /download        {chat, msg_id, out_dir}
    POST /send            {chat, text, reply_to?}
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import re

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import audit, auth
from .telegram import TGConfig, TGSession

# Pid-keyed instance-id store. We can't just use a module-level constant:
# a `importlib.reload` (or fork) would mint a fresh ID on the same process,
# breaking the binding contract. Keying by os.getpid() guarantees:
#   - one ID per actual process (survives reload in same pid)
#   - a forked child gets a fresh ID (different pid)
#
# Note: reload re-executes the module body, which would normally reset this
# dict to {}. We preserve the existing mapping across reloads by reusing
# whatever the current module globals already hold. (For fresh imports the
# fallback {} kicks in.)
_INSTANCE_IDS: dict[int, str] = globals().get("_INSTANCE_IDS", {})


def get_instance_id() -> str:
    pid = os.getpid()
    existing = _INSTANCE_IDS.get(pid)
    if existing is not None:
        return existing
    new_id = secrets.token_hex(16)
    _INSTANCE_IDS[pid] = new_id
    return new_id



from .paths import LOCK_PATH, SOCKET_PATH  # noqa: E402,F401  re-exported


class _State:
    # Multi-account: lazy-loaded sessions keyed by account label. The active
    # label points at whichever session subsequent requests should use.
    sessions: dict[str, TGSession] = {}
    active_label: Optional[str] = None
    uvicorn_server: Optional["uvicorn.Server"] = None
    # Per-label asyncio locks to serialize concurrent loads of the same
    # account. Without this, two `/accounts/switch` calls racing on the same
    # cold label could both `await _open_session(...)`; the later assignment
    # would overwrite the first live TGSession, leaving the first
    # unreachable (and so never stopped by lifespan teardown).
    load_locks: dict[str, "asyncio.Lock"] = {}


state = _State()

# Foreground/in-process passphrase override. Set by `tgmcp daemon start
# --foreground` BEFORE main() so that the secret never enters the process
# environment (where /proc/<pid>/environ would expose it to same-user procs).
_passphrase_override: Optional[str] = None


def set_passphrase_override(p: Optional[str]) -> None:
    global _passphrase_override
    _passphrase_override = p


def _consume_passphrase() -> Optional[str]:
    """Resolve the passphrase from one of three sources, in priority order:
        1. Module-level override (foreground/in-process path).
        2. TGMCP_PASSPHRASE_FD — a pipe inherited from the launcher. The launcher
           writes the secret then closes the write end, so a single read is enough.
        3. None — for keychain-encrypted accounts.

    After reading we wipe every artifact: clear the module var, close the FD,
    and pop the env var. We never let the secret outlive its single use.
    """
    global _passphrase_override
    if _passphrase_override is not None:
        v = _passphrase_override
        _passphrase_override = None
        return v

    fd_str = os.environ.pop("TGMCP_PASSPHRASE_FD", None)
    if fd_str is not None:
        try:
            fd = int(fd_str)
        except ValueError:
            return None
        try:
            with os.fdopen(fd, "rb") as f:
                return f.read().decode("utf-8").rstrip("\n")
        except OSError:
            return None

    # Legacy/back-compat: TGMCP_PASSPHRASE in env. Pop immediately. This path
    # is discouraged because /proc/<pid>/environ on Linux can leak it.
    return os.environ.pop("TGMCP_PASSPHRASE", None)


def _read_app_creds() -> tuple[int, str]:
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            "TG_API_ID / TG_API_HASH must be set. Get them from https://my.telegram.org"
        )
    return int(api_id), api_hash


async def _open_session(label: str, passphrase: Optional[str]) -> TGSession:
    """Decrypt the on-disk session and bring up a connected TGSession.

    Caller passes `passphrase` only for accounts encrypted with --passphrase
    (keychain mode auto-resolves). The local reference is wiped right after
    `auth.load_session` succeeds — we never keep the decrypted secret in
    scope longer than necessary.
    """
    api_id, api_hash = _read_app_creds()
    try:
        session_str = auth.load_session(label, passphrase=passphrase)
    finally:
        passphrase = None
    kind = auth.get_account_kind(label)
    cfg = TGConfig(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_str,
        label=label,
        is_bot=(kind == "bot"),
    )
    sess = TGSession(cfg=cfg)
    await sess.start()
    return sess


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    label = os.environ.get("TGMCP_ACCOUNT", "main")
    passphrase = _consume_passphrase()
    sess = await _open_session(label, passphrase)
    state.sessions[label] = sess
    state.active_label = label
    try:
        yield
    finally:
        # Stop every session we ever loaded, not just the active one.
        for s in list(state.sessions.values()):
            try:
                await s.stop()
            except Exception:
                pass
        state.sessions.clear()
        state.active_label = None


app = FastAPI(title="slim-tg-mcp daemon", lifespan=lifespan)


def _err(status: int, kind: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": kind, "detail": detail})


@app.exception_handler(RequestValidationError)
async def _handle_validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
    """Map FastAPI's default 422 body-validation errors to a uniform 400.

    All client-facing schema errors in this daemon are 400 ("client sent
    bad input"). FastAPI's default 422 is a fine HTTP semantic, but it
    splits the error surface — pydantic field rejections become 422 while
    custom HTTPException(400) calls inside handlers stay 400. Coercing
    here means callers (the CLI, the skill dispatchers, anything else
    using DaemonClient) can branch on a single status code.
    """
    return _err(400, "ValidationError", str(exc.errors()))


@app.exception_handler(ValueError)
async def _handle_value(_req: Request, exc: ValueError) -> JSONResponse:
    return _err(400, "ValueError", str(exc))


@app.exception_handler(auth.KeychainUnavailable)
async def _handle_keychain(_req: Request, exc: auth.KeychainUnavailable) -> JSONResponse:
    return _err(503, "KeychainUnavailable", str(exc))


@app.exception_handler(FileNotFoundError)
async def _handle_missing(_req: Request, exc: FileNotFoundError) -> JSONResponse:
    return _err(404, "NotFound", str(exc))


@app.exception_handler(Exception)
async def _handle_any(_req: Request, exc: Exception) -> JSONResponse:
    # Telethon RPC errors and connection errors land here. We return 502 to
    # signal "upstream Telegram problem", with the exception class name so the
    # MCP/skill layer can branch on the kind.
    #
    # Telethon's auto-generated error classes don't all share a common
    # suffix: most end in "RPCError", but specific named errors like
    # PremiumAccountRequiredError, AuthKeyError, FloodWaitError end in
    # plain "Error". Inspect the module path as the authoritative source —
    # any exception class that originates from `telethon.errors.*` is
    # upstream-Telegram by construction.
    name = type(exc).__name__
    module = getattr(type(exc), "__module__", "") or ""
    if (
        name.endswith("RPCError")
        or "Telethon" in name
        or "Telegram" in name
        or module.startswith("telethon.errors")
        or module.startswith("telethon.tl.")
    ):
        return _err(502, name, str(exc))
    return _err(500, name, str(exc))


def _sess() -> TGSession:
    label = state.active_label
    if not label or label not in state.sessions:
        raise HTTPException(503, "session not ready")
    return state.sessions[label]


# ---------- request schemas ----------


class SearchGlobalReq(BaseModel):
    query: str
    limit: int = Field(30, ge=1, le=200)


class SearchInChatReq(BaseModel):
    chat: str | int
    query: Optional[str] = None
    limit: int = Field(50, ge=1, le=200)
    from_user: Optional[str | int] = None
    min_date: Optional[datetime] = None
    max_date: Optional[datetime] = None


class ListDialogsReq(BaseModel):
    limit: int = Field(50, ge=1, le=500)


class GetMessagesReq(BaseModel):
    chat: str | int
    limit: int = Field(50, ge=1, le=200)
    offset_id: int = 0


class GetContextReq(BaseModel):
    chat: str | int
    msg_id: int
    before: int = Field(5, ge=0, le=50)
    after: int = Field(5, ge=0, le=50)


class ResolveReq(BaseModel):
    query: str | int


class ChatInfoReq(BaseModel):
    chat: str | int


class DownloadReq(BaseModel):
    chat: str | int
    msg_id: int


class SendReq(BaseModel):
    chat: str | int
    text: str
    reply_to: Optional[int] = None


class EditReq(BaseModel):
    chat: str | int
    msg_id: int
    text: str


class DeleteReq(BaseModel):
    chat: str | int
    msg_ids: list[int]
    revoke: bool = True


class ForwardReq(BaseModel):
    from_chat: str | int
    to_chat: str | int
    msg_ids: list[int]


class PinReq(BaseModel):
    chat: str | int
    msg_id: int
    notify: bool = True


class UnpinReq(BaseModel):
    chat: str | int
    msg_id: Optional[int] = None


class ReactReq(BaseModel):
    chat: str | int
    msg_id: int
    emoji: Optional[str] = None  # None clears the reaction
    custom_emoji_id: Optional[int] = None  # Premium custom-emoji document id
    big: bool = False  # Premium "big" reaction burst

    @model_validator(mode="after")
    def _react_xor(self) -> "ReactReq":
        if self.emoji is not None and self.custom_emoji_id is not None:
            raise ValueError(
                "react takes emoji OR custom_emoji_id, not both"
            )
        return self


class SetEmojiStatusReq(BaseModel):
    document_id: Optional[int] = None  # None clears the status
    until: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_until(self) -> "SetEmojiStatusReq":
        if self.until is not None:
            if self.document_id is None:
                raise ValueError(
                    "until is only meaningful with a non-null document_id"
                )
            if self.until.tzinfo is None:
                raise ValueError("until must be timezone-aware")
            if self.until <= datetime.now(timezone.utc):
                raise ValueError("until must be in the future")
        return self


class MarkReadReq(BaseModel):
    chat: str | int


class ShutdownReq(BaseModel):
    instance_id: str


class SwitchAccountReq(BaseModel):
    label: str
    passphrase: Optional[str] = None  # only required for --passphrase accounts


# ----- Group/Channel admin -----


class CreateGroupReq(BaseModel):
    title: str
    users: list[str | int] = []
    megagroup: bool = False
    broadcast: bool = False
    about: str = ""


class ChatMemberReq(BaseModel):
    chat: str | int
    user: str | int


class InviteLinkReq(BaseModel):
    chat: str | int
    expire_seconds: Optional[int] = None
    usage_limit: Optional[int] = None


class SetTitleReq(BaseModel):
    chat: str | int
    title: str


class LeaveReq(BaseModel):
    chat: str | int


# ----- Contacts -----


_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class AddContactReq(BaseModel):
    phone: str
    first_name: str
    last_name: str = ""

    @field_validator("phone")
    @classmethod
    def _validate_e164(cls, v: str) -> str:
        """Enforce E.164 at the HTTP boundary so direct daemon/client
        callers can't bypass the skill's input check. Telegram itself
        requires + and country code for ImportContactsRequest."""
        if not _E164_RE.fullmatch(v):
            raise ValueError(
                f"phone must be E.164 format (start with + and country code): {v!r}"
            )
        return v


class ContactUserReq(BaseModel):
    user: str | int


class SearchContactsReq(BaseModel):
    query: str
    limit: int = Field(20, ge=1, le=100)


# ----- Media upload -----


# Hard cap on uploadable file size. Telegram's per-file limit (premium
# excluded) is 2 GB. We refuse anything larger up front to avoid
# silently truncating, and to keep a runaway agent from filling disk
# while reading a giant file into memory.
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GiB


# Telegram caption hard limit (per the API). Reject earlier here so the
# error surface is a clean 400 at the daemon boundary, not a late upstream
# Telethon failure mid-upload.
MAX_CAPTION_CHARS = 1024

# Voice notes must be Opus-encoded audio. We can't sniff that without
# decoding, but we can refuse anything whose extension isn't compatible
# up front so callers get a clear error before we burn an upload.
VOICE_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a"}


# Telegram rejects usernames that end in `_` even though intermediate
# underscores are fine. Anchor the last char to alphanumeric only:
#   1 letter prefix + 3..30 mid chars (alnum or `_`) + 1 alnum suffix
# = 5..32 chars total.
_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$")


class SearchGifsReq(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(20, ge=1, le=50)


class StickerSetReq(BaseModel):
    set_id: int
    access_hash: int


class SendDocByRefReq(BaseModel):
    """Used by both /gif/send and /sticker/send. The caller passes the
    triple from a prior search/listing — the daemon never accepts a
    raw file path here (that path goes through tg-media-upload)."""

    chat: str | int
    doc_id: int
    access_hash: int
    file_reference_hex: str = Field(..., min_length=1, max_length=512)

    @field_validator("file_reference_hex")
    @classmethod
    def _valid_hex(cls, v: str) -> str:
        try:
            bytes.fromhex(v)
        except ValueError as e:
            raise ValueError(f"file_reference_hex must be hex: {e}") from e
        return v


class GetParticipantsReq(BaseModel):
    chat: str | int
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    search: str = Field("", max_length=200)
    filter_kind: str = "all"

    @field_validator("filter_kind")
    @classmethod
    def _valid_filter(cls, v: str) -> str:
        valid = {"all", "admins", "kicked", "banned", "bots", "search"}
        if v not in valid:
            raise ValueError(f"filter_kind must be one of {sorted(valid)}; got {v!r}")
        return v


class SignaturesReq(BaseModel):
    chat: str | int
    enabled: bool


class SlowModeReq(BaseModel):
    chat: str | int
    # 0 disables; non-zero must be one of Telegram's allowed slot values.
    seconds: int = Field(...)

    @field_validator("seconds")
    @classmethod
    def _valid_seconds(cls, v: int) -> int:
        allowed = {0, 10, 30, 60, 300, 900, 3600}
        if v not in allowed:
            raise ValueError(f"seconds must be one of {sorted(allowed)}; got {v}")
        return v


class DiscussionReq(BaseModel):
    broadcast: str | int
    group: Optional[str | int] = None  # None = unbind


class AdminLogReq(BaseModel):
    chat: str | int
    limit: int = Field(50, ge=1, le=500)
    search: str = Field("", max_length=200)


_PRIVACY_KEYS = {
    "status",
    "photo",
    "calls",
    "forwards",
    "chat_invite",
    "phone",
    "added_by_phone",
    "voice",
    "about",
    "p2p",
}
_PRIVACY_RULE_KINDS = {
    "allow_all",
    "disallow_all",
    "allow_contacts",
    "disallow_contacts",
    "allow_users",
    "disallow_users",
}


class PrivacyRule(BaseModel):
    kind: str
    user_ids: list[int] = []

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in _PRIVACY_RULE_KINDS:
            raise ValueError(
                f"unknown rule kind {v!r}; valid: {sorted(_PRIVACY_RULE_KINDS)}"
            )
        return v

    @model_validator(mode="after")
    def _user_ids_match_kind(self) -> "PrivacyRule":
        """`allow_users` / `disallow_users` need a non-empty list. Every
        other kind must NOT carry user_ids — silently dropping them
        would surprise callers."""
        needs_users = self.kind in ("allow_users", "disallow_users")
        if needs_users and not self.user_ids:
            raise ValueError(
                f"rule kind {self.kind!r} requires a non-empty user_ids list"
            )
        if not needs_users and self.user_ids:
            raise ValueError(
                f"rule kind {self.kind!r} does not accept user_ids; got {self.user_ids!r}"
            )
        return self


class GetPrivacyReq(BaseModel):
    key: str

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if v not in _PRIVACY_KEYS:
            raise ValueError(
                f"unknown privacy key {v!r}; valid: {sorted(_PRIVACY_KEYS)}"
            )
        return v


class SetPrivacyReq(BaseModel):
    key: str
    rules: list[PrivacyRule] = Field(..., min_length=1, max_length=10)

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if v not in _PRIVACY_KEYS:
            raise ValueError(
                f"unknown privacy key {v!r}; valid: {sorted(_PRIVACY_KEYS)}"
            )
        return v


class FolderPeerSpec(BaseModel):
    """Caller-friendly folder spec — peer references are username/id strings.

    `title` cap is 12 (UTF-8 chars) per Telegram's dialogFilter.title
    limit. Anything wider would have been MESSAGE_TOO_LONG-ed upstream.
    """

    folder_id: int = Field(..., ge=2, le=255)  # Telegram reserves 0/1 for special
    title: str = Field(..., min_length=1, max_length=12)
    include_peers: list[str | int] = []
    exclude_peers: list[str | int] = []
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False

    @model_validator(mode="after")
    def _at_least_one_inclusion(self) -> "FolderPeerSpec":
        """messages.updateDialogFilter rejects FILTER_INCLUDE_EMPTY — an
        empty folder definition with no peers and no kind-of-chats flag
        is meaningless. Bounce here so the caller gets a deterministic
        400, not an upstream Telegram error."""
        if not (
            self.include_peers
            or self.contacts
            or self.non_contacts
            or self.groups
            or self.broadcasts
            or self.bots
        ):
            raise ValueError(
                "folder needs at least one inclusion: pass include_peers, "
                "or set one of contacts/non_contacts/groups/broadcasts/bots"
            )
        return self


class FolderIdReq(BaseModel):
    folder_id: int = Field(..., ge=2, le=255)


class ExportChatReq(BaseModel):
    chat: str | int
    out_dir: str
    limit: int = Field(1000, ge=1, le=100000)
    include_media: bool = False
    since_date: Optional[datetime] = None
    until_date: Optional[datetime] = None

    @field_validator("since_date", "until_date")
    @classmethod
    def _normalize_to_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Reject naive datetimes — comparing them to Telethon's tz-aware
        Message.date raises TypeError, and host-tz interpretation is
        unpredictable. Normalize to UTC so the comparison in
        export_chat is deterministic."""
        from datetime import timezone as _tz

        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError(
                "since_date / until_date must be timezone-aware "
                "(e.g. ...+00:00 or ...Z)"
            )
        return v.astimezone(_tz.utc)


class UpdateProfileReq(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=64)
    last_name: Optional[str] = Field(None, max_length=64)
    # Telegram caps: 70 chars (premium goes to 140 but we conservatively
    # bound to 70 so non-premium users can't trigger a late upstream error).
    about: Optional[str] = Field(None, max_length=140)


class UpdateUsernameReq(BaseModel):
    """Empty string clears the public username."""
    username: str = Field(..., max_length=32)

    @field_validator("username")
    @classmethod
    def _validate(cls, v: str) -> str:
        if v == "":
            return v  # clear-username request
        if not _USERNAME_RE.fullmatch(v):
            raise ValueError(
                "username must be 5–32 chars, start with a letter, and "
                "contain only [a-zA-Z0-9_]"
            )
        return v


class SetPhotoReq(BaseModel):
    file_path: str


class SetStatusReq(BaseModel):
    online: bool


class SendScheduledReq(BaseModel):
    chat: str | int
    text: str = Field(..., min_length=1, max_length=4096)
    schedule_date: datetime
    reply_to: Optional[int] = None

    @field_validator("schedule_date")
    @classmethod
    def _must_be_future_and_within_year(cls, v: datetime) -> datetime:
        from datetime import timezone as _tz

        if v.tzinfo is None:
            raise ValueError("schedule_date must be timezone-aware (UTC recommended)")
        now = datetime.now(_tz.utc)
        delta = (v - now).total_seconds()
        if delta < 10:
            raise ValueError(
                f"schedule_date must be at least 10 seconds in the future "
                f"(got {delta:.1f}s)"
            )
        if delta > 365 * 24 * 3600:
            raise ValueError("schedule_date must be within 365 days from now")
        return v


class ListScheduledReq(BaseModel):
    chat: str | int
    limit: int = Field(100, ge=1, le=500)


class DeleteScheduledReq(BaseModel):
    chat: str | int
    msg_ids: list[int] = Field(..., min_length=1, max_length=100)


class SaveDraftReq(BaseModel):
    chat: str | int
    # Empty drafts must go through /draft/clear so the API has one obvious
    # path for each intent. Otherwise empty-save's behavior depends on
    # Telegram's interpretation and can leave a blank draft visible in the
    # client.
    text: str = Field(..., min_length=1, max_length=4096)
    reply_to: Optional[int] = None


class GetDraftReq(BaseModel):
    chat: str | int


class CreatePollReq(BaseModel):
    chat: str | int
    question: str = Field(..., min_length=1, max_length=300)
    # Telegram limits: 2..10 options, each up to 100 chars.
    options: list[str] = Field(..., min_length=2, max_length=10)
    anonymous: bool = True
    multiple_choice: bool = False
    quiz: bool = False
    correct_option: Optional[int] = None
    explanation: str = Field("", max_length=200)

    @field_validator("options")
    @classmethod
    def _validate_options(cls, v: list[str]) -> list[str]:
        for i, opt in enumerate(v):
            if not opt.strip():
                raise ValueError(f"option[{i}] is empty")
            if len(opt) > 100:
                raise ValueError(f"option[{i}] exceeds 100 chars (got {len(opt)})")
        return v


class PollMsgReq(BaseModel):
    chat: str | int
    msg_id: int


class EditPollReq(BaseModel):
    chat: str | int
    msg_id: int
    question: Optional[str] = Field(None, min_length=1, max_length=300)
    options: Optional[list[str]] = Field(None, min_length=2, max_length=10)

    @field_validator("options")
    @classmethod
    def _options_each_in_bounds(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        for i, opt in enumerate(v):
            if not opt.strip():
                raise ValueError(f"option[{i}] is empty")
            if len(opt) > 100:
                raise ValueError(f"option[{i}] exceeds 100 chars")
        return v

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "EditPollReq":
        if self.question is None and self.options is None:
            raise ValueError("edit_poll needs question and/or options")
        return self


class EditScheduledReq(BaseModel):
    chat: str | int
    msg_id: int
    text: Optional[str] = Field(None, min_length=1, max_length=4096)
    schedule_date: Optional[datetime] = None

    @field_validator("schedule_date")
    @classmethod
    def _tz_aware_window(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Same validation as SendScheduledReq.schedule_date — tz-aware,
        ≥10s, ≤365 days."""
        if v is None:
            return v
        from datetime import timezone as _tz

        if v.tzinfo is None:
            raise ValueError("schedule_date must be timezone-aware")
        delta = (v - datetime.now(_tz.utc)).total_seconds()
        if delta < 10:
            raise ValueError(
                f"schedule_date must be ≥10s in the future (got {delta:.1f}s)"
            )
        if delta > 365 * 24 * 3600:
            raise ValueError("schedule_date must be within 365 days from now")
        return v.astimezone(_tz.utc)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "EditScheduledReq":
        if self.text is None and self.schedule_date is None:
            raise ValueError("edit_scheduled needs text and/or schedule_date")
        return self


class Change2faReq(BaseModel):
    """At least one of current_password / new_password must be set:
      - both → CHANGE password
      - only new → ENABLE 2FA on an account that had none
      - only current → REMOVE 2FA
    """

    current_password: Optional[str] = Field(None, min_length=1, max_length=256)
    new_password: Optional[str] = Field(None, min_length=8, max_length=256)
    hint: str = Field("", max_length=64)
    email: Optional[str] = Field(None, max_length=256)

    @model_validator(mode="after")
    def _at_least_one_password(self) -> "Change2faReq":
        if not self.current_password and not self.new_password:
            raise ValueError(
                "change_2fa needs at least one of current_password / new_password"
            )
        return self


class SendMediaReq(BaseModel):
    chat: str | int
    file_path: str
    caption: str = Field("", max_length=MAX_CAPTION_CHARS)
    reply_to: Optional[int] = None
    as_voice: bool = False
    force_document: bool = False


# ---------- routes ----------


@app.get("/health")
async def health() -> dict[str, Any]:
    label = state.active_label
    s = state.sessions.get(label) if label else None
    return {
        "ok": s is not None,
        # The daemon publishes its own pid so a launching parent can verify it
        # is talking to the child it just spawned, not an unrelated daemon
        # already serving the socket.
        "pid": os.getpid(),
        # Pid-keyed per-process identity. Required to /shutdown the right
        # instance; protects against a stale stop request hitting a
        # successor daemon that took over the socket.
        "instance_id": get_instance_id(),
        "account": s.cfg.label if s else None,
        "me_id": s.me_id if s else None,
        "is_bot": bool(s.cfg.is_bot) if s else None,
        "active_label": label,
        "loaded_labels": sorted(state.sessions.keys()),
    }


@app.get("/accounts")
async def accounts() -> dict[str, Any]:
    """List on-disk accounts with their kind ('user'/'bot').

    Reading the kind requires only the envelope JSON, not decryption — so
    we surface it freely. A label whose envelope file has been corrupted
    (or replaced with a non-JSON blob) shows kind="unknown" rather than
    raising; the corresponding load_session call would still surface the
    real error.
    """
    labels = auth.list_accounts()
    items = []
    for lbl in labels:
        try:
            kind = auth.get_account_kind(lbl)
        except Exception:
            kind = "unknown"
        items.append({"label": lbl, "kind": kind})
    return {
        "accounts": labels,  # back-compat: bare list of labels
        "items": items,
        # Same field names as /health and /accounts/switch — one shape across
        # the multi-account API surface so clients can read/cache uniformly.
        "active_label": state.active_label,
        "loaded_labels": sorted(state.sessions.keys()),
    }


@app.post("/accounts/switch")
async def switch_account(req: SwitchAccountReq) -> dict[str, Any]:
    """Make `req.label` the active session for subsequent requests.

    Loads the session lazily on first switch and caches it in state.sessions
    so repeat switches between accounts don't pay the auth/connect cost
    again. The decrypted passphrase is wiped from the local frame as soon
    as `auth.load_session` returns; it's never logged.
    """
    if req.label not in auth.list_accounts():
        raise HTTPException(404, f"unknown account label: {req.label!r}")

    if req.label not in state.sessions:
        # Serialize concurrent loads of the same label so we never start two
        # TGSession objects pointing at the same account. The first awaiter
        # opens; the second awaiter sees the cached session after the lock
        # releases (re-check inside the critical section).
        lock = state.load_locks.setdefault(req.label, asyncio.Lock())
        async with lock:
            if req.label not in state.sessions:
                try:
                    sess = await _open_session(req.label, req.passphrase)
                except Exception:
                    # Failed load: don't cache anything; let a subsequent
                    # caller retry with possibly-different passphrase. Wipe
                    # the request body's secret on the way out.
                    req.passphrase = None
                    raise
                state.sessions[req.label] = sess
        # Don't keep the request body around — pydantic holds the passphrase
        # as a model field which would otherwise outlive the call.
        req.passphrase = None

    state.active_label = req.label
    audit.log("account_switch", label=req.label)  # NOTE: passphrase is never logged
    s = state.sessions[req.label]
    return {
        "ok": True,
        # Standardized field names across /health, /accounts, /accounts/switch.
        "active_label": req.label,
        "loaded_labels": sorted(state.sessions.keys()),
        "me_id": s.me_id,
    }


@app.post("/search/global")
async def search_global(req: SearchGlobalReq) -> dict[str, Any]:
    msgs = await _sess().search_global(req.query, limit=req.limit)
    return {"messages": [m.__dict__ for m in msgs]}


@app.post("/search/in_chat")
async def search_in_chat(req: SearchInChatReq) -> dict[str, Any]:
    msgs = await _sess().search_in_chat(
        req.chat,
        req.query or "",
        limit=req.limit,
        from_user=req.from_user,
        min_date=req.min_date,
        max_date=req.max_date,
    )
    return {"messages": [m.__dict__ for m in msgs]}


@app.post("/list_dialogs")
async def list_dialogs(req: ListDialogsReq) -> dict[str, Any]:
    dialogs = await _sess().list_dialogs(limit=req.limit)
    return {"dialogs": [d.__dict__ for d in dialogs]}


@app.post("/get_messages")
async def get_messages(req: GetMessagesReq) -> dict[str, Any]:
    msgs = await _sess().get_messages(req.chat, limit=req.limit, offset_id=req.offset_id)
    return {"messages": [m.__dict__ for m in msgs]}


@app.post("/get_context")
async def get_context(req: GetContextReq) -> dict[str, Any]:
    msgs = await _sess().get_message_context(
        req.chat, req.msg_id, before=req.before, after=req.after
    )
    return {"messages": [m.__dict__ for m in msgs]}


@app.post("/resolve")
async def resolve(req: ResolveReq) -> dict[str, Any]:
    return await _sess().resolve_entity(req.query)


@app.post("/chat_info")
async def chat_info(req: ChatInfoReq) -> dict[str, Any]:
    return await _sess().get_chat_info(req.chat)


@app.post("/download")
async def download(req: DownloadReq) -> dict[str, Any]:
    path = await _sess().download_media(req.chat, req.msg_id)
    return {"path": path}


@app.post("/shutdown")
async def shutdown_endpoint(req: ShutdownReq) -> dict[str, Any]:
    """Graceful self-shutdown bound to the caller-named instance.

    The CLI inspects daemon A via /health, learns A's instance_id, and
    sends that instance_id with /shutdown. If a successor daemon B has
    replaced A by the time the RPC arrives (different INSTANCE_ID), B
    refuses with 409 — preventing a stale stop request from collateral-
    killing the wrong daemon.

    Authn: the daemon listens on a 0700 Unix socket inside an owned 0700
    runtime dir, so reaching this endpoint already requires same-user
    access. The instance_id is anti-mistake (TOCTOU), not anti-forgery.
    """
    current_id = get_instance_id()
    if req.instance_id != current_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"instance_id mismatch: caller asked for "
                f"{req.instance_id!r}, this daemon is {current_id!r}"
            ),
        )
    if state.uvicorn_server is not None:
        state.uvicorn_server.should_exit = True
    return {"ok": True, "pid": os.getpid(), "instance_id": current_id}


@app.post("/send")
async def send(req: SendReq) -> dict[str, Any]:
    msg_id = await _sess().send_message(req.chat, req.text, reply_to=req.reply_to)
    audit.log(
        "send",
        chat=str(req.chat),
        reply_to=req.reply_to,
        msg_id=msg_id,
        text_len=len(req.text),
    )
    return {"msg_id": msg_id}


@app.post("/edit")
async def edit(req: EditReq) -> dict[str, Any]:
    msg_id = await _sess().edit_message(req.chat, req.msg_id, req.text)
    audit.log("edit", chat=str(req.chat), msg_id=msg_id, text_len=len(req.text))
    return {"msg_id": msg_id}


@app.post("/delete")
async def delete(req: DeleteReq) -> dict[str, Any]:
    requested = await _sess().delete_messages(req.chat, req.msg_ids, revoke=req.revoke)
    audit.log(
        "delete",
        chat=str(req.chat),
        msg_ids=req.msg_ids,
        revoke=req.revoke,
        requested=requested,
    )
    # `requested` is the number of ids we asked Telegram to delete.
    # Telethon raises on RPC failure, so absent an exception the request was
    # accepted — but global-revoke is best-effort and a successful response
    # does NOT guarantee every recipient saw the deletion.
    return {"ok": True, "requested": requested}


@app.post("/forward")
async def forward(req: ForwardReq) -> dict[str, Any]:
    new_ids = await _sess().forward_messages(req.from_chat, req.to_chat, req.msg_ids)
    audit.log(
        "forward",
        from_chat=str(req.from_chat),
        to_chat=str(req.to_chat),
        src_msg_ids=req.msg_ids,
        new_msg_ids=new_ids,
    )
    return {"msg_ids": new_ids}


@app.post("/pin")
async def pin(req: PinReq) -> dict[str, Any]:
    await _sess().pin_message(req.chat, req.msg_id, notify=req.notify)
    audit.log("pin", chat=str(req.chat), msg_id=req.msg_id, notify=req.notify)
    return {"ok": True}


@app.post("/unpin")
async def unpin(req: UnpinReq) -> dict[str, Any]:
    await _sess().unpin_message(req.chat, req.msg_id)
    audit.log("unpin", chat=str(req.chat), msg_id=req.msg_id)
    return {"ok": True}


@app.post("/react")
async def react(req: ReactReq) -> dict[str, Any]:
    await _sess().react(
        req.chat,
        req.msg_id,
        req.emoji,
        custom_emoji_id=req.custom_emoji_id,
        big=req.big,
    )
    audit.log(
        "react",
        chat=str(req.chat),
        msg_id=req.msg_id,
        kind=(
            "custom" if req.custom_emoji_id is not None
            else ("emoji" if req.emoji else "clear")
        ),
        big=req.big,
    )
    return {"ok": True}


@app.post("/mark_read")
async def mark_read(req: MarkReadReq) -> dict[str, Any]:
    await _sess().mark_as_read(req.chat)
    audit.log("mark_read", chat=str(req.chat))
    return {"ok": True}


# ---------- Group/Channel admin ----------


@app.post("/chat/create")
async def chat_create(req: CreateGroupReq) -> dict[str, Any]:
    info = await _sess().create_group(
        req.title,
        req.users,
        megagroup=req.megagroup,
        broadcast=req.broadcast,
        about=req.about,
    )
    audit.log(
        "chat_create",
        title=req.title,
        kind=info.get("kind"),
        chat_id=info.get("id"),
        member_count=len(req.users),
    )
    return info


@app.post("/chat/add_member")
async def chat_add_member(req: ChatMemberReq) -> dict[str, Any]:
    await _sess().add_chat_member(req.chat, req.user)
    audit.log("chat_add_member", chat=str(req.chat), user=str(req.user))
    return {"ok": True}


@app.post("/chat/kick_member")
async def chat_kick_member(req: ChatMemberReq) -> dict[str, Any]:
    await _sess().kick_chat_member(req.chat, req.user)
    audit.log("chat_kick_member", chat=str(req.chat), user=str(req.user))
    return {"ok": True}


@app.post("/chat/ban_member")
async def chat_ban_member(req: ChatMemberReq) -> dict[str, Any]:
    await _sess().ban_chat_member(req.chat, req.user)
    audit.log("chat_ban_member", chat=str(req.chat), user=str(req.user))
    return {"ok": True}


@app.post("/chat/unban_member")
async def chat_unban_member(req: ChatMemberReq) -> dict[str, Any]:
    await _sess().unban_chat_member(req.chat, req.user)
    audit.log("chat_unban_member", chat=str(req.chat), user=str(req.user))
    return {"ok": True}


@app.post("/chat/invite_link")
async def chat_invite_link(req: InviteLinkReq) -> dict[str, Any]:
    link = await _sess().create_invite_link(
        req.chat,
        expire_seconds=req.expire_seconds,
        usage_limit=req.usage_limit,
    )
    audit.log(
        "chat_invite_link",
        chat=str(req.chat),
        expire_seconds=req.expire_seconds,
        usage_limit=req.usage_limit,
    )
    return {"link": link}


@app.post("/chat/set_title")
async def chat_set_title(req: SetTitleReq) -> dict[str, Any]:
    await _sess().set_chat_title(req.chat, req.title)
    audit.log("chat_set_title", chat=str(req.chat), title=req.title)
    return {"ok": True}


@app.post("/chat/leave")
async def chat_leave(req: LeaveReq) -> dict[str, Any]:
    await _sess().leave_chat(req.chat)
    audit.log("chat_leave", chat=str(req.chat))
    return {"ok": True}


# ---------- Contacts ----------


@app.post("/contacts/add")
async def contacts_add(req: AddContactReq) -> dict[str, Any]:
    info = await _sess().add_contact(req.phone, req.first_name, req.last_name)
    audit.log(
        "contact_add",
        phone_suffix=req.phone[-4:] if len(req.phone) >= 4 else "",
        imported=info.get("imported"),
        user_id=info.get("id"),
    )
    return info


@app.post("/contacts/delete")
async def contacts_delete(req: ContactUserReq) -> dict[str, Any]:
    await _sess().delete_contact(req.user)
    audit.log("contact_delete", user=str(req.user))
    return {"ok": True}


@app.post("/contacts/block")
async def contacts_block(req: ContactUserReq) -> dict[str, Any]:
    await _sess().block_user(req.user)
    audit.log("contact_block", user=str(req.user))
    return {"ok": True}


@app.post("/contacts/unblock")
async def contacts_unblock(req: ContactUserReq) -> dict[str, Any]:
    await _sess().unblock_user(req.user)
    audit.log("contact_unblock", user=str(req.user))
    return {"ok": True}


@app.post("/contacts/search")
async def contacts_search(req: SearchContactsReq) -> dict[str, Any]:
    users = await _sess().search_contacts(req.query, limit=req.limit)
    return {"users": users}


# ---------- Media upload ----------


def _walk_parents_for_symlink(abs_path: str) -> Optional[str]:
    """Return the first symlink found in any parent component, or None.

    `os.lstat(abs_path)` only checks the leaf. An attacker who can plant
    a symlink in any ancestor directory (`/foo/link/secret`) would bypass
    that check, since the leaf `secret` is a regular file. Walking the
    chain catches the case.
    """
    cur = os.path.dirname(abs_path)
    while cur and cur != os.path.dirname(cur):
        if os.path.islink(cur):
            return cur
        cur = os.path.dirname(cur)
    return None


def _check_upload_path(path_str: str) -> tuple[str, os.stat_result]:
    """Internal: full validation. Returns (abs_path, lstat_info).

    The lstat snapshot is captured here AND used downstream by
    `_open_validated_upload` to verify the file we open is the same
    inode/dev as what we just validated. This is what closes the
    "regular-file-replaced-with-different-regular-file" race that
    O_NOFOLLOW alone cannot detect (O_NOFOLLOW only blocks symlink
    swaps).
    """
    import stat as _stat

    from . import paths as _paths

    if not path_str:
        raise HTTPException(400, "file_path is empty")

    abs_path = os.path.abspath(path_str)

    bad_parent = _walk_parents_for_symlink(abs_path)
    if bad_parent is not None:
        raise HTTPException(
            400,
            f"refusing: symlink in parent component at {bad_parent!r} could "
            "redirect the upload — pass a path with no symlinks in it",
        )

    try:
        info = os.lstat(abs_path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"file not found: {abs_path}") from e
    except OSError as e:
        raise HTTPException(400, f"cannot stat {abs_path}: {e}") from e

    if _stat.S_ISLNK(info.st_mode):
        raise HTTPException(
            400,
            f"refusing to upload via symlink at {abs_path} — pass the real "
            "path; symlinks could redirect the read to a sensitive file",
        )
    if not _stat.S_ISREG(info.st_mode):
        raise HTTPException(400, f"{abs_path} is not a regular file")
    if info.st_size > MAX_UPLOAD_SIZE:
        raise HTTPException(
            413,
            f"{abs_path} is {info.st_size} bytes; max is {MAX_UPLOAD_SIZE}",
        )

    real_path = os.path.realpath(abs_path)
    real_runtime = os.path.realpath(_paths.RUNTIME_DIR)
    if os.path.commonpath([real_path, real_runtime]) == real_runtime:
        raise HTTPException(
            400,
            f"refusing to upload from the daemon's runtime directory "
            f"(resolves to {real_runtime})",
        )

    return abs_path, info


def _validate_upload_path(path_str: str) -> str:
    """Public wrapper that returns just the validated absolute path.

    Internally this is a snapshot check — the actual upload pipeline
    goes through `_open_validated_upload` which keeps the lstat metadata
    around to detect both symlink AND regular-file replacements between
    validate and open.
    """
    abs_path, _ = _check_upload_path(path_str)
    return abs_path


def _open_validated_upload(path_str: str) -> tuple[str, int, int]:
    """Validate + open atomically, with full TOCTOU defense.

    Threat model: between `_check_upload_path` returning and `os.open`
    completing, an attacker with write access to the file's parent dir
    could:
      (a) replace the file with a symlink → blocked by O_NOFOLLOW
      (b) replace the file with a different regular file (e.g. via
          `mv attacker.bin victim`)  → blocked by the dev+ino check
          below: we compare `lstat` (pre-open) and `fstat` (post-open)
          and refuse if they don't match.
      (c) replace it with a fifo/device/etc → blocked by the post-open
          S_ISREG check.

    Returns (abs_path, size, fd). Caller closes the fd when done.
    """
    import stat as _stat

    abs_path, lstat_info = _check_upload_path(path_str)

    # Build open flags. We want:
    #   O_RDONLY    — read access only
    #   O_CLOEXEC   — don't leak the fd into child processes
    #   O_NOFOLLOW  — late symlink swap → ELOOP, not silent follow
    #   O_NONBLOCK  — late FIFO/device swap would otherwise BLOCK indefinitely
    #                 inside os.open (the FIFO read-side waits for a writer);
    #                 a remote prompt-injected agent could DoS the daemon by
    #                 racing in a FIFO. Open non-blocking, then fstat, then
    #                 reject non-regular fds before doing any I/O. We clear
    #                 O_NONBLOCK with fcntl after the type check so Telethon
    #                 sees a normal blocking read.
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(abs_path, flags)
    except OSError as e:
        if e.errno in (40, 62):  # ELOOP varies (Linux=40, macOS=62)
            raise HTTPException(
                400, f"refusing: {abs_path} became a symlink after validation"
            ) from e
        raise HTTPException(400, f"cannot open {abs_path}: {e}") from e

    try:
        fstat_info = os.fstat(fd)

        # Same file? Compare (dev, ino) — uniquely identifies the inode.
        if (fstat_info.st_dev, fstat_info.st_ino) != (
            lstat_info.st_dev,
            lstat_info.st_ino,
        ):
            raise HTTPException(
                400,
                f"refusing: {abs_path} was replaced between validation and "
                f"open (lstat ino={lstat_info.st_ino}, fstat ino={fstat_info.st_ino}). "
                "Aborting upload.",
            )

        # Reject anything that isn't a regular file: FIFOs, character/block
        # devices, sockets. The open succeeded only because O_NONBLOCK
        # short-circuited the FIFO read-side wait; if we proceeded we'd
        # either burn CPU on a slow read or block on a real char device.
        if not _stat.S_ISREG(fstat_info.st_mode):
            raise HTTPException(
                400,
                f"refusing: {abs_path} resolved to a non-regular file "
                f"(mode={oct(fstat_info.st_mode)}); FIFOs / devices / sockets "
                "are blocked to prevent DoS via late TOCTOU swap",
            )

        if fstat_info.st_size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                413,
                f"{abs_path} is {fstat_info.st_size} bytes after fstat; max is {MAX_UPLOAD_SIZE}",
            )

        # Clear O_NONBLOCK so Telethon's read sees normal blocking I/O.
        # We've now confirmed the fd points at our validated regular file,
        # so blocking semantics are safe.
        if hasattr(os, "O_NONBLOCK"):
            import fcntl as _fcntl

            current = _fcntl.fcntl(fd, _fcntl.F_GETFL)
            _fcntl.fcntl(fd, _fcntl.F_SETFL, current & ~os.O_NONBLOCK)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    return abs_path, fstat_info.st_size, fd


def _audit_path_redacted(abs_path: str) -> dict[str, Any]:
    """Return a redacted view of a path suitable for the audit log.

    Logging full absolute paths would persist sensitive directory and
    filename data into ~/.config/tgmcp/audit.log (and any backups). We
    record:
      - basename (so the user can identify what was sent)
      - parent_hash (8 hex chars of sha256 of the parent dir, lets the
        operator correlate uploads from the same source without leaking
        the actual directory tree)
    """
    import hashlib
    import os

    parent_hash = hashlib.sha256(
        os.path.dirname(abs_path).encode("utf-8")
    ).hexdigest()[:8]
    return {"name": os.path.basename(abs_path), "parent_hash": parent_hash}


def _validate_export_dir(path_str: str) -> str:
    """Validate caller-supplied EXPORT directory.

    Unlike upload paths, export must accept a caller-chosen target — it's
    the whole point of the operation. But the export will write a
    potentially-large amount of data (messages + media) into that
    directory, so the validator must close every redirect/clobber vector
    we know about:

      1. Symlink at leaf or in any parent → reject (could redirect
         writes to a sensitive directory).
      2. Path inside the daemon's RUNTIME_DIR or CONFIG_DIR → reject.
         The runtime dir hosts the socket/lock/pid; the config dir
         hosts encrypted sessions and the audit log. Writing the
         export under either could clobber state.
      3. Not a directory or doesn't exist → reject. We deliberately
         do NOT auto-create arbitrary directories: that surface lets a
         prompt-injected agent splash files anywhere writable.
      4. Wrong owner → reject (squatting attempt).

    Returns the validated absolute path.
    """
    import stat as _stat

    from . import paths as _paths

    if not path_str:
        raise HTTPException(400, "out_dir is empty")
    abs_path = os.path.abspath(path_str)

    bad_parent = _walk_parents_for_symlink(abs_path)
    if bad_parent is not None:
        raise HTTPException(
            400,
            f"refusing: symlink in parent component at {bad_parent!r} could "
            "redirect the export — pass a path with no symlinks",
        )

    try:
        info = os.lstat(abs_path)
    except FileNotFoundError as e:
        raise HTTPException(
            404,
            f"out_dir does not exist: {abs_path}. Create it explicitly first; "
            "the daemon will not auto-mkdir arbitrary directories.",
        ) from e
    except OSError as e:
        raise HTTPException(400, f"cannot stat {abs_path}: {e}") from e

    if _stat.S_ISLNK(info.st_mode):
        raise HTTPException(400, f"out_dir {abs_path} is a symlink — refusing")
    if not _stat.S_ISDIR(info.st_mode):
        raise HTTPException(400, f"out_dir {abs_path} is not a directory")
    if info.st_uid != os.getuid():
        raise HTTPException(
            400,
            f"out_dir {abs_path} is owned by uid={info.st_uid}, not us — refusing",
        )

    real_target = os.path.realpath(abs_path)
    for blocked, label in (
        (_paths.RUNTIME_DIR, "runtime"),
        (_paths.CONFIG_DIR, "config"),
    ):
        try:
            real_blocked = os.path.realpath(blocked)
        except OSError:
            continue
        if os.path.commonpath([real_target, real_blocked]) == real_blocked:
            raise HTTPException(
                400,
                f"refusing: {abs_path} is inside the daemon's {label} dir "
                f"({real_blocked}); export elsewhere",
            )

    return abs_path


def _open_validated_export_dir(path_str: str) -> tuple[str, int]:
    """Validate AND open as a directory fd, closing the validate→use TOCTOU.

    `_validate_export_dir` snapshots the path's metadata. Without an fd
    handoff, an attacker can swap `out_dir` to a symlink right before
    `export_chat` does `os.mkdir(out_dir / 'chat_<id>')`. The defense:
    open the validated path with O_DIRECTORY|O_NOFOLLOW, then have all
    subsequent writes use `dir_fd=fd` syscalls so they're bound to the
    inode we validated, not the name.

    Returns (abs_path, dir_fd). Caller closes the fd when done.
    """
    import stat as _stat

    abs_path = _validate_export_dir(path_str)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(abs_path, flags)
    except OSError as e:
        if e.errno in (40, 62):  # ELOOP
            raise HTTPException(
                400, f"refusing: {abs_path} became a symlink after validation"
            ) from e
        raise HTTPException(400, f"cannot open {abs_path}: {e}") from e

    try:
        info = os.fstat(fd)
        if not _stat.S_ISDIR(info.st_mode):
            raise HTTPException(400, f"{abs_path} is no longer a directory after open")
        if info.st_uid != os.getuid():
            raise HTTPException(
                400,
                f"{abs_path} owner changed between validate and open — "
                "refusing the export",
            )
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    return abs_path, fd


@app.post("/export/chat")
async def export_chat(req: ExportChatReq) -> dict[str, Any]:
    abs_dir, dir_fd = _open_validated_export_dir(req.out_dir)
    try:
        res = await _sess().export_chat(
            req.chat,
            abs_dir,
            dir_fd,
            limit=req.limit,
            include_media=req.include_media,
            since_date=req.since_date,
            until_date=req.until_date,
        )
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass
    audit.log(
        "export_chat",
        chat=str(req.chat),
        # Audit logs the export TARGET (operator visibility) but NOT the
        # message content. The export file itself contains the bodies; the
        # audit just records that an export happened.
        out_dir=abs_dir,
        message_count=res.get("message_count"),
        media_count=res.get("media_count"),
        include_media=req.include_media,
    )
    return res


# ---------- Stickers + GIFs (Phase 3) ----------


# Note on GIF search: Telegram's user API (and Telethon ≥1.36) does not
# expose a direct "search the GIF index" RPC; that surface is delivered
# via inline bots (e.g. @gif), which is a different request shape. We
# only ship saved-GIF list + send here. A future skill can wrap the
# inline-bot path if/when there's demand.


@app.get("/gif/saved")
async def gif_saved() -> dict[str, Any]:
    return {"gifs": await _sess().get_saved_gifs()}


@app.post("/gif/send")
async def gif_send(req: SendDocByRefReq) -> dict[str, Any]:
    msg_id = await _sess().send_gif(
        req.chat, req.doc_id, req.access_hash, req.file_reference_hex
    )
    audit.log("gif_send", chat=str(req.chat), msg_id=msg_id, doc_id=req.doc_id)
    return {"ok": True, "msg_id": msg_id}


@app.get("/sticker/saved")
async def sticker_saved() -> dict[str, Any]:
    return {"sets": await _sess().get_saved_stickers()}


@app.post("/sticker/set")
async def sticker_set(req: StickerSetReq) -> dict[str, Any]:
    """Resolve a sticker pack id+access_hash to its sendable stickers."""
    return {
        "stickers": await _sess().get_sticker_set(req.set_id, req.access_hash)
    }


@app.post("/sticker/send")
async def sticker_send(req: SendDocByRefReq) -> dict[str, Any]:
    msg_id = await _sess().send_sticker(
        req.chat, req.doc_id, req.access_hash, req.file_reference_hex
    )
    audit.log("sticker_send", chat=str(req.chat), msg_id=msg_id, doc_id=req.doc_id)
    return {"ok": True, "msg_id": msg_id}


# ---------- Channels (Phase 3) ----------


@app.post("/chat/participants")
async def chat_participants(req: GetParticipantsReq) -> dict[str, Any]:
    return await _sess().get_participants(
        req.chat,
        limit=req.limit,
        offset=req.offset,
        search=req.search,
        filter_kind=req.filter_kind,
    )


@app.post("/chat/signatures")
async def chat_signatures(req: SignaturesReq) -> dict[str, Any]:
    await _sess().channel_set_signatures(req.chat, req.enabled)
    audit.log("chat_signatures", chat=str(req.chat), enabled=req.enabled)
    return {"ok": True}


@app.post("/chat/slow_mode")
async def chat_slow_mode(req: SlowModeReq) -> dict[str, Any]:
    await _sess().channel_set_slow_mode(req.chat, req.seconds)
    audit.log("chat_slow_mode", chat=str(req.chat), seconds=req.seconds)
    return {"ok": True}


@app.post("/chat/discussion")
async def chat_discussion(req: DiscussionReq) -> dict[str, Any]:
    await _sess().channel_set_discussion(req.broadcast, req.group)
    audit.log(
        "chat_discussion",
        broadcast=str(req.broadcast),
        group=str(req.group) if req.group is not None else "(unbind)",
    )
    return {"ok": True}


@app.post("/chat/admin_log")
async def chat_admin_log(req: AdminLogReq) -> dict[str, Any]:
    events = await _sess().channel_admin_log(
        req.chat, limit=req.limit, search=req.search
    )
    return {"events": events}


# ---------- Privacy ----------


@app.post("/privacy/get")
async def privacy_get(req: GetPrivacyReq) -> dict[str, Any]:
    return await _sess().get_privacy(req.key)


@app.post("/privacy/set")
async def privacy_set(req: SetPrivacyReq) -> dict[str, Any]:
    rules_raw = [r.model_dump() for r in req.rules]
    res = await _sess().set_privacy(req.key, rules_raw)
    audit.log(
        "privacy_set",
        key=req.key,
        rule_count=len(req.rules),
        # Audit records what KEY changed and how many rules — not the raw
        # user-id allowlists, which can be sensitive (who you've blocked).
    )
    return res


# ---------- Folders ----------


@app.get("/folders/list")
async def folders_list() -> dict[str, Any]:
    return {"folders": await _sess().list_folders()}


@app.post("/folders/update")
async def folders_update(req: FolderPeerSpec) -> dict[str, Any]:
    res = await _sess().update_folder(
        req.folder_id,
        title=req.title,
        include_peers=req.include_peers,
        exclude_peers=req.exclude_peers,
        contacts=req.contacts,
        non_contacts=req.non_contacts,
        groups=req.groups,
        broadcasts=req.broadcasts,
        bots=req.bots,
    )
    audit.log(
        "folder_update",
        folder_id=req.folder_id,
        title_len=len(req.title),
        include_count=len(req.include_peers),
        exclude_count=len(req.exclude_peers),
    )
    return res


@app.post("/folders/delete")
async def folders_delete(req: FolderIdReq) -> dict[str, Any]:
    await _sess().delete_folder(req.folder_id)
    audit.log("folder_delete", folder_id=req.folder_id)
    return {"ok": True}


@app.post("/profile/update")
async def profile_update(req: UpdateProfileReq) -> dict[str, Any]:
    info = await _sess().update_profile(
        first_name=req.first_name, last_name=req.last_name, about=req.about
    )
    audit.log(
        "profile_update",
        # Record what FIELDS were touched and their lengths, never the values.
        # Display names + bio can be very personal (real names, contact info).
        # `is not None` (vs truthiness) so that an explicit clear with `""`
        # is recorded as length 0 — distinguishable from "field not supplied".
        first_name_len=len(req.first_name) if req.first_name is not None else None,
        last_name_len=len(req.last_name) if req.last_name is not None else None,
        about_len=len(req.about) if req.about is not None else None,
    )
    return info


@app.post("/profile/2fa")
async def profile_2fa(req: Change2faReq) -> dict[str, Any]:
    """Change cloud-password (two-factor auth). The audit log records
    only WHICH transition happened — set / change / remove — never the
    passwords themselves."""
    transition = (
        "set"
        if req.current_password is None and req.new_password
        else "change"
        if req.current_password and req.new_password
        else "remove"
    )
    res = await _sess().change_2fa_password(
        current_password=req.current_password,
        new_password=req.new_password,
        hint=req.hint,
        email=req.email,
    )
    audit.log("profile_2fa", transition=transition)
    return res


@app.post("/profile/username")
async def profile_username(req: UpdateUsernameReq) -> dict[str, Any]:
    info = await _sess().update_username(req.username)
    # The username is public, so logging it is fine — and useful, since
    # username changes affect how others reach the user.
    audit.log("profile_username", new_username=req.username or "(cleared)")
    return info


@app.post("/profile/photo")
async def profile_photo(req: SetPhotoReq) -> dict[str, Any]:
    """Reuse the same TOCTOU-hardened pipeline media uploads use."""
    abs_path, size, fd = _open_validated_upload(req.file_path)
    file_obj = os.fdopen(fd, "rb")
    try:
        info = await _sess().set_profile_photo(file_obj)
    finally:
        try:
            file_obj.close()
        except Exception:
            pass
    audit.log(
        "profile_photo_set",
        size=size,
        photo_id=info.get("photo_id"),
        **_audit_path_redacted(abs_path),
    )
    return info


@app.post("/profile/photo_delete")
async def profile_photo_delete() -> dict[str, Any]:
    deleted = await _sess().delete_current_profile_photo()
    audit.log("profile_photo_delete", deleted=deleted)
    return {"ok": True, "deleted": deleted}


@app.post("/profile/status")
async def profile_status(req: SetStatusReq) -> dict[str, Any]:
    await _sess().set_online_status(req.online)
    audit.log("profile_status", online=req.online)
    return {"ok": True}


@app.post("/profile/emoji_status")
async def profile_emoji_status(req: SetEmojiStatusReq) -> dict[str, Any]:
    """Set or clear the user's emoji status (Premium-only).

    Returns 502 PREMIUM_REQUIRED via the generic exception handler if the
    account doesn't hold Premium. We don't pre-check Premium here; the
    server-side check is authoritative and a stale local cache would
    let us mis-report.
    """
    await _sess().set_emoji_status(req.document_id, until=req.until)
    audit.log(
        "profile_emoji_status",
        kind="clear" if req.document_id is None else "set",
        has_until=req.until is not None,
    )
    return {"ok": True}


@app.post("/scheduled/send")
async def scheduled_send(req: SendScheduledReq) -> dict[str, Any]:
    # Re-check the schedule window right before issuing the send. Pydantic
    # validates at parse time; any in-process delay (queueing, slow auth,
    # etc.) could push a borderline request below the 10s minimum, in
    # which case Telethon would surface an upstream 502. Bouncing here
    # gives callers a deterministic 400 instead.
    from datetime import timezone as _tz
    delta = (req.schedule_date - datetime.now(_tz.utc)).total_seconds()
    if delta < 10:
        raise HTTPException(
            400,
            f"schedule_date is now only {delta:.1f}s in the future; "
            "must be ≥10s. Re-issue with a fresh timestamp.",
        )

    msg_id = await _sess().send_scheduled(
        req.chat, req.text, req.schedule_date, reply_to=req.reply_to
    )
    audit.log(
        "scheduled_send",
        chat=str(req.chat),
        msg_id=msg_id,
        # Log timestamp + length, not body — same content-redaction stance
        # as polls. The audit confirms WHEN something was queued, not WHAT.
        scheduled_for=req.schedule_date.isoformat(),
        text_len=len(req.text),
    )
    return {"ok": True, "msg_id": msg_id}


@app.post("/scheduled/edit")
async def scheduled_edit(req: EditScheduledReq) -> dict[str, Any]:
    # Same just-before-dispatch recheck as /scheduled/send: a request
    # that's borderline at parse time can age below the 10s minimum
    # while queueing, in which case Telethon would surface a 502.
    # Bouncing here gives callers a deterministic 400.
    if req.schedule_date is not None:
        from datetime import timezone as _tz

        delta = (req.schedule_date - datetime.now(_tz.utc)).total_seconds()
        if delta < 10:
            raise HTTPException(
                400,
                f"schedule_date is now only {delta:.1f}s in the future; "
                "must be ≥10s. Re-issue with a fresh timestamp.",
            )

    msg_id = await _sess().edit_scheduled(
        req.chat,
        req.msg_id,
        text=req.text,
        schedule_date=req.schedule_date,
    )
    audit.log(
        "scheduled_edit",
        chat=str(req.chat),
        msg_id=msg_id,
        changed_text=req.text is not None,
        changed_schedule=req.schedule_date is not None,
    )
    return {"ok": True, "msg_id": msg_id}


@app.post("/scheduled/list")
async def scheduled_list(req: ListScheduledReq) -> dict[str, Any]:
    items = await _sess().list_scheduled(req.chat, limit=req.limit)
    return {"scheduled": items}


@app.post("/scheduled/delete")
async def scheduled_delete(req: DeleteScheduledReq) -> dict[str, Any]:
    requested = await _sess().delete_scheduled(req.chat, req.msg_ids)
    audit.log(
        "scheduled_delete",
        chat=str(req.chat),
        msg_ids=req.msg_ids,
        requested=requested,
    )
    return {"ok": True, "requested": requested}


@app.post("/draft/save")
async def draft_save(req: SaveDraftReq) -> dict[str, Any]:
    await _sess().save_draft(req.chat, req.text, reply_to=req.reply_to)
    # Drafts are private to the user but still recorded as a write. Don't
    # leak the body — only metadata.
    audit.log("draft_save", chat=str(req.chat), text_len=len(req.text))
    return {"ok": True}


@app.post("/draft/get")
async def draft_get(req: GetDraftReq) -> dict[str, Any]:
    draft = await _sess().get_draft(req.chat)
    return {"draft": draft}


@app.post("/draft/clear")
async def draft_clear(req: GetDraftReq) -> dict[str, Any]:
    await _sess().clear_draft(req.chat)
    audit.log("draft_clear", chat=str(req.chat))
    return {"ok": True}


@app.post("/poll/create")
async def poll_create(req: CreatePollReq) -> dict[str, Any]:
    msg_id = await _sess().create_poll(
        req.chat,
        req.question,
        req.options,
        anonymous=req.anonymous,
        multiple_choice=req.multiple_choice,
        quiz=req.quiz,
        correct_option=req.correct_option,
        explanation=req.explanation,
    )
    audit.log(
        "poll_create",
        chat=str(req.chat),
        msg_id=msg_id,
        n_options=len(req.options),
        quiz=req.quiz,
        anonymous=req.anonymous,
    )
    return {"ok": True, "msg_id": msg_id}


@app.post("/poll/edit")
async def poll_edit(req: EditPollReq) -> dict[str, Any]:
    await _sess().edit_poll(
        req.chat, req.msg_id, question=req.question, options=req.options
    )
    audit.log(
        "poll_edit",
        chat=str(req.chat),
        msg_id=req.msg_id,
        # Audit records WHICH parts changed, not the new content.
        changed_question=req.question is not None,
        changed_options=req.options is not None,
    )
    return {"ok": True}


@app.post("/poll/close")
async def poll_close(req: PollMsgReq) -> dict[str, Any]:
    await _sess().close_poll(req.chat, req.msg_id)
    audit.log("poll_close", chat=str(req.chat), msg_id=req.msg_id)
    return {"ok": True}


@app.post("/poll/results")
async def poll_results(req: PollMsgReq) -> dict[str, Any]:
    return await _sess().poll_results(req.chat, req.msg_id)


@app.post("/send_media")
async def send_media(req: SendMediaReq) -> dict[str, Any]:
    if req.as_voice:
        ext = os.path.splitext(req.file_path)[1].lower()
        if ext not in VOICE_EXTS:
            raise HTTPException(
                400,
                f"as_voice requires audio extension {sorted(VOICE_EXTS)}; got {ext!r}",
            )

    abs_path, size, fd = _open_validated_upload(req.file_path)
    file_obj = os.fdopen(fd, "rb")  # closing the file closes the fd
    try:
        msg_id = await _sess().send_media(
            req.chat,
            file_obj,
            caption=req.caption,
            reply_to=req.reply_to,
            as_voice=req.as_voice,
            force_document=req.force_document,
            display_name=os.path.basename(abs_path),
        )
    finally:
        try:
            file_obj.close()
        except Exception:
            pass

    audit.log(
        "send_media",
        chat=str(req.chat),
        size=size,
        as_voice=req.as_voice,
        force_document=req.force_document,
        msg_id=msg_id,
        **_audit_path_redacted(abs_path),
    )
    return {"ok": True, "msg_id": msg_id}


# ---------- geo / live location ----------

# Telegram's documented live-location range: any duration from 60 s
# (1 min) to 86400 s (24 h). 0x7FFFFFFF is the sentinel for "indefinite,
# until manually stopped" (per current Bot API docs). Anything else is
# rejected by Telegram upstream.
LIVE_PERIOD_MIN = 60
LIVE_PERIOD_MAX = 86400
LIVE_PERIOD_INDEFINITE = 0x7FFFFFFF


class LatLng(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    accuracy: Optional[int] = Field(None, ge=0, le=1500)


class SendLocationReq(LatLng):
    chat: str | int
    reply_to: Optional[int] = None


class SendLiveLocationReq(LatLng):
    chat: str | int
    period: int
    heading: Optional[int] = Field(None, ge=1, le=360)
    proximity: Optional[int] = Field(None, ge=0, le=100000)
    reply_to: Optional[int] = None

    @field_validator("period")
    @classmethod
    def _period_valid(cls, v: int) -> int:
        if v == LIVE_PERIOD_INDEFINITE:
            return v
        if not (LIVE_PERIOD_MIN <= v <= LIVE_PERIOD_MAX):
            raise ValueError(
                f"period must be in [{LIVE_PERIOD_MIN}, {LIVE_PERIOD_MAX}] "
                f"seconds, or {LIVE_PERIOD_INDEFINITE} (0x7FFFFFFF) for "
                f"indefinite sharing"
            )
        return v


class EditLiveLocationReq(LatLng):
    chat: str | int
    msg_id: int = Field(ge=1)
    heading: Optional[int] = Field(None, ge=1, le=360)
    proximity: Optional[int] = Field(None, ge=0, le=100000)


class StopLiveLocationReq(BaseModel):
    chat: str | int
    msg_id: int = Field(ge=1)


@app.post("/location/send")
async def location_send(req: SendLocationReq) -> dict[str, Any]:
    msg_id = await _sess().send_location(
        req.chat, req.lat, req.lng, accuracy=req.accuracy, reply_to=req.reply_to
    )
    audit.log("location_send", chat=str(req.chat), msg_id=msg_id)
    return {"ok": True, "msg_id": msg_id}


@app.post("/location/send_live")
async def location_send_live(req: SendLiveLocationReq) -> dict[str, Any]:
    msg_id = await _sess().send_live_location(
        req.chat,
        req.lat,
        req.lng,
        req.period,
        accuracy=req.accuracy,
        heading=req.heading,
        proximity=req.proximity,
        reply_to=req.reply_to,
    )
    audit.log(
        "location_send_live",
        chat=str(req.chat),
        msg_id=msg_id,
        period=req.period,
    )
    return {"ok": True, "msg_id": msg_id}


@app.post("/location/edit_live")
async def location_edit_live(req: EditLiveLocationReq) -> dict[str, Any]:
    await _sess().edit_live_location(
        req.chat,
        req.msg_id,
        req.lat,
        req.lng,
        accuracy=req.accuracy,
        heading=req.heading,
        proximity=req.proximity,
    )
    audit.log("location_edit_live", chat=str(req.chat), msg_id=req.msg_id)
    return {"ok": True}


@app.post("/location/stop_live")
async def location_stop_live(req: StopLiveLocationReq) -> dict[str, Any]:
    await _sess().stop_live_location(req.chat, req.msg_id)
    audit.log("location_stop_live", chat=str(req.chat), msg_id=req.msg_id)
    return {"ok": True}


# ---------- stories ----------


class StoriesPeerReq(BaseModel):
    peer: str | int


class StoriesPinnedReq(BaseModel):
    peer: str | int
    limit: int = Field(50, ge=1, le=100)
    offset_id: int = Field(0, ge=0)


class StoriesReadReq(BaseModel):
    peer: str | int
    max_id: int = Field(ge=1)


class StoriesDeleteReq(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("ids")
    @classmethod
    def _ids_positive(cls, v: list[int]) -> list[int]:
        for x in v:
            if x < 1:
                raise ValueError(f"story id must be ≥1, got {x}")
        return v


@app.post("/stories/active")
async def stories_active(req: StoriesPeerReq) -> dict[str, Any]:
    items = await _sess().list_active_stories(req.peer)
    return {"stories": items, "count": len(items)}


@app.post("/stories/pinned")
async def stories_pinned(req: StoriesPinnedReq) -> dict[str, Any]:
    items = await _sess().list_pinned_stories(
        req.peer, limit=req.limit, offset_id=req.offset_id
    )
    return {"stories": items, "count": len(items)}


@app.post("/stories/mark_read")
async def stories_mark_read(req: StoriesReadReq) -> dict[str, Any]:
    await _sess().mark_stories_read(req.peer, req.max_id)
    audit.log("stories_mark_read", peer=str(req.peer), max_id=req.max_id)
    return {"ok": True}


@app.post("/stories/delete")
async def stories_delete(req: StoriesDeleteReq) -> dict[str, Any]:
    count = await _sess().delete_own_stories(req.ids)
    audit.log("stories_delete", count=count, ids=req.ids)
    return {"ok": True, "deleted": count}


# ---------- forum topics ----------


class ListTopicsReq(BaseModel):
    chat: str | int
    limit: int = Field(100, ge=1, le=100)
    query: Optional[str] = Field(None, max_length=64)


class CreateTopicReq(BaseModel):
    chat: str | int
    title: str = Field(min_length=1, max_length=128)  # Telegram caps at 128
    icon_color: Optional[int] = Field(None, ge=0, le=0xFFFFFF)
    icon_emoji_id: Optional[int] = None


class EditTopicReq(BaseModel):
    chat: str | int
    topic_id: int = Field(ge=1)
    title: Optional[str] = Field(None, min_length=1, max_length=128)
    icon_emoji_id: Optional[int] = None
    closed: Optional[bool] = None
    hidden: Optional[bool] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "EditTopicReq":
        if all(x is None for x in (self.title, self.icon_emoji_id, self.closed, self.hidden)):
            raise ValueError(
                "edit_topic needs at least one of title / icon_emoji_id / closed / hidden"
            )
        return self


class TopicReq(BaseModel):
    chat: str | int
    topic_id: int = Field(ge=1)


class PinTopicReq(BaseModel):
    chat: str | int
    topic_id: int = Field(ge=1)
    pinned: bool


@app.post("/topics/list")
async def topics_list(req: ListTopicsReq) -> dict[str, Any]:
    items = await _sess().list_topics(req.chat, limit=req.limit, query=req.query)
    return {"topics": items, "count": len(items)}


@app.post("/topics/create")
async def topics_create(req: CreateTopicReq) -> dict[str, Any]:
    topic_id = await _sess().create_topic(
        req.chat,
        req.title,
        icon_color=req.icon_color,
        icon_emoji_id=req.icon_emoji_id,
    )
    audit.log("topic_create", chat=str(req.chat), topic_id=topic_id, title_len=len(req.title))
    return {"ok": True, "topic_id": topic_id}


@app.post("/topics/edit")
async def topics_edit(req: EditTopicReq) -> dict[str, Any]:
    await _sess().edit_topic(
        req.chat,
        req.topic_id,
        title=req.title,
        icon_emoji_id=req.icon_emoji_id,
        closed=req.closed,
        hidden=req.hidden,
    )
    audit.log(
        "topic_edit",
        chat=str(req.chat),
        topic_id=req.topic_id,
        fields=[
            k for k, v in (
                ("title", req.title),
                ("icon", req.icon_emoji_id),
                ("closed", req.closed),
                ("hidden", req.hidden),
            ) if v is not None
        ],
    )
    return {"ok": True}


@app.post("/topics/delete")
async def topics_delete(req: TopicReq) -> dict[str, Any]:
    await _sess().delete_topic(req.chat, req.topic_id)
    audit.log("topic_delete", chat=str(req.chat), topic_id=req.topic_id)
    return {"ok": True}


@app.post("/topics/pin")
async def topics_pin(req: PinTopicReq) -> dict[str, Any]:
    await _sess().pin_topic(req.chat, req.topic_id, req.pinned)
    audit.log(
        "topic_pin", chat=str(req.chat), topic_id=req.topic_id, pinned=req.pinned
    )
    return {"ok": True}


# ---------- bot mode ----------


class _Button(BaseModel):
    kind: str  # "callback" | "url"
    text: str = Field(min_length=1, max_length=64)
    data: Optional[str] = None  # for kind="callback"; ≤64 bytes per Telegram
    url: Optional[str] = None  # for kind="url"

    @model_validator(mode="after")
    def _check_kind(self) -> "_Button":
        if self.kind == "callback":
            if not self.data:
                raise ValueError("callback buttons require non-empty data")
            if len(self.data.encode("utf-8")) > 64:
                raise ValueError("callback data must be ≤64 UTF-8 bytes")
            if self.url is not None:
                raise ValueError("callback buttons must not carry url")
        elif self.kind == "url":
            if not self.url:
                raise ValueError("url buttons require non-empty url")
            if not (self.url.startswith("https://") or self.url.startswith("tg://")):
                raise ValueError("url must be https:// or tg://")
            if self.data is not None:
                raise ValueError("url buttons must not carry data")
        else:
            raise ValueError(f"button kind must be 'callback' or 'url', got {self.kind!r}")
        return self


class BotSendKeyboardReq(BaseModel):
    chat: str | int
    text: str = Field(min_length=1, max_length=4096)
    rows: list[list[_Button]] = Field(min_length=1, max_length=8)
    reply_to: Optional[int] = None

    @model_validator(mode="after")
    def _row_widths(self) -> "BotSendKeyboardReq":
        # Telegram caps each row at 8 buttons.
        for r in self.rows:
            if not 1 <= len(r) <= 8:
                raise ValueError("each keyboard row must have 1–8 buttons")
        return self


class BotAnswerCallbackReq(BaseModel):
    query_id: int
    text: str = Field("", max_length=200)  # Telegram caps callback answer text
    alert: bool = False
    url: Optional[str] = None
    cache_time: int = Field(0, ge=0, le=86400)

    @model_validator(mode="after")
    def _check_url(self) -> "BotAnswerCallbackReq":
        if self.url is not None:
            if not self.url:
                raise ValueError("url must be non-empty when set; pass null to omit")
            if not (self.url.startswith("https://") or self.url.startswith("tg://")):
                raise ValueError("url must be https:// or tg://")
        return self


class BotPollCallbacksReq(BaseModel):
    timeout: float = Field(0.0, ge=0.0, le=30.0)
    limit: int = Field(50, ge=1, le=200)


class BotCommand(BaseModel):
    command: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    description: str = Field(min_length=1, max_length=256)


class BotSetCommandsReq(BaseModel):
    commands: list[BotCommand] = Field(default_factory=list, max_length=100)
    language_code: str = Field("", max_length=8)


def _require_bot_session() -> TGSession:
    sess = _sess()
    if not sess.cfg.is_bot:
        raise HTTPException(
            400,
            "this endpoint requires a bot-mode session; the active account "
            "is a user account",
        )
    return sess


@app.post("/bot/send_keyboard")
async def bot_send_keyboard(req: BotSendKeyboardReq) -> dict[str, Any]:
    sess = _require_bot_session()
    rows = [[b.model_dump(exclude_none=True) for b in row] for row in req.rows]
    msg_id = await sess.bot_send_keyboard(
        req.chat, req.text, rows, reply_to=req.reply_to
    )
    audit.log("bot_send_keyboard", chat=str(req.chat), msg_id=msg_id, rows=len(rows))
    return {"ok": True, "msg_id": msg_id}


@app.post("/bot/answer_callback")
async def bot_answer_callback(req: BotAnswerCallbackReq) -> dict[str, Any]:
    sess = _require_bot_session()
    await sess.bot_answer_callback(
        req.query_id,
        text=req.text,
        alert=req.alert,
        url=req.url,
        cache_time=req.cache_time,
    )
    audit.log(
        "bot_answer_callback", query_id=req.query_id, alert=req.alert, has_text=bool(req.text)
    )
    return {"ok": True}


@app.post("/bot/poll_callbacks")
async def bot_poll_callbacks(req: BotPollCallbacksReq) -> dict[str, Any]:
    sess = _require_bot_session()
    items = await sess.bot_poll_callbacks(timeout=req.timeout, limit=req.limit)
    return {"items": items, "count": len(items)}


@app.post("/bot/set_commands")
async def bot_set_commands(req: BotSetCommandsReq) -> dict[str, Any]:
    sess = _require_bot_session()
    cmds = [c.model_dump() for c in req.commands]
    await sess.bot_set_commands(cmds, language_code=req.language_code)
    audit.log("bot_set_commands", count=len(cmds), lang=req.language_code or "default")
    return {"ok": True, "count": len(cmds)}


# ---------- entry point ----------


def _import_fcntl():
    """Import fcntl, exiting cleanly on non-POSIX platforms.

    The whole daemon depends on Unix domain sockets and POSIX advisory locks.
    Rather than letting a Windows user hit a cryptic `ModuleNotFoundError`
    deep in startup, exit with a clear message right here.
    """
    if sys.platform == "win32":
        sys.stderr.write(
            "[tgmcp] daemon is POSIX-only (uses Unix domain sockets and "
            "fcntl flock). Windows is not supported.\n"
        )
        raise SystemExit(1)
    try:
        import fcntl  # noqa: PLC0415

        return fcntl
    except ImportError as e:
        sys.stderr.write(
            f"[tgmcp] fcntl module unavailable on this platform ({e}). "
            "The daemon requires POSIX advisory locks.\n"
        )
        raise SystemExit(1) from e


def is_daemon_locked() -> tuple[bool, Optional[int]]:
    """Probe whether some process currently holds the daemon flock.

    This is the authoritative liveness signal — `flock` is auto-released by
    the kernel when the holding process exits, so a held lock means the
    daemon process is genuinely alive (whether or not it's responding to
    HTTP, whether or not a pid file exists).

    Returns (locked, holder_pid). holder_pid comes from LOCK_PATH content
    (best effort) and may be None on race / read failure.
    """
    if sys.platform == "win32":
        return False, None
    if not LOCK_PATH.exists():
        return False, None
    try:
        fcntl = _import_fcntl()
    except SystemExit:
        return False, None

    import errno

    try:
        fd = os.open(str(LOCK_PATH), os.O_RDWR)
    except OSError:
        return False, None

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                # Held by someone. Read the pid the holder wrote at acquire time.
                try:
                    pid_str = LOCK_PATH.read_text().strip()
                    return True, int(pid_str) if pid_str else None
                except (OSError, ValueError):
                    return True, None
            return False, None
        # We got the lock → no holder was there. Release immediately.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False, None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _acquire_singleton_lock() -> int:
    """Take an exclusive non-blocking flock on LOCK_PATH.

    This guarantees only one daemon binds the socket at a time, even if a
    concurrent caller raced past the parent's pre-spawn probe. Without this
    lock, blindly `unlink(SOCKET_PATH); bind()` would let a second daemon
    delete the still-live socket of a first daemon and rebind, leaving two
    daemons "running" in inconsistent state.

    Returns the open fd; caller must keep it open for the daemon's lifetime.
    Raises SystemExit(1) if another daemon already holds the lock or the
    platform doesn't support POSIX advisory locks.
    """
    import errno

    fcntl = _import_fcntl()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        # Some platforms surface "lock held by other" as BlockingIOError
        # (subclass of OSError, errno=EWOULDBLOCK/EAGAIN). Anything else is a
        # real fault and should propagate distinctly.
        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            sys.stderr.write(
                f"[tgmcp] flock on {LOCK_PATH} failed with unexpected error: {exc!r}\n"
            )
            raise SystemExit(1) from exc

        existing_pid = None
        try:
            existing_pid = LOCK_PATH.read_text().strip() or None
        except OSError:
            pass
        sys.stderr.write(
            f"[tgmcp] another daemon is running (lock={LOCK_PATH}"
            + (f", pid={existing_pid}" if existing_pid else "")
            + "). Exiting.\n"
        )
        raise SystemExit(1) from exc

    # Record our pid in the lock file so observers can identify the holder.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _release_socket(lock_fd: int) -> None:
    fcntl = _import_fcntl()

    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = _acquire_singleton_lock()
    # Now we hold exclusive ownership. Stale socket can be safely removed
    # because no other daemon could be using it (the lock proves that).
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except OSError as e:
            sys.stderr.write(f"[tgmcp] could not unlink stale socket: {e}\n")
            _release_socket(lock_fd)
            raise SystemExit(1) from e

    config = uvicorn.Config(
        app,
        uds=str(SOCKET_PATH),
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    state.uvicorn_server = server
    try:
        asyncio.run(server.serve())
    finally:
        state.uvicorn_server = None
        _release_socket(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
