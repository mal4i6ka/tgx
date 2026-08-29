#!/usr/bin/env python3
"""Статистика каналов, групп, постов и историй.

Telegram отдаёт статистику двумя разными способами в одном ответе: часть чисел
приходит сразу, а графики — токенами, которые надо догрузить отдельным вызовом
`stats.loadAsyncGraph`. Поэтому здесь числа и графики разделены: числа читаются
всегда, графики — по запросу и по одному, иначе один вызов тянет десяток.

Статистика появляется не сразу: каналу нужно набрать участников (обычно от
пятидесяти), иначе сервер отвечает отказом, а не пустыми числами.
"""
from __future__ import annotations

from typing import Any


class StatsError(RuntimeError):
    """Статистика недоступна или не запрошена."""


def value(item: Any) -> dict[str, Any]:
    """StatsAbsValueAndPrev → текущее значение и рост."""
    current = getattr(item, "current", None)
    previous = getattr(item, "previous", None)
    if current is None:
        return {}
    delta = round(current - previous, 2) if previous is not None else None
    growth = None
    if previous:
        growth = round((current - previous) / previous * 100, 1)
    return {"сейчас": round(current, 2), "было": round(previous, 2) if previous else None,
            "изменение": delta, "процентов": growth}


def graphs(source: Any) -> list[str]:
    """Названия графиков, которые можно догрузить."""
    return [name for name in dir(source)
            if name.endswith("_graph") and getattr(source, name, None) is not None]


class Stats:
    """Числа и графики по каналу, группе, посту или истории."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def channel(self, peer: Any) -> dict[str, Any]:
        """Сводка по каналу: подписчики, просмотры, пересылки, доля уведомлений."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        try:
            result = await self.client(functions.stats.GetBroadcastStatsRequest(
                channel=entity, dark=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {
            "подписчики": value(getattr(result, "followers", None)),
            "просмотров на пост": value(getattr(result, "views_per_post", None)),
            "пересылок на пост": value(getattr(result, "shares_per_post", None)),
            "реакций на пост": value(getattr(result, "reactions_per_post", None)),
            "уведомления включены, %": round(getattr(result, "enabled_notifications", None)
                                             and getattr(result.enabled_notifications, "part", 0)
                                             / (getattr(result.enabled_notifications, "total", 1) or 1)
                                             * 100, 1) if getattr(result, "enabled_notifications", None) else None,
            "графики": graphs(result),
        }

    async def group(self, peer: Any) -> dict[str, Any]:
        """Сводка по группе: участники, сообщения, активные."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        try:
            result = await self.client(functions.stats.GetMegagroupStatsRequest(
                channel=entity, dark=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {
            "участники": value(getattr(result, "members", None)),
            "сообщений": value(getattr(result, "messages", None)),
            "писали": value(getattr(result, "posters", None)),
            "просмотров": value(getattr(result, "viewers", None)),
            "графики": graphs(result),
        }

    async def message(self, peer: Any, msg_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        try:
            result = await self.client(functions.stats.GetMessageStatsRequest(
                channel=entity, msg_id=int(msg_id), dark=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"сообщение": int(msg_id), "графики": graphs(result)}

    async def story(self, peer: Any, story_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        try:
            result = await self.client(functions.stats.GetStoryStatsRequest(
                peer=entity, id=int(story_id), dark=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"история": int(story_id), "графики": graphs(result)}

    async def forwards(self, peer: Any, msg_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Кто публично переслал пост."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        try:
            result = await self.client(functions.stats.GetMessagePublicForwardsRequest(
                channel=entity, msg_id=int(msg_id), offset="", limit=int(limit)))
        except Exception as exc:
            raise self._explain(exc) from exc
        out = []
        for item in getattr(result, "forwards", None) or []:
            message = getattr(item, "message", None)
            out.append({"куда": getattr(getattr(item, "story", None) or message, "peer_id", None),
                        "просмотров": getattr(message, "views", None),
                        "id": getattr(message, "id", None)})
        return out

    async def graph(self, peer: Any, name: str, *, kind: str = "channel",
                    msg_id: int = 0) -> dict[str, Any]:
        """Догрузить один график по имени из списка «графики».

        Сервер отдаёт графики токенами, а не данными: пока токен не обменян,
        цифр в ответе нет.
        """
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        source = {
            "channel": lambda: functions.stats.GetBroadcastStatsRequest(channel=entity, dark=None),
            "group": lambda: functions.stats.GetMegagroupStatsRequest(channel=entity, dark=None),
            "message": lambda: functions.stats.GetMessageStatsRequest(
                channel=entity, msg_id=int(msg_id), dark=None),
        }.get(kind)
        if source is None:
            raise StatsError(f"вид «{kind}» неизвестен; есть: channel, group, message")
        summary = await self.client(source())
        holder = getattr(summary, name, None)
        if holder is None:
            raise StatsError(f"графика «{name}» здесь нет; доступны: "
                             f"{', '.join(graphs(summary)) or 'никакие'}")
        token = getattr(holder, "token", None)
        if token is None:
            return {"график": name, "данные": "пришли сразу", "json": getattr(holder, "json", None)}
        loaded = await self.client(functions.stats.LoadAsyncGraphRequest(token=token, x=None))
        data = getattr(getattr(loaded, "json", None), "data", None)
        return {"график": name, "точек": data.count("[") if isinstance(data, str) else None,
                "json": data}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        import tgx_net

        hints = {
            # Тем же кодом сервер отвечает и владельцу маленького чата: пока
            # участников мало, статистики просто нет.
            "CHAT_ADMIN_REQUIRED": "статистики пока нет: она включается у администратора "
                                   "и только когда в чате набралось достаточно участников",
            "BROADCAST_REQUIRED": "это не канал — для групп есть tgx stats group",
            "MEGAGROUP_REQUIRED": "это не группа — для каналов есть tgx stats channel",
            "CHANNEL_PRIVATE": "нет доступа к этому каналу",
            "STATS_MIGRATE": "статистика лежит в другом дата-центре",
            "MSG_ID_INVALID": "такого сообщения нет",
        }
        return tgx_net.explain(exc, hints, StatsError)
