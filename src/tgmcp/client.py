"""Shared HTTP-over-Unix-socket client used by both the MCP server and skill scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .daemon.paths import SOCKET_PATH as DEFAULT_SOCKET  # noqa: F401


class DaemonClient:
    def __init__(self, socket_path: Path | str = DEFAULT_SOCKET, timeout: float = 30.0):
        self.socket_path = str(socket_path)
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url="http://daemon",
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post(path, json=payload)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict[str, Any]:
        r = self._client.get(path)
        r.raise_for_status()
        return r.json()

    # Convenience wrappers
    def get_metrics_text(self) -> str:
        """Fetch the /metrics endpoint as raw Prometheus exposition text.

        Public alternative to reaching into self._client. Returns the
        text body unchanged.
        """
        r = self._client.get("/metrics")
        r.raise_for_status()
        return r.text

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def search_global(self, query: str, limit: int = 30) -> dict[str, Any]:
        return self._post("/search/global", {"query": query, "limit": limit})

    def search_in_chat(self, chat: str | int, query: str | None = None, **kw: Any) -> dict[str, Any]:
        return self._post("/search/in_chat", {"chat": chat, "query": query, **kw})

    def list_dialogs(self, limit: int = 50) -> dict[str, Any]:
        return self._post("/list_dialogs", {"limit": limit})

    def get_messages(self, chat: str | int, limit: int = 50, offset_id: int = 0) -> dict[str, Any]:
        return self._post("/get_messages", {"chat": chat, "limit": limit, "offset_id": offset_id})

    def get_context(self, chat: str | int, msg_id: int, before: int = 5, after: int = 5) -> dict[str, Any]:
        return self._post(
            "/get_context",
            {"chat": chat, "msg_id": msg_id, "before": before, "after": after},
        )

    def resolve(self, query: str | int) -> dict[str, Any]:
        return self._post("/resolve", {"query": query})

    def chat_info(self, chat: str | int) -> dict[str, Any]:
        return self._post("/chat_info", {"chat": chat})

    def download(self, chat: str | int, msg_id: int) -> dict[str, Any]:
        return self._post("/download", {"chat": chat, "msg_id": msg_id})

    def send(self, chat: str | int, text: str, reply_to: int | None = None) -> dict[str, Any]:
        return self._post("/send", {"chat": chat, "text": text, "reply_to": reply_to})

    def edit(self, chat: str | int, msg_id: int, text: str) -> dict[str, Any]:
        return self._post("/edit", {"chat": chat, "msg_id": msg_id, "text": text})

    def delete(
        self,
        chat: str | int,
        msg_ids: list[int],
        revoke: bool = True,
    ) -> dict[str, Any]:
        return self._post(
            "/delete", {"chat": chat, "msg_ids": msg_ids, "revoke": revoke}
        )

    def forward(
        self,
        from_chat: str | int,
        to_chat: str | int,
        msg_ids: list[int],
    ) -> dict[str, Any]:
        return self._post(
            "/forward",
            {"from_chat": from_chat, "to_chat": to_chat, "msg_ids": msg_ids},
        )

    def pin(self, chat: str | int, msg_id: int, notify: bool = True) -> dict[str, Any]:
        return self._post("/pin", {"chat": chat, "msg_id": msg_id, "notify": notify})

    def unpin(self, chat: str | int, msg_id: int | None = None) -> dict[str, Any]:
        return self._post("/unpin", {"chat": chat, "msg_id": msg_id})

    def react(
        self,
        chat: str | int,
        msg_id: int,
        emoji: str | None,
        *,
        custom_emoji_id: int | None = None,
        big: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/react",
            {
                "chat": chat,
                "msg_id": msg_id,
                "emoji": emoji,
                "custom_emoji_id": custom_emoji_id,
                "big": big,
            },
        )

    def mark_read(self, chat: str | int) -> dict[str, Any]:
        return self._post("/mark_read", {"chat": chat})

    def shutdown(self, instance_id: str) -> dict[str, Any]:
        return self._post("/shutdown", {"instance_id": instance_id})

    def accounts(self) -> dict[str, Any]:
        return self._get("/accounts")

    def switch_account(self, label: str, passphrase: str | None = None) -> dict[str, Any]:
        return self._post(
            "/accounts/switch",
            {"label": label, "passphrase": passphrase},
        )

    # ----- group admin -----

    def chat_create(
        self,
        title: str,
        users: list[str | int] | None = None,
        *,
        megagroup: bool = False,
        broadcast: bool = False,
        about: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/chat/create",
            {
                "title": title,
                "users": users or [],
                "megagroup": megagroup,
                "broadcast": broadcast,
                "about": about,
            },
        )

    def chat_add_member(self, chat: str | int, user: str | int) -> dict[str, Any]:
        return self._post("/chat/add_member", {"chat": chat, "user": user})

    def chat_kick_member(self, chat: str | int, user: str | int) -> dict[str, Any]:
        return self._post("/chat/kick_member", {"chat": chat, "user": user})

    def chat_ban_member(self, chat: str | int, user: str | int) -> dict[str, Any]:
        return self._post("/chat/ban_member", {"chat": chat, "user": user})

    def chat_unban_member(self, chat: str | int, user: str | int) -> dict[str, Any]:
        return self._post("/chat/unban_member", {"chat": chat, "user": user})

    def chat_invite_link(
        self,
        chat: str | int,
        *,
        expire_seconds: int | None = None,
        usage_limit: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/chat/invite_link",
            {
                "chat": chat,
                "expire_seconds": expire_seconds,
                "usage_limit": usage_limit,
            },
        )

    def chat_set_title(self, chat: str | int, title: str) -> dict[str, Any]:
        return self._post("/chat/set_title", {"chat": chat, "title": title})

    def chat_leave(self, chat: str | int) -> dict[str, Any]:
        return self._post("/chat/leave", {"chat": chat})

    # ----- contacts -----

    def contact_add(
        self,
        phone: str,
        first_name: str,
        last_name: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/contacts/add",
            {"phone": phone, "first_name": first_name, "last_name": last_name},
        )

    def contact_delete(self, user: str | int) -> dict[str, Any]:
        return self._post("/contacts/delete", {"user": user})

    def contact_block(self, user: str | int) -> dict[str, Any]:
        return self._post("/contacts/block", {"user": user})

    def contact_unblock(self, user: str | int) -> dict[str, Any]:
        return self._post("/contacts/unblock", {"user": user})

    def contact_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        return self._post("/contacts/search", {"query": query, "limit": limit})

    # ----- stickers + gifs -----

    def gif_saved(self) -> dict[str, Any]:
        return self._get("/gif/saved")

    def gif_send(
        self,
        chat: str | int,
        doc_id: int,
        access_hash: int,
        file_reference_hex: str,
    ) -> dict[str, Any]:
        return self._post(
            "/gif/send",
            {
                "chat": chat,
                "doc_id": doc_id,
                "access_hash": access_hash,
                "file_reference_hex": file_reference_hex,
            },
        )

    def sticker_saved(self) -> dict[str, Any]:
        return self._get("/sticker/saved")

    def sticker_set(self, set_id: int, access_hash: int) -> dict[str, Any]:
        return self._post(
            "/sticker/set", {"set_id": set_id, "access_hash": access_hash}
        )

    def sticker_send(
        self,
        chat: str | int,
        doc_id: int,
        access_hash: int,
        file_reference_hex: str,
    ) -> dict[str, Any]:
        return self._post(
            "/sticker/send",
            {
                "chat": chat,
                "doc_id": doc_id,
                "access_hash": access_hash,
                "file_reference_hex": file_reference_hex,
            },
        )

    # ----- channel admin -----

    def chat_participants(
        self,
        chat: str | int,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
        filter_kind: str = "all",
    ) -> dict[str, Any]:
        return self._post(
            "/chat/participants",
            {
                "chat": chat,
                "limit": limit,
                "offset": offset,
                "search": search,
                "filter_kind": filter_kind,
            },
        )

    def chat_signatures(self, chat: str | int, enabled: bool) -> dict[str, Any]:
        return self._post("/chat/signatures", {"chat": chat, "enabled": enabled})

    def chat_slow_mode(self, chat: str | int, seconds: int) -> dict[str, Any]:
        return self._post("/chat/slow_mode", {"chat": chat, "seconds": seconds})

    def chat_discussion(
        self, broadcast: str | int, group: str | int | None
    ) -> dict[str, Any]:
        return self._post(
            "/chat/discussion", {"broadcast": broadcast, "group": group}
        )

    def chat_admin_log(
        self, chat: str | int, *, limit: int = 50, search: str = ""
    ) -> dict[str, Any]:
        return self._post(
            "/chat/admin_log",
            {"chat": chat, "limit": limit, "search": search},
        )

    # ----- privacy -----

    def privacy_get(self, key: str) -> dict[str, Any]:
        return self._post("/privacy/get", {"key": key})

    def privacy_set(self, key: str, rules: list[dict]) -> dict[str, Any]:
        return self._post("/privacy/set", {"key": key, "rules": rules})

    # ----- folders -----

    def folders_list(self) -> dict[str, Any]:
        return self._get("/folders/list")

    def folders_update(
        self,
        folder_id: int,
        *,
        title: str,
        include_peers: list[str | int] | None = None,
        exclude_peers: list[str | int] | None = None,
        contacts: bool = False,
        non_contacts: bool = False,
        groups: bool = False,
        broadcasts: bool = False,
        bots: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/folders/update",
            {
                "folder_id": folder_id,
                "title": title,
                "include_peers": include_peers or [],
                "exclude_peers": exclude_peers or [],
                "contacts": contacts,
                "non_contacts": non_contacts,
                "groups": groups,
                "broadcasts": broadcasts,
                "bots": bots,
            },
        )

    def folders_delete(self, folder_id: int) -> dict[str, Any]:
        return self._post("/folders/delete", {"folder_id": folder_id})

    # ----- export -----

    def export_chat(
        self,
        chat: str | int,
        out_dir: str,
        *,
        limit: int = 1000,
        include_media: bool = False,
        since_date: str | None = None,
        until_date: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/export/chat",
            {
                "chat": chat,
                "out_dir": out_dir,
                "limit": limit,
                "include_media": include_media,
                "since_date": since_date,
                "until_date": until_date,
            },
        )

    # ----- profile -----

    def profile_update(
        self,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        about: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/profile/update",
            {
                "first_name": first_name,
                "last_name": last_name,
                "about": about,
            },
        )

    def profile_username(self, username: str) -> dict[str, Any]:
        return self._post("/profile/username", {"username": username})

    def profile_2fa(
        self,
        *,
        current_password: str | None,
        new_password: str | None,
        hint: str = "",
        email: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/profile/2fa",
            {
                "current_password": current_password,
                "new_password": new_password,
                "hint": hint,
                "email": email,
            },
        )

    def profile_photo(self, file_path: str) -> dict[str, Any]:
        return self._post("/profile/photo", {"file_path": file_path})

    def profile_photo_delete(self) -> dict[str, Any]:
        return self._post("/profile/photo_delete", {})

    def profile_emoji_status(
        self,
        document_id: int | None = None,
        *,
        until_iso: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/profile/emoji_status",
            {"document_id": document_id, "until": until_iso},
        )

    def profile_status(self, online: bool) -> dict[str, Any]:
        return self._post("/profile/status", {"online": online})

    # ----- scheduling + drafts -----

    def scheduled_send(
        self,
        chat: str | int,
        text: str,
        schedule_date: str,  # ISO-8601 timezone-aware
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/scheduled/send",
            {
                "chat": chat,
                "text": text,
                "schedule_date": schedule_date,
                "reply_to": reply_to,
            },
        )

    def scheduled_edit(
        self,
        chat: str | int,
        msg_id: int,
        *,
        text: str | None = None,
        schedule_date: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/scheduled/edit",
            {
                "chat": chat,
                "msg_id": msg_id,
                "text": text,
                "schedule_date": schedule_date,
            },
        )

    def scheduled_list(self, chat: str | int, limit: int = 100) -> dict[str, Any]:
        return self._post("/scheduled/list", {"chat": chat, "limit": limit})

    def scheduled_delete(self, chat: str | int, msg_ids: list[int]) -> dict[str, Any]:
        return self._post(
            "/scheduled/delete", {"chat": chat, "msg_ids": msg_ids}
        )

    def draft_save(
        self, chat: str | int, text: str, reply_to: int | None = None
    ) -> dict[str, Any]:
        return self._post(
            "/draft/save", {"chat": chat, "text": text, "reply_to": reply_to}
        )

    def draft_get(self, chat: str | int) -> dict[str, Any]:
        return self._post("/draft/get", {"chat": chat})

    def draft_clear(self, chat: str | int) -> dict[str, Any]:
        return self._post("/draft/clear", {"chat": chat})

    # ----- polls -----

    def poll_create(
        self,
        chat: str | int,
        question: str,
        options: list[str],
        *,
        anonymous: bool = True,
        multiple_choice: bool = False,
        quiz: bool = False,
        correct_option: int | None = None,
        explanation: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/poll/create",
            {
                "chat": chat,
                "question": question,
                "options": options,
                "anonymous": anonymous,
                "multiple_choice": multiple_choice,
                "quiz": quiz,
                "correct_option": correct_option,
                "explanation": explanation,
            },
        )

    def poll_edit(
        self,
        chat: str | int,
        msg_id: int,
        *,
        question: str | None = None,
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/poll/edit",
            {
                "chat": chat,
                "msg_id": msg_id,
                "question": question,
                "options": options,
            },
        )

    def poll_close(self, chat: str | int, msg_id: int) -> dict[str, Any]:
        return self._post("/poll/close", {"chat": chat, "msg_id": msg_id})

    def poll_results(self, chat: str | int, msg_id: int) -> dict[str, Any]:
        return self._post("/poll/results", {"chat": chat, "msg_id": msg_id})

    # ----- media upload -----

    def send_media(
        self,
        chat: str | int,
        file_path: str,
        *,
        caption: str = "",
        reply_to: int | None = None,
        as_voice: bool = False,
        force_document: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/send_media",
            {
                "chat": chat,
                "file_path": file_path,
                "caption": caption,
                "reply_to": reply_to,
                "as_voice": as_voice,
                "force_document": force_document,
            },
        )

    # ----- Geo / live location -----

    def location_send(
        self,
        chat: str | int,
        lat: float,
        lng: float,
        *,
        accuracy: int | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/location/send",
            {
                "chat": chat,
                "lat": lat,
                "lng": lng,
                "accuracy": accuracy,
                "reply_to": reply_to,
            },
        )

    def location_send_live(
        self,
        chat: str | int,
        lat: float,
        lng: float,
        period: int,
        *,
        accuracy: int | None = None,
        heading: int | None = None,
        proximity: int | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/location/send_live",
            {
                "chat": chat,
                "lat": lat,
                "lng": lng,
                "period": period,
                "accuracy": accuracy,
                "heading": heading,
                "proximity": proximity,
                "reply_to": reply_to,
            },
        )

    def location_edit_live(
        self,
        chat: str | int,
        msg_id: int,
        lat: float,
        lng: float,
        *,
        accuracy: int | None = None,
        heading: int | None = None,
        proximity: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/location/edit_live",
            {
                "chat": chat,
                "msg_id": msg_id,
                "lat": lat,
                "lng": lng,
                "accuracy": accuracy,
                "heading": heading,
                "proximity": proximity,
            },
        )

    def location_stop_live(self, chat: str | int, msg_id: int) -> dict[str, Any]:
        return self._post(
            "/location/stop_live", {"chat": chat, "msg_id": msg_id}
        )

    # ----- Stories -----

    def stories_active(self, peer: str | int) -> dict[str, Any]:
        return self._post("/stories/active", {"peer": peer})

    def stories_pinned(
        self, peer: str | int, *, limit: int = 50, offset_id: int = 0
    ) -> dict[str, Any]:
        return self._post(
            "/stories/pinned",
            {"peer": peer, "limit": limit, "offset_id": offset_id},
        )

    def stories_mark_read(self, peer: str | int, max_id: int) -> dict[str, Any]:
        return self._post(
            "/stories/mark_read", {"peer": peer, "max_id": max_id}
        )

    def stories_delete(self, ids: list[int]) -> dict[str, Any]:
        return self._post("/stories/delete", {"ids": ids})

    # ----- Forum topics -----

    def topics_list(
        self, chat: str | int, *, limit: int = 100, query: str | None = None
    ) -> dict[str, Any]:
        return self._post(
            "/topics/list", {"chat": chat, "limit": limit, "query": query}
        )

    def topics_create(
        self,
        chat: str | int,
        title: str,
        *,
        icon_color: int | None = None,
        icon_emoji_id: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/topics/create",
            {
                "chat": chat,
                "title": title,
                "icon_color": icon_color,
                "icon_emoji_id": icon_emoji_id,
            },
        )

    def topics_edit(
        self,
        chat: str | int,
        topic_id: int,
        *,
        title: str | None = None,
        icon_emoji_id: int | None = None,
        closed: bool | None = None,
        hidden: bool | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/topics/edit",
            {
                "chat": chat,
                "topic_id": topic_id,
                "title": title,
                "icon_emoji_id": icon_emoji_id,
                "closed": closed,
                "hidden": hidden,
            },
        )

    def topics_delete(self, chat: str | int, topic_id: int) -> dict[str, Any]:
        return self._post(
            "/topics/delete", {"chat": chat, "topic_id": topic_id}
        )

    def topics_pin(
        self, chat: str | int, topic_id: int, pinned: bool
    ) -> dict[str, Any]:
        return self._post(
            "/topics/pin",
            {"chat": chat, "topic_id": topic_id, "pinned": pinned},
        )

    # ----- Bot mode -----

    def bot_send_keyboard(
        self,
        chat: str | int,
        text: str,
        rows: list[list[dict[str, Any]]],
        *,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/bot/send_keyboard",
            {"chat": chat, "text": text, "rows": rows, "reply_to": reply_to},
        )

    def bot_answer_callback(
        self,
        query_id: int,
        *,
        text: str = "",
        alert: bool = False,
        url: str | None = None,
        cache_time: int = 0,
    ) -> dict[str, Any]:
        return self._post(
            "/bot/answer_callback",
            {
                "query_id": query_id,
                "text": text,
                "alert": alert,
                "url": url,
                "cache_time": cache_time,
            },
        )

    def bot_poll_callbacks(
        self, *, timeout: float = 0.0, limit: int = 50
    ) -> dict[str, Any]:
        return self._post(
            "/bot/poll_callbacks", {"timeout": timeout, "limit": limit}
        )

    def bot_set_commands(
        self, commands: list[dict[str, str]], *, language_code: str = ""
    ) -> dict[str, Any]:
        return self._post(
            "/bot/set_commands",
            {"commands": commands, "language_code": language_code},
        )
