#!/usr/bin/env python3
"""Групповые звонки: управление из терминала, звук — снаружи.

Голос в звонке идёт по WebRTC, и на чистом Python его не сыграть — а вот всё
вокруг звука делается обычными вызовами: начать и завершить, позвать, заглушить,
поднять руку, записать, выдать ссылку, получить адрес для трансляции, писать в
чат звонка.

Поэтому разделение здесь честное. Терминал распоряжается звонком; чтобы
подключиться со звуком, он открывает настоящий клиент по ссылке `tg://`, а для
живой картины участников поднимает маленькую страницу — там видно, кто говорит,
кто заглушён и кто поднял руку.

Звонок адресуется не чатом, а своим `InputGroupCall`, и он появляется только
после того, как звонок создан: у чата без активного звонка его нет.
"""
from __future__ import annotations

from typing import Any, Sequence


class CallError(RuntimeError):
    """Действие со звонком, которое не удалось выполнить."""


def participant_row(participant: Any, names: dict[int, str]) -> dict[str, Any]:
    peer = getattr(participant, "peer", None)
    who = (getattr(peer, "user_id", None) or getattr(peer, "channel_id", None)
           or getattr(peer, "chat_id", None))
    return {
        "кто": names.get(who, who),
        "заглушён": bool(getattr(participant, "muted", False)),
        "может включить себя": bool(getattr(participant, "can_self_unmute", False)),
        "рука поднята": getattr(participant, "raise_hand_rating", None) is not None,
        "видео": bool(getattr(participant, "video", None)),
        "громкость": (getattr(participant, "volume", None) or 10000) // 100,
        "только слушает": bool(getattr(participant, "just_joined", False)),
    }


class Calls:
    """Групповые звонки и голосовые чаты."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise self._explain(exc) from exc

    async def _ref(self, chat: Any) -> Any:
        """`InputGroupCall` чата. Его нет, пока звонок не создан."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        full = await self.client(functions.channels.GetFullChannelRequest(channel=entity))
        call = getattr(getattr(full, "full_chat", None), "call", None)
        if call is None:
            raise CallError("в этом чате нет активного звонка — начните его: tgx call start")
        return call

    async def start(self, chat: Any, *, title: str = "", rtmp: bool = False,
                    schedule: Any = None) -> dict[str, Any]:
        """Начать голосовой чат или назначить его на время."""
        from telethon import helpers
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        result = await self._call(functions.phone.CreateGroupCallRequest(
            # random_id у звонка — 32-битное, а generate_random_long даёт 64:
            # сервер такое не примет, поэтому берём младшую половину.
            peer=entity, random_id=helpers.generate_random_long() & 0x7FFFFFFF,
            title=title or None, rtmp_stream=rtmp or None, schedule_date=schedule))
        for update in getattr(result, "updates", None) or []:
            call = getattr(update, "call", None)
            if call is not None:
                return {"звонок начат": getattr(call, "id", None), "название": title or None,
                        "трансляция": rtmp}
        return {"звонок начат": True}

    async def info(self, chat: Any) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        result = await self._call(functions.phone.GetGroupCallRequest(call=ref, limit=1))
        call = getattr(result, "call", None)
        schedule = getattr(call, "schedule_date", None)
        return {
            "id": getattr(call, "id", None),
            "название": getattr(call, "title", None),
            "участников": getattr(call, "participants_count", None),
            "идёт запись": bool(getattr(call, "record_video_active", False))
            or getattr(call, "record_start_date", None) is not None,
            "входят заглушёнными": bool(getattr(call, "join_muted", False)),
            "трансляция": bool(getattr(call, "rtmp_stream", False)),
            "назначен на": schedule.isoformat(timespec="minutes") if schedule else None,
            "чат внутри звонка": bool(getattr(call, "messages_enabled", False)),
        }

    async def participants(self, chat: Any, limit: int = 50) -> list[dict[str, Any]]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        result = await self._call(functions.phone.GetGroupParticipantsRequest(
            call=ref, ids=[], sources=[], offset="", limit=int(limit)))
        names = {}
        for holder in ("users", "chats"):
            for item in getattr(result, holder, None) or []:
                names[item.id] = (getattr(item, "title", None) or getattr(item, "username", None)
                                  or " ".join(filter(None, [getattr(item, "first_name", None),
                                                            getattr(item, "last_name", None)])))
        return [participant_row(p, names)
                for p in (getattr(result, "participants", None) or [])]

    async def invite(self, chat: Any, users: Sequence[Any]) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        people = [await self.client.get_input_entity(u) for u in users]
        await self._call(functions.phone.InviteToGroupCallRequest(call=ref, users=people))
        return {"приглашено": len(people)}

    async def link(self, chat: Any, *, speaker: bool = False) -> dict[str, Any]:
        """Ссылка на звонок. `speaker` — с правом говорить, иначе только слушать."""
        from telethon.tl import functions

        ref = await self._ref(chat)
        result = await self._call(functions.phone.ExportGroupCallInviteRequest(
            call=ref, can_self_unmute=speaker or None))
        return {"ссылка": getattr(result, "link", None),
                "право говорить": speaker}

    async def mute(self, chat: Any, user: Any, muted: bool = True, *,
                   volume: int | None = None) -> dict[str, Any]:
        """Заглушить участника или вернуть ему слово."""
        from telethon.tl import functions

        ref = await self._ref(chat)
        person = await self.client.get_input_entity(user)
        await self._call(functions.phone.EditGroupCallParticipantRequest(
            call=ref, participant=person, muted=muted,
            volume=int(volume) * 100 if volume is not None else None,
            raise_hand=None, video_stopped=None, video_paused=None,
            presentation_paused=None))
        return {"участник": str(user), "заглушён": muted,
                "громкость": volume if volume is not None else "не менялась"}

    async def raise_hand(self, chat: Any, up: bool = True) -> dict[str, Any]:
        from telethon.tl import functions, types

        ref = await self._ref(chat)
        await self._call(functions.phone.EditGroupCallParticipantRequest(
            call=ref, participant=types.InputPeerSelf(), muted=None, volume=None,
            raise_hand=up, video_stopped=None, video_paused=None, presentation_paused=None))
        return {"рука": "поднята" if up else "опущена"}

    async def title(self, chat: Any, text: str) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.EditGroupCallTitleRequest(call=ref, title=text.strip()))
        return {"название звонка": text.strip()}

    async def record(self, chat: Any, *, start: bool = True, title: str = "",
                     video: bool = False, portrait: bool = False) -> dict[str, Any]:
        """Запись звонка. Готовая запись приходит в «Избранное»."""
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.ToggleGroupCallRecordRequest(
            call=ref, start=start, video=video or None, title=title or None,
            video_portrait=portrait or None))
        return {"запись": "идёт" if start else "остановлена",
                "видео": video,
                "куда придёт": "в «Избранное» после завершения" if start else None}

    async def settings(self, chat: Any, *, join_muted: bool | None = None,
                       messages: bool | None = None,
                       reset_link: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.ToggleGroupCallSettingsRequest(
            call=ref, join_muted=join_muted, messages_enabled=messages,
            reset_invite_hash=reset_link or None, send_paid_messages_stars=None))
        return {"входят заглушёнными": join_muted if join_muted is not None else "не менялось",
                "чат внутри звонка": messages if messages is not None else "не менялось",
                "ссылка обновлена": reset_link}

    async def say(self, chat: Any, text: str) -> dict[str, Any]:
        """Написать в чат внутри звонка."""
        from telethon import helpers
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.SendGroupCallMessageRequest(
            call=ref, message=text, random_id=helpers.generate_random_long(),
            allow_paid_stars=None, send_as=None))
        return {"сказано": text[:60]}

    async def join_as(self, chat: Any) -> list[dict[str, Any]]:
        """От чьего имени можно войти в звонок."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        result = await self._call(functions.phone.GetGroupCallJoinAsRequest(peer=entity))
        out = []
        for holder in ("users", "chats"):
            for item in getattr(result, holder, None) or []:
                out.append({"имя": getattr(item, "title", None) or getattr(item, "username", None)
                            or getattr(item, "first_name", None), "id": item.id})
        return out

    async def stream_url(self, chat: Any, *, revoke: bool = False) -> dict[str, Any]:
        """Адрес и ключ для трансляции в звонок из внешней программы."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(chat)
        result = await self._call(functions.phone.GetGroupCallStreamRtmpUrlRequest(
            peer=entity, revoke=revoke or None, live_story=None))
        return {"адрес": getattr(result, "url", None), "ключ": getattr(result, "key", None),
                "примечание": "ключ — это пароль трансляции; не публикуйте его"}

    async def start_scheduled(self, chat: Any) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.StartScheduledGroupCallRequest(call=ref))
        return {"назначенный звонок": "начат"}

    async def end(self, chat: Any) -> dict[str, Any]:
        """Завершить звонок для всех."""
        from telethon.tl import functions

        ref = await self._ref(chat)
        await self._call(functions.phone.DiscardGroupCallRequest(call=ref))
        return {"звонок завершён": True}

    async def stars(self, chat: Any) -> dict[str, Any]:
        from telethon.tl import functions

        ref = await self._ref(chat)
        result = await self._call(functions.phone.GetGroupCallStarsRequest(call=ref))
        return {"звёзд собрано": getattr(result, "stars", None) or 0}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        import tgx_net

        hints = {
            "GROUPCALL_INVALID": "этот звонок уже завершён",
            "GROUPCALL_NOT_MODIFIED": "ничего не изменилось",
            "GROUPCALL_FORBIDDEN": "нет доступа к этому звонку",
            "CHAT_ADMIN_REQUIRED": "звонком распоряжается администратор",
            "CALL_ALREADY_ACCEPTED": "звонок уже начат",
            "PARTICIPANT_JOIN_MISSING": "этот участник не в звонке",
            "SCHEDULE_DATE_INVALID": "назначить звонок можно только на будущее",
            "RTMP_STREAM_NOT_ALLOWED": "трансляция в этом чате не разрешена",
            "PUBLIC_CHANNEL_MISSING": "ссылку на звонок выдают только публичные чаты — "
                                      "у приватного её не существует, зовите по одному",
            "UNTIL_DATE_INVALID": "неверная дата",
        }
        return tgx_net.explain(exc, hints, CallError)
