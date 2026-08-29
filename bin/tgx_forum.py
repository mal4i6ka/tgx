#!/usr/bin/env python3
"""Форумы и темы целиком: core.telegram.org/api/forum.

Тема — это не «папка», а тред служебного сообщения, которым её создали: id темы
равен id этого сообщения. Отсюда все особенности, которые здесь и закодированы.
«Общая» тема (id 1) существует всегда, её нельзя удалить, и она единственная,
которую можно скрыть. Цвет стандартной иконки выбирается один раз при создании и
потом не меняется — у `editForumTopic` такого поля просто нет. Удаления темы
отдельным апдейтом не приходит: пропадает корневое сообщение, а повторный запрос
по id возвращает `forumTopicDeleted`.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

# Тема, которая есть в каждом форуме и не удаляется.
GENERAL_ID = 1

# Шесть цветов стандартной иконки. Другого значения сервер не примет.
ICON_COLORS = {
    "синий": 0x6FB9F0, "жёлтый": 0xFFD67E, "сиреневый": 0xCB86DB,
    "зелёный": 0x8EEE98, "розовый": 0xFF93B2, "красный": 0xFB6F5F,
}

# Сколько тем можно закрепить — приходит в конфигурации приложения.
PINNED_LIMIT_KEY = "topics_pinned_limit"
DEFAULT_PINNED_LIMIT = 5


class ForumError(RuntimeError):
    """Правило форума, которое нарушено, — с объяснением, что делать."""


def parse_color(spec: str | int | None) -> int | None:
    """`зелёный`, `green`, `0x8EEE98` или `8EEE98` → цвет стандартной иконки."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        value = spec
    else:
        text = str(spec).strip().lower()
        names = {**ICON_COLORS,
                 "blue": 0x6FB9F0, "yellow": 0xFFD67E, "violet": 0xCB86DB,
                 "green": 0x8EEE98, "pink": 0xFF93B2, "red": 0xFB6F5F}
        if text in names:
            return names[text]
        try:
            value = int(text.lstrip("#"), 16 if not text.isdigit() else 10)
        except ValueError:
            raise ForumError(
                f"цвет «{spec}» не понят; допустимы {', '.join(ICON_COLORS)} "
                f"или их коды вида 8EEE98") from None
    if value not in ICON_COLORS.values():
        allowed = ", ".join(f"{n} ({v:06X})" for n, v in ICON_COLORS.items())
        raise ForumError(f"цвет {value:06X} сервер не примет; можно только: {allowed}")
    return value


def topic_row(topic: Any) -> dict[str, Any]:
    """ForumTopic → плоская запись. Удалённая тема приходит как forumTopicDeleted."""
    if not hasattr(topic, "title"):
        return {"id": int(getattr(topic, "id", 0)), "deleted": True}
    date = getattr(topic, "date", None)
    return {
        "id": int(topic.id),
        "title": str(topic.title),
        "general": int(topic.id) == GENERAL_ID,
        "closed": bool(getattr(topic, "closed", False)),
        "hidden": bool(getattr(topic, "hidden", False)),
        "pinned": bool(getattr(topic, "pinned", False)),
        "mine": bool(getattr(topic, "my", False)),
        "icon_color": getattr(topic, "icon_color", None),
        "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
        "unread": int(getattr(topic, "unread_count", 0) or 0),
        "mentions": int(getattr(topic, "unread_mentions_count", 0) or 0),
        "reactions": int(getattr(topic, "unread_reactions_count", 0) or 0),
        "top_message": getattr(topic, "top_message", None),
        "created": date.isoformat() if date else None,
        "deleted": False,
    }


class Forum:
    """Всё, что можно сделать с форумом и его темами."""

    def __init__(self, client: Any) -> None:
        self.client = client

    # ── сам форум ────────────────────────────────────────────────────────────
    async def toggle(self, chat: Any, enabled: bool, tabs: bool | None = None) -> dict[str, Any]:
        """Включить форум в супергруппе или вернуть её обратно.

        Только владелец. Обычную группу Telegram сперва требует поднять до
        супергруппы — отдельным действием, здесь оно не подразумевается.
        """
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        try:
            await self.client(functions.channels.ToggleForumRequest(
                channel=entity, enabled=bool(enabled), tabs=bool(tabs) if tabs is not None else False))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"forum": bool(enabled), "tabs": tabs}

    async def set_tabs(self, chat: Any, tabs: bool) -> dict[str, Any]:
        """Вкладки вместо списка тем. Тем же методом, но форум остаётся включён."""
        return await self.toggle(chat, True, tabs=tabs)

    async def view_as_messages(self, chat: Any, enabled: bool) -> dict[str, Any]:
        """Показывать форум сплошной лентой. Настройка личная, но синхронная."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        await self.client(functions.channels.ToggleViewForumAsMessagesRequest(
            channel=entity, enabled=bool(enabled)))
        return {"view_as_messages": bool(enabled)}

    # ── чтение ───────────────────────────────────────────────────────────────
    async def topics(self, chat: Any, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Список тем; `query` ищет по названию. Сервер отдаёт страницами."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        found: list[dict[str, Any]] = []
        offset_date, offset_id, offset_topic = None, 0, 0
        while len(found) < limit:
            result = await self.client(functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=offset_date, offset_id=offset_id,
                offset_topic=offset_topic, limit=min(100, limit - len(found)),
                q=query or None))
            batch = list(getattr(result, "topics", None) or [])
            if not batch:
                break
            found += [topic_row(t) for t in batch]
            last = batch[-1]
            offset_topic = int(getattr(last, "id", 0))
            offset_id = int(getattr(last, "top_message", 0) or 0)
            offset_date = getattr(last, "date", None)
            if len(batch) < 100:
                break
        return found[:limit]

    async def by_id(self, chat: Any, ids: Sequence[int]) -> list[dict[str, Any]]:
        """Темы по id. Именно так и узнают, что тему удалили."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        result = await self.client(functions.messages.GetForumTopicsByIDRequest(
            peer=entity, topics=[int(i) for i in ids]))
        return [topic_row(t) for t in (getattr(result, "topics", None) or [])]

    async def pinned_limit(self) -> int:
        """Сколько тем разрешено закрепить — из конфигурации приложения."""
        from telethon.tl import functions

        try:
            config = await self.client(functions.help.GetAppConfigRequest(hash=0))
            for item in getattr(getattr(config, "config", None), "value", None) or []:
                if getattr(item, "key", None) == PINNED_LIMIT_KEY:
                    return int(getattr(item.value, "value", DEFAULT_PINNED_LIMIT))
        except Exception:
            pass
        return DEFAULT_PINNED_LIMIT

    async def icons(self, limit: int = 40) -> list[dict[str, Any]]:
        """Иконки тем, доступные всем.

        Произвольное премиум-эмодзи возьмёт только Telegram Premium; остальным
        сервер разрешает лишь этот набор, поэтому его стоит показать до попытки.
        """
        from telethon.tl import functions, types

        result = await self.client(functions.messages.GetStickerSetRequest(
            stickerset=types.InputStickerSetEmojiDefaultTopicIcons(), hash=0))
        out = []
        for doc in (getattr(result, "documents", None) or [])[:limit]:
            alt = ""
            for attribute in getattr(doc, "attributes", None) or []:
                if isinstance(attribute, types.DocumentAttributeCustomEmoji):
                    alt = attribute.alt
            out.append({"emoji": alt, "icon_emoji_id": doc.id})
        return out

    # ── темы ─────────────────────────────────────────────────────────────────
    async def create(self, chat: Any, title: str, *, color: str | int | None = None,
                     icon_emoji_id: int | None = None, send_as: str | None = None) -> dict[str, Any]:
        """Создать тему. Цвет стандартной иконки задаётся только здесь."""
        from telethon import helpers
        from telethon.tl import functions

        if not (title or "").strip():
            raise ForumError("у темы должно быть название")
        if color is not None and icon_emoji_id is not None:
            raise ForumError("иконка либо своя (--emoji), либо стандартная цветная (--color)")

        entity = await self.client.get_input_entity(chat)
        author = await self.client.get_input_entity(send_as) if send_as else None
        try:
            result = await self.client(functions.messages.CreateForumTopicRequest(
                peer=entity, title=title.strip(), icon_color=parse_color(color),
                icon_emoji_id=icon_emoji_id, send_as=author,
                random_id=helpers.generate_random_long()))
        except Exception as exc:
            raise self._explain(exc) from exc

        # id темы равен id служебного сообщения, которым её создали.
        topic_id = 0
        for update in getattr(result, "updates", None) or []:
            topic_id = (getattr(update, "id", 0)
                        or getattr(getattr(update, "message", None), "id", 0) or topic_id)
        return {"id": int(topic_id), "title": title.strip()}

    async def edit(self, chat: Any, topic_id: int, *, title: str | None = None,
                   icon_emoji_id: int | None = None, closed: bool | None = None,
                   hidden: bool | None = None) -> dict[str, Any]:
        """Переименовать, сменить иконку, закрыть или скрыть тему."""
        from telethon.tl import functions

        topic_id = int(topic_id)
        if hidden is not None and topic_id != GENERAL_ID:
            raise ForumError(
                f"скрыть можно только «Общую» тему (id {GENERAL_ID}); "
                f"остальные закрывают — --close")
        if all(v is None for v in (title, icon_emoji_id, closed, hidden)):
            raise ForumError("нечего менять: укажите название, иконку, --close или --hide")

        entity = await self.client.get_input_entity(chat)

        # Сервер отвечает TOPIC_CLOSE_SEPARATELY, если в одном запросе поменять и
        # содержимое темы, и её состояние. Разбиваем на два — просили-то оба.
        content = {"title": title, "icon_emoji_id": icon_emoji_id}
        state = {"closed": closed, "hidden": hidden}
        steps = []
        if any(v is not None for v in content.values()):
            steps.append(content)
        if any(v is not None for v in state.values()):
            steps.append(state)

        for step in steps:
            fields = {"title": None, "icon_emoji_id": None, "closed": None, "hidden": None}
            fields.update(step)
            try:
                await self.client(functions.messages.EditForumTopicRequest(
                    peer=entity, topic_id=topic_id, **fields))
            except Exception as exc:
                raise self._explain(exc) from exc
        return {"id": topic_id, "title": title, "closed": closed, "hidden": hidden,
                "icon_emoji_id": icon_emoji_id, "requests": len(steps)}

    async def delete(self, chat: Any, topic_id: int) -> dict[str, Any]:
        """Удалить тему вместе со всей перепиской. Необратимо."""
        from telethon.tl import functions

        topic_id = int(topic_id)
        if topic_id == GENERAL_ID:
            raise ForumError("«Общую» тему удалить нельзя — её можно только скрыть (--hide)")

        entity = await self.client.get_input_entity(chat)
        removed = 0
        while True:                        # сервер удаляет историю порциями
            result = await self.client(functions.messages.DeleteTopicHistoryRequest(
                peer=entity, top_msg_id=topic_id))
            removed += int(getattr(result, "pts_count", 0) or 0)
            if not getattr(result, "offset", 0):
                break
        return {"deleted": topic_id, "messages": removed}

    async def pin(self, chat: Any, topic_id: int, pinned: bool = True) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        try:
            await self.client(functions.messages.UpdatePinnedForumTopicRequest(
                peer=entity, topic_id=int(topic_id), pinned=bool(pinned)))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"id": int(topic_id), "pinned": bool(pinned)}

    async def reorder(self, chat: Any, order: Iterable[int], force: bool = False) -> dict[str, Any]:
        """Порядок закреплённых тем: сверху вниз.

        `force` открепляет всё, чего нет в списке, — иначе прежние закрепления
        остаются, и порядок получается не тот, который просили.
        """
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        wanted = [int(i) for i in order]
        if not wanted:
            raise ForumError("нужен порядок: перечислите id тем сверху вниз")
        limit = await self.pinned_limit()
        if len(wanted) > limit:
            raise ForumError(f"закрепить можно не больше {limit} тем, а указано {len(wanted)}")
        await self.client(functions.messages.ReorderPinnedForumTopicsRequest(
            peer=entity, order=wanted, force=bool(force)))
        return {"order": wanted, "force": bool(force), "limit": limit}

    # ── ошибки сервера, переведённые на человеческий ─────────────────────────
    @staticmethod
    def _explain(exc: Exception) -> Exception:
        text = str(exc)
        hints = {
            "CHAT_ADMIN_REQUIRED": "нужны права администратора «управление темами» (manage_topics)",
            "CHAT_NOT_MODIFIED": "ничего не изменилось — такие значения уже стоят",
            "TOPIC_NOT_MODIFIED": "ничего не изменилось — такие значения уже стоят",
            "TOPIC_ID_INVALID": "темы с таким id в этом форуме нет",
            "TOPIC_CLOSED": "тема закрыта: сперва откройте её — --open",
            "TOPIC_CLOSE_SEPARATELY": "закрывать тему нужно отдельным запросом от переименования",
            "TOPIC_HIDE_SEPARATELY": "скрывать тему нужно отдельным запросом от переименования",
            "TOPIC_DELETED": "эта тема уже удалена",
            "PINNED_TOPIC_NOT_MODIFIED": "тема уже в этом состоянии",
            "DOCUMENT_INVALID": "это эмодзи не подходит для иконки темы; "
                                "без Telegram Premium доступен только стандартный набор — tgx forum icons",
            "BOT_FORUM_CREATE_FORBIDDEN": "этот бот не разрешает собеседникам заводить темы",
            "FORUM_ENABLED": "в этой группе форум уже включён",
            "BROADCAST_FORBIDDEN": "в канале тем не бывает — форум включается в супергруппе",
        }
        for code, message in hints.items():
            if code in text:
                return ForumError(message)
        if "ADMIN_RIGHTS" in text or "CREATOR" in text:
            return ForumError("это может сделать только владелец группы")
        return exc
