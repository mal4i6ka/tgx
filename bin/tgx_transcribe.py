#!/usr/bin/env python3
"""Расшифровка голосовых: core.telegram.org/api/transcribe.

Ответ на `messages.transcribeAudio` почти всегда приходит незаконченным: с
флагом `pending` и пустым или обрывочным текстом. Готовая расшифровка прилетает
позже отдельным апдейтом `updateTranscribedAudio` с тем же `transcription_id`.
Поэтому здесь обработчик апдейта ставится *до* запроса — иначе быстрый ответ
успевает проскочить мимо, и команда молча возвращает пустую строку.

Без Telegram Premium расшифровок дают `transcribe_audio_trial_weekly_number`
в неделю, каждая не длиннее `transcribe_audio_trial_duration_max` секунд;
в супергруппе с бустом от `group_transcribe_level_min` они не тратятся.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

CONFIG_KEYS = ("transcribe_audio_trial_weekly_number", "transcribe_audio_trial_duration_max",
               "transcribe_audio_trial_cooldown_until", "group_transcribe_level_min")

# Сколько ждать готовую расшифровку, если сервер ответил pending.
WAIT_SECONDS = 60.0


class TranscribeError(RuntimeError):
    """Расшифровка не вышла — с объяснением, что делать."""


def is_voice(message: Any) -> bool:
    """Расшифровать можно голосовое или кружок, но не музыку и не файл."""
    from telethon.tl import types

    document = getattr(getattr(message, "media", None), "document", None)
    for attribute in getattr(document, "attributes", None) or []:
        if isinstance(attribute, types.DocumentAttributeAudio) and getattr(attribute, "voice", False):
            return True
        if isinstance(attribute, types.DocumentAttributeVideo) and getattr(attribute, "round_message", False):
            return True
    return False


def describe_kind(message: Any) -> str:
    """Чем оказалось сообщение — чтобы отказ был понятным, а не «нельзя»."""
    from telethon.tl import types

    media = getattr(message, "media", None)
    if media is None:
        return "текстовое сообщение"
    document = getattr(media, "document", None)
    if document is None:
        return "фото или другое вложение"
    for attribute in getattr(document, "attributes", None) or []:
        if isinstance(attribute, types.DocumentAttributeAudio):
            return "музыка" if not getattr(attribute, "voice", False) else "голосовое"
        if isinstance(attribute, types.DocumentAttributeVideo):
            return "видео"
    return "файл"


class Transcriber:
    """Запрос расшифровки, ожидание готового текста и оценка результата."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def status(self) -> dict[str, Any]:
        """Доступна ли расшифровка этому аккаунту и на каких условиях."""
        from telethon.tl import functions

        me = await self.client.get_me()
        limits: dict[str, Any] = {}
        try:
            config = await self.client(functions.help.GetAppConfigRequest(hash=0))
            for item in getattr(getattr(config, "config", None), "value", None) or []:
                if item.key in CONFIG_KEYS:
                    limits[item.key] = getattr(item.value, "value", None)
        except Exception:
            pass
        premium = bool(getattr(me, "premium", False))
        return {
            "premium": premium,
            "available": premium or bool(limits.get("transcribe_audio_trial_weekly_number")),
            "free_per_week": limits.get("transcribe_audio_trial_weekly_number"),
            "free_max_seconds": limits.get("transcribe_audio_trial_duration_max"),
            "group_boost_level": limits.get("group_transcribe_level_min"),
        }

    async def transcribe(self, peer: Any, msg_id: int, *, wait: float = WAIT_SECONDS,
                         check_kind: bool = True) -> dict[str, Any]:
        """Расшифровать голосовое или кружок и дождаться готового текста."""
        from telethon.tl import functions, types

        entity = await self.client.get_input_entity(peer)
        msg_id = int(msg_id)

        if check_kind:
            message = await self.client.get_messages(entity, ids=msg_id)
            if message is None:
                raise TranscribeError(f"сообщения {msg_id} в этом чате нет")
            if not is_voice(message):
                raise TranscribeError(
                    f"расшифровать можно голосовое или кружок, а это {describe_kind(message)}")

        done: asyncio.Future = asyncio.get_running_loop().create_future()
        wanted: dict[str, Any] = {}

        async def on_update(update: Any) -> None:
            # Апдейт приходит по transcription_id, а он известен только после
            # ответа на запрос — поэтому сверяем ещё и по сообщению.
            if not isinstance(update, types.UpdateTranscribedAudio):
                return
            if wanted and update.transcription_id != wanted.get("id"):
                return
            if update.msg_id != msg_id:
                return
            if not update.pending and not done.done():
                done.set_result(update.text)

        from telethon import events

        handler = self.client.add_event_handler(on_update, events.Raw)
        try:
            try:
                result = await self.client(functions.messages.TranscribeAudioRequest(
                    peer=entity, msg_id=msg_id))
            except Exception as exc:
                raise self._explain(exc) from exc

            wanted["id"] = result.transcription_id
            text = result.text or ""
            pending = bool(getattr(result, "pending", False))
            if pending:
                try:
                    text = await asyncio.wait_for(done, timeout=wait)
                    pending = False
                except asyncio.TimeoutError:
                    pass                       # отдадим что есть и честно скажем, что не дождались
        finally:
            self.client.remove_event_handler(on_update, handler)

        remains = getattr(result, "trial_remains_num", None)
        until = getattr(result, "trial_remains_until_date", None)
        return {
            # Имя чата не выдумываем: его знает тот, кто вызвал, — а `str(peer)`
            # печатает весь репр сущности Telethon и делает вывод нечитаемым.
            "message_id": msg_id,
            "transcription_id": result.transcription_id,
            "text": text,
            "pending": pending,
            "free_left": remains,
            "free_reset": datetime.fromtimestamp(until, tz=timezone.utc).isoformat() if until else None,
        }

    async def rate(self, peer: Any, msg_id: int, transcription_id: int, good: bool) -> dict[str, Any]:
        """Оценить расшифровку — это влияет на качество распознавания."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(peer)
        await self.client(functions.messages.RateTranscribedAudioRequest(
            peer=entity, msg_id=int(msg_id),
            transcription_id=int(transcription_id), good=bool(good)))
        return {"message_id": int(msg_id), "rated": "хорошо" if good else "плохо"}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        text = str(exc)
        hints = {
            "TRANSCRIPTION_FAILED": "Telegram не смог разобрать эту запись",
            "PREMIUM_ACCOUNT_REQUIRED": "бесплатные расшифровки на этой неделе кончились; "
                                        "нужен Telegram Premium — см. tgx transcribe status",
            "MSG_ID_INVALID": "сообщения с таким id в этом чате нет",
            "MSG_VOICE_MISSING": "в этом сообщении нет голосовой записи",
            "PEER_ID_INVALID": "чат не найден",
        }
        for code, message in hints.items():
            if code in text:
                return TranscribeError(message)
        if "FLOOD_WAIT" in text:
            seconds = "".join(c for c in text.split("FLOOD_WAIT")[-1] if c.isdigit())
            return TranscribeError(f"слишком часто; подождите {seconds or 'немного'} секунд")
        return exc
