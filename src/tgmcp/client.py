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
    ) -> dict[str, Any]:
        return self._post(
            "/react", {"chat": chat, "msg_id": msg_id, "emoji": emoji}
        )

    def mark_read(self, chat: str | int) -> dict[str, Any]:
        return self._post("/mark_read", {"chat": chat})

    def shutdown(self, instance_id: str) -> dict[str, Any]:
        return self._post("/shutdown", {"instance_id": instance_id})
