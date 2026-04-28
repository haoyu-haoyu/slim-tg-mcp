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
