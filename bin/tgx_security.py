#!/usr/bin/env python3
"""Сессии, приватность и сроки — то, что отвечает за безопасность аккаунта.

Здесь две разные вещи, которые в интерфейсе Telegram лежат рядом, а в API
далеко друг от друга.

**Сессии** — устройства и сайты, вошедшие в аккаунт. Их две породы: обычные
входы (`authorizations`) и входы через «Войти в Telegram» на сайтах
(`web authorizations`). Сбрасываются они разными методами, и сброс одного вида
не трогает другой — отсюда две команды вместо одной.

**Приватность** задаётся отдельно для каждого предмета: номер, последний визит,
фото, звонки, голосовые, дата рождения и прочее. Правила складываются, как и у
историй: «контактам, кроме этих двоих» — это три правила, а не одно.

Правило без явного «всем/контактам/никому» опасно тем, что молча оставляет
прежнюю аудиторию, поэтому здесь основа обязательна.
"""
from __future__ import annotations

from typing import Any, Sequence

# Предметы приватности: короткое имя → ключ Telegram.
TOPICS = {
    "phone": "PhoneNumber",
    "last-seen": "StatusTimestamp",
    "photo": "ProfilePhoto",
    "bio": "About",
    "birthday": "Birthday",
    "forwards": "Forwards",
    "calls": "PhoneCall",
    "call-p2p": "PhoneP2P",
    "voice": "VoiceMessages",
    "invites": "ChatInvite",
    "by-phone": "AddedByPhone",
    "gifts": "StarGiftsAutoSave",
    "music": "SavedMusic",
    "paid-messages": "NoPaidMessages",
}

AUDIENCES = ("everyone", "contacts", "nobody")


class SecurityError(RuntimeError):
    """Настройка безопасности, которую не удалось прочитать или изменить."""


def topic_key(name: str) -> Any:
    from telethon.tl import types

    short = (name or "").strip().lower()
    if short not in TOPICS:
        raise SecurityError(f"предмет «{name}» неизвестен; есть: {', '.join(sorted(TOPICS))}")
    return getattr(types, f"InputPrivacyKey{TOPICS[short]}")()


def rules(audience: str, allow: Sequence[Any] = (), deny: Sequence[Any] = ()) -> list[Any]:
    """Правила приватности. Основа обязательна — иначе аудитория остаётся прежней."""
    from telethon.tl import types

    audience = (audience or "").strip().lower()
    if audience not in AUDIENCES:
        raise SecurityError(f"аудитория «{audience}» неизвестна; есть: {', '.join(AUDIENCES)}")
    base = {"everyone": types.InputPrivacyValueAllowAll,
            "contacts": types.InputPrivacyValueAllowContacts,
            "nobody": types.InputPrivacyValueDisallowAll}[audience]
    out: list[Any] = [base()]
    if allow:
        out.append(types.InputPrivacyValueAllowUsers(users=list(allow)))
    if deny:
        out.append(types.InputPrivacyValueDisallowUsers(users=list(deny)))
    return out


def describe_rules(items: Sequence[Any]) -> str:
    """Правила сервера → человеческая строка.

    Telegram хранит «только контактам» как два правила: разрешить контактам и
    запретить всем остальным. Печатать их подряд — получается «контактам,
    никому», что читается как противоречие. Поэтому основа выбирается одна, а
    исключения дописываются к ней.
    """
    names = [type(r).__name__.replace("PrivacyValue", "") for r in items]
    base = "не задано"
    if "AllowAll" in names:
        base = "всем"
    elif "AllowContacts" in names:
        base = "контактам"
    elif "AllowCloseFriends" in names:
        base = "близким друзьям"
    elif "DisallowAll" in names:
        base = "никому"

    extras = []
    counts = {}
    for rule, name in zip(items, names):
        if name in {"AllowUsers", "DisallowUsers", "AllowChatParticipants",
                    "DisallowChatParticipants", "AllowBots", "DisallowBots",
                    "AllowPremium"}:
            counts[name] = counts.get(name, 0) + len(getattr(rule, "users", None)
                                                     or getattr(rule, "chats", None) or [1])
    words = {"AllowUsers": "плюс {} выбранных", "DisallowUsers": "кроме {} выбранных",
             "AllowChatParticipants": "плюс участники {} чатов",
             "DisallowChatParticipants": "кроме участников {} чатов",
             "AllowBots": "плюс боты", "DisallowBots": "кроме ботов",
             "AllowPremium": "плюс Premium"}
    for name, count in counts.items():
        template = words.get(name, name)
        extras.append(template.format(count) if "{}" in template else template)
    return base + (" · " + ", ".join(extras) if extras else "")


class Security:
    """Сессии, приватность, сроки и уведомления."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise self._explain(exc) from exc

    # ── сессии ───────────────────────────────────────────────────────────────
    async def sessions(self) -> list[dict[str, Any]]:
        """Устройства, вошедшие в аккаунт. Текущее помечено."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetAuthorizationsRequest())
        out = []
        for item in getattr(result, "authorizations", None) or []:
            active = getattr(item, "date_active", None)
            out.append({
                "hash": getattr(item, "hash", None),
                "устройство": " ".join(filter(None, [getattr(item, "device_model", None),
                                                     getattr(item, "platform", None)])),
                "программа": " ".join(filter(None, [getattr(item, "app_name", None),
                                                    getattr(item, "app_version", None)])),
                "откуда": " ".join(filter(None, [getattr(item, "country", None),
                                                 getattr(item, "ip", None)])),
                "активна": active.isoformat(timespec="minutes") if active else None,
                "текущая": bool(getattr(item, "current", False)),
                "официальная": bool(getattr(item, "official_app", False)),
                "звонки запрещены": bool(getattr(item, "call_requests_disabled", False)),
            })
        return out

    async def websites(self) -> list[dict[str, Any]]:
        """Сайты, куда вы входили через Telegram. Сбрасываются отдельно от сессий."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetWebAuthorizationsRequest())
        names = {u.id: (u.username or getattr(u, "first_name", None))
                 for u in (getattr(result, "users", None) or [])}
        out = []
        for item in getattr(result, "authorizations", None) or []:
            out.append({"hash": getattr(item, "hash", None),
                        "сайт": getattr(item, "domain", None),
                        "через бота": names.get(getattr(item, "bot_id", None)),
                        "браузер": getattr(item, "browser", None),
                        "откуда": " ".join(filter(None, [getattr(item, "region", None),
                                                         getattr(item, "ip", None)]))})
        return out

    async def close_session(self, session_hash: int) -> dict[str, Any]:
        """Завершить одну сессию. Текущую завершить нельзя — это выход."""
        from telethon.tl import functions

        await self._call(functions.account.ResetAuthorizationRequest(hash=int(session_hash)))
        return {"сессия завершена": int(session_hash)}

    async def close_website(self, session_hash: int | None = None) -> dict[str, Any]:
        """Отозвать доступ у одного сайта или у всех сразу."""
        from telethon.tl import functions

        if session_hash is None:
            await self._call(functions.account.ResetWebAuthorizationsRequest())
            return {"отозваны": "все сайты"}
        await self._call(functions.account.ResetWebAuthorizationRequest(hash=int(session_hash)))
        return {"отозван сайт": int(session_hash)}

    async def session_settings(self, session_hash: int, *, calls: bool | None = None,
                               secret_chats: bool | None = None) -> dict[str, Any]:
        """Что разрешено конкретной сессии."""
        from telethon.tl import functions

        await self._call(functions.account.ChangeAuthorizationSettingsRequest(
            hash=int(session_hash), confirmed=None,
            call_requests_disabled=None if calls is None else not calls,
            encrypted_requests_disabled=None if secret_chats is None else not secret_chats))
        return {"сессия": int(session_hash),
                "звонки": "не менялось" if calls is None else calls,
                "секретные чаты": "не менялось" if secret_chats is None else secret_chats}

    async def session_ttl(self, days: int | None = None) -> dict[str, Any]:
        """Через сколько дней бездействия сессия закрывается сама."""
        from telethon.tl import functions

        if days is None:
            result = await self._call(functions.account.GetAuthorizationsRequest())
            return {"дней бездействия до выхода": getattr(result, "authorization_ttl_days", None)}
        await self._call(functions.account.SetAuthorizationTTLRequest(
            authorization_ttl_days=int(days)))
        return {"дней бездействия до выхода": int(days)}

    # ── приватность ──────────────────────────────────────────────────────────
    async def privacy(self, topic: str | None = None) -> list[dict[str, Any]]:
        """Кто что о вас видит. Без предмета — сводка по всем."""
        from telethon.tl import functions

        wanted = [topic] if topic else sorted(TOPICS)
        out = []
        for name in wanted:
            try:
                result = await self.client(functions.account.GetPrivacyRequest(
                    key=topic_key(name)))
            except Exception as exc:
                out.append({"предмет": name, "видно": f"недоступно ({type(exc).__name__})"})
                continue
            out.append({"предмет": name,
                        "видно": describe_rules(getattr(result, "rules", None) or [])})
        return out

    async def set_privacy(self, topic: str, audience: str, *, allow: Sequence[Any] = (),
                          deny: Sequence[Any] = ()) -> dict[str, Any]:
        from telethon.tl import functions

        allowed = [await self.client.get_input_entity(u) for u in allow]
        denied = [await self.client.get_input_entity(u) for u in deny]
        result = await self._call(functions.account.SetPrivacyRequest(
            key=topic_key(topic), rules=rules(audience, allowed, denied)))
        return {"предмет": topic,
                "стало": describe_rules(getattr(result, "rules", None) or [])}

    async def global_privacy(self) -> dict[str, Any]:
        """Настройки, действующие на весь аккаунт сразу."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetGlobalPrivacySettingsRequest())
        return {
            "архивировать новые чаты": bool(getattr(result, "archive_and_mute_new_noncontact_peers", False)),
            "скрывать статус прочтения": bool(getattr(result, "hide_read_marks", False)),
            "не пускать незнакомцев": bool(getattr(result, "new_noncontact_peers_require_premium", False)),
            "подарки только от контактов": bool(getattr(result, "display_gifts_button", False)),
        }

    async def set_global_privacy(self, *, archive_new: bool | None = None,
                                 hide_read: bool | None = None,
                                 premium_only: bool | None = None) -> dict[str, Any]:
        from telethon.tl import functions

        current = await self._call(functions.account.GetGlobalPrivacySettingsRequest())
        if archive_new is not None:
            current.archive_and_mute_new_noncontact_peers = archive_new
        if hide_read is not None:
            current.hide_read_marks = hide_read
        if premium_only is not None:
            current.new_noncontact_peers_require_premium = premium_only
        await self._call(functions.account.SetGlobalPrivacySettingsRequest(settings=current))
        return await self.global_privacy()

    # ── сроки и уведомления ──────────────────────────────────────────────────
    async def account_ttl(self, days: int | None = None) -> dict[str, Any]:
        """Через сколько месяцев бездействия аккаунт удаляется сам."""
        from telethon.tl import functions, types

        if days is None:
            result = await self._call(functions.account.GetAccountTTLRequest())
            return {"дней бездействия до удаления аккаунта": getattr(result, "days", None)}
        await self._call(functions.account.SetAccountTTLRequest(
            ttl=types.AccountDaysTTL(days=int(days))))
        return {"дней бездействия до удаления аккаунта": int(days)}

    async def notify_exceptions(self, limit: int = 30) -> list[dict[str, Any]]:
        """Чаты, для которых уведомления настроены отдельно."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetNotifyExceptionsRequest(
            compare_sound=True, compare_stories=None, peer=None))
        names = {}
        for holder in ("users", "chats"):
            for item in getattr(result, holder, None) or []:
                names[item.id] = getattr(item, "title", None) or getattr(item, "username", None)
        out = []
        for dialog in (getattr(result, "dialogs", None) or [])[:limit]:
            peer = getattr(dialog, "peer", None)
            who = (getattr(peer, "user_id", None) or getattr(peer, "channel_id", None)
                   or getattr(peer, "chat_id", None))
            settings = getattr(dialog, "notify_settings", None)
            until = getattr(settings, "mute_until", None)
            out.append({"чат": names.get(who, who),
                        "заглушён до": until.isoformat(timespec="minutes") if until else "нет",
                        "звук": getattr(settings, "sound", None) is not None})
        return out

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        import tgx_net

        hints = {
            "HASH_INVALID": "сессии с таким номером нет — список: tgx security sessions",
            "FRESH_RESET_AUTHORISATION_FORBIDDEN": "недавно вошедшую сессию нельзя завершить "
                                                   "сразу — Telegram ждёт сутки",
            "PRIVACY_KEY_INVALID": "этот предмет приватности сервер не знает",
            "PRIVACY_TOO_LONG": "слишком много исключений в правиле",
            "TTL_DAYS_INVALID": "такой срок не принимается",
            "AUTH_KEY_PERM_EMPTY": "эту сессию нельзя изменить",
        }
        return tgx_net.explain(exc, hints, SecurityError)
