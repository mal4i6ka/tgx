"""То, что осталось за краем работы с чатами и каналами.

Мелочи, которых не хватало каждый раз по отдельности: постоянная ссылка на
сообщение, общие с человеком группы, свои публичные адреса, похожие каналы,
брошенные чаты, жёсткий антиспам, срок жизни сообщений по умолчанию.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import tgx_net


class ChatXError(RuntimeError):
    """Не вышло."""


HINTS = {
    "CHANNEL_PRIVATE": "канал закрыт или вас из него удалили",
    "CHANNEL_INVALID": "это не канал и не супергруппа",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "MESSAGE_ID_INVALID": "такого сообщения здесь нет",
    "USER_NOT_PARTICIPANT": "этого человека нет в чате",
    "USER_ID_INVALID": "такого пользователя нет",
    "PARTICIPANT_ID_INVALID": "такого участника нет",
    "BOOSTS_REQUIRED": "каналу не хватает бустов для этой возможности",
    "TTL_PERIOD_INVALID": "срок должен быть 0, 86400 (сутки), 604800 (неделя) или 2678400 (месяц)",
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
}

TTL_NAMES = {0: "выключено", 86400: "сутки", 604800: "неделя", 2678400: "месяц"}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, ChatXError)


def _when(value: Any) -> str | None:
    """Дата в читаемый вид. Часть ответов приходит числом, часть — датой."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return None


class Extras:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def message_link(self, peer: Any, message_id: int, *, album: bool = False,
                           thread: bool = False) -> dict[str, Any]:
        """Постоянная ссылка на сообщение — та же, что даёт «копировать ссылку»."""
        from telethon.tl import functions

        channel = await self.client.get_input_entity(peer)
        result = await self._call(functions.channels.ExportMessageLinkRequest(
            channel=channel, id=message_id, grouped=album or None, thread=thread or None))
        return {"ссылка": getattr(result, "link", None),
                "встраивание": getattr(result, "html", None)}

    async def common_chats(self, user: Any, limit: int = 100) -> list[dict[str, Any]]:
        """Где вы с этим человеком состоите вместе."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        result = await self._call(functions.messages.GetCommonChatsRequest(
            user_id=entity, max_id=0, limit=limit))
        return [{"чат": getattr(c, "title", None), "id": getattr(c, "id", None),
                 "участников": getattr(c, "participants_count", None)}
                for c in getattr(result, "chats", None) or []]

    async def my_public(self, *, for_location: bool = False,
                        check_limit: bool = False) -> list[dict[str, Any]]:
        """Ваши публичные каналы и группы — те, что заняли адрес."""
        from telethon.tl import functions

        result = await self._call(functions.channels.GetAdminedPublicChannelsRequest(
            by_location=for_location or None, check_limit=check_limit or None))
        return [{"чат": getattr(c, "title", None), "адрес": getattr(c, "username", None),
                 "id": getattr(c, "id", None)} for c in getattr(result, "chats", None) or []]

    async def similar(self, channel: Any = None) -> list[dict[str, Any]]:
        """Похожие каналы. Без аргумента — что Telegram советует вам вообще."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(channel) if channel else None
        result = await self._call(functions.channels.GetChannelRecommendationsRequest(
            channel=entity))
        return [{"канал": getattr(c, "title", None), "адрес": getattr(c, "username", None),
                 "подписчиков": getattr(c, "participants_count", None)}
                for c in getattr(result, "chats", None) or []]

    async def inactive(self) -> list[dict[str, Any]]:
        """Чаты, где давно ничего не происходит — кандидаты на уборку."""
        from telethon.tl import functions

        result = await self._call(functions.channels.GetInactiveChannelsRequest())
        dates = list(getattr(result, "dates", None) or [])
        rows = []
        for index, chat in enumerate(getattr(result, "chats", None) or []):
            stamp = dates[index] if index < len(dates) else None
            rows.append({"чат": getattr(chat, "title", None), "id": getattr(chat, "id", None),
                         "участников": getattr(chat, "participants_count", None),
                         "затих": _when(stamp)})
        return rows

    async def participant(self, chat: Any, who: Any) -> dict[str, Any]:
        """Кто этот человек в этом чате — точнее, чем перебор списка."""
        from telethon.tl import functions

        channel = await self.client.get_input_entity(chat)
        member = await self.client.get_input_entity(who)
        result = await self._call(functions.channels.GetParticipantRequest(
            channel=channel, participant=member))
        role = getattr(result, "participant", None)
        kind = type(role).__name__.replace("ChannelParticipant", "") or "участник"
        row: dict[str, Any] = {"роль": {"": "участник", "Self": "вы", "Creator": "владелец",
                                        "Admin": "администратор", "Banned": "ограничен",
                                        "Left": "вышел"}.get(kind, kind.lower())}
        if getattr(role, "date", None):
            row["с"] = _when(role.date)
        if getattr(role, "rank", None):
            row["звание"] = role.rank
        if getattr(role, "promoted_by", None):
            row["назначил"] = role.promoted_by
        return row

    async def antispam(self, chat: Any, enabled: bool) -> dict[str, Any]:
        """Жёсткий антиспам. Нужен уровень буста — иначе BOOSTS_REQUIRED."""
        from telethon.tl import functions

        channel = await self.client.get_input_entity(chat)
        await self._call(functions.channels.ToggleAntiSpamRequest(
            channel=channel, enabled=enabled))
        return {"антиспам": "включён" if enabled else "выключен"}

    async def default_ttl(self, seconds: int | None = None) -> dict[str, Any]:
        """Через сколько сообщения исчезают в новых чатах."""
        from telethon.tl import functions

        if seconds is None:
            result = await self._call(functions.messages.GetDefaultHistoryTTLRequest())
            period = getattr(result, "period", 0) or 0
        else:
            await self._call(functions.messages.SetDefaultHistoryTTLRequest(period=seconds))
            period = seconds
        return {"срок": TTL_NAMES.get(period, f"{period} с"), "секунд": period}
