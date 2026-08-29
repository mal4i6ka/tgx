"""Правка текста руками Telegram.

Сервер умеет вычитывать, расставлять эмодзи, переводить и переписывать текст
в заданном тоне — и возвращает не только результат, но и разметку правок:
что заменено, что убрано, что добавлено. Мы её показываем, потому что
принимать чужую правку вслепую — плохая привычка.

Тонов семь встроенных (formal, short, tribal, corp, biblical, viking, zen),
и можно завести свой: эмодзи, название и указание, как писать. Свои тоны
живут как наборы стикеров — их сохраняют, делятся ими, удаляют.
"""

from __future__ import annotations

from typing import Any

import tgx_net


class AIError(RuntimeError):
    """Сервер отказался править текст."""


HINTS = {
    "MSG_ID_INVALID": "такого сообщения в этом чате нет",
    "INPUT_TEXT_EMPTY": "нечего переводить",
    "TRANSLATE_REQ_INVALID": "перевод здесь недоступен",
    "AI_COMPOSE_UNAVAILABLE": "правка текста недоступна на этом аккаунте",
    "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
    "MESSAGE_EMPTY": "нечего править — текст пустой",
    "MESSAGE_TOO_LONG": "текст длиннее, чем сервер берётся править",
    "TONE_INVALID": "такого тона нет; посмотрите `tgx ai tones`",
    "LANG_CODE_INVALID": "непонятный код языка; нужен двухбуквенный, например en",
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
    "EMOJI_INVALID": "эмодзи не подходит; нужен идентификатор премиального эмодзи",
    "TONE_TITLE_INVALID": "название тона не принято",
    "TONE_PROMPT_INVALID": "указание слишком короткое или слишком длинное",
}

BUILT_IN = ("formal", "short", "tribal", "corp", "biblical", "viking", "zen")


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, AIError)


def _tone(value: str | None) -> Any:
    """Строка → тон. Встроенные по имени, свои по слагу или id:hash."""
    from telethon.tl import types

    if not value:
        return None
    name = value.strip()
    if name in BUILT_IN:
        return types.InputAiComposeToneDefault(tone=name)
    if ":" in name and all(p.lstrip("-").isdigit() for p in name.split(":", 1)):
        ident, access = name.split(":", 1)
        return types.InputAiComposeToneID(id=int(ident), access_hash=int(access))
    return types.InputAiComposeToneSlug(slug=name.lstrip("@"))


def _edits(diff: Any) -> tuple[str, list[dict[str, Any]]]:
    """Разметку правок — в читаемую строку и список.

    `diff_text` — это слитый текст: куски, помеченные Delete, есть только в
    старой версии, помеченные Insert — только в новой, Replace несёт обе. Мы
    проходим его один раз и собираем `[-убрано-]{+добавлено+}`, потому что
    одну строку глазами разобрать быстрее, чем список.
    """
    text = getattr(diff, "text", "") or ""
    entities = sorted(getattr(diff, "entities", None) or [], key=lambda e: e.offset)
    rows: list[dict[str, Any]] = []
    marked: list[str] = []
    cursor = 0
    for entity in entities:
        marked.append(text[cursor:entity.offset])
        piece = text[entity.offset:entity.offset + entity.length]
        kind = type(entity).__name__.replace("MessageEntityDiff", "")
        if kind == "Delete":
            marked.append(f"[-{piece}-]")
            rows.append({"правка": "убрано", "было": piece})
        elif kind == "Insert":
            marked.append(f"{{+{piece}+}}")
            rows.append({"правка": "добавлено", "стало": piece})
        else:
            was = getattr(entity, "old_text", "") or ""
            marked.append(f"[-{was}-]{{+{piece}+}}")
            rows.append({"правка": "замена", "было": was, "стало": piece})
        cursor = entity.offset + entity.length
    marked.append(text[cursor:])
    return "".join(marked), rows


class Compose:
    """Одна операция правки — и всё, что о ней стоит знать."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def run(self, text: str, *, proofread: bool = False, emojify: bool = False,
                  translate: str = "", tone: str = "") -> dict[str, Any]:
        from telethon.tl import functions, types

        if not any((proofread, emojify, translate, tone)):
            proofread = True  # без указаний вычитываем: самое безобидное
        result = await self._call(functions.messages.ComposeMessageWithAIRequest(
            text=types.TextWithEntities(text=text, entities=[]),
            proofread=proofread or None, emojify=emojify or None,
            translate_to_lang=translate or None, tone=_tone(tone)))
        done = getattr(result, "result_text", None)
        asked = [n for n, v in (("вычитать", proofread), ("эмодзи", emojify),
                                (f"перевести на {translate}", translate),
                                (f"тон {tone}", tone)) if v]
        marked, rows = _edits(getattr(result, "diff_text", None))
        answer = {"было": text, "стало": getattr(done, "text", "") or "", "просили": asked}
        if rows:
            answer["правки"] = marked
            answer["подробно"] = rows
        return answer


class Reader:
    """Пересказ и перевод — то, чем сервер помогает разгребать чужие тексты."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def summarize(self, peer: Any, message_id: int, *, lang: str = "",
                        tone: str = "") -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.messages.SummarizeTextRequest(
            peer=peer, id=message_id, to_lang=lang or None, tone=tone or None))
        return {"сообщение": message_id, "пересказ": getattr(result, "text", "") or ""}

    async def translate(self, to_lang: str, *, peer: Any = None,
                        ids: list[int] | None = None, text: str = "",
                        tone: str = "") -> list[dict[str, Any]]:
        """Перевести свой текст или чужие сообщения — сервер умеет и так, и так."""
        from telethon.tl import functions, types

        pieces = [types.TextWithEntities(text=text, entities=[])] if text else None
        result = await self._call(functions.messages.TranslateTextRequest(
            to_lang=to_lang, peer=peer if ids else None, id=ids or None,
            text=pieces, tone=tone or None))
        out = getattr(result, "result", None) or []
        rows = [{"перевод": getattr(t, "text", "") or ""} for t in out]
        for index, row in enumerate(rows):
            if ids and index < len(ids):
                row["сообщение"] = ids[index]
        return rows

    async def auto_translate(self, peer: Any, on: bool) -> dict[str, Any]:
        """Полоска «перевести» в этом чате."""
        from telethon.tl import functions

        await self._call(functions.messages.TogglePeerTranslationsRequest(
            peer=peer, disabled=None if on else True))
        return {"перевод в чате": "включён" if on else "выключен"}

    async def digest(self, peer: Any, messages: list[Any], *, lang: str = "",
                     long_at: int = 400) -> dict[str, Any]:
        """Сводка по чату: длинное пересказать, короткое взять как есть.

        Сервер пересказывает по одному сообщению, поэтому сводку собираем сами.
        Короткие сообщения пересказывать незачем — они и так короткие, а лишний
        вызов стоит времени и попадает под ограничение частоты.
        """
        rows: list[dict[str, Any]] = []
        for message in messages:
            body = (getattr(message, "text", "") or "").strip()
            if not body:
                continue
            row: dict[str, Any] = {
                "id": message.id,
                "от": getattr(getattr(message, "sender", None), "username", None)
                     or getattr(message, "sender_id", None)}
            if len(body) >= long_at:
                try:
                    told = await self.summarize(peer, message.id, lang=lang)
                    row["пересказ"] = told["пересказ"]
                except AIError as exc:
                    row["пересказ"] = f"не вышло: {exc}"
            else:
                row["текст"] = body
            rows.append(row)
        return {"сообщений": len(rows),
                "пересказано": sum(1 for r in rows if "пересказ" in r),
                "лента": rows}


class Tones:
    """Тоны письма: свои и встроенные."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    @staticmethod
    def _row(tone: Any) -> dict[str, Any]:
        row = {"тон": getattr(tone, "slug", None) or getattr(tone, "tone", ""),
               "название": getattr(tone, "title", "")}
        if getattr(tone, "prompt", None):
            row["указание"] = tone.prompt
        if getattr(tone, "installs_count", None):
            row["установок"] = tone.installs_count
        if getattr(tone, "creator", False):
            row["ваш"] = True
        if getattr(tone, "id", None) is not None and getattr(tone, "access_hash", None) is not None:
            row["ссылка"] = f"{tone.id}:{tone.access_hash}"
        return row

    async def listing(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.aicompose.GetTonesRequest(hash=0))
        return [self._row(t) for t in getattr(result, "tones", None) or []]

    async def show(self, tone: str) -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.aicompose.GetToneRequest(tone=_tone(tone)))
        return self._row(getattr(result, "tone", result))

    async def example(self, tone: str, num: int = 0) -> dict[str, Any]:
        """Как этот тон звучит — сервер показывает образец."""
        from telethon.tl import functions

        result = await self._call(functions.aicompose.GetToneExampleRequest(
            tone=_tone(tone), num=num))
        sample = getattr(result, "example", result)
        return {"тон": tone,
                "до": getattr(getattr(sample, "text_before", None), "text", None),
                "после": getattr(getattr(sample, "text_after", None), "text", None)}

    async def create(self, title: str, prompt: str, *, emoji_id: int = 0,
                     credit: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.aicompose.CreateToneRequest(
            emoji_id=emoji_id, title=title.strip(), prompt=prompt.strip(),
            display_author=credit or None))
        return self._row(getattr(result, "tone", result))

    async def update(self, tone: str, *, title: str = "", prompt: str = "",
                     emoji_id: int | None = None, credit: bool | None = None) -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.aicompose.UpdateToneRequest(
            tone=_tone(tone), title=title or None, prompt=prompt or None,
            emoji_id=emoji_id, display_author=credit))
        return self._row(getattr(result, "tone", result))

    async def save(self, tone: str, *, unsave: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.aicompose.SaveToneRequest(tone=_tone(tone), unsave=unsave))
        return {"тон": tone, "сохранён": not unsave}

    async def delete(self, tone: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.aicompose.DeleteToneRequest(tone=_tone(tone)))
        return {"тон": tone, "удалён": True}
