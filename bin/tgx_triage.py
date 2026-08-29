"""Разбор непрочитанного: где вас звали, кому вы понравились, кто дочитал.

В графическом Telegram это значки и кружки, которые видно боковым зрением.
В терминале ничего не видно боковым зрением, поэтому нужны прямые вопросы:
где меня упомянули, на что мне отреагировали, прочитали ли моё сообщение,
чем вообще набит этот чат.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import tgx_net


class TriageError(RuntimeError):
    """Не получилось спросить."""


HINTS = {
    "PEER_ID_INVALID": "такого чата нет",
    "MSG_ID_INVALID": "такого сообщения в этом чате нет",
    "USER_PRIVACY_RESTRICTED": (
        "человек скрыл время прочтения. Это взаимно: пока вы прячете своё, "
        "чужое вам тоже не покажут"),
    "MESSAGE_NOT_READ_YET": "сообщение ещё не прочитали",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "BROADCAST_FORBIDDEN": "в канале так нельзя — там нет участников в этом смысле",
    "CHANNEL_PRIVATE": "к этому чату больше нет доступа",
}

# чем набит чат: фильтры поиска, которые считает сервер
COUNTED = (("фото", "InputMessagesFilterPhotos"), ("видео", "InputMessagesFilterVideo"),
           ("голосовые", "InputMessagesFilterVoice"), ("кружки", "InputMessagesFilterRoundVideo"),
           ("музыка", "InputMessagesFilterMusic"), ("файлы", "InputMessagesFilterDocument"),
           ("ссылки", "InputMessagesFilterUrl"), ("гифки", "InputMessagesFilterGif"),
           ("упоминания", "InputMessagesFilterMyMentions"),
           ("закреплённые", "InputMessagesFilterPinned"))


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, TriageError)


def _when(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return None


class Triage:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    @staticmethod
    def _brief(message: Any) -> dict[str, Any]:
        text = (getattr(message, "message", "") or "").strip().replace("\n", " ")
        return {"id": getattr(message, "id", None), "когда": _when(getattr(message, "date", None)),
                "от": getattr(message, "from_id", None) and
                      getattr(message.from_id, "user_id", None) or
                      getattr(message, "from_id", None),
                "текст": text[:160] + ("…" if len(text) > 160 else "")}

    async def mentions(self, peer: Any, *, limit: int = 30, topic: int = 0) -> list[dict[str, Any]]:
        """Где вас звали и вы ещё не видели."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetUnreadMentionsRequest(
            peer=peer, offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0,
            top_msg_id=topic or None))
        return [self._brief(m) for m in getattr(result, "messages", None) or []]

    async def reactions(self, peer: Any, *, limit: int = 30,
                        topic: int = 0) -> list[dict[str, Any]]:
        """На что вам отреагировали и вы ещё не видели."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetUnreadReactionsRequest(
            peer=peer, offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0,
            top_msg_id=topic or None))
        rows = []
        for message in getattr(result, "messages", None) or []:
            row = self._brief(message)
            marks = getattr(getattr(message, "reactions", None), "recent_reactions", None) or []
            row["реакции"] = [getattr(getattr(r, "reaction", None), "emoticon", None) or "?"
                              for r in marks]
            rows.append(row)
        return rows

    async def clear(self, peer: Any, *, what: str = "both", topic: int = 0) -> dict[str, Any]:
        """Пометить упоминания и реакции просмотренными."""
        from telethon.tl import functions

        done = []
        if what in {"both", "mentions"}:
            await self._call(functions.messages.ReadMentionsRequest(
                peer=peer, top_msg_id=topic or None))
            done.append("упоминания")
        if what in {"both", "reactions"}:
            await self._call(functions.messages.ReadReactionsRequest(
                peer=peer, top_msg_id=topic or None))
            done.append("реакции")
        return {"отмечено просмотренным": done}

    async def read_by(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Кто прочитал это сообщение в группе.

        Сервер отвечает только про недавние сообщения в небольших группах —
        так задумано, иначе это стало бы слежкой.
        """
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMessageReadParticipantsRequest(
            peer=peer, msg_id=message_id))
        rows = []
        for item in result or []:
            rows.append({"кто": getattr(item, "user_id", item),
                         "когда": _when(getattr(item, "date", None))})
        return {"сообщение": message_id, "прочитали": rows, "сколько": len(rows)}

    async def read_at(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Когда собеседник прочитал ваше сообщение. Только в личной переписке."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetOutboxReadDateRequest(
            peer=peer, msg_id=message_id))
        return {"сообщение": message_id, "прочитано": _when(getattr(result, "date", None))}

    async def views(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        """Просмотры и пересылки постов."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMessagesViewsRequest(
            peer=peer, id=ids, increment=False))
        rows = []
        for index, item in enumerate(getattr(result, "views", None) or []):
            rows.append({"сообщение": ids[index] if index < len(ids) else None,
                         "просмотров": getattr(item, "views", None),
                         "пересылок": getattr(item, "forwards", None)})
        return rows

    async def counts(self, peer: Any, *, topic: int = 0) -> dict[str, Any]:
        """Чем набит чат: фото, видео, ссылки, файлы — числами."""
        from telethon.tl import functions, types

        filters = [getattr(types, name)() for _, name in COUNTED]
        result = await self._call(functions.messages.GetSearchCountersRequest(
            peer=peer, filters=filters, top_msg_id=topic or None))
        names = [label for label, _ in COUNTED]
        out: dict[str, Any] = {}
        for index, item in enumerate(result or []):
            if index < len(names) and getattr(item, "count", 0):
                out[names[index]] = item.count
        return out or {"пусто": True}

    async def online(self, peer: Any) -> dict[str, Any]:
        """Сколько человек сейчас в чате."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetOnlinesRequest(peer=peer))
        return {"сейчас в сети": getattr(result, "onlines", 0)}

    async def mark_unread(self, peer: Any, unread: bool = True) -> dict[str, Any]:
        """Вернуть чату жирную точку — чтобы не забыть вернуться."""
        from telethon.tl import functions, types

        await self._call(functions.messages.MarkDialogUnreadRequest(
            peer=types.InputDialogPeer(peer=peer), unread=unread or None))
        return {"помечен непрочитанным": unread}

    async def marked(self) -> list[Any]:
        """Какие чаты вы пометили непрочитанными."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetDialogUnreadMarksRequest())
        return [getattr(getattr(d, "peer", None), "user_id", None) or
                getattr(getattr(d, "peer", None), "channel_id", None) or
                getattr(getattr(d, "peer", None), "chat_id", None)
                for d in result or []]

    async def pin_dialog(self, peer: Any, pinned: bool = True) -> dict[str, Any]:
        """Закрепить чат в списке — не сообщение, а сам чат."""
        from telethon.tl import functions, types

        await self._call(functions.messages.ToggleDialogPinRequest(
            peer=types.InputDialogPeer(peer=peer), pinned=pinned or None))
        return {"чат закреплён": pinned}

    async def no_forwards(self, peer: Any, enabled: bool) -> dict[str, Any]:
        """Запретить пересылку и копирование из чата."""
        from telethon.tl import functions

        await self._call(functions.messages.ToggleNoForwardsRequest(
            peer=peer, enabled=enabled))
        return {"пересылка": "запрещена" if enabled else "разрешена"}
