#!/usr/bin/env python3
"""Кнопки под сообщением — одинаково на слоях TL 227 и 229.

До Telethon 1.45 (слой 227) вид кнопки был её классом: `KeyboardButtonUrl(text,
url, style)`, `KeyboardButtonCallback(text, data, …)` и так дальше по списку.
Со слоя 229 класс у кнопки один — `KeyboardInlineButton(text, type, style)` под
сообщением и `KeyboardButton(text, type, style)` в клавиатуре, — а вид со своими
полями уехал в отдельный объект `type`: `InlineButtonTypeUrl(url)`,
`InlineButtonTypeCallback(data, …)`.

Сравнение кнопок по имени класса после этого не падает, а молча перестаёт
совпадать: «это веб-приложение?» отвечает «нет» на любую кнопку, и нажатие
уходит не туда. Поэтому всё, что читает или собирает сырые кнопки, ходит сюда,
а не к `telethon.tl.types` напрямую. Имена полей в обоих слоях одни и те же
(`url`, `data`, `query`, `copy_text`, `user_id`), меняется только то, где они
лежат.
"""
from __future__ import annotations

from typing import Any

from telethon.tl import types
from telethon.tl.tlobject import TLObject

# Слой 229 узнаётся по наличию типа, а не по номеру версии: так честнее и для
# сборок Telethon с нестандартной нумерацией.
TYPED = hasattr(types, "KeyboardInlineButton")

# Вид кнопки — по классу самой кнопки (227) или её поля `type` (229).
_KINDS = {
    "KeyboardButton": "text",
    "KeyboardButtonUrl": "url",
    "KeyboardButtonCallback": "callback",
    "KeyboardButtonWebView": "web_view",
    "KeyboardButtonSimpleWebView": "simple_web_view",
    "KeyboardButtonSwitchInline": "switch_inline",
    "KeyboardButtonCopy": "copy",
    "KeyboardButtonUserProfile": "user_profile",
    "InputKeyboardButtonUserProfile": "user_profile",
    "KeyboardButtonUrlAuth": "url_auth",
    "InputKeyboardButtonUrlAuth": "url_auth",
    "KeyboardButtonGame": "game",
    "KeyboardButtonBuy": "buy",
    "KeyboardButtonRequestPhone": "request_phone",
    "KeyboardButtonRequestGeoLocation": "request_geo",
    "KeyboardButtonRequestPoll": "request_poll",
    "KeyboardButtonRequestPeer": "request_peer",
    "InputKeyboardButtonRequestPeer": "request_peer",

    "ButtonTypeDefault": "text",
    "ButtonTypeSimpleWebView": "simple_web_view",
    "ButtonTypeRequestPhone": "request_phone",
    "ButtonTypeRequestGeoLocation": "request_geo",
    "ButtonTypeRequestPoll": "request_poll",
    "ButtonTypeRequestPeer": "request_peer",
    "InputButtonTypeRequestPeer": "request_peer",
    "InlineButtonTypeUrl": "url",
    "InlineButtonTypeCallback": "callback",
    "InlineButtonTypeWebView": "web_view",
    "InlineButtonTypeSwitchInline": "switch_inline",
    "InlineButtonTypeCopy": "copy",
    "InlineButtonTypeUserProfile": "user_profile",
    "InputInlineButtonTypeUserProfile": "user_profile",
    "InlineButtonTypeUrlAuth": "url_auth",
    "InputInlineButtonTypeUrlAuth": "url_auth",
    "InlineButtonTypeGame": "game",
    "InlineButtonTypeBuy": "buy",
    "InlineButtonTypeDisabled": "disabled",
}

# Что tgx умеет собирать сам — инлайн-кнопки из `--button` и поста.
_LEGACY = {
    "url": "KeyboardButtonUrl",
    "web_view": "KeyboardButtonWebView",
    "callback": "KeyboardButtonCallback",
    "switch_inline": "KeyboardButtonSwitchInline",
    "copy": "KeyboardButtonCopy",
    "user_profile": "KeyboardButtonUserProfile",
}
_TYPES = {
    "url": "InlineButtonTypeUrl",
    "web_view": "InlineButtonTypeWebView",
    "callback": "InlineButtonTypeCallback",
    "switch_inline": "InlineButtonTypeSwitchInline",
    "copy": "InlineButtonTypeCopy",
    "user_profile": "InlineButtonTypeUserProfile",
}


def _carrier(button: Any) -> Any:
    """Объект, в котором лежат вид и поля: сама кнопка (227) или её `type` (229)."""
    inner = getattr(button, "type", None)
    return inner if isinstance(inner, TLObject) else button


def kind(button: Any) -> str:
    """Вид кнопки словом — `url`, `callback`, `web_view`…; незнакомый — имя класса."""
    name = type(_carrier(button)).__name__
    return _KINDS.get(name, name)


def field(button: Any, name: str) -> Any:
    """Поле кнопки там, где оно лежит в этом слое; `text` всегда у самой кнопки."""
    carrier = _carrier(button)
    if carrier is not button and hasattr(carrier, name):
        return getattr(carrier, name)
    return getattr(button, name, None)


def inline(text: str, what: str, *, style: Any = None, **fields: Any) -> Any:
    """Инлайн-кнопка вида `what` с полями `fields` — в устройстве установленного слоя."""
    if what not in _TYPES:
        raise ValueError(f"кнопку вида «{what}» tgx собирать не умеет")
    if TYPED:
        return types.KeyboardInlineButton(
            text=text, type=getattr(types, _TYPES[what])(**fields), style=style)
    return getattr(types, _LEGACY[what])(text=text, style=style, **fields)
