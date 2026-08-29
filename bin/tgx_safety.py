"""Блокировки, жалобы и уборка — то, что делают, когда всё пошло не так.

Здесь почти всё необратимо, поэтому команды этого раздела ходят через
подтверждение. Исключение — чтение: список заблокированных и то, что Telegram
сам думает о собеседнике (полоска «заблокировать / пожаловаться» под новым
чатом — это не украшение, а ответ сервера, и его можно спросить прямо).

Жалоба устроена не как «выбери причину из списка в коде»: сервер сам ведёт по
меню и на каждом шаге отдаёт варианты. Причины меняются, и зашивать их к себе
значит устареть к следующему обновлению. Поэтому мы показываем то, что прислал
сервер, и передаём обратно выбранное.
"""

from __future__ import annotations

import base64
from typing import Any

import tgx_net


class SafetyError(RuntimeError):
    """Не вышло."""


HINTS = {
    "PEER_ID_INVALID": "такого собеседника нет",
    "MSG_ID_INVALID": "такого сообщения нет",
    "USER_ID_INVALID": "такого пользователя нет",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "CHAT_NOT_MODIFIED": "и так уже так",
    "MESSAGE_DELETE_FORBIDDEN": "эти сообщения удалять нельзя",
    "PINNED_DIALOGS_TOO_MUCH": "слишком много закреплённых чатов",
    "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
    "REPORT_OPTION_INVALID": "такого варианта в этом меню нет — спросите меню заново",
    "BLOCKED_TOO_MUCH": "список заблокированных переполнен",
}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, SafetyError)


class Safety:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def block(self, who: Any, *, unblock: bool = False,
                    stories_only: bool = False) -> dict[str, Any]:
        """Заблокировать или разблокировать. `stories_only` — только истории."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(who)
        request = (functions.contacts.UnblockRequest if unblock
                   else functions.contacts.BlockRequest)
        await self._call(request(id=entity, my_stories_from=stories_only or None))
        what = "истории" if stories_only else "всё"
        return {"кто": str(who), "действие": "разблокирован" if unblock else "заблокирован",
                "что закрыто": what}

    async def blocked(self, *, limit: int = 100, stories_only: bool = False) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.contacts.GetBlockedRequest(
            offset=0, limit=limit, my_stories_from=stories_only or None))
        return [{"кто": getattr(u, "username", None) or getattr(u, "first_name", None),
                 "id": getattr(u, "id", None)} for u in getattr(result, "users", None) or []]

    async def block_replier(self, message_id: int, *, delete: bool = False,
                            wipe: bool = False, spam: bool = False) -> dict[str, Any]:
        """Заблокировать того, кто ответил на ваш пост.

        Отдельный вызов существует потому, что в комментариях под каналом вы
        часто не знаете, кто это, — знаете только сообщение.
        """
        from telethon.tl import functions

        await self._call(functions.contacts.BlockFromRepliesRequest(
            msg_id=message_id, delete_message=delete or None,
            delete_history=wipe or None, report_spam=spam or None))
        done = [n for n, v in (("сообщение удалено", delete), ("переписка стёрта", wipe),
                               ("отправлена жалоба", spam)) if v]
        return {"сообщение": message_id, "автор": "заблокирован", "заодно": done or ["ничего"]}

    async def peer_settings(self, peer: Any) -> dict[str, Any]:
        """Что Telegram думает об этом собеседнике — та самая полоска сверху."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetPeerSettingsRequest(peer=peer))
        settings = getattr(result, "settings", result)
        flags = [name for name, label in (
            ("report_spam", "можно пожаловаться на спам"),
            ("add_contact", "предлагает добавить в контакты"),
            ("block_contact", "предлагает заблокировать"),
            ("share_contact", "предлагает поделиться номером"),
            ("report_geo", "предлагает пожаловаться на геочат"),
            ("autoarchived", "чат убран в архив автоматически"),
            ("business_bot_paused", "секретарь приостановлен"),
        ) if getattr(settings, name, False)]
        row: dict[str, Any] = {"полоска": flags or ["обычный собеседник"]}
        if getattr(settings, "charge_paid_message_stars", None):
            row["платное сообщение"] = f"{settings.charge_paid_message_stars} звёзд"
        if getattr(settings, "registration_month", None):
            row["в Telegram с"] = settings.registration_month
        if getattr(settings, "phone_country", None):
            row["номер из"] = settings.phone_country
        return row

    async def hide_bar(self, peer: Any) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.HidePeerSettingsBarRequest(peer=peer))
        return {"полоска": "скрыта"}

    async def report_spam(self, peer: Any) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ReportSpamRequest(peer=peer))
        return {"жалоба": "отправлена как спам"}

    async def report(self, peer: Any, ids: list[int], *, option: str = "",
                     comment: str = "") -> dict[str, Any]:
        """Жалоба по меню сервера.

        Без варианта показываем, что предлагает сервер; с вариантом — идём
        дальше. Варианты приходят байтами, для терминала кодируем их в base64.
        """
        from telethon.tl import functions

        picked = base64.urlsafe_b64decode(option + "==") if option else b""
        result = await self._call(functions.messages.ReportRequest(
            peer=peer, id=ids, option=picked, message=comment))
        kind = type(result).__name__

        if kind == "ReportResultChooseOption":
            return {"шаг": getattr(result, "title", "выберите причину"),
                    "варианты": [
                        {"что": o.text,
                         "ключ": base64.urlsafe_b64encode(o.option).decode().rstrip("=")}
                        for o in getattr(result, "options", None) or []]}
        if kind == "ReportResultAddComment":
            return {"шаг": "нужен комментарий",
                    "обязателен": not getattr(result, "optional", False),
                    "ключ": base64.urlsafe_b64encode(
                        getattr(result, "option", b"")).decode().rstrip("=")}
        return {"жалоба": "отправлена"}

    async def clear_history(self, peer: Any, *, both_sides: bool = False,
                            keep_chat: bool = True) -> dict[str, Any]:
        """Стереть переписку. `both_sides` убирает её и у собеседника."""
        from telethon.tl import functions

        await self._call(functions.messages.DeleteHistoryRequest(
            peer=peer, max_id=0, just_clear=keep_chat or None, revoke=both_sides or None))
        return {"переписка": "стёрта", "у собеседника тоже": both_sides,
                "чат остался в списке": keep_chat}

    async def unpin_all(self, peer: Any, *, topic: int = 0) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.UnpinAllMessagesRequest(
            peer=peer, top_msg_id=topic or None))
        return {"закреплённые": "сняты", "где": f"тема {topic}" if topic else "весь чат"}

    async def sponsored(self, enabled: bool) -> dict[str, Any]:
        """Показывать ли рекламу. Выключение — возможность Premium."""
        from telethon.tl import functions

        await self._call(functions.account.ToggleSponsoredMessagesRequest(enabled=enabled))
        return {"реклама": "показывается" if enabled else "скрыта"}
