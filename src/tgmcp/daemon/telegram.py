"""Thin Telethon wrapper. The MCP server / Skills never touch Telethon directly;
they go through the daemon HTTP API which calls into this module.

Goal: keep Telethon-specific quirks contained here so we can swap to Pyrogram
or another backend later without touching MCP/skill code.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom import Dialog, Message
from telethon.tl.types import (
    Channel,
    Chat,
    User,
)

from .sanitizer import TrustContext, wrap_message


@dataclass
class TGConfig:
    api_id: int
    api_hash: str
    session_string: str
    label: str = "main"


@dataclass
class TGMessage:
    id: int
    chat_id: int
    sender_id: Optional[int]
    date: str
    text: str
    has_media: bool
    reply_to_msg_id: Optional[int]
    wrapped: str = ""

    @classmethod
    def from_telethon(cls, m: Message, *, ctx: TrustContext) -> "TGMessage":
        text = m.message or ""
        date = m.date.astimezone(timezone.utc).isoformat() if m.date else ""
        wrapped = wrap_message(text, ctx, msg_id=m.id, date=date)
        # m.chat_id is Telethon's marked-format chat reference (e.g. -100<channel_id>
        # for channels). Use it directly — manual peer_id reconstruction would
        # strip the marker and produce a value unusable as a chat argument.
        return cls(
            id=m.id,
            chat_id=m.chat_id or 0,
            sender_id=m.sender_id,
            date=date,
            text=text,
            has_media=m.media is not None,
            reply_to_msg_id=m.reply_to_msg_id,
            wrapped=wrapped,
        )


@dataclass
class TGDialog:
    id: int
    title: str
    type: str  # "user" | "group" | "channel" | "bot"
    unread_count: int
    last_message_date: Optional[str]


def _entity_kind(e: Any) -> str:
    if isinstance(e, User):
        return "bot" if e.bot else "user"
    if isinstance(e, Channel):
        return "channel" if e.broadcast else "group"
    if isinstance(e, Chat):
        return "group"
    return "unknown"


@dataclass
class TGSession:
    """Holds a connected TelegramClient. One per process."""

    cfg: TGConfig
    client: TelegramClient = field(init=False)
    me_id: Optional[int] = None
    contact_ids: set[int] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        self.client = TelegramClient(
            StringSession(self.cfg.session_string),
            self.cfg.api_id,
            self.cfg.api_hash,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram session not authorized. Run `tgmcp init` to (re)login."
            )
        me = await self.client.get_me()
        self.me_id = me.id
        await self._refresh_contacts()

    async def _refresh_contacts(self) -> None:
        try:
            from telethon.tl.functions.contacts import GetContactsRequest

            res = await self.client(GetContactsRequest(hash=0))
            self.contact_ids = {u.id for u in res.users}
        except Exception:
            self.contact_ids = set()

    async def stop(self) -> None:
        await self.client.disconnect()

    def _trust_for(self, m: Message) -> TrustContext:
        """Derive trust from the *content author*, not the relayer.

        If a message is a forward (m.fwd_from != None), the text was composed
        by someone else — even if the user themselves forwarded it into a saved
        messages chat. Treating such a message as trust=high would let any
        attacker reach the high-trust tier by getting a victim to forward
        their malicious message.
        """
        sender_id = m.sender_id
        is_forwarded = getattr(m, "fwd_from", None) is not None
        is_private = getattr(m, "is_private", False)
        return TrustContext(
            sender_id=sender_id,
            chat_id=m.chat_id,
            is_self=(not is_forwarded) and sender_id is not None and sender_id == self.me_id,
            is_contact=(not is_forwarded)
            and sender_id is not None
            and sender_id in self.contact_ids,
            is_private=is_private,
        )

    # ----- Read-side -----

    async def list_dialogs(self, limit: int = 50) -> list[TGDialog]:
        out: list[TGDialog] = []
        async for d in self.client.iter_dialogs(limit=limit):
            assert isinstance(d, Dialog)
            entity = d.entity
            kind = _entity_kind(entity)
            out.append(
                TGDialog(
                    id=d.id,
                    title=d.name or "",
                    type=kind,
                    unread_count=d.unread_count,
                    last_message_date=d.date.astimezone(timezone.utc).isoformat()
                    if d.date
                    else None,
                )
            )
        return out

    async def search_global(
        self,
        query: str,
        *,
        limit: int = 30,
    ) -> list[TGMessage]:
        out: list[TGMessage] = []
        async for m in self.client.iter_messages(None, search=query, limit=limit):
            out.append(TGMessage.from_telethon(m, ctx=self._trust_for(m)))
        return out

    async def search_in_chat(
        self,
        chat: str | int,
        query: str,
        *,
        limit: int = 50,
        from_user: Optional[str | int] = None,
        min_date: Optional[datetime] = None,
        max_date: Optional[datetime] = None,
    ) -> list[TGMessage]:
        entity = await self.client.get_entity(chat)
        from_entity = await self.client.get_entity(from_user) if from_user else None
        out: list[TGMessage] = []
        async for m in self.client.iter_messages(
            entity,
            search=query or None,
            limit=limit,
            from_user=from_entity,
            offset_date=max_date,
        ):
            if min_date and m.date and m.date < min_date:
                break
            out.append(TGMessage.from_telethon(m, ctx=self._trust_for(m)))
        return out

    async def get_messages(
        self,
        chat: str | int,
        *,
        limit: int = 50,
        offset_id: int = 0,
    ) -> list[TGMessage]:
        entity = await self.client.get_entity(chat)
        out: list[TGMessage] = []
        async for m in self.client.iter_messages(entity, limit=limit, offset_id=offset_id):
            out.append(TGMessage.from_telethon(m, ctx=self._trust_for(m)))
        return out

    async def get_message_context(
        self,
        chat: str | int,
        msg_id: int,
        *,
        before: int = 5,
        after: int = 5,
    ) -> list[TGMessage]:
        entity = await self.client.get_entity(chat)
        # Telethon: max_id excludes itself, min_id excludes itself.
        # Get `before` messages older than msg_id, then msg_id, then `after` newer.
        older = await self.client.get_messages(entity, limit=before, max_id=msg_id)
        target = await self.client.get_messages(entity, ids=msg_id)
        newer = await self.client.get_messages(entity, limit=after, min_id=msg_id, reverse=True)

        all_msgs = list(reversed(older)) + ([target] if target else []) + list(newer)
        out = []
        for m in all_msgs:
            if not m:
                continue
            out.append(TGMessage.from_telethon(m, ctx=self._trust_for(m)))
        return out

    async def resolve_entity(self, query: str | int) -> dict[str, Any]:
        e = await self.client.get_entity(query)
        # get_peer_id returns the marked-format ID (e.g. -100<channel_id>) that
        # can be fed back as a `chat` argument. Raw e.id lacks the marker for
        # channels/groups and produces ambiguous lookups downstream.
        return {
            "id": await self.client.get_peer_id(e),
            "raw_id": e.id,
            "kind": _entity_kind(e),
            "title": getattr(e, "title", None) or getattr(e, "first_name", None) or "",
            "username": getattr(e, "username", None),
        }

    async def get_chat_info(self, chat: str | int) -> dict[str, Any]:
        e = await self.client.get_entity(chat)
        info: dict[str, Any] = {
            "id": await self.client.get_peer_id(e),
            "raw_id": e.id,
            "kind": _entity_kind(e),
            "title": getattr(e, "title", None) or getattr(e, "first_name", None) or "",
            "username": getattr(e, "username", None),
        }
        # `get_entity` returns just the constructor, not the full chat info.
        # For an accurate participants_count we have to call the
        # GetFull*Request RPCs explicitly. Channels/megagroups expose
        # `participants_count` directly; classic Chat groups expose a
        # `participants` list — we count its items.
        info["participants_count"] = await self._participants_count(e)
        return info

    async def _participants_count(self, e: Any) -> Optional[int]:
        try:
            if isinstance(e, Channel):
                from telethon.tl.functions.channels import GetFullChannelRequest

                full = await self.client(GetFullChannelRequest(e))
                return getattr(full.full_chat, "participants_count", None)
            if isinstance(e, Chat):
                from telethon.tl.functions.messages import GetFullChatRequest

                full = await self.client(GetFullChatRequest(e.id))
                participants = getattr(full.full_chat, "participants", None)
                # ChatParticipants has a `.participants` list; ChatParticipantsForbidden
                # does not — fall back to the lightweight count on the entity.
                inner = getattr(participants, "participants", None)
                if inner is not None:
                    return len(inner)
                return getattr(e, "participants_count", None)
        except Exception:
            # Member counts are nice-to-have; permission errors / privacy
            # restrictions shouldn't break the basic chat_info response.
            return getattr(e, "participants_count", None)
        return None

    async def download_media(
        self,
        chat: str | int,
        msg_id: int,
    ) -> Optional[str]:
        """Download a message's media into the app-owned downloads directory.

        Filename is server-generated (`<msg_id>-<random>.<ext>`) so neither the
        caller nor the original Telegram filename can drive a path-traversal.
        Returns the absolute saved path, or None if the message had no media.

        Caller-controlled output paths are deliberately NOT supported here
        because tg_download_media is an always-loaded MCP tool: a prompt-
        injected model would otherwise have a filesystem-write primitive
        with attacker-controlled content. Arbitrary export targets belong
        in an explicit user-driven skill (future tg-export).
        """
        import secrets

        from .paths import RUNTIME_DIR, ensure_safe_subdir

        # Validate (or create) DOWNLOADS_DIR every time. exist_ok=True alone
        # would silently use a pre-existing symlink, redirecting our write
        # outside the runtime dir. ensure_safe_subdir applies the same
        # lstat/owner/mode checks as RUNTIME_DIR itself.
        downloads_dir = ensure_safe_subdir(RUNTIME_DIR, "downloads")

        entity = await self.client.get_entity(chat)
        m = await self.client.get_messages(entity, ids=msg_id)
        if not m or not m.media:
            return None

        # Pick an extension from the file metadata, but reject anything
        # containing path separators or starting with a dot+component.
        ext = ""
        f = getattr(m, "file", None)
        if f is not None:
            raw = getattr(f, "ext", "") or ""
            if raw and "/" not in raw and "\\" not in raw and len(raw) <= 16:
                ext = raw if raw.startswith(".") else f".{raw}"

        filename = f"{msg_id}-{secrets.token_hex(4)}{ext}"
        full_path = downloads_dir / filename
        result = await self.client.download_media(m, file=str(full_path))
        return str(result) if result else None

    # ----- Write-side -----

    async def send_message(
        self,
        chat: str | int,
        text: str,
        *,
        reply_to: Optional[int] = None,
    ) -> int:
        entity = await self.client.get_entity(chat)
        m = await self.client.send_message(entity, text, reply_to=reply_to)
        return m.id

    async def edit_message(self, chat: str | int, msg_id: int, text: str) -> int:
        entity = await self.client.get_entity(chat)
        m = await self.client.edit_message(entity, msg_id, text)
        return m.id

    async def delete_messages(
        self,
        chat: str | int,
        msg_ids: list[int],
        *,
        revoke: bool = True,
    ) -> int:
        """Request deletion of `msg_ids` and return the number of ids we asked
        Telegram to delete.

        We deliberately do NOT derive a "messages deleted" count from
        Telethon's `AffectedMessages.pts_count`. That field is the size of the
        updates-state delta, not the per-message delete count, and treating
        it as such would silently misreport deletes. Telethon raises on RPC
        failure; absent an exception, the request was accepted by the server.
        Callers that need ground truth should read the chat back and confirm
        the messages are gone.

        `revoke=True` (default) ASKS Telegram to delete for everyone, matching
        the visible behaviour of the official client. Telegram does not
        guarantee global-revoke for every kind of message — incoming
        non-self messages and messages older than the per-chat
        delete-for-everyone window may only get a local delete. Treat
        `revoke=True` as best-effort, not a hard guarantee.

        `revoke=False` deletes only for the current account; copies remain in
        other participants' chats.
        """
        entity = await self.client.get_entity(chat)
        await self.client.delete_messages(entity, msg_ids, revoke=revoke)
        return len(msg_ids)

    async def forward_messages(
        self,
        from_chat: str | int,
        to_chat: str | int,
        msg_ids: list[int],
    ) -> list[int]:
        src = await self.client.get_entity(from_chat)
        dst = await self.client.get_entity(to_chat)
        forwarded = await self.client.forward_messages(dst, msg_ids, from_peer=src)
        if not isinstance(forwarded, list):
            forwarded = [forwarded]
        return [m.id for m in forwarded if m is not None]

    async def pin_message(
        self,
        chat: str | int,
        msg_id: int,
        *,
        notify: bool = True,
    ) -> bool:
        entity = await self.client.get_entity(chat)
        await self.client.pin_message(entity, msg_id, notify=notify)
        return True

    async def unpin_message(
        self,
        chat: str | int,
        msg_id: Optional[int] = None,
    ) -> bool:
        """Unpin a specific message, or all if msg_id is None."""
        entity = await self.client.get_entity(chat)
        await self.client.unpin_message(entity, msg_id)
        return True

    async def react(
        self,
        chat: str | int,
        msg_id: int,
        emoji: Optional[str],
    ) -> bool:
        """Add or remove an emoji reaction. Pass emoji=None to clear."""
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        entity = await self.client.get_entity(chat)
        reactions = [ReactionEmoji(emoticon=emoji)] if emoji else []
        await self.client(
            SendReactionRequest(peer=entity, msg_id=msg_id, reaction=reactions)
        )
        return True

    async def mark_as_read(self, chat: str | int) -> bool:
        entity = await self.client.get_entity(chat)
        await self.client.send_read_acknowledge(entity)
        return True

    # ----- Group/Channel admin -----

    async def create_group(
        self,
        title: str,
        users: list[str | int],
        *,
        megagroup: bool = False,
        broadcast: bool = False,
        about: str = "",
    ) -> dict[str, Any]:
        """Create a new chat. Three flavours:
          - Default: classic basic group (capped at 200 members).
          - megagroup=True: supergroup (unlimited members, message history).
          - broadcast=True: broadcast channel (one-way posting).

        Returns {id, kind, title}. The new chat is reachable via the
        returned `id` (Telethon-marked format).
        """
        if broadcast and megagroup:
            raise ValueError("megagroup and broadcast are mutually exclusive")

        entities = []
        unresolved: list[str | int] = []
        for u in users:
            try:
                entities.append(await self.client.get_entity(u))
            except Exception:
                unresolved.append(u)

        if not (megagroup or broadcast) and not entities:
            # Basic groups (`messages.createChat`) require invitees on
            # creation — Telegram returns UsersTooFewError otherwise. Fail
            # early with a useful message instead of leaking that RPC error.
            raise ValueError(
                "basic group creation requires at least one resolvable invitee; "
                f"unresolved: {unresolved!r}. Use --megagroup if you want to "
                "create an empty supergroup and add members later."
            )

        if megagroup or broadcast:
            from telethon.tl.functions.channels import CreateChannelRequest

            result = await self.client(
                CreateChannelRequest(
                    title=title,
                    about=about,
                    megagroup=megagroup,
                    broadcast=broadcast,
                )
            )
            new_chat = result.chats[0]
            if entities and megagroup:
                from telethon.tl.functions.channels import InviteToChannelRequest

                try:
                    await self.client(
                        InviteToChannelRequest(channel=new_chat, users=entities)
                    )
                except Exception:
                    pass  # creation succeeded; partial invite is acceptable
            kind = "channel" if broadcast else "group"
        else:
            from telethon.tl.functions.messages import CreateChatRequest

            result = await self.client(CreateChatRequest(users=entities, title=title))
            # Basic group result has .updates.chats
            updates = result.updates if hasattr(result, "updates") else result
            new_chat = updates.chats[0]
            kind = "group"

        return {
            "id": await self.client.get_peer_id(new_chat),
            "raw_id": new_chat.id,
            "kind": kind,
            "title": getattr(new_chat, "title", title),
        }

    async def add_chat_member(self, chat: str | int, user: str | int) -> bool:
        from telethon.tl.functions.channels import InviteToChannelRequest
        from telethon.tl.functions.messages import AddChatUserRequest
        from telethon.tl.types import Channel

        chat_entity = await self.client.get_entity(chat)
        user_entity = await self.client.get_entity(user)
        if isinstance(chat_entity, Channel):
            await self.client(
                InviteToChannelRequest(channel=chat_entity, users=[user_entity])
            )
        else:
            await self.client(
                AddChatUserRequest(
                    chat_id=chat_entity.id, user_id=user_entity, fwd_limit=50
                )
            )
        return True

    async def kick_chat_member(self, chat: str | int, user: str | int) -> bool:
        """Kick a member. They CAN rejoin (unlike ban)."""
        chat_entity = await self.client.get_entity(chat)
        user_entity = await self.client.get_entity(user)
        await self.client.kick_participant(chat_entity, user_entity)
        return True

    async def ban_chat_member(self, chat: str | int, user: str | int) -> bool:
        """Ban a member from a channel/supergroup. They cannot rejoin."""
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        chat_entity = await self.client.get_entity(chat)
        user_entity = await self.client.get_entity(user)
        rights = ChatBannedRights(
            until_date=None,
            view_messages=True,
            send_messages=True,
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
        )
        await self.client(
            EditBannedRequest(channel=chat_entity, participant=user_entity, banned_rights=rights)
        )
        return True

    async def unban_chat_member(self, chat: str | int, user: str | int) -> bool:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        chat_entity = await self.client.get_entity(chat)
        user_entity = await self.client.get_entity(user)
        # All-False rights = restore default permissions (no ban).
        rights = ChatBannedRights(until_date=None)
        await self.client(
            EditBannedRequest(channel=chat_entity, participant=user_entity, banned_rights=rights)
        )
        return True

    async def create_invite_link(
        self,
        chat: str | int,
        *,
        expire_seconds: Optional[int] = None,
        usage_limit: Optional[int] = None,
    ) -> str:
        from telethon.tl.functions.messages import ExportChatInviteRequest

        chat_entity = await self.client.get_entity(chat)
        kwargs: dict[str, Any] = {"peer": chat_entity}
        if expire_seconds is not None:
            from datetime import datetime, timedelta, timezone

            kwargs["expire_date"] = datetime.now(timezone.utc) + timedelta(
                seconds=expire_seconds
            )
        if usage_limit is not None:
            kwargs["usage_limit"] = usage_limit
        result = await self.client(ExportChatInviteRequest(**kwargs))
        return result.link

    async def set_chat_title(self, chat: str | int, title: str) -> bool:
        from telethon.tl.functions.channels import EditTitleRequest as ChEditTitle
        from telethon.tl.functions.messages import EditChatTitleRequest as MsgEditTitle
        from telethon.tl.types import Channel

        chat_entity = await self.client.get_entity(chat)
        if isinstance(chat_entity, Channel):
            await self.client(ChEditTitle(channel=chat_entity, title=title))
        else:
            await self.client(MsgEditTitle(chat_id=chat_entity.id, title=title))
        return True

    async def leave_chat(self, chat: str | int) -> bool:
        from telethon.tl.functions.channels import LeaveChannelRequest
        from telethon.tl.functions.messages import DeleteChatUserRequest
        from telethon.tl.types import Channel

        chat_entity = await self.client.get_entity(chat)
        if isinstance(chat_entity, Channel):
            await self.client(LeaveChannelRequest(channel=chat_entity))
        else:
            me = await self.client.get_me()
            await self.client(
                DeleteChatUserRequest(chat_id=chat_entity.id, user_id=me)
            )
        return True

    # ----- Contacts -----

    async def add_contact(
        self,
        phone: str,
        first_name: str,
        last_name: str = "",
        *,
        add_phone_privacy_exception: bool = False,
    ) -> dict[str, Any]:
        """Add a phone contact. Phone must include country code (e.g. +14155552671)."""
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        result = await self.client(
            ImportContactsRequest(
                contacts=[
                    InputPhoneContact(
                        client_id=0,
                        phone=phone,
                        first_name=first_name,
                        last_name=last_name,
                    )
                ]
            )
        )
        await self._refresh_contacts()
        if not result.users:
            return {"imported": False, "phone": phone}
        u = result.users[0]
        return {
            "imported": True,
            "id": u.id,
            "username": getattr(u, "username", None),
            "phone": getattr(u, "phone", None),
        }

    async def delete_contact(self, user: str | int) -> bool:
        from telethon.tl.functions.contacts import DeleteContactsRequest

        user_entity = await self.client.get_entity(user)
        await self.client(DeleteContactsRequest(id=[user_entity]))
        await self._refresh_contacts()
        return True

    async def block_user(self, user: str | int) -> bool:
        from telethon.tl.functions.contacts import BlockRequest

        user_entity = await self.client.get_entity(user)
        await self.client(BlockRequest(id=user_entity))
        return True

    async def unblock_user(self, user: str | int) -> bool:
        from telethon.tl.functions.contacts import UnblockRequest

        user_entity = await self.client.get_entity(user)
        await self.client(UnblockRequest(id=user_entity))
        return True

    # ----- Stickers + GIFs (Phase 3 batch 3) -----

    async def get_saved_gifs(self) -> list[dict[str, Any]]:
        """User's saved GIFs (the heart-tabbed ones).

        Output shape matches what `send_gif` consumes — every entry
        includes the full (doc_id, access_hash, file_reference_hex)
        triple so the caller can list-then-send without an extra
        round-trip. Round-3 review caught the original output missing
        `file_reference_hex`, breaking the list→send flow.
        """
        from telethon.tl.functions.messages import GetSavedGifsRequest

        result = await self.client(GetSavedGifsRequest(hash=0))
        gifs = getattr(result, "gifs", None) or []
        out = []
        for doc in gifs:
            ref = getattr(doc, "file_reference", None)
            out.append(
                {
                    "doc_id": doc.id,
                    "access_hash": doc.access_hash,
                    "file_reference_hex": ref.hex()
                    if isinstance(ref, (bytes, bytearray))
                    else None,
                    "mime_type": getattr(doc, "mime_type", None),
                }
            )
        return out

    async def send_gif(
        self, chat: str | int, doc_id: int, access_hash: int, file_reference_hex: str
    ) -> int:
        """Send a previously-found GIF to `chat` by document reference."""
        from telethon.tl.types import InputDocument

        try:
            file_ref = bytes.fromhex(file_reference_hex)
        except ValueError as e:
            raise ValueError(f"file_reference_hex is not valid hex: {e}") from e
        entity = await self.client.get_entity(chat)
        input_doc = InputDocument(
            id=doc_id, access_hash=access_hash, file_reference=file_ref
        )
        m = await self.client.send_file(entity, input_doc)
        return m.id

    async def get_saved_stickers(self) -> list[dict[str, Any]]:
        """List the user's installed sticker SETS (packs).

        These are PACK descriptors, not individual stickers. To get the
        sendable per-sticker triple (doc_id, access_hash, file_reference_hex)
        for a specific pack, pass the (set_id, access_hash) returned here
        to `get_sticker_set`.
        """
        from telethon.tl.functions.messages import GetAllStickersRequest

        result = await self.client(GetAllStickersRequest(hash=0))
        sets = getattr(result, "sets", None) or []
        out = []
        for s in sets:
            out.append(
                {
                    "set_id": s.id,
                    "access_hash": s.access_hash,
                    "title": getattr(s, "title", ""),
                    "short_name": getattr(s, "short_name", ""),
                    "count": getattr(s, "count", 0),
                }
            )
        return out

    async def get_sticker_set(
        self, set_id: int, access_hash: int
    ) -> list[dict[str, Any]]:
        """Resolve a sticker pack to its individual sticker documents,
        each with the (doc_id, access_hash, file_reference_hex) triple
        that `send_sticker` consumes."""
        from telethon.tl.functions.messages import GetStickerSetRequest
        from telethon.tl.types import InputStickerSetID

        result = await self.client(
            GetStickerSetRequest(
                stickerset=InputStickerSetID(id=set_id, access_hash=access_hash),
                hash=0,
            )
        )
        docs = getattr(result, "documents", None) or []
        out = []
        for doc in docs:
            ref = getattr(doc, "file_reference", None)
            out.append(
                {
                    "doc_id": doc.id,
                    "access_hash": doc.access_hash,
                    "file_reference_hex": ref.hex()
                    if isinstance(ref, (bytes, bytearray))
                    else None,
                    "mime_type": getattr(doc, "mime_type", None),
                }
            )
        return out

    async def send_sticker(
        self, chat: str | int, doc_id: int, access_hash: int, file_reference_hex: str
    ) -> int:
        """Send a sticker by document reference (same shape as send_gif)."""
        from telethon.tl.types import InputDocument

        try:
            file_ref = bytes.fromhex(file_reference_hex)
        except ValueError as e:
            raise ValueError(f"file_reference_hex is not valid hex: {e}") from e
        entity = await self.client.get_entity(chat)
        input_doc = InputDocument(
            id=doc_id, access_hash=access_hash, file_reference=file_ref
        )
        m = await self.client.send_file(entity, input_doc)
        return m.id

    # ----- Channel admin (Phase 3) -----

    async def get_participants(
        self,
        chat: str | int,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
        filter_kind: str = "all",
    ) -> dict[str, Any]:
        """Paginated list of group/channel members.

        `filter_kind` ∈ {"all", "admins", "kicked", "banned", "bots",
        "search"}. Telegram's API requires admins-of-channels permission
        to enumerate members of large channels; smaller groups are
        accessible to any member.
        """
        from telethon.tl.types import (
            ChannelParticipantsAdmins,
            ChannelParticipantsBanned,
            ChannelParticipantsBots,
            ChannelParticipantsKicked,
            ChannelParticipantsSearch,
        )

        filter_obj = None
        if filter_kind == "admins":
            filter_obj = ChannelParticipantsAdmins()
        elif filter_kind == "kicked":
            filter_obj = ChannelParticipantsKicked(q="")
        elif filter_kind == "banned":
            filter_obj = ChannelParticipantsBanned(q="")
        elif filter_kind == "bots":
            filter_obj = ChannelParticipantsBots()
        elif filter_kind == "search":
            filter_obj = ChannelParticipantsSearch(q=search)
        elif filter_kind != "all":
            raise ValueError(
                f"unknown filter_kind {filter_kind!r}; valid: all/admins/kicked/banned/bots/search"
            )

        from telethon.tl.types import (
            ChannelParticipantAdmin,
            ChannelParticipantCreator,
            ChatParticipantAdmin,
            ChatParticipantCreator,
        )

        admin_types = (
            ChannelParticipantAdmin,
            ChannelParticipantCreator,
            ChatParticipantAdmin,
            ChatParticipantCreator,
        )

        entity = await self.client.get_entity(chat)
        users = []
        async for u in self.client.iter_participants(
            entity, limit=limit, offset=offset, filter=filter_obj, search=search or None
        ):
            participant = getattr(u, "participant", None)
            users.append(
                {
                    "id": u.id,
                    "username": getattr(u, "username", None),
                    "first_name": getattr(u, "first_name", None),
                    "last_name": getattr(u, "last_name", None),
                    "is_bot": getattr(u, "bot", False),
                    # `isinstance` covers both Channel* (megagroups/channels)
                    # and Chat* (classic basic groups) admin/creator types.
                    # The previous string-name check missed Chat* and silently
                    # mis-reported every admin in a basic group as non-admin.
                    "is_admin": isinstance(participant, admin_types),
                }
            )
        return {"users": users, "total_returned": len(users)}

    async def channel_set_signatures(self, chat: str | int, enabled: bool) -> bool:
        """Toggle "Sign messages with author name" on a broadcast channel.

        Telethon's `ToggleSignaturesRequest` keyword is `signatures_enabled`
        (NOT `enabled` — passing the wrong name `TypeError`s before any
        RPC).
        """
        from telethon.tl.functions.channels import ToggleSignaturesRequest

        entity = await self.client.get_entity(chat)
        await self.client(
            ToggleSignaturesRequest(channel=entity, signatures_enabled=enabled)
        )
        return True

    async def channel_set_slow_mode(self, chat: str | int, seconds: int) -> bool:
        """Set per-user slow mode on a megagroup. 0 disables, valid
        non-zero values are 10, 30, 60, 300, 900, 3600."""
        from telethon.tl.functions.channels import ToggleSlowModeRequest

        ALLOWED = {0, 10, 30, 60, 300, 900, 3600}
        if seconds not in ALLOWED:
            raise ValueError(
                f"slow_mode seconds must be one of {sorted(ALLOWED)}; got {seconds}"
            )
        entity = await self.client.get_entity(chat)
        await self.client(ToggleSlowModeRequest(channel=entity, seconds=seconds))
        return True

    async def channel_set_discussion(
        self,
        broadcast: str | int,
        group: Optional[str | int],
    ) -> bool:
        """Bind a discussion megagroup to a broadcast channel, or unbind
        when `group` is None."""
        from telethon.tl.functions.channels import SetDiscussionGroupRequest
        from telethon.tl.types import InputChannelEmpty

        bcast = await self.client.get_input_entity(broadcast)
        grp = (
            await self.client.get_input_entity(group)
            if group is not None
            else InputChannelEmpty()
        )
        await self.client(SetDiscussionGroupRequest(broadcast=bcast, group=grp))
        return True

    async def channel_admin_log(
        self,
        chat: str | int,
        *,
        limit: int = 50,
        search: str = "",
    ) -> list[dict[str, Any]]:
        """Read the channel/megagroup admin recent-events log. The user
        must be an admin of the chat or this raises CHAT_ADMIN_REQUIRED."""
        entity = await self.client.get_entity(chat)
        out = []
        async for ev in self.client.iter_admin_log(
            entity, limit=limit, search=search or None
        ):
            out.append(
                {
                    "id": ev.id,
                    "date": ev.date.astimezone(timezone.utc).isoformat()
                    if ev.date
                    else None,
                    "user_id": ev.user_id,
                    "action_kind": type(ev.action).__name__ if ev.action else None,
                }
            )
        return out

    # ----- Privacy settings -----

    @staticmethod
    def _privacy_key(name: str):
        from telethon.tl.types import (
            InputPrivacyKeyAbout,
            InputPrivacyKeyAddedByPhone,
            InputPrivacyKeyChatInvite,
            InputPrivacyKeyForwards,
            InputPrivacyKeyPhoneCall,
            InputPrivacyKeyPhoneNumber,
            InputPrivacyKeyPhoneP2P,
            InputPrivacyKeyProfilePhoto,
            InputPrivacyKeyStatusTimestamp,
            InputPrivacyKeyVoiceMessages,
        )

        mapping = {
            "status": InputPrivacyKeyStatusTimestamp,
            "photo": InputPrivacyKeyProfilePhoto,
            "calls": InputPrivacyKeyPhoneCall,
            "forwards": InputPrivacyKeyForwards,
            "chat_invite": InputPrivacyKeyChatInvite,
            "phone": InputPrivacyKeyPhoneNumber,
            "added_by_phone": InputPrivacyKeyAddedByPhone,
            "voice": InputPrivacyKeyVoiceMessages,
            "about": InputPrivacyKeyAbout,
            "p2p": InputPrivacyKeyPhoneP2P,
        }
        if name not in mapping:
            raise ValueError(
                f"unknown privacy key {name!r}; valid: {sorted(mapping.keys())}"
            )
        return mapping[name]()

    async def _resolve_privacy_rules(self, rules: list[dict]) -> list:
        """Convert friendly rule dicts into Telethon InputPrivacyRule* objects.

        Each rule is `{"kind": "...", "user_ids": [...optional...]}`.
        Order matters: rules are evaluated top-down by Telegram.
        """
        from telethon.tl.types import (
            InputPrivacyValueAllowAll,
            InputPrivacyValueAllowContacts,
            InputPrivacyValueAllowUsers,
            InputPrivacyValueDisallowAll,
            InputPrivacyValueDisallowContacts,
            InputPrivacyValueDisallowUsers,
        )

        out = []
        for r in rules:
            kind = r.get("kind")
            if kind == "allow_all":
                out.append(InputPrivacyValueAllowAll())
            elif kind == "disallow_all":
                out.append(InputPrivacyValueDisallowAll())
            elif kind == "allow_contacts":
                out.append(InputPrivacyValueAllowContacts())
            elif kind == "disallow_contacts":
                out.append(InputPrivacyValueDisallowContacts())
            elif kind in ("allow_users", "disallow_users"):
                user_ids = r.get("user_ids") or []
                input_users = []
                for uid in user_ids:
                    e = await self.client.get_input_entity(uid)
                    input_users.append(e)
                if kind == "allow_users":
                    out.append(InputPrivacyValueAllowUsers(users=input_users))
                else:
                    out.append(InputPrivacyValueDisallowUsers(users=input_users))
            else:
                raise ValueError(f"unknown rule kind {kind!r}")
        return out

    async def get_privacy(self, key: str) -> dict[str, Any]:
        from telethon.tl.functions.account import GetPrivacyRequest

        result = await self.client(GetPrivacyRequest(key=self._privacy_key(key)))
        rules = []
        for r in result.rules:
            cls_name = type(r).__name__
            rules.append(
                {
                    "kind": cls_name,
                    "user_ids": [u.user_id for u in getattr(r, "users", []) or []],
                }
            )
        return {"key": key, "rules": rules}

    async def set_privacy(self, key: str, rules: list[dict]) -> dict[str, Any]:
        from telethon.tl.functions.account import SetPrivacyRequest

        input_rules = await self._resolve_privacy_rules(rules)
        await self.client(
            SetPrivacyRequest(key=self._privacy_key(key), rules=input_rules)
        )
        return await self.get_privacy(key)

    # ----- Folders (dialog filters) -----

    async def list_folders(self) -> list[dict[str, Any]]:
        from telethon.tl.functions.messages import GetDialogFiltersRequest

        result = await self.client(GetDialogFiltersRequest())
        # Telethon ≥1.36 returns DialogFilters wrapper; older returns list.
        filters = getattr(result, "filters", result)
        out = []
        for f in filters:
            if hasattr(f, "id") and getattr(f, "title", None) is not None:
                out.append(
                    {
                        "id": f.id,
                        "title": f.title,
                        "include_count": len(getattr(f, "include_peers", []) or []),
                        "exclude_count": len(getattr(f, "exclude_peers", []) or []),
                        "pinned_count": len(getattr(f, "pinned_peers", []) or []),
                        "contacts": getattr(f, "contacts", False),
                        "non_contacts": getattr(f, "non_contacts", False),
                        "groups": getattr(f, "groups", False),
                        "broadcasts": getattr(f, "broadcasts", False),
                        "bots": getattr(f, "bots", False),
                    }
                )
        return out

    async def update_folder(
        self,
        folder_id: int,
        *,
        title: str,
        include_peers: Optional[list[str | int]] = None,
        exclude_peers: Optional[list[str | int]] = None,
        contacts: bool = False,
        non_contacts: bool = False,
        groups: bool = False,
        broadcasts: bool = False,
        bots: bool = False,
    ) -> dict[str, Any]:
        """Create or update a folder (dialog filter). Same RPC for both —
        a new id creates; an existing id replaces."""
        from telethon.tl.functions.messages import UpdateDialogFilterRequest
        from telethon.tl.types import DialogFilter

        async def _resolve(items):
            return [
                await self.client.get_input_entity(p)
                for p in (items or [])
            ]

        f = DialogFilter(
            id=folder_id,
            title=title,
            pinned_peers=[],
            include_peers=await _resolve(include_peers),
            exclude_peers=await _resolve(exclude_peers),
            contacts=contacts,
            non_contacts=non_contacts,
            groups=groups,
            broadcasts=broadcasts,
            bots=bots,
            exclude_muted=False,
            exclude_read=False,
            exclude_archived=False,
        )
        await self.client(UpdateDialogFilterRequest(id=folder_id, filter=f))
        return {"id": folder_id, "title": title}

    async def delete_folder(self, folder_id: int) -> bool:
        from telethon.tl.functions.messages import UpdateDialogFilterRequest

        # Passing filter=None tells Telegram to delete the filter id.
        await self.client(UpdateDialogFilterRequest(id=folder_id, filter=None))
        return True

    # ----- Chat export -----

    async def export_chat(
        self,
        chat: str | int,
        out_dir: str,
        out_dir_fd: int,
        *,
        limit: int = 1000,
        include_media: bool = False,
        since_date: Optional[datetime] = None,
        until_date: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Export `chat`'s history to `<out_dir>/chat_<peer_id>/`.

        TOCTOU note: every filesystem operation is performed RELATIVE TO
        an fd (`dir_fd=` kwargs and openat-style flags). The caller has
        already validated `out_dir` and opened it as `out_dir_fd`; we
        never re-resolve `out_dir` by path here. `chat_<id>` and
        `media/` are mkdir'd via dir_fd so a symlink swap of the parent
        cannot redirect us. Pre-existing children must be real
        directories owned by us — symlinked children with the same name
        are rejected via O_NOFOLLOW open + fstat.

        messages.json is written through a dir_fd-relative O_CREAT|
        O_EXCL|O_NOFOLLOW open. Each media download streams into a file
        opened the same way and passed to Telethon as a file object,
        so download_media never reopens by path either.
        """
        import json
        import os
        import secrets
        import stat as _stat

        def _open_subdir(name: str, *, parent_fd: int) -> int:
            """Create or open a child directory safely under parent_fd.

            We don't trust O_NOFOLLOW alone for the "pre-existing
            symlink" detection because errno varies by OS (ELOOP on
            Linux, ENOTDIR on macOS for symlink→dir under O_NOFOLLOW).
            Instead, fstatat the name first via `os.stat(..., dir_fd=,
            follow_symlinks=False)`:
              - If absent: mkdir atomically.
              - If present as symlink/non-dir/foreign-owned: refuse.
              - If present as our real dir: continue.
            Then open the now-known-good name with O_NOFOLLOW for fd
            ownership.
            """
            try:
                pre = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pre = None
            except OSError as e:
                raise RuntimeError(f"stat {name!r} failed: {e}") from e

            if pre is None:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except OSError as e:
                    raise RuntimeError(f"mkdir {name!r} failed: {e}") from e
            else:
                if _stat.S_ISLNK(pre.st_mode):
                    raise RuntimeError(
                        f"refusing: {name!r} is a symlink under the export root"
                    )
                if not _stat.S_ISDIR(pre.st_mode):
                    raise RuntimeError(
                        f"refusing: {name!r} exists and is not a directory"
                    )
                if pre.st_uid != os.getuid():
                    raise RuntimeError(
                        f"refusing: {name!r} is not owned by us"
                    )

            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(name, flags, dir_fd=parent_fd)
            info = os.fstat(fd)
            if not _stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                os.close(fd)
                raise RuntimeError(
                    f"refusing: {name!r} is not a real directory owned by us"
                )
            return fd

        entity = await self.client.get_entity(chat)
        chat_id = await self.client.get_peer_id(entity)
        chat_subname = f"chat_{chat_id}"

        chat_dir_fd = _open_subdir(chat_subname, parent_fd=out_dir_fd)
        media_dir_fd: Optional[int] = None
        try:
            if include_media:
                media_dir_fd = _open_subdir("media", parent_fd=chat_dir_fd)

            messages: list[dict[str, Any]] = []
            media_count = 0
            async for m in self.client.iter_messages(
                entity, limit=limit, offset_date=until_date
            ):
                if since_date and m.date and m.date < since_date:
                    break
                text = m.message or ""
                rec: dict[str, Any] = {
                    "id": m.id,
                    "date": m.date.astimezone(timezone.utc).isoformat()
                    if m.date
                    else None,
                    "sender_id": m.sender_id,
                    "text": text,
                    "reply_to_msg_id": m.reply_to_msg_id,
                    "has_media": m.media is not None,
                }
                if include_media and m.media and media_dir_fd is not None:
                    ext = ""
                    fmeta = getattr(m, "file", None)
                    if fmeta is not None:
                        raw = getattr(fmeta, "ext", "") or ""
                        if raw and "/" not in raw and "\\" not in raw and len(raw) <= 16:
                            ext = raw if raw.startswith(".") else f".{raw}"
                    fname = f"{m.id}-{secrets.token_hex(4)}{ext}"
                    flags = (
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                    )
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    media_fd = os.open(fname, flags, 0o600, dir_fd=media_dir_fd)
                    media_file_obj = os.fdopen(media_fd, "wb")
                    try:
                        downloaded = await self.client.download_media(
                            m, file=media_file_obj
                        )
                    finally:
                        try:
                            media_file_obj.close()
                        except Exception:
                            pass
                    if downloaded:
                        rec["media_file"] = f"media/{fname}"
                        media_count += 1
                messages.append(rec)

            chat_meta = {
                "id": chat_id,
                "kind": _entity_kind(entity),
                "title": getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or "",
                "username": getattr(entity, "username", None),
            }
            payload = {
                "chat": chat_meta,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "message_count": len(messages),
                "media_count": media_count,
                "messages": messages,
            }

            # messages.json: openat-style with O_EXCL so a pre-existing file
            # at that name (e.g. someone planted one between our chat_<id>
            # mkdir and now, or a prior failed export) makes the open fail
            # outright. We never silently overwrite an export target.
            json_flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            )
            if hasattr(os, "O_NOFOLLOW"):
                json_flags |= os.O_NOFOLLOW
            try:
                json_fd = os.open(
                    "messages.json", json_flags, 0o600, dir_fd=chat_dir_fd
                )
            except FileExistsError as e:
                raise RuntimeError(
                    "refusing: chat_<id>/messages.json already exists. "
                    "Move the previous export aside before re-running."
                ) from e
            with os.fdopen(json_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        finally:
            if media_dir_fd is not None:
                try:
                    os.close(media_dir_fd)
                except OSError:
                    pass
            try:
                os.close(chat_dir_fd)
            except OSError:
                pass

        # The returned paths are informational; we don't re-use them for I/O.
        chat_dir_path = os.path.join(out_dir, chat_subname)
        return {
            "out_dir": chat_dir_path,
            "json_file": os.path.join(chat_dir_path, "messages.json"),
            "message_count": len(messages),
            "media_count": media_count,
        }

    # ----- Profile -----

    async def update_profile(
        self,
        *,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        about: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update display name and/or bio. Pass None to leave a field unchanged.

        Bio (`about`) is only present on UserFull, not the bare User
        Telethon's `get_me()` returns, so we issue a follow-up
        GetFullUserRequest to read back the canonical post-update value.
        """
        from telethon.tl.functions.account import UpdateProfileRequest
        from telethon.tl.functions.users import GetFullUserRequest

        await self.client(
            UpdateProfileRequest(
                first_name=first_name, last_name=last_name, about=about
            )
        )
        me = await self.client.get_me()
        new_about: Optional[str] = None
        try:
            full = await self.client(GetFullUserRequest(me))
            # Telethon ≥1.30 returns UserFull with .full_user.about
            new_about = getattr(getattr(full, "full_user", full), "about", None)
        except Exception:
            new_about = None
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "about": new_about,
        }

    async def change_2fa_password(
        self,
        *,
        current_password: Optional[str],
        new_password: Optional[str],
        hint: str = "",
        email: Optional[str] = None,
    ) -> dict[str, Any]:
        """Set, change, or remove the cloud-password (two-factor auth).

        - To ENABLE 2FA on an account that has none:
            current_password=None, new_password=<new>
        - To CHANGE an existing password:
            current_password=<old>, new_password=<new>
        - To REMOVE 2FA entirely:
            current_password=<old>, new_password=None

        Telethon exposes this as `client.edit_2fa(current_password,
        new_password, hint, email)`.
        """
        if not current_password and not new_password:
            raise ValueError(
                "edit_2fa needs at least one of current_password / new_password"
            )
        try:
            ok = await self.client.edit_2fa(
                current_password=current_password,
                new_password=new_password,
                hint=hint or None,
                email=email or None,
            )
        finally:
            # Wipe local references immediately. CPython doesn't promise
            # the strings are zeroed, but at least drop our handle so they
            # don't outlive the call.
            current_password = None
            new_password = None
        return {"ok": bool(ok)}

    async def update_username(self, username: str) -> dict[str, Any]:
        """Set or clear the public @username. Pass empty string to clear."""
        from telethon.tl.functions.account import UpdateUsernameRequest

        await self.client(UpdateUsernameRequest(username=username))
        me = await self.client.get_me()
        return {"id": me.id, "username": me.username}

    async def set_profile_photo(self, file: Any) -> dict[str, Any]:
        """Upload a new profile photo. `file` is a file-like object opened
        through _open_validated_upload (same TOCTOU defenses as media)."""
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        # Telethon's upload_file handles the multi-part upload chunking.
        uploaded = await self.client.upload_file(file)
        result = await self.client(UploadProfilePhotoRequest(file=uploaded))
        # The new photo id is in result.photo.id (Telethon Photo).
        return {"photo_id": getattr(result.photo, "id", None)}

    async def delete_current_profile_photo(self) -> bool:
        """Remove the active (most recent) profile photo.

        Telethon doesn't expose a `get_full_user` shortcut on the client,
        and DeletePhotosRequest requires InputPhoto, not the bare Photo
        object. We fetch the most-recent profile photo via the public
        helper and convert it via telethon.utils.get_input_photo, which
        is the documented bridge between read-side Photo and write-side
        InputPhoto.
        """
        from telethon.tl.functions.photos import DeletePhotosRequest
        from telethon.utils import get_input_photo

        photos = await self.client.get_profile_photos("me", limit=1)
        if not photos:
            return False
        input_photo = get_input_photo(photos[0])
        await self.client(DeletePhotosRequest(id=[input_photo]))
        return True

    async def set_online_status(self, online: bool) -> bool:
        from telethon.tl.functions.account import UpdateStatusRequest

        # Telethon's flag is `offline`, opposite of our parameter.
        await self.client(UpdateStatusRequest(offline=not online))
        return True

    # ----- Scheduled messages + Drafts -----

    async def send_scheduled(
        self,
        chat: str | int,
        text: str,
        schedule_date: datetime,
        *,
        reply_to: Optional[int] = None,
    ) -> int:
        """Schedule `text` for delivery at `schedule_date` (must be UTC-aware).

        Telegram requires the schedule to be at least a few seconds in the
        future and at most ~365 days out. We let Telethon raise the
        upstream error for boundary cases; the daemon's schema layer
        catches obviously-bad inputs (past timestamps).
        """
        entity = await self.client.get_entity(chat)
        m = await self.client.send_message(
            entity, text, schedule=schedule_date, reply_to=reply_to
        )
        return m.id

    async def edit_scheduled(
        self,
        chat: str | int,
        msg_id: int,
        *,
        text: Optional[str] = None,
        schedule_date: Optional[datetime] = None,
    ) -> int:
        """Change the text and/or send time of a queued scheduled message.

        Pass `text=None` to leave the body unchanged; pass
        `schedule_date=None` to leave the timestamp unchanged. At least
        one of the two must be provided.

        Telethon edits a scheduled message via `client.edit_message`
        with `schedule=` — the same path it uses for any edit.
        """
        if text is None and schedule_date is None:
            raise ValueError("edit_scheduled needs at least one of text/schedule_date")
        entity = await self.client.get_entity(chat)
        m = await self.client.edit_message(
            entity, msg_id, text=text, schedule=schedule_date
        )
        return m.id if m else msg_id

    async def list_scheduled(
        self, chat: str | int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        entity = await self.client.get_entity(chat)
        out: list[dict[str, Any]] = []
        async for m in self.client.iter_messages(entity, limit=limit, scheduled=True):
            out.append(
                {
                    "id": m.id,
                    "scheduled_for": m.date.astimezone(timezone.utc).isoformat() if m.date else None,
                    "text": m.message or "",
                    "has_media": m.media is not None,
                    "reply_to_msg_id": m.reply_to_msg_id,
                }
            )
        return out

    async def delete_scheduled(self, chat: str | int, msg_ids: list[int]) -> int:
        """Cancel scheduled messages. Telethon's `scheduled=True` routes via
        the messages.deleteScheduledMessages RPC, which is the right kind
        of delete for the scheduled queue (regular delete_messages would
        not see them)."""
        entity = await self.client.get_entity(chat)
        await self.client.delete_messages(entity, msg_ids, scheduled=True)
        return len(msg_ids)

    async def save_draft(
        self,
        chat: str | int,
        text: str,
        *,
        reply_to: Optional[int] = None,
    ) -> bool:
        from telethon.tl.functions.messages import SaveDraftRequest

        entity = await self.client.get_entity(chat)
        await self.client(
            SaveDraftRequest(peer=entity, message=text, reply_to_msg_id=reply_to)
        )
        return True

    async def get_draft(self, chat: str | int) -> Optional[dict[str, Any]]:
        """Return the saved draft for `chat`, or None if none is set.

        `iter_drafts()` returns placeholder DraftMessage objects for chats
        the user has merely opened — empty `text` AND no `reply_to_msg_id`
        means there's no real draft. Treating those as "None" matches the
        official client's UX (an empty draft isn't shown in the chat list).
        """
        entity = await self.client.get_entity(chat)
        target_peer_id = await self.client.get_peer_id(entity)
        async for draft in self.client.iter_drafts():
            try:
                draft_peer_id = await self.client.get_peer_id(draft.entity)
            except Exception:
                continue
            if draft_peer_id != target_peer_id:
                continue

            text = draft.text or ""
            reply_to = getattr(draft, "reply_to_msg_id", None)
            if not text and not reply_to:
                # Placeholder for an opened-but-empty chat — not a real draft.
                return None
            return {
                "text": text,
                "date": draft.date.astimezone(timezone.utc).isoformat()
                if draft.date
                else None,
                "reply_to_msg_id": reply_to,
            }
        return None

    async def clear_draft(self, chat: str | int) -> bool:
        """Clearing is just SaveDraft with an empty message."""
        from telethon.tl.functions.messages import SaveDraftRequest

        entity = await self.client.get_entity(chat)
        await self.client(SaveDraftRequest(peer=entity, message=""))
        return True

    # ----- Polls -----

    async def create_poll(
        self,
        chat: str | int,
        question: str,
        options: list[str],
        *,
        anonymous: bool = True,
        multiple_choice: bool = False,
        quiz: bool = False,
        correct_option: Optional[int] = None,
        explanation: str = "",
    ) -> int:
        """Send a poll to `chat`. Returns the message id of the poll.

        - `anonymous=False` reveals voters' identities to the chat.
        - `quiz=True` makes a single-correct-answer quiz; `correct_option`
          is the 0-based index of the right answer and is required.
          `multiple_choice` is incompatible with `quiz`.
        - `explanation` shows after a quiz answer is selected (max 200 chars
          per Telegram docs).
        """
        from telethon.tl.types import (
            InputMediaPoll,
            Poll,
            PollAnswer,
        )

        if quiz:
            if multiple_choice:
                raise ValueError("quiz polls cannot be multiple_choice")
            if correct_option is None:
                raise ValueError("quiz polls require correct_option")
            if not (0 <= correct_option < len(options)):
                raise ValueError(
                    f"correct_option {correct_option} out of range for {len(options)} options"
                )

        # Telethon expects answer.option to be unique short bytes used as
        # internal identifiers. Use the index, encoded as bytes.
        answers = [
            PollAnswer(text=text, option=bytes([i])) for i, text in enumerate(options)
        ]
        poll = Poll(
            id=0,
            question=question,
            answers=answers,
            closed=False,
            public_voters=not anonymous,
            multiple_choice=multiple_choice,
            quiz=quiz,
        )
        media = InputMediaPoll(
            poll=poll,
            correct_answers=[bytes([correct_option])] if quiz and correct_option is not None else None,
            solution=explanation if quiz and explanation else None,
            solution_entities=[] if quiz and explanation else None,
        )

        entity = await self.client.get_entity(chat)
        m = await self.client.send_file(entity, media)
        return m.id

    async def edit_poll(
        self,
        chat: str | int,
        msg_id: int,
        *,
        question: Optional[str] = None,
        options: Optional[list[str]] = None,
    ) -> bool:
        """Edit an existing poll's question and/or answer texts.

        Telegram lets you change the human-readable text but NOT the
        opaque option bytes — the option count must stay the same as the
        original (changing it would invalidate every existing vote).

        Pass `question=None` to keep the existing question; pass
        `options=None` to keep the existing options.
        """
        import copy as _copy

        from telethon.tl.functions.messages import EditMessageRequest
        from telethon.tl.types import InputMediaPoll, PollAnswer

        entity = await self.client.get_entity(chat)
        existing = await self.client.get_messages(entity, ids=msg_id)
        if not existing or not existing.poll:
            raise ValueError(f"message {msg_id} in {chat} is not a poll")

        # Quiz polls store `correct_answers` and `solution` on
        # InputMediaPoll alongside the Poll object — those fields are
        # NOT stored on the Poll itself. The original poll author knows
        # the correct answer, but Telegram doesn't echo it back via
        # GetMessages, so reconstructing it on edit would either be a
        # silent loss or require the caller to re-supply it. Refusing
        # quiz edits is the conservative path; the caller should
        # delete + recreate instead.
        if existing.poll.poll.quiz:
            raise ValueError(
                "editing quiz polls is not supported (correct_answers "
                "would be lost). Delete the poll and create a new one."
            )

        edited_poll = _copy.copy(existing.poll.poll)
        if question is not None:
            edited_poll.question = question
        if options is not None:
            if len(options) != len(edited_poll.answers):
                raise ValueError(
                    f"option count must match the existing poll "
                    f"({len(edited_poll.answers)}); got {len(options)}"
                )
            edited_poll.answers = [
                # Preserve each existing option's opaque bytes — votes
                # are tied to that, not to the displayed text.
                PollAnswer(text=text, option=ans.option)
                for text, ans in zip(options, edited_poll.answers)
            ]

        await self.client(
            EditMessageRequest(
                peer=entity,
                id=msg_id,
                media=InputMediaPoll(poll=edited_poll),
            )
        )
        return True

    async def close_poll(self, chat: str | int, msg_id: int) -> bool:
        """Close a previously-sent poll so no further votes are accepted.

        We preserve the entire original Poll object via shallow copy and
        only flip `closed=True`. A previous version reconstructed the
        Poll from a hand-picked subset of fields, which could silently
        drop optional metadata (e.g. close_period / close_date for
        timer-closed polls) on the round trip.
        """
        import copy as _copy

        from telethon.tl.functions.messages import EditMessageRequest
        from telethon.tl.types import InputMediaPoll

        entity = await self.client.get_entity(chat)
        existing = await self.client.get_messages(entity, ids=msg_id)
        if not existing or not existing.poll:
            raise ValueError(f"message {msg_id} in {chat} is not a poll")

        closed_poll = _copy.copy(existing.poll.poll)
        closed_poll.closed = True
        await self.client(
            EditMessageRequest(
                peer=entity,
                id=msg_id,
                media=InputMediaPoll(poll=closed_poll),
            )
        )
        return True

    async def poll_results(self, chat: str | int, msg_id: int) -> dict[str, Any]:
        """Read current poll standings."""
        entity = await self.client.get_entity(chat)
        m = await self.client.get_messages(entity, ids=msg_id)
        if not m or not m.poll:
            raise ValueError(f"message {msg_id} in {chat} is not a poll")

        poll = m.poll.poll
        results = m.poll.results

        # Build an explicit (option_bytes -> answer_index) map from the poll
        # itself. Don't assume option bytes are bytes([i]) — Telegram allows
        # arbitrary opaque IDs, and a poll authored by the official client
        # uses different bytes than ours. Looking up by exact bytes is the
        # only correct way to align result buckets to answer indices.
        option_to_index: dict[bytes, int] = {
            ans.option: i for i, ans in enumerate(poll.answers)
        }

        per_option_votes: dict[int, int] = {}
        if results and getattr(results, "results", None):
            for r in results.results:
                idx = option_to_index.get(r.option)
                if idx is not None:
                    per_option_votes[idx] = r.voters

        out = {
            "msg_id": msg_id,
            "question": poll.question,
            "closed": poll.closed,
            "anonymous": not poll.public_voters,
            "multiple_choice": poll.multiple_choice,
            "quiz": poll.quiz,
            "total_voters": getattr(results, "total_voters", 0) if results else 0,
            "options": [
                {
                    "index": i,
                    "text": ans.text,
                    "votes": per_option_votes.get(i, 0),
                }
                for i, ans in enumerate(poll.answers)
            ],
        }
        return out

    # ----- Media upload -----

    async def send_media(
        self,
        chat: str | int,
        file: Any,
        *,
        caption: str = "",
        reply_to: Optional[int] = None,
        as_voice: bool = False,
        force_document: bool = False,
        display_name: Optional[str] = None,
    ) -> int:
        """Upload a file and send it to `chat`.

        `file` is intentionally typed broadly: the daemon passes a
        validated, O_NOFOLLOW-opened binary file object so we never
        re-resolve the path after the validation check. Telethon's
        `send_file` accepts file-likes natively and uses
        `display_name` to set the attachment filename presented to
        recipients (we override Telethon's default — a generic blob
        name — with the basename we already validated).

        Returns the new message id.
        """
        from telethon.tl.types import DocumentAttributeFilename

        entity = await self.client.get_entity(chat)
        attributes = (
            [DocumentAttributeFilename(file_name=display_name)] if display_name else None
        )
        m = await self.client.send_file(
            entity,
            file,
            caption=caption or None,
            reply_to=reply_to,
            voice_note=as_voice,
            force_document=force_document,
            attributes=attributes,
        )
        if isinstance(m, list):
            return m[0].id if m else 0
        return m.id

    async def search_contacts(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search the user's saved contacts.

        Telegram's `contacts.search` returns BOTH the user's contacts AND
        global username matches, including bots. Under an endpoint named
        "contacts/search" it would be misleading (and a privacy footgun
        for a prompt-injected agent) to leak strangers and bots. We filter
        the result down to the user's actual contacts; for global search,
        the existing `tg_resolve_entity` MCP tool is the right path.
        """
        from telethon.tl.functions.contacts import SearchRequest

        result = await self.client(SearchRequest(q=query, limit=limit))
        out = []
        for u in result.users:
            if getattr(u, "bot", False):
                continue
            if u.id not in self.contact_ids:
                continue
            out.append(
                {
                    "id": u.id,
                    "username": getattr(u, "username", None),
                    "first_name": getattr(u, "first_name", None),
                    "last_name": getattr(u, "last_name", None),
                    "is_contact": True,
                }
            )
        return out


@asynccontextmanager
async def session_lifespan(cfg: TGConfig) -> AsyncIterator[TGSession]:
    sess = TGSession(cfg=cfg)
    await sess.start()
    try:
        yield sess
    finally:
        await sess.stop()
