#!/usr/bin/env python3
"""Общие папки: ссылка-приглашение на набор чатов и слежение за ней.

Папку можно раздать ссылкой — принявший получит сразу несколько чатов. Дальше
она живёт своей жизнью: автор добавляет туда чаты, и у принявших появляются
«обновления папки», которые надо либо принять, либо скрыть. Отсюда три вещи,
которых нет у обычного приглашения.

Ссылку можно выписать не на всю папку, а на выбранные чаты из неё. И делится
только то, что вообще делимо: личные переписки в общую папку не отдаются —
сервер отвечает отказом, а не молча их пропускает.

Выход из папки не то же самое, что её удаление: `leave` покидает чаты, которые
пришли вместе с ней, и Telegram сперва подсказывает, какие именно.
"""
from __future__ import annotations

from typing import Any, Sequence


class FolderError(RuntimeError):
    """Действие с общей папкой, которое не удалось выполнить."""


class Folders:
    """Ссылки-приглашения на папки и их обновления."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        """Один проводник для всех вызовов: отказ сервера объясняется один раз,
        а не тринадцатью одинаковыми `try` подряд."""
        try:
            return await self.client(request)
        except Exception as exc:
            raise self._explain(exc) from exc

    @staticmethod
    def _ref(folder_id: int) -> Any:
        from telethon.tl import types
        return types.InputChatlistDialogFilter(filter_id=int(folder_id))

    @staticmethod
    def _slug(value: str) -> str:
        return value.rstrip("/").split("/")[-1].lstrip("+")

    async def _peers(self, peers: Sequence[Any]) -> list[Any]:
        return [await self.client.get_input_entity(p) for p in peers]

    # ── ссылки на свою папку ─────────────────────────────────────────────────
    async def invites(self, folder_id: int) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.chatlists.GetExportedInvitesRequest(
            chatlist=self._ref(folder_id)))
        return [{"название": getattr(i, "title", None), "чатов": len(getattr(i, "peers", None) or []),
                 "ссылка": getattr(i, "url", None)}
                for i in (getattr(result, "invites", None) or [])]

    async def share(self, folder_id: int, title: str,
                    peers: Sequence[Any] = ()) -> dict[str, Any]:
        """Выписать ссылку. Без списка чатов — на все делимые из папки."""
        from telethon.tl import functions

        chats = await self._peers(peers) or await self._shareable(folder_id)
        if not chats:
            raise FolderError("в этой папке нет чатов, которыми можно поделиться: "
                              "личные переписки в общую папку не отдаются")
        result = await self._call(functions.chatlists.ExportChatlistInviteRequest(
            chatlist=self._ref(folder_id), title=title.strip(), peers=chats))
        return {"ссылка": getattr(getattr(result, "invite", None), "url", None),
                "название": title.strip(), "чатов": len(chats)}

    async def _shareable(self, folder_id: int) -> list[Any]:
        """Чаты папки, которыми в принципе можно поделиться."""
        from telethon.tl import functions

        filters = await self.client(functions.messages.GetDialogFiltersRequest())
        for item in getattr(filters, "filters", None) or []:
            if getattr(item, "id", None) == int(folder_id):
                # Личные переписки Telegram делить не даёт.
                return [p for p in (getattr(item, "include_peers", None) or [])
                        if type(p).__name__ != "InputPeerUser"]
        raise FolderError(f"папки {folder_id} нет — список папок: tgx folders")

    async def edit_invite(self, folder_id: int, slug: str, *, title: str = "",
                          peers: Sequence[Any] = ()) -> dict[str, Any]:
        from telethon.tl import functions

        chats = await self._peers(peers)
        await self._call(functions.chatlists.EditExportedInviteRequest(
            chatlist=self._ref(folder_id), slug=self._slug(slug),
            title=title or None, peers=chats or None))
        return {"ссылка": slug, "название": title or "не менялось",
                "чатов": len(chats) or "не менялось"}

    async def revoke(self, folder_id: int, slug: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.chatlists.DeleteExportedInviteRequest(
            chatlist=self._ref(folder_id), slug=self._slug(slug)))
        return {"отозвана": slug}

    # ── чужая ссылка ─────────────────────────────────────────────────────────
    async def check(self, slug: str) -> dict[str, Any]:
        """Что внутри чужой ссылки — до того, как принять её."""
        from telethon.tl import functions

        result = await self._call(functions.chatlists.CheckChatlistInviteRequest(
            slug=self._slug(slug)))
        chats = getattr(result, "chats", None) or []
        return {"название": getattr(result, "title", None), "чатов": len(chats),
                "уже состою": len(getattr(result, "already_peers", None) or []),
                "названия": [getattr(c, "title", None) for c in chats][:12]}

    async def join(self, slug: str, peers: Sequence[Any] = ()) -> dict[str, Any]:
        """Принять папку. Без списка — все чаты, которых ещё нет."""
        from telethon.tl import functions

        preview = await self._call(functions.chatlists.CheckChatlistInviteRequest(
            slug=self._slug(slug)))
        chats = await self._peers(peers)
        if not chats:
            chats = await self._peers(getattr(preview, "chats", None) or [])
        await self._call(functions.chatlists.JoinChatlistInviteRequest(
            slug=self._slug(slug), peers=chats))
        return {"принято чатов": len(chats), "папка": getattr(preview, "title", None)}

    # ── обновления папки ─────────────────────────────────────────────────────
    async def updates(self, folder_id: int) -> dict[str, Any]:
        """Какие чаты автор папки добавил с прошлого раза."""
        from telethon.tl import functions

        result = await self._call(functions.chatlists.GetChatlistUpdatesRequest(
            chatlist=self._ref(folder_id)))
        return {"новых чатов": len(getattr(result, "missing_peers", None) or []),
                "названия": [getattr(c, "title", None)
                             for c in (getattr(result, "chats", None) or [])][:12]}

    async def accept_updates(self, folder_id: int, peers: Sequence[Any] = ()) -> dict[str, Any]:
        from telethon.tl import functions

        chats = await self._peers(peers)
        if not chats:
            pending = await self._call(functions.chatlists.GetChatlistUpdatesRequest(
                chatlist=self._ref(folder_id)))
            chats = list(getattr(pending, "missing_peers", None) or [])
        if not chats:
            return {"новых чатов": 0}
        await self._call(functions.chatlists.JoinChatlistUpdatesRequest(
            chatlist=self._ref(folder_id), peers=chats))
        return {"добавлено чатов": len(chats)}

    async def hide_updates(self, folder_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.chatlists.HideChatlistUpdatesRequest(
            chatlist=self._ref(folder_id)))
        return {"обновления скрыты": int(folder_id)}

    # ── выход ────────────────────────────────────────────────────────────────
    async def leave_suggestions(self, folder_id: int) -> dict[str, Any]:
        """Какие чаты Telegram советует покинуть вместе с папкой."""
        from telethon.tl import functions

        result = await self._call(functions.chatlists.GetLeaveChatlistSuggestionsRequest(
            chatlist=self._ref(folder_id)))
        return {"предлагается покинуть": len(result or [])}

    async def leave(self, folder_id: int, peers: Sequence[Any] = ()) -> dict[str, Any]:
        """Покинуть папку вместе с выбранными чатами. Без списка — только папка."""
        from telethon.tl import functions

        chats = await self._peers(peers)
        await self._call(functions.chatlists.LeaveChatlistRequest(
            chatlist=self._ref(folder_id), peers=chats))
        return {"папка": int(folder_id), "покинуто чатов": len(chats)}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        hints = {
            "FILTER_ID_INVALID": "папки с таким номером нет — список: tgx folders",
            "FILTER_NOT_SUPPORTED": "это обычная папка: поделиться можно только той, "
                                    "что создана как общая",
            "PEERS_LIST_EMPTY": "в ссылке должен быть хотя бы один чат; "
                                "личные переписки в общую папку не отдаются",
            "INVITE_SLUG_EXPIRED": "ссылка на папку больше не действует",
            "CHATLISTS_TOO_MUCH": "слишком много общих папок",
            "INVITES_TOO_MUCH": "на эту папку выписано слишком много ссылок",
        }
        import tgx_net

        return tgx_net.explain(exc, hints, FolderError)
        return exc
