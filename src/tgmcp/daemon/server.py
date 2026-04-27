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
from datetime import datetime
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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
    session: Optional[TGSession] = None
    uvicorn_server: Optional["uvicorn.Server"] = None


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


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    label = os.environ.get("TGMCP_ACCOUNT", "main")
    api_id, api_hash = _read_app_creds()
    passphrase = _consume_passphrase()
    try:
        session_str = auth.load_session(label, passphrase=passphrase)
    finally:
        # Best-effort wipe of the local reference. CPython doesn't guarantee
        # GC, but we at least drop our reference promptly.
        passphrase = None
    cfg = TGConfig(api_id=api_id, api_hash=api_hash, session_string=session_str, label=label)
    state.session = TGSession(cfg=cfg)
    await state.session.start()
    try:
        yield
    finally:
        if state.session:
            await state.session.stop()


app = FastAPI(title="slim-tg-mcp daemon", lifespan=lifespan)


def _err(status: int, kind: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": kind, "detail": detail})


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
    name = type(exc).__name__
    if name.endswith("RPCError") or "Telethon" in name or "Telegram" in name:
        return _err(502, name, str(exc))
    return _err(500, name, str(exc))


def _sess() -> TGSession:
    if state.session is None:
        raise HTTPException(503, "session not ready")
    return state.session


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


class ShutdownReq(BaseModel):
    instance_id: str


# ---------- routes ----------


@app.get("/health")
async def health() -> dict[str, Any]:
    s = state.session
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
    }


@app.get("/accounts")
async def accounts() -> dict[str, list[str]]:
    return {"accounts": auth.list_accounts()}


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
