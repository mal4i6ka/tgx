#!/usr/bin/env python3
"""Опросы и викторины — включая то, что добавил Bot API 10.0.

Слой 227 умеет больше, чем видно из обычного клиента: опрос можно ограничить
подписчиками (`subscribers_only`) и списком стран (`countries_iso2`), прятать
результаты до закрытия, перемешивать варианты, а с 10.0 разрешён и один
вариант вместо прежнего минимума в два.

Вариант ответа адресуется байтовым ключом, а не номером: Telegram возвращает
результаты именно по нему, и порядок вариантов может быть перемешан у каждого
зрителя. Поэтому ключом здесь всегда служит порядковый номер в байтах — он
переживает перемешивание.
"""
from __future__ import annotations

from typing import Any, Sequence

MAX_OPTIONS = 12
MAX_QUESTION = 300
MAX_OPTION = 100
# Bot API 9.6 поднял предел автозакрытия с суток до месяца.
MAX_CLOSE_PERIOD = 2628000


class PollError(RuntimeError):
    """Опрос, который Telegram не примет, — с объяснением почему."""


def option_key(index: int) -> bytes:
    """Ключ варианта. Номер, а не текст: текст может повторяться."""
    return bytes([index])


# Викторину по MTProto отправить не выходит: `inputMediaPoll.correct_answers`
# по схеме — вектор байтовых строк, а Telethon 1.44 объявляет и пакует его как
# вектор int32. Сервер отвечает QUIZ_CORRECT_ANSWER_INVALID на любое значение.
# Поэтому викторина уходит через Bot API, где правильный ответ — просто номер.
QUIZ_NEEDS_BOT = ("викторину нельзя отправить от своего имени: в Telethon 1.44 поле "
                  "correct_answers объявлено вектором чисел вместо байтовых строк, "
                  "и сервер отвергает любое значение. Добавьте --as @бот — "
                  "через Bot API викторина уходит нормально")


def send_quiz(token: str, chat_id: str, question: str, options: Sequence[str], *,
              correct: Sequence[int] | int, explanation: str = "", topic: int | None = None,
              multiple: bool = False, anonymous: bool = True, revoting: bool = False,
              shuffle: bool = False, close_in: int | None = None,
              silent: bool = False) -> dict[str, Any]:
    """Викторина через Bot API — там правильные ответы задаются номерами.

    С Bot API 9.6 правильных ответов может быть несколько, поэтому поле
    называется `correct_option_ids`, а не `correct_option_id`.
    """
    import json

    import tgx_net

    answers = list(correct) if isinstance(correct, (list, tuple)) else [correct]
    check(question, options, quiz_answer=answers, close_in=close_in)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "question": question.strip(),
        "options": json.dumps([{"text": o.strip()} for o in options if o.strip()],
                              ensure_ascii=False),
        "type": "quiz",
        "correct_option_ids": json.dumps([int(a) for a in answers]),
        "is_anonymous": "true" if anonymous else "false",
        "allows_multiple_answers": "true" if (multiple or len(answers) > 1) else "false",
    }
    if revoting:
        payload["allows_revoting"] = "true"
    if shuffle:
        payload["shuffle_options"] = "true"
    if explanation:
        payload["explanation"] = explanation
    if topic:
        payload["message_thread_id"] = int(topic)
    if close_in:
        payload["open_period"] = int(close_in)
    if silent:
        payload["disable_notification"] = "true"
    try:
        answer = tgx_net.post_form(f"https://api.telegram.org/bot{token}/sendPoll",
                                   payload, "Bot API")
    except tgx_net.NetError as exc:
        raise PollError(str(exc)) from exc
    if not answer.get("ok"):
        raise PollError(f"Bot API отказал: {answer.get('description', 'без объяснений')}")
    return {"message_id": answer["result"]["message_id"], "quiz": True,
            "question": question.strip(), "options": len([o for o in options if o.strip()])}


def check(question: str, options: Sequence[str], *, quiz_answer: Any = None,
          close_in: int | None = None) -> None:
    """Всё, что сервер проверит сам, но объяснит скупо."""
    if not (question or "").strip():
        raise PollError("у опроса должен быть вопрос")
    if len(question) > MAX_QUESTION:
        raise PollError(f"вопрос длиннее {MAX_QUESTION} знаков ({len(question)})")
    clean = [o for o in options if (o or "").strip()]
    if not clean:
        raise PollError("нужен хотя бы один вариант ответа")
    if len(clean) > MAX_OPTIONS:
        raise PollError(f"вариантов больше {MAX_OPTIONS} ({len(clean)})")
    for option in clean:
        if len(option) > MAX_OPTION:
            raise PollError(f"вариант «{option[:30]}…» длиннее {MAX_OPTION} знаков")
    # С 9.6 правильных ответов может быть несколько — принимаем и число, и список.
    for answer in ([] if quiz_answer is None else
                   (quiz_answer if isinstance(quiz_answer, (list, tuple)) else [quiz_answer])):
        if not 0 <= int(answer) < len(clean):
            raise PollError(f"правильный ответ {answer} вне списка из {len(clean)} вариантов")
    if close_in is not None and not 0 < int(close_in) <= MAX_CLOSE_PERIOD:
        raise PollError(f"автозакрытие — от 1 до {MAX_CLOSE_PERIOD} секунд "
                        f"(это месяц), а указано {close_in}")


def parse_countries(spec: str | None) -> list[str] | None:
    """`RU,DE` → список ISO-кодов. Пустое — значит без ограничения по странам."""
    if not spec:
        return None
    codes = [c.strip().upper() for c in spec.split(",") if c.strip()]
    bad = [c for c in codes if len(c) != 2 or not c.isalpha()]
    if bad:
        raise PollError(f"код страны пишется двумя буквами: {', '.join(bad)} — не подходит")
    return codes


def describe(poll: Any, results: Any = None) -> dict[str, Any]:
    """Опрос и его результаты — плоской записью."""
    question = getattr(poll, "question", None)
    counts = {}
    total = 0
    if results is not None:
        total = int(getattr(results, "total_voters", 0) or 0)
        for row in getattr(results, "results", None) or []:
            counts[bytes(row.option)] = int(getattr(row, "voters", 0) or 0)
    answers = []
    for index, answer in enumerate(getattr(poll, "answers", None) or []):
        text = getattr(answer, "text", None)
        voters = counts.get(bytes(answer.option), 0)
        answers.append({
            "n": index,
            "text": getattr(text, "text", None) or str(text or ""),
            "voters": voters,
            "share": round(voters * 100 / total) if total else 0,
        })
    return {
        "question": getattr(question, "text", None) or str(question or ""),
        "quiz": bool(getattr(poll, "quiz", False)),
        "multiple": bool(getattr(poll, "multiple_choice", False)),
        "anonymous": not bool(getattr(poll, "public_voters", False)),
        "closed": bool(getattr(poll, "closed", False)),
        "members_only": bool(getattr(poll, "subscribers_only", False)),
        "countries": list(getattr(poll, "countries_iso2", None) or []),
        "total_voters": total,
        "answers": answers,
    }


class Polls:
    """Создание, голосование, результаты и закрытие."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def create(self, chat: Any, question: str, options: Sequence[str], *,
                     quiz_answer: int | None = None, multiple: bool = False,
                     public: bool = False, shuffle: bool = False,
                     hide_until_close: bool = False, members_only: bool = False,
                     allow_revoting: bool = False,
                     countries: str | None = None, close_in: int | None = None,
                     explanation: str = "", topic: int | None = None,
                     silent: bool = False) -> dict[str, Any]:
        from telethon import helpers
        from telethon.tl import functions, types

        clean = [o.strip() for o in options if (o or "").strip()]
        check(question, clean, quiz_answer=quiz_answer, close_in=close_in)
        if quiz_answer is not None:
            raise PollError(QUIZ_NEEDS_BOT)
        if explanation and quiz_answer is None:
            raise PollError("пояснение показывается только в викторине — укажите --quiz")

        poll = types.Poll(
            id=helpers.generate_random_long(),
            hash=0,                       # заполняет сервер; при отправке всегда 0
            question=types.TextWithEntities(text=question.strip(), entities=[]),
            answers=[types.PollAnswer(text=types.TextWithEntities(text=o, entities=[]),
                                      option=option_key(i)) for i, o in enumerate(clean)],
            closed=False,
            public_voters=bool(public),
            multiple_choice=bool(multiple),
            quiz=quiz_answer is not None,
            shuffle_answers=bool(shuffle),
            hide_results_until_close=bool(hide_until_close),
            subscribers_only=bool(members_only) or None,
            revoting_disabled=None if allow_revoting else True,
            countries_iso2=parse_countries(countries),
            close_period=int(close_in) if close_in else None,
        )
        media = types.InputMediaPoll(
            poll=poll,
            solution=explanation or None,
            solution_entities=[] if explanation else None,
        )
        peer = await self.client.get_input_entity(chat)
        reply = types.InputReplyToMessage(reply_to_msg_id=int(topic)) if topic else None
        try:
            result = await self.client(functions.messages.SendMediaRequest(
                peer=peer, media=media, message="", random_id=helpers.generate_random_long(),
                reply_to=reply, silent=silent or None))
        except Exception as exc:
            raise self._explain(exc) from exc

        sent = 0
        for update in getattr(result, "updates", None) or []:
            sent = getattr(update, "id", 0) or getattr(getattr(update, "message", None), "id", 0) or sent
        return {"message_id": int(sent), "question": question.strip(), "options": len(clean),
                "quiz": quiz_answer is not None}

    async def vote(self, chat: Any, msg_id: int, choices: Sequence[int]) -> dict[str, Any]:
        """Проголосовать. Пустой список снимает свой голос."""
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        try:
            result = await self.client(functions.messages.SendVoteRequest(
                peer=peer, msg_id=int(msg_id), options=[option_key(int(c)) for c in choices]))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"message_id": int(msg_id), "voted": list(choices) or "голос снят",
                **self._from_updates(result)}

    async def results(self, chat: Any, msg_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        message = await self.client.get_messages(peer, ids=int(msg_id))
        poll = getattr(getattr(message, "media", None), "poll", None)
        if poll is None:
            raise PollError(f"в сообщении {msg_id} нет опроса")
        fresh = await self.client(functions.messages.GetPollResultsRequest(
            peer=peer, msg_id=int(msg_id), poll_hash=0))   # 0 — «отдай всё, что есть»
        results = None
        for update in getattr(fresh, "updates", None) or []:
            results = getattr(update, "results", None) or results
        return describe(poll, results or getattr(message.media, "results", None))

    async def close(self, chat: Any, msg_id: int) -> dict[str, Any]:
        """Закрыть опрос — голосовать больше нельзя, результаты видны всем."""
        from telethon.tl import functions, types

        peer = await self.client.get_input_entity(chat)
        message = await self.client.get_messages(peer, ids=int(msg_id))
        poll = getattr(getattr(message, "media", None), "poll", None)
        if poll is None:
            raise PollError(f"в сообщении {msg_id} нет опроса")
        closed = types.Poll(id=poll.id, hash=0, question=poll.question,
                            answers=poll.answers, closed=True)
        try:
            await self.client(functions.messages.EditMessageRequest(
                peer=peer, id=int(msg_id), media=types.InputMediaPoll(poll=closed)))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"message_id": int(msg_id), "closed": True}

    async def voters(self, chat: Any, msg_id: int, option: int | None = None,
                     limit: int = 50) -> list[dict[str, Any]]:
        """Кто как проголосовал — только если опрос не анонимный."""
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        try:
            result = await self.client(functions.messages.GetPollVotesRequest(
                peer=peer, id=int(msg_id), limit=int(limit),
                option=option_key(option) if option is not None else None, offset=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        names = {u.id: (u.username or " ".join(filter(None, [u.first_name, u.last_name])))
                 for u in getattr(result, "users", None) or []}
        out = []
        for vote in getattr(result, "votes", None) or []:
            picked = getattr(vote, "option", None)
            out.append({"user": names.get(getattr(vote, "user_id", None), vote.user_id),
                        "option": picked[0] if picked else None})
        return out

    @staticmethod
    def _from_updates(result: Any) -> dict[str, Any]:
        for update in getattr(result, "updates", None) or []:
            poll = getattr(update, "poll", None)
            if poll is not None:
                return describe(poll, getattr(update, "results", None))
        return {}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        text = str(exc)
        hints = {
            "POLL_OPTION_INVALID": "такого варианта в этом опросе нет",
            "POLL_ANSWERS_INVALID": "варианты ответа Telegram не принял",
            "POLL_QUESTION_INVALID": "вопрос Telegram не принял",
            "POLL_VOTE_REQUIRED": "сначала нужно проголосовать, чтобы увидеть результаты",
            "MESSAGE_POLL_CLOSED": "опрос уже закрыт",
            "REVOTE_NOT_ALLOWED": "в этом опросе нельзя переголосовать",
            "CHAT_SEND_POLL_FORBIDDEN": "в этом чате запрещены опросы",
            "POLL_UNSUPPORTED": "этот чат не поддерживает опросы",
        }
        for code, message in hints.items():
            if code in text:
                return PollError(message)
        return exc
