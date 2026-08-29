"""Уведомления и заявки: чем вас беспокоят и кого вы впускаете.

Две темы, которые в графическом клиенте лежат в разных местах, а на деле про
одно — управление вниманием. Кто может вас разбудить и кто может войти.

Тишина здесь считается не в «часах молчания», а в отметке времени, до которой
чат молчит. Поэтому «заглушить на два часа» — это `сейчас + 7200`, а «навсегда»
— далёкая дата, которую Telegram трактует как «никогда». Мы прячем эту
арифметику, но храним её честно: в ответе видно, до какого момента тихо.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import tgx_net


class NotifyError(RuntimeError):
    """Не вышло."""


HINTS = {
    "PEER_ID_INVALID": "такого чата нет",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "HIDE_REQUESTER_MISSING": "заявка уже обработана — кем-то другим или раньше вами",
    "USER_ALREADY_PARTICIPANT": "человек уже в чате",
    "INVITE_HASH_EXPIRED": "ссылка больше не действует",
    "INVITE_REQUEST_SENT": "заявка отправлена и ждёт решения",
    "REACTIONS_TOO_MANY": "столько реакций Telegram в одном чате не разрешает",
    "REACTION_INVALID": "такой реакции нет; премиальные эмодзи задаются идентификатором",
    "CHAT_NOT_MODIFIED": "и так уже так",
    "USERS_TOO_MUCH": "слишком много за раз",
}

# «навсегда» у Telegram — это отметка далеко в будущем, а не отдельный признак
FOREVER = 2 ** 31 - 1

SPANS = {"1h": 3600, "8h": 28800, "2d": 172800, "forever": FOREVER}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, NotifyError)


def _when(value: Any) -> str | None:
    """Отметку тишины — в человеческий вид.

    Незаглушённый чат сервер отдаёт нулём, а Telethon превращает ноль в начало
    эпохи. Показать «тихо до 1 января 1970 года» — значит соврать, поэтому и
    ноль, и дату в прошлом считаем отсутствием тишины. «Навсегда» — это дата на
    краю тридцатидвухбитного времени, её тоже называем словом.
    """
    if isinstance(value, datetime):
        seconds = int(value.timestamp())
    elif isinstance(value, int):
        seconds = value
    else:
        return None
    if seconds <= 0:
        return None
    if seconds >= FOREVER - 86400:
        return "навсегда"
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    if moment <= datetime.now(timezone.utc):
        return None  # срок тишины уже вышел
    return moment.isoformat(timespec="seconds")


def mute_until(span: str) -> datetime | None:
    """«2h», «30m», «forever», «off» → до какого момента молчать."""
    text = (span or "").strip().lower()
    if text in {"off", "0", ""}:
        return None
    if text in SPANS:
        seconds = SPANS[text]
    elif text[-1] in "smhd" and text[:-1].isdigit():
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
        seconds = int(text[:-1]) * unit
    elif text.isdigit():
        seconds = int(text)
    else:
        raise NotifyError(f"не понял срок «{span}»; годится 30m, 2h, 3d, forever или off")
    if seconds >= FOREVER - 86400:
        return datetime.fromtimestamp(FOREVER, timezone.utc)
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class Notify:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    @staticmethod
    def _scope(kind: str, peer: Any = None) -> Any:
        """Чат, или все личные, или все группы, или все каналы."""
        from telethon.tl import types

        if peer is not None:
            return types.InputNotifyPeer(peer=peer)
        return {"users": types.InputNotifyUsers(), "chats": types.InputNotifyChats(),
                "channels": types.InputNotifyBroadcasts(),
                "forum": types.InputNotifyForumTopic}.get(kind, types.InputNotifyUsers())

    async def get(self, *, peer: Any = None, kind: str = "users") -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.account.GetNotifySettingsRequest(
            peer=self._scope(kind, peer)))
        silent = getattr(result, "mute_until", None)
        return {"где": "этот чат" if peer is not None else kind,
                "тихо до": _when(silent) or "не заглушено",
                "показывать текст": getattr(result, "show_previews", None),
                "истории": {True: "молча", False: "со звуком"}.get(
                    getattr(result, "stories_muted", None), "как везде")}

    async def set(self, *, peer: Any = None, kind: str = "users", span: str = "",
                  previews: bool | None = None, stories: bool | None = None) -> dict[str, Any]:
        from telethon.tl import functions, types

        until = mute_until(span) if span else None
        settings = types.InputPeerNotifySettings(
            show_previews=previews,
            silent=bool(span) and span not in {"off", "0"} or None,
            mute_until=until,
            stories_muted=None if stories is None else not stories)
        await self._call(functions.account.UpdateNotifySettingsRequest(
            peer=self._scope(kind, peer), settings=settings))
        return {"где": "этот чат" if peer is not None else kind,
                "тихо до": _when(until) or "не заглушено"}

    async def reset(self) -> dict[str, Any]:
        """Сбросить все настройки уведомлений к исходным."""
        from telethon.tl import functions

        await self._call(functions.account.ResetNotifySettingsRequest())
        return {"уведомления": "сброшены к исходным"}

    async def reactions(self, *, from_whom: str = "", previews: bool | None = None) -> dict[str, Any]:
        """Кто может уведомлять вас реакциями: все или только контакты."""
        from telethon.tl import functions, types

        if not from_whom and previews is None:
            result = await self._call(functions.account.GetReactionsNotifySettingsRequest())
            source = type(getattr(result, "messages_notify_from", None)).__name__
            return {"реакции от": "контактов" if "Contacts" in source else "всех",
                    "показывать текст": getattr(result, "show_previews", None)}
        whom = (types.ReactionNotificationsFromContacts() if from_whom == "contacts"
                else types.ReactionNotificationsFromAll())
        settings = types.ReactionsNotifySettings(
            sound=types.NotificationSoundDefault(),
            show_previews=True if previews is None else previews,
            messages_notify_from=whom, stories_notify_from=whom)
        await self._call(functions.account.SetReactionsNotifySettingsRequest(settings=settings))
        return {"реакции от": "контактов" if from_whom == "contacts" else "всех"}

    async def new_contacts(self, silent: bool) -> dict[str, Any]:
        """Сообщать ли, когда ваш контакт зарегистрировался в Telegram."""
        from telethon.tl import functions

        await self._call(functions.account.SetContactSignUpNotificationRequest(silent=silent))
        return {"о новых контактах": "молчать" if silent else "сообщать"}


class Requests:
    """Заявки на вступление — оборотная сторона именных приглашений."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def decide(self, peer: Any, who: Any, approved: bool) -> dict[str, Any]:
        from telethon.tl import functions

        user = await self.client.get_input_entity(who)
        await self._call(functions.messages.HideChatJoinRequestRequest(
            peer=peer, user_id=user, approved=approved or None))
        return {"заявка": "принята" if approved else "отклонена"}

    async def decide_all(self, peer: Any, approved: bool, *, link: str = "") -> dict[str, Any]:
        """Все разом — или все по одной конкретной ссылке."""
        from telethon.tl import functions

        await self._call(functions.messages.HideAllChatJoinRequestsRequest(
            peer=peer, approved=approved or None, link=link or None))
        return {"все заявки": "приняты" if approved else "отклонены",
                "по ссылке": link or "любой"}

    async def admins_with_invites(self, peer: Any) -> list[dict[str, Any]]:
        """Кто из админов сколько ссылок наделал."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetAdminsWithInvitesRequest(peer=peer))
        names = {u.id: (getattr(u, "username", None) or getattr(u, "first_name", None))
                 for u in getattr(result, "users", None) or []}
        return [{"админ": names.get(a.admin_id, a.admin_id), "ссылок": a.invites_count}
                for a in getattr(result, "admins", None) or []]

    async def edit_link(self, peer: Any, link: str, *, title: str = "",
                        limit: int | None = None, expires: str = "",
                        request_needed: bool | None = None,
                        revoke: bool = False) -> dict[str, Any]:
        """Поправить ссылку-приглашение, не выпуская новую."""
        from telethon.tl import functions

        until = mute_until(expires) if expires else None
        result = await self._call(functions.messages.EditExportedChatInviteRequest(
            peer=peer, link=link, revoked=revoke or None, expire_date=until,
            usage_limit=limit, request_needed=request_needed, title=title or None))
        fresh = getattr(result, "invite", None)
        return {"ссылка": getattr(fresh, "link", link),
                "название": getattr(fresh, "title", None),
                "предел": getattr(fresh, "usage_limit", None),
                "до": _when(getattr(fresh, "expire_date", None)),
                "по заявке": getattr(fresh, "request_needed", None),
                "отозвана": bool(getattr(fresh, "revoked", False))}

    async def purge_revoked(self, peer: Any, *, admin: Any = None) -> dict[str, Any]:
        """Выбросить отозванные ссылки — они копятся и мешают читать список."""
        from telethon.tl import functions, types

        who = await self.client.get_input_entity(admin) if admin else types.InputUserSelf()
        await self._call(functions.messages.DeleteRevokedExportedChatInvitesRequest(
            peer=peer, admin_id=who))
        return {"отозванные ссылки": "убраны"}


class Reactions:
    """Какие реакции разрешены и какая у вас по умолчанию."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def available(self) -> list[str]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetAvailableReactionsRequest(hash=0))
        return [r.reaction for r in getattr(result, "reactions", None) or []
                if not getattr(r, "inactive", False)]

    async def top(self, limit: int = 20) -> list[str]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetTopReactionsRequest(limit=limit, hash=0))
        return [getattr(r, "emoticon", None) or str(getattr(r, "document_id", ""))
                for r in getattr(result, "reactions", None) or []]

    async def set_default(self, emoji: str) -> dict[str, Any]:
        from telethon.tl import functions, types

        reaction = (types.ReactionCustomEmoji(document_id=int(emoji)) if emoji.isdigit()
                    else types.ReactionEmoji(emoticon=emoji))
        await self._call(functions.messages.SetDefaultReactionRequest(reaction=reaction))
        return {"реакция по умолчанию": emoji}

    async def allow(self, peer: Any, emojis: list[str], *, limit: int | None = None,
                    paid: bool | None = None) -> dict[str, Any]:
        """Что можно ставить в этом чате. Пустой список — запретить все."""
        from telethon.tl import functions, types

        if not emojis:
            allowed: Any = types.ChatReactionsNone()
            told = "запрещены"
        elif emojis == ["all"]:
            allowed = types.ChatReactionsAll(allow_custom=True)
            told = "любые"
        else:
            allowed = types.ChatReactionsSome(reactions=[
                types.ReactionCustomEmoji(document_id=int(e)) if e.isdigit()
                else types.ReactionEmoji(emoticon=e) for e in emojis])
            told = ", ".join(emojis)
        await self._call(functions.messages.SetChatAvailableReactionsRequest(
            peer=peer, available_reactions=allowed, reactions_limit=limit,
            paid_enabled=paid))
        return {"реакции в чате": told, "не больше": limit}
