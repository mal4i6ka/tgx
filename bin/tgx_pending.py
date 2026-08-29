#!/usr/bin/env python3
"""Незаконченное и отложенное: черновики, расписание, быстрые ответы, закладки.

Четыре разных способа отложить сообщение, и Telegram держит их порознь:

* **Черновик** живёт в чате и виден только вам. Он не отправится сам никогда.
* **Отложенное** уже полноценное сообщение с назначенным временем — оно уйдёт
  само, и отменить это можно, только удалив его до срока.
* **Быстрый ответ** — заготовка под ярлыком, которую отправляют в любой чат
  вручную. Это она стоит за приветствием и автоответом бизнес-режима.
* **Закладка** — «Избранное»: пересланное себе, разложенное по авторам.

Черновик перезаписывается целиком: сохранить его — значит заменить прежний, а
пустой текст стирает черновик. Поэтому «дописать к черновику» здесь нет: сперва
читаем, потом сохраняем всё вместе.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence


class PendingError(RuntimeError):
    """Черновик, расписание или заготовка, с которыми не вышло."""


def when(value: str) -> datetime:
    """`2026-09-01T10:00`, `+2h`, `+30m` или `+3d` → момент отправки."""
    text = (value or "").strip()
    if text.startswith("+"):
        amount, unit = text[1:-1], text[-1].lower()
        factors = {"m": 60, "h": 3600, "d": 86400}
        if unit not in factors or not amount.isdigit():
            raise PendingError(f"не разобрать «{value}»; пишите +30m, +2h, +3d "
                               f"или полное время 2026-09-01T10:00")
        from datetime import timedelta

        return datetime.now(timezone.utc) + timedelta(seconds=int(amount) * factors[unit])
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise PendingError(f"не разобрать время «{value}»; пишите 2026-09-01T10:00 "
                           f"или +2h") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    if moment <= datetime.now(timezone.utc):
        raise PendingError("это время уже прошло — отложить можно только вперёд")
    return moment


class Pending:
    """Черновики, отложенные, быстрые ответы и закладки."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise self._explain(exc) from exc

    # ── черновики ────────────────────────────────────────────────────────────
    async def drafts(self) -> list[dict[str, Any]]:
        """Все черновики. Они видны только вам и сами не уходят."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetAllDraftsRequest())
        names = {}
        for holder in ("users", "chats"):
            for item in getattr(result, holder, None) or []:
                names[item.id] = getattr(item, "title", None) or getattr(item, "username", None) \
                    or " ".join(filter(None, [getattr(item, "first_name", None),
                                              getattr(item, "last_name", None)]))
        out = []
        for update in getattr(result, "updates", None) or []:
            draft = getattr(update, "draft", None)
            peer = getattr(update, "peer", None)
            who = (getattr(peer, "user_id", None) or getattr(peer, "channel_id", None)
                   or getattr(peer, "chat_id", None))
            text = getattr(draft, "message", None)
            if not text:
                continue
            date = getattr(draft, "date", None)
            out.append({"чат": names.get(who, who), "текст": text[:70],
                        "изменён": date.isoformat(timespec="minutes") if date else None})
        return out

    async def save_draft(self, peer: Any, text: str, *, reply_to: int | None = None,
                         no_preview: bool = False) -> dict[str, Any]:
        """Сохранить черновик. Пустой текст стирает прежний."""
        from telethon.tl import functions, types

        entity = await self.client.get_input_entity(peer)
        reply = types.InputReplyToMessage(reply_to_msg_id=int(reply_to)) if reply_to else None
        await self._call(functions.messages.SaveDraftRequest(
            peer=entity, message=text, reply_to=reply,
            no_webpage=no_preview or None, entities=None))
        return {"черновик": "стёрт" if not text else f"сохранён ({len(text)} знаков)",
                "примечание": "черновик перезаписывается целиком и сам не отправится"}

    async def clear_drafts(self) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ClearAllDraftsRequest())
        return {"черновики": "все стёрты"}

    # ── отложенные ───────────────────────────────────────────────────────────
    async def scheduled(self, peer: Any) -> list[dict[str, Any]]:
        """Что уйдёт из этого чата само и когда."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        result = await self._call(functions.messages.GetScheduledHistoryRequest(
            peer=entity, hash=0))
        out = []
        for message in getattr(result, "messages", None) or []:
            date = getattr(message, "date", None)
            out.append({"id": message.id, "текст": (message.message or "")[:60],
                        "уйдёт": date.isoformat(timespec="minutes") if date else None,
                        "вложение": type(getattr(message, "media", None)).__name__
                        .replace("MessageMedia", "") if getattr(message, "media", None) else ""})
        return out

    async def send_now(self, peer: Any, ids: Sequence[int]) -> dict[str, Any]:
        """Отправить отложенное немедленно, не дожидаясь срока."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        await self._call(functions.messages.SendScheduledMessagesRequest(
            peer=entity, id=[int(i) for i in ids]))
        return {"отправлено сейчас": list(ids)}

    async def cancel(self, peer: Any, ids: Sequence[int]) -> dict[str, Any]:
        """Отменить отложенное — то есть удалить до срока."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        await self._call(functions.messages.DeleteScheduledMessagesRequest(
            peer=entity, id=[int(i) for i in ids]))
        return {"отменено": list(ids)}

    # ── быстрые ответы ───────────────────────────────────────────────────────
    async def shortcuts(self) -> list[dict[str, Any]]:
        """Заготовки под ярлыками. На них ссылаются приветствие и автоответ."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetQuickRepliesRequest(hash=0))
        return [{"id": getattr(s, "shortcut_id", None), "ярлык": getattr(s, "shortcut", None),
                 "сообщений": getattr(s, "count", None)}
                for s in (getattr(result, "quick_replies", None) or [])]

    async def shortcut_messages(self, shortcut_id: int) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetQuickReplyMessagesRequest(
            shortcut_id=int(shortcut_id), hash=0, id=None))
        return [{"id": m.id, "текст": (m.message or "")[:70]}
                for m in (getattr(result, "messages", None) or [])]

    async def send_shortcut(self, peer: Any, shortcut_id: int) -> dict[str, Any]:
        """Отправить заготовку в чат."""
        from telethon import helpers
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        messages = await self.shortcut_messages(shortcut_id)
        ids = [m["id"] for m in messages]
        if not ids:
            raise PendingError(f"в заготовке {shortcut_id} нет сообщений")
        await self._call(functions.messages.SendQuickReplyMessagesRequest(
            peer=entity, shortcut_id=int(shortcut_id), id=ids,
            random_id=[helpers.generate_random_long() for _ in ids]))
        return {"отправлено сообщений": len(ids)}

    async def rename_shortcut(self, shortcut_id: int, name: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.EditQuickReplyShortcutRequest(
            shortcut_id=int(shortcut_id), shortcut=name.strip()))
        return {"ярлык": name.strip()}

    async def delete_shortcut(self, shortcut_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.DeleteQuickReplyShortcutRequest(
            shortcut_id=int(shortcut_id)))
        return {"удалена заготовка": int(shortcut_id)}

    # ── закладки («Избранное») ───────────────────────────────────────────────
    async def saved(self, limit: int = 30) -> list[dict[str, Any]]:
        """Избранное разложено по авторам — это отдельные «чаты» внутри него."""
        from telethon.tl import functions, types

        result = await self._call(functions.messages.GetSavedDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(),
            limit=int(limit), hash=0, exclude_pinned=None, parent_peer=None))
        names = {}
        for holder in ("users", "chats"):
            for item in getattr(result, holder, None) or []:
                names[item.id] = getattr(item, "title", None) or getattr(item, "username", None) \
                    or " ".join(filter(None, [getattr(item, "first_name", None),
                                              getattr(item, "last_name", None)]))
        out = []
        for dialog in getattr(result, "dialogs", None) or []:
            peer = getattr(dialog, "peer", None)
            who = (getattr(peer, "user_id", None) or getattr(peer, "channel_id", None)
                   or getattr(peer, "chat_id", None))
            out.append({"от кого": names.get(who, who),
                        "закреплено": bool(getattr(dialog, "pinned", False))})
        return out

    async def tags(self) -> list[dict[str, Any]]:
        """Метки-реакции в избранном."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSavedReactionTagsRequest(
            hash=0, peer=None))
        # Метка бывает обычным эмодзи и премиум-эмодзи: у первого есть
        # `emoticon`, у второго только `document_id`, и поле пустует.
        out = []
        for tag in getattr(result, "tags", None) or []:
            reaction = getattr(tag, "reaction", None)
            emoji = getattr(reaction, "emoticon", None)
            custom = getattr(reaction, "document_id", None)
            out.append({"метка": emoji or (f"премиум-эмодзи {custom}" if custom else "?"),
                        "название": getattr(tag, "title", None) or "без названия",
                        "сообщений": getattr(tag, "count", None)})
        return out

    async def name_tag(self, emoji: str, title: str) -> dict[str, Any]:
        """Назвать метку. Числовой аргумент — id премиум-эмодзи."""
        from telethon.tl import functions, types

        reaction = (types.ReactionCustomEmoji(document_id=int(emoji)) if emoji.isdigit()
                    else types.ReactionEmoji(emoticon=emoji))
        await self._call(functions.messages.UpdateSavedReactionTagRequest(
            reaction=reaction, title=title or None))
        return {"метка": emoji, "название": title or "снято"}

    # ── проверка фактов ──────────────────────────────────────────────────────
    async def fact_check(self, peer: Any, msg_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        result = await self._call(functions.messages.GetFactCheckRequest(
            peer=entity, msg_id=[int(msg_id)]))
        found = list(result or [])
        if not found:
            return {"проверка фактов": "не добавлена"}
        item = found[0]
        return {"страна": getattr(item, "country", None),
                "текст": getattr(getattr(item, "text", None), "text", None)}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        import tgx_net

        hints = {
            "SCHEDULE_DATE_TOO_LATE": "отложить так далеко нельзя — предел около года",
            "SCHEDULE_DATE_INVALID": "это время уже прошло",
            "SCHEDULE_TOO_MUCH": "слишком много отложенных сообщений в этом чате",
            "MESSAGE_ID_INVALID": "такого отложенного сообщения нет",
            "QUICK_REPLIES_TOO_MUCH": "слишком много заготовок",
            "SHORTCUT_INVALID": "заготовки с таким ярлыком нет",
            "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
            "PEER_ID_INVALID": "чат не найден",
        }
        return tgx_net.explain(exc, hints, PendingError)
