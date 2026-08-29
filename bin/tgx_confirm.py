#!/usr/bin/env python3
"""Человек в контуре: опасное действие подтверждается кнопкой в Telegram.

Агент не решает сам, а спрашивает: бот отправляет карточку с описанием того,
что сейчас произойдёт, и двумя кнопками. Дальше он ждёт нажатия и возвращает
решение. Пока решения нет — действие не выполняется.

Две вещи, без которых подтверждение бессмысленно:

* **Нажать может только тот, кого спросили.** Кнопки видны всем в чате, поэтому
  автор нажатия сверяется с тем, у кого спрашивали; чужое нажатие отклоняется и
  записывается в ответ.
* **Одноразовость.** У каждого запроса свой случайный ключ, он живёт до первого
  решения и до истечения срока. Повторное нажатие ничего не меняет.

Опрос идёт через `getUpdates`, поэтому у бота не должно быть установленного
вебхука и параллельного получателя обновлений — иначе нажатие уйдёт не сюда.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

API = "https://api.telegram.org"
DEFAULT_TIMEOUT = 300.0
POLL_SECONDS = 25          # длинный опрос: столько сервер держит соединение

APPROVE, REJECT = "ok", "no"


class ConfirmError(RuntimeError):
    """Подтверждение не удалось получить."""


def call(token: str, method: str, payload: dict[str, Any]) -> Any:
    import tgx_net

    try:
        answer = tgx_net.post_form(f"{API}/bot{token}/{method}", payload, "Bot API")
    except tgx_net.NetError as exc:
        raise ConfirmError(str(exc)) from exc
    if not answer.get("ok"):
        raise ConfirmError(f"Bot API отказал: {answer.get('description', 'без объяснений')}")
    return answer["result"]


def card(title: str, details: str, danger: str = "") -> str:
    """Текст карточки: что произойдёт и почему это опасно."""
    lines = [f"*{title}*"]
    if details:
        lines += ["", details]
    if danger:
        lines += ["", f"⚠️ {danger}"]
    lines += ["", "_Действие не выполнится, пока вы не нажмёте кнопку._"]
    return "\n".join(lines)


class Approval:
    """Запрос подтверждения и ожидание ответа."""

    def __init__(self, token: str) -> None:
        self.token = token

    def ask(self, chat_id: str | int, title: str, details: str = "", *, danger: str = "",
            approver_id: int | None = None, timeout: float = DEFAULT_TIMEOUT,
            approve_label: str = "✅ Разрешить", reject_label: str = "✋ Отклонить",
            ) -> dict[str, Any]:
        """Спросить и дождаться. Возвращает решение, а не бросает исключение."""
        nonce = secrets.token_urlsafe(9)
        keyboard = {"inline_keyboard": [[
            {"text": approve_label, "callback_data": f"{APPROVE}:{nonce}"},
            {"text": reject_label, "callback_data": f"{REJECT}:{nonce}"},
        ]]}
        import json

        sent = call(self.token, "sendMessage", {
            "chat_id": str(chat_id), "text": card(title, details, danger),
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(keyboard, ensure_ascii=False)})
        message_id = sent["message_id"]

        decision = self._wait(nonce, approver_id, timeout)
        self._close(chat_id, message_id, title, decision)
        return decision

    def _wait(self, nonce: str, approver_id: int | None, timeout: float) -> dict[str, Any]:
        """Ждать нажатия по своему ключу. Чужие обновления не трогаем."""
        import json

        deadline = time.monotonic() + float(timeout)
        offset = None
        strangers: list[int] = []
        while time.monotonic() < deadline:
            left = max(1, min(POLL_SECONDS, int(deadline - time.monotonic())))
            payload: dict[str, Any] = {"timeout": left,
                                       "allowed_updates": json.dumps(["callback_query"])}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = call(self.token, "getUpdates", payload)
            except ConfirmError:
                time.sleep(1)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                query = update.get("callback_query")
                if not query:
                    continue
                data = str(query.get("data") or "")
                if not data.endswith(f":{nonce}"):
                    continue
                who = int(query.get("from", {}).get("id", 0))
                if approver_id is not None and who != approver_id:
                    # Кнопку видят все в чате — чужое нажатие не считается.
                    strangers.append(who)
                    call(self.token, "answerCallbackQuery", {
                        "callback_query_id": query["id"],
                        "text": "Это подтверждение адресовано другому человеку",
                        "show_alert": "true"})
                    continue
                call(self.token, "answerCallbackQuery", {"callback_query_id": query["id"]})
                approved = data.startswith(f"{APPROVE}:")
                return {"decision": "approved" if approved else "rejected",
                        "by": who, "strangers": strangers}
        return {"decision": "timeout", "by": None, "strangers": strangers}

    def _close(self, chat_id: str | int, message_id: int, title: str,
               decision: dict[str, Any]) -> None:
        """Убрать кнопки и записать исход прямо в сообщение."""
        mark = {"approved": "✅ разрешено", "rejected": "✋ отклонено",
                "timeout": "⌛️ время вышло — действие не выполнено"}[decision["decision"]]
        try:
            call(self.token, "editMessageText", {
                "chat_id": str(chat_id), "message_id": message_id,
                "text": f"*{title}*\n\n{mark}", "parse_mode": "Markdown"})
        except ConfirmError:
            pass          # исход уже получен; не смогли переписать — не беда
