"""Чужие боты изнутри терминала: инлайн-запросы, кнопки, мини-приложения.

Инлайн-бот — это тот, кого вызывают через `@бот запрос` прямо в поле ввода:
@gif, @pic, @vid и тысячи других. Обычно его результаты выбирают пальцем из
всплывающего списка; здесь список печатается, а выбор делается номером.

Мини-приложения из терминала не показать — это веб-страница. Но получить
подписанный адрес можно, а открыть его есть чем: браузером. Адрес одноразовый
и содержит подпись сессии, поэтому в чужие руки его отдавать нельзя.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import tgx_net


class InlineError(RuntimeError):
    """Бот не ответил или отказал."""


HINTS = {
    "BOT_INLINE_DISABLED": "у этого бота инлайн-режим выключен",
    "BOT_INVALID": "это не бот",
    "BOT_RESPONSE_TIMEOUT": "бот не ответил вовремя — попробуйте ещё раз",
    "QUERY_ID_INVALID": "список результатов устарел; спросите заново",
    "RESULT_ID_INVALID": "такого результата в списке нет",
    "INLINE_RESULT_EXPIRED": "результаты протухли; спросите заново",
    "PEER_ID_INVALID": "такого чата нет",
    "USER_BOT_INVALID": "у этого бота нет мини-приложения",
    "BOT_WEBVIEW_DISABLED": "мини-приложение у бота выключено",
    "WEBDOCUMENT_URL_INVALID": "бот прислал негодную ссылку",
    "DATA_INVALID": "кнопка не принимает эти данные",
    "BUTTON_DATA_INVALID": "у этой кнопки нет данных для нажатия",
    "PASSWORD_HASH_INVALID": "неверный пароль двухфакторной защиты",
    "URL_INVALID": "у этого бота не заведено мини-приложение — его включают в BotFather",
    "BOT_APP_INVALID": "такого приложения у бота нет",
    "BOT_APP_SHORTNAME_INVALID": "у приложения другое короткое имя",
}

# где бот из меню вложений соглашается работать
WHERE = {"SameBotPM": "в переписке с собой", "BotPM": "в переписке с ботами",
         "PM": "в личных чатах", "Chat": "в группах", "Broadcast": "в каналах"}

PLATFORM = "tdesktop"  # ближе всего к обычному оконному клиенту

# Чем можно представиться приложению. Многие мини-приложения рисуют себя
# по-разному на телефоне и на настольном клиенте, а иные показывают на
# настольном заглушку. Смена платформы — не обман ради обмана: это то же
# самое, что выбор устройства в отладчике браузера, и без неё половину
# приложений в терминале не посмотреть.
PLATFORMS = ("tdesktop", "android", "ios", "web", "macos", "weba")


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, InlineError)


def _describe(item: Any) -> dict[str, Any]:
    """Один результат инлайн-бота — в строку, которую можно прочесть."""
    row: dict[str, Any] = {"id": getattr(item, "id", None),
                           "вид": getattr(item, "type", None)}
    for field in ("title", "description", "url"):
        value = getattr(item, field, None)
        if value:
            row[{"title": "заголовок", "description": "описание", "url": "адрес"}[field]] = value
    # вид вложения берём из поля, в котором оно лежит, а не из имени класса:
    # классы у Telethon называются по-разному в зависимости от источника
    for field, label in (("document", "документ"), ("photo", "фото")):
        if getattr(item, field, None) is not None:
            row["вложение"] = label
            break
    return row


class Inline:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def ask(self, bot: Any, peer: Any, query: str, *,
                  offset: str = "") -> dict[str, Any]:
        """Спросить инлайн-бота. Возвращает список и метку, нужную для отправки."""
        from telethon.tl import functions

        who = await self.client.get_input_entity(bot)
        result = await self._call(functions.messages.GetInlineBotResultsRequest(
            bot=who, peer=peer, query=query, offset=offset))
        items = getattr(result, "results", None) or []
        answer: dict[str, Any] = {
            "метка": getattr(result, "query_id", None),
            "результаты": [_describe(i) for i in items]}
        if getattr(result, "next_offset", None):
            answer["дальше"] = result.next_offset
        switch = getattr(result, "switch_pm", None)
        if switch is not None:
            answer["бот просит"] = {"текст": switch.text, "параметр": switch.start_param}
        return answer

    async def send(self, peer: Any, query_id: int, result_id: str, *,
                   silent: bool = False, hide_via: bool = False,
                   reply_to: int = 0) -> dict[str, Any]:
        """Отправить выбранный результат.

        `query_id` живёт недолго: список результатов протухает, и сервер тогда
        отвечает INLINE_RESULT_EXPIRED — это не поломка, а срок годности.
        """
        from telethon.tl import functions, types

        reply = types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None
        await self._call(functions.messages.SendInlineBotResultRequest(
            peer=peer, query_id=query_id, id=result_id, silent=silent or None,
            hide_via=hide_via or None, reply_to=reply,
            random_id=secrets.randbits(63)))
        return {"отправлено": result_id, "через бота": not hide_via}

    async def start(self, bot: Any, peer: Any, param: str = "") -> dict[str, Any]:
        """Запустить бота — то же, что нажать «Начать», с параметром из ссылки."""
        from telethon.tl import functions

        who = await self.client.get_input_entity(bot)
        await self._call(functions.messages.StartBotRequest(
            bot=who, peer=peer, start_param=param, random_id=secrets.randbits(63)))
        return {"бот": str(bot), "запущен": True, "параметр": param or "без параметра"}

    async def press(self, peer: Any, message_id: int, data: bytes, *,
                    password: str = "") -> dict[str, Any]:
        """Нажать кнопку под сообщением и услышать, что бот ответил.

        Часть кнопок требует пароль двухфакторной защиты — так сделаны кнопки,
        подтверждающие траты. Пароль сюда передаётся, а не спрашивается: это
        решает вызывающая сторона.
        """
        from telethon.tl import functions

        check = None
        if password:
            from telethon.password import compute_check

            state = await self.client(functions.account.GetPasswordRequest())
            check = compute_check(state, password)
        result = await self._call(functions.messages.GetBotCallbackAnswerRequest(
            peer=peer, msg_id=message_id, data=data, password=check))
        return {"ответ": getattr(result, "message", None) or "(бот промолчал)",
                "всплывающим окном": bool(getattr(result, "alert", False)),
                "адрес": getattr(result, "url", None)}

    # --- кнопки, открывающие мини-приложение ---

    @staticmethod
    def is_web_button(button: Any) -> bool:
        """Открывает ли эта кнопка мини-приложение.

        Отдельная проверка нужна всем трём поверхностям — окну, командам и
        коннектору, — и держать её надо в одном месте: разъедется здесь, и
        каждая начнёт молчать по-своему.
        """
        return type(button).__name__ in {"KeyboardButtonWebView",
                                         "KeyboardButtonSimpleWebView"}

    async def web_button_url(self, peer: Any, message: Any, button: Any, *,
                             platform: str = "") -> str:
        """Подписанный адрес приложения из уже найденной кнопки."""
        from telethon.tl import functions, types

        bot = await self.client.get_input_entity(
            getattr(message, "sender_id", None) or getattr(message, "via_bot_id", None))
        params = types.DataJSON(data=json.dumps({"bg_color": "#121212"}))
        platform = platform or PLATFORM
        if type(button).__name__ == "KeyboardButtonSimpleWebView":
            result = await self._call(functions.messages.RequestSimpleWebViewRequest(
                bot=bot, platform=platform, url=button.url, theme_params=params))
        else:
            result = await self._call(functions.messages.RequestWebViewRequest(
                peer=peer, bot=bot, platform=platform, url=button.url,
                theme_params=params))
        return getattr(result, "url", "")

    async def press_web_button(self, peer: Any, message_id: int, *, text: str = "",
                               row: int = 0, col: int = 0,
                               platform: str = "") -> dict[str, Any]:
        """Нажать кнопку, которая открывает мини-приложение.

        Такие кнопки — не callback: у Telethon для них нет ветки в `click()`, и
        нажатие молча возвращает пустоту. Со стороны это выглядит как «нажал, и
        ничего не произошло», хотя на деле запрос никуда не уходил. Здесь мы
        берём из кнопки её адрес и просим у сервера подписанный — тот самый,
        который открыл бы настоящий клиент.
        """
        from telethon.tl import functions, types

        message = await self.client.get_messages(peer, ids=message_id)
        if message is None:
            raise InlineError(f"сообщения {message_id} здесь нет")

        lines = getattr(getattr(message, "reply_markup", None), "rows", None) or []
        button = None
        if text:
            for line in lines:
                for item in line.buttons:
                    if getattr(item, "text", "") == text:
                        button = item
                        break
                if button is not None:
                    break
            if button is None:
                names = [b.text for line in lines for b in line.buttons]
                raise InlineError(f"кнопки «{text}» нет; есть: {', '.join(names) or 'ни одной'}")
        else:
            if row >= len(lines) or col >= len(lines[row].buttons):
                raise InlineError(f"кнопки в ряду {row}, месте {col} нет: "
                                  f"рядов {len(lines)}")
            button = lines[row].buttons[col]

        if not self.is_web_button(button):
            raise InlineError(
                f"кнопка «{getattr(button, 'text', '')}» — это "
                f"{type(button).__name__}, а не запуск приложения; "
                f"обычные кнопки жмёт `tgx message-click`")
        return {"кнопка": getattr(button, "text", None),
                "адрес": await self.web_button_url(peer, message, button,
                                                   platform=platform),
                "осторожно": "адрес подписан вашей сессией — не передавайте его никому"}

    async def press_menu_button(self, bot: Any, *, peer: Any = None,
                                platform: str = "") -> dict[str, Any]:
        """Нажать главную кнопку-меню бота — ту, что слева от поля ввода.

        Это отдельная сущность: не кнопка под сообщением, а свойство самого
        бота. Её адрес спрашивается у сервера, потому что бот вправе задать
        разную кнопку разным людям.
        """
        from telethon.tl import functions, types

        # Кнопку читаем из полного профиля бота, а не через bots.getBotMenuButton:
        # тот отвечает только самому боту, а нам нужна та кнопка, которую видит
        # человек. Настоящий клиент берёт её отсюда же.
        who = await self.client.get_input_entity(bot)
        full = await self._call(functions.users.GetFullUserRequest(id=who))
        info = getattr(getattr(full, "full_user", None), "bot_info", None)
        menu = getattr(info, "menu_button", None)
        url = getattr(menu, "url", None)
        if not url:
            shown = type(menu).__name__ if menu else ""
            what = ("список команд" if shown.endswith("Commands")
                    else "ничего" if not shown else shown)
            raise InlineError(
                f"у бота нет кнопки-меню с приложением — там {what}. "
                f"Приложение может открываться иначе: `tgx inline run`")
        params = types.DataJSON(data=json.dumps({"bg_color": "#121212"}))
        result = await self._call(functions.messages.RequestWebViewRequest(
            peer=peer or who, bot=who, platform=platform or PLATFORM, url=url,
            from_bot_menu=True, theme_params=params))
        return {"кнопка-меню": getattr(menu, "text", None) or "Открыть",
                "адрес": getattr(result, "url", None),
                "осторожно": "адрес подписан вашей сессией — не передавайте его никому"}

    # --- мини-приложения ---

    async def attach_menu(self) -> list[dict[str, Any]]:
        """Боты, добавленные в меню вложений."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetAttachMenuBotsRequest(hash=0))
        rows = []
        for item in getattr(result, "bots", None) or []:
            where = [WHERE.get(type(p).__name__.replace("AttachMenuPeerType", ""), "?")
                     for p in getattr(item, "peer_types", None) or []]
            rows.append({"бот": getattr(item, "short_name", None),
                         "id": getattr(item, "bot_id", None),
                         "работает": where or ["везде"],
                         "добавлен": not getattr(item, "inactive", False),
                         "может писать вам": bool(getattr(item, "request_write_access", False))})
        return rows

    async def attach_toggle(self, bot: Any, enabled: bool, *,
                            allow_write: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        who = await self.client.get_input_entity(bot)
        await self._call(functions.messages.ToggleBotInAttachMenuRequest(
            bot=who, enabled=enabled, write_allowed=allow_write or None))
        return {"бот": str(bot), "в меню вложений": enabled,
                "может писать вам": allow_write}

    async def web_app(self, bot: Any, *, peer: Any = None, url: str = "",
                      param: str = "", theme: dict[str, Any] | None = None,
                      platform: str = "") -> dict[str, Any]:
        """Подписанный адрес мини-приложения.

        У Telegram три разных вызова под три способа открыть одно и то же
        приложение: из бокового меню, из чата и по короткому имени. Какой
        подойдёт — зависит от того, как бот его завёл, и снаружи это неизвестно.
        Поэтому пробуем боковое меню, а на отказ переходим к чату: для человека
        это одно действие, а не выбор из трёх непонятных.

        Адрес одноразовый и несёт подпись сессии — по сути ключ от вашего
        аккаунта внутри приложения. Поэтому он не пишется в файлы и не уходит
        никуда, кроме вашего браузера.
        """
        import json

        from telethon.tl import functions, types

        who = await self.client.get_input_entity(bot)
        params = types.DataJSON(data=json.dumps(theme or {"bg_color": "#121212"}))
        platform = platform or PLATFORM
        if platform not in PLATFORMS:
            raise InlineError(f"платформа «{platform}»; есть: {', '.join(PLATFORMS)}")

        async def from_chat(where: Any) -> Any:
            return await self._call(functions.messages.RequestWebViewRequest(
                peer=where, bot=who, platform=platform, url=url or None,
                start_param=param or None, theme_params=params))

        async def main_app(where: Any) -> Any:
            """Главное мини-приложение — то, что открывается кнопкой «Запустить»."""
            return await self._call(functions.messages.RequestMainWebViewRequest(
                peer=where, bot=who, platform=platform, start_param=param or None,
                theme_params=params))

        if peer is not None:
            result = await from_chat(peer)
            opened = "из чата"
        else:
            # Три способа под три вида приложений, и снаружи не видно, какой
            # заведён у этого бота. Пробуем по очереди: боковое меню, главное
            # приложение, вложение в чате. Для человека это одно действие.
            attempts = (
                ("из бокового меню", lambda: self._call(
                    functions.messages.RequestSimpleWebViewRequest(
                        bot=who, platform=platform, from_side_menu=True, url=url or None,
                        start_param=param or None, theme_params=params))),
                ("главным приложением", lambda: main_app(types.InputPeerSelf())),
                ("из чата с собой", lambda: from_chat(types.InputPeerSelf())),
            )
            result = opened = None
            trouble: Exception | None = None
            for label, attempt in attempts:
                try:
                    result, opened = await attempt(), label
                    break
                except InlineError as exc:
                    trouble = trouble or exc
            if result is None:
                raise trouble or InlineError("у этого бота нет мини-приложения")

        return {"адрес": getattr(result, "url", None), "как открыто": opened,
                "осторожно": "адрес подписан вашей сессией — не передавайте его никому"}
