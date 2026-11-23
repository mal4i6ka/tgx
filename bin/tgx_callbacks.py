#!/usr/bin/env python3
"""Нажатия на кнопки — со стороны самого бота, через Bot API.

Кнопка с `callback_data` ничего не делает сама: Telegram присылает боту
`callback_query`, и пока бот не ответит, у человека крутится часики. tgx умеет
это целиком: получать нажатия, отвечать (всплывающей строкой, окном или
ссылкой), переписывать сообщение с кнопкой и держать обработчик, который на
каждое нажатие запускает вашу программу.

**Где сообщение — решает `callback_query_mode` (Bot API 10.5).** Раньше нажатие
всегда несло поле `message`, а если бот сообщение уже не видел — заглушку
`InaccessibleMessage` с датой 0. Теперь бот может попросить другое:

* `default` — всегда `message`, недоступное — заглушкой;
* `auto` — `inline_message_id` вместо `message`, когда сообщение недоступно;
* `inline` — всегда `inline_message_id`.

Править сообщение по `inline_message_id` — это другие параметры тех же методов
(`editMessageText` и компания), и путать их нельзя: по `chat_id` такое сообщение
не найдётся. Поэтому всё, что правит сообщение после нажатия, берёт адрес из
`target()`, а не собирает его само.

Нажатия получаются через `getUpdates`: у бота не должно быть вебхука и второго
получателя обновлений, иначе нажатие уйдёт туда.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any, Callable, Iterator

API = "https://api.telegram.org"
POLL_SECONDS = 25
HANDLER_SECONDS = 10.0      # Telegram ждёт ответа на нажатие около 15 секунд
MAX_ANSWER = 200            # длиннее всплывающая строка не показывается


class CallbackError(RuntimeError):
    """Не вышло."""


def call(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    import tgx_net

    try:
        answer = tgx_net.post_json(f"{API}/bot{token}/{method}", payload or {}, "Bot API")
    except tgx_net.NetError as exc:
        text = str(exc)
        if "409" in text or "webhook" in text.lower():
            raise CallbackError("у бота установлен вебхук или работает другой получатель "
                                "обновлений — нажатия уходят туда. Снимите вебхук: "
                                "tgx bot updates БОТ --drop-webhook") from exc
        raise CallbackError(text) from exc
    if not answer.get("ok"):
        description = answer.get("description", "без объяснений")
        if "query is too old" in description or "query ID is invalid" in description:
            raise CallbackError("на это нажатие отвечать уже поздно — Telegram ждёт около 15 секунд")
        if "message is not modified" in description:
            return True
        raise CallbackError(f"Bot API отказал: {description}")
    return answer["result"]


# ── разбор ───────────────────────────────────────────────────────────────────
def target(query: dict[str, Any]) -> dict[str, Any]:
    """Как адресовать сообщение с нажатой кнопкой в методах правки.

    `inline_message_id` — у сообщений, отправленных через инлайн, и у любых
    сообщений в режимах `auto`/`inline`. Иначе — чат и номер. Недоступное
    сообщение (заглушка с датой 0) править нельзя ни так, ни так.
    """
    if query.get("inline_message_id"):
        return {"inline_message_id": query["inline_message_id"]}
    message = query.get("message") or {}
    if not message:
        raise CallbackError("в нажатии нет ни сообщения, ни inline_message_id")
    if not message.get("date"):
        raise CallbackError("сообщение с кнопкой боту недоступно (старше 48 часов или удалено) — "
                            "чтобы получать его как inline_message_id, включите "
                            "tgx bot settings БОТ --callback-mode auto")
    return {"chat_id": message["chat"]["id"], "message_id": message["message_id"]}


def normalize(update: dict[str, Any]) -> dict[str, Any] | None:
    """Обновление Bot API → плоская запись нажатия. Не нажатие — None."""
    query = update.get("callback_query")
    if not query:
        return None
    who = query.get("from") or {}
    message = query.get("message") or {}
    row: dict[str, Any] = {
        "update_id": update.get("update_id"),
        "id": query.get("id"),
        "от": who.get("id"),
        "кто": f"@{who['username']}" if who.get("username") else who.get("first_name", ""),
        "data": query.get("data"),
        "игра": query.get("game_short_name"),
        "chat_instance": query.get("chat_instance"),
    }
    if query.get("inline_message_id"):
        row["inline_message_id"] = query["inline_message_id"]
        row["сообщение"] = "инлайн"
    elif message:
        row["чат"] = (message.get("chat") or {}).get("id")
        row["message_id"] = message.get("message_id")
        row["сообщение"] = "доступно" if message.get("date") else "недоступно"
        if message.get("date"):
            row["текст"] = (message.get("text") or message.get("caption") or "")[:200]
    else:
        row["сообщение"] = "нет"
    return {k: v for k, v in row.items() if v is not None}


# ── получение ────────────────────────────────────────────────────────────────
class Updates:
    """Длинный опрос `getUpdates`, только нажатия, с честным подтверждением.

    Обновление считается полученным, когда следующий запрос идёт со сдвигом за
    него. Поэтому на выходе делается ещё один короткий запрос — иначе Telegram
    пришлёт те же нажатия снова. `peek` этого не делает: посмотреть, не забирая.
    """

    def __init__(self, token: str, *, kinds: tuple[str, ...] = ("callback_query",),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.kinds = kinds
        self.offset: int | None = None
        self.sleep = sleep

    def poll(self, timeout: int = POLL_SECONDS) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": list(self.kinds)}
        if self.offset is not None:
            payload["offset"] = self.offset
        updates = call(self.token, "getUpdates", payload)
        for update in updates:
            self.offset = int(update["update_id"]) + 1
        return updates

    def stream(self, *, until: float | None = None, limit: int = 0) -> Iterator[dict[str, Any]]:
        seen = 0
        while until is None or time.monotonic() < until:
            left = POLL_SECONDS if until is None else max(0, min(POLL_SECONDS, int(until - time.monotonic())))
            try:
                updates = self.poll(left)
            except CallbackError as exc:
                if "вебхук" in str(exc):
                    raise
                self.sleep(1)              # сеть моргнула — не повод выходить
                continue
            for update in updates:
                yield update
                seen += 1
                if limit and seen >= limit:
                    return

    def commit(self) -> None:
        """Подтвердить всё полученное, чтобы оно не пришло снова."""
        if self.offset is not None:
            call(self.token, "getUpdates", {"offset": self.offset, "timeout": 0, "limit": 1,
                                            "allowed_updates": list(self.kinds)})


def drop_webhook(token: str, *, drop_pending: bool = False) -> dict[str, Any]:
    info = call(token, "getWebhookInfo")
    call(token, "deleteWebhook", {"drop_pending_updates": bool(drop_pending)})
    return {"вебхук был": info.get("url") or "не было", "снят": True,
            "старые обновления": "выброшены" if drop_pending else "сохранены"}


# ── ответ и правка ───────────────────────────────────────────────────────────
def answer_payload(query_id: str, *, text: str = "", alert: bool = False, url: str = "",
                   cache: int = 0) -> dict[str, Any]:
    if not query_id:
        raise CallbackError("нужен id нажатия — он есть в выводе tgx bot updates")
    if len(text) > MAX_ANSWER:
        raise CallbackError(f"ответ на нажатие — до {MAX_ANSWER} знаков, а здесь {len(text)}")
    payload: dict[str, Any] = {"callback_query_id": str(query_id)}
    if text:
        payload["text"] = text
    if alert:
        payload["show_alert"] = True
    if url:
        payload["url"] = url
    if cache:
        payload["cache_time"] = int(cache)
    return payload


def answer(token: str, query_id: str, **kwargs: Any) -> dict[str, Any]:
    call(token, "answerCallbackQuery", answer_payload(query_id, **kwargs))
    return {"нажатие": query_id, "отвечено": True}


def edit_payload(where: dict[str, Any], *, text: str | None = None, buttons: str | None = None,
                 parse_mode: str = "") -> tuple[str, dict[str, Any]]:
    """Какой метод и с чем: текст с кнопками, или только кнопки."""
    import tgx_rich

    payload = dict(where)
    if buttons is not None:
        markup = tgx_rich.buttons_json(buttons) if buttons.strip() else {"inline_keyboard": []}
        payload["reply_markup"] = markup
    if text is not None:
        payload["text"] = text
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return "editMessageText", payload
    if buttons is None:
        raise CallbackError("нечего менять: нужен --text или --buttons")
    return "editMessageReplyMarkup", payload


def address(*, chat: Any = None, message_id: int | None = None,
            inline_message_id: str = "") -> dict[str, Any]:
    if inline_message_id:
        if chat is not None or message_id is not None:
            raise CallbackError("либо --inline, либо чат и номер сообщения — не всё сразу")
        return {"inline_message_id": inline_message_id}
    if chat is None or message_id is None:
        raise CallbackError("нужны чат и номер сообщения — или --inline ID из нажатия")
    return {"chat_id": chat, "message_id": int(message_id)}


def edit(token: str, where: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    method, payload = edit_payload(where, **kwargs)
    call(token, method, payload)
    return {"исправлено": True, **where}


# ── обработчик ───────────────────────────────────────────────────────────────
def run_handler(command: list[str], press: dict[str, Any], *,
                timeout: float = HANDLER_SECONDS) -> dict[str, Any]:
    """Запустить программу на одно нажатие и разобрать, что она ответила.

    На вход — JSON нажатия в stdin и `TGX_CALLBACK_DATA` с прочим в окружении.
    Ответ — JSON вида `{"answer": "…", "alert": true, "url": "…",
    "edit": {"text": "…", "buttons": "…"}}` или просто строка — она станет
    всплывающим ответом. Упала, молчит или не уложилась — ответ пустой, чтобы
    у человека хотя бы перестали крутиться часики.
    """
    env = dict(os.environ)
    env.update({"TGX_CALLBACK_ID": str(press.get("id", "")),
                "TGX_CALLBACK_DATA": str(press.get("data") or ""),
                "TGX_CALLBACK_FROM": str(press.get("от", "")),
                "TGX_CALLBACK_INLINE": str(press.get("inline_message_id", "")),
                "TGX_CALLBACK_CHAT": str(press.get("чат", "")),
                "TGX_CALLBACK_MESSAGE": str(press.get("message_id", ""))})
    try:
        done = subprocess.run(command, input=json.dumps(press, ensure_ascii=False), text=True,
                              capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"answer": "", "ошибка": f"обработчик не уложился в {timeout:g} с"}
    except OSError as exc:
        return {"answer": "", "ошибка": f"обработчик не запустился: {exc}"}
    out = (done.stdout or "").strip()
    if done.returncode != 0:
        return {"answer": "", "ошибка": f"обработчик вышел с кодом {done.returncode}: "
                                        f"{(done.stderr or '').strip()[:200]}"}
    if out.startswith("{"):
        try:
            reply = json.loads(out)
            if isinstance(reply, dict):
                return reply
        except ValueError:
            pass
    return {"answer": out[:MAX_ANSWER]}


def handle(token: str, press: dict[str, Any], query: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    """Ответить на нажатие и, если просили, переписать сообщение — по верному адресу."""
    outcome = {"id": press.get("id"), "data": press.get("data")}
    text = str(reply.get("answer") or "")[:MAX_ANSWER]
    try:
        answer(token, press["id"], text=text, alert=bool(reply.get("alert")), url=str(reply.get("url") or ""))
        outcome["ответ"] = text or "(пусто)"
    except CallbackError as exc:
        outcome["ошибка ответа"] = str(exc)
    change = reply.get("edit")
    if isinstance(change, dict):
        try:
            edit(token, target(query), text=change.get("text"), buttons=change.get("buttons"),
                 parse_mode=str(change.get("parse_mode") or ""))
            outcome["сообщение"] = "исправлено"
        except CallbackError as exc:
            outcome["ошибка правки"] = str(exc)
    if reply.get("ошибка"):
        outcome["ошибка обработчика"] = reply["ошибка"]
    return outcome


def serve(token: str, command: str, *, data_prefix: str = "", limit: int = 0,
          seconds: float = 0, emit: Callable[[dict[str, Any]], None] = print,
          runner: Callable[..., dict[str, Any]] = run_handler,
          updates: Updates | None = None) -> int:
    """Обрабатывать нажатия, пока не прервут (или `limit` штук, или `seconds`)."""
    argv = shlex.split(command)
    if not argv:
        raise CallbackError("нужна команда-обработчик; для конвейера — sh -c '…'")
    source = updates or Updates(token)
    until = time.monotonic() + seconds if seconds else None
    handled = 0
    try:
        for update in source.stream(until=until):
            press = normalize(update)
            if press is None:
                continue
            if not str(press.get("data") or "").startswith(data_prefix):
                # Получатель обновлений у бота один: не ответим — у человека так и
                # будут крутиться часики. Отвечаем пустым и идём дальше.
                try:
                    answer(token, press["id"])
                except CallbackError:
                    pass
                continue
            emit(handle(token, press, update["callback_query"], runner(argv, press)))
            handled += 1
            if limit and handled >= limit:
                break
    finally:
        try:
            source.commit()
        except CallbackError:
            pass
    return handled
