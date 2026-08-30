#!/usr/bin/env python3
"""Контакты, чёрный список и поиск людей.

Три вещи, которые здесь легко перепутать и которые Telegram различает строго:

* **Контакт** — запись в вашей адресной книге. Удалить контакт не значит
  заблокировать: человек по-прежнему может писать.
* **Блокировка** — запрет писать вам. Она отдельная от контакта и имеет второй,
  независимый список: кому запрещено видеть ваши истории.
* **Близкие друзья** — отдельная отметка, влияющая только на видимость историй.

Заметка к контакту (`note`) видна лишь вам и живёт на серверах Telegram — это
не локальный файл, и её стоит воспринимать как часть аккаунта.
"""
from __future__ import annotations

from typing import Any, Sequence


class ContactError(RuntimeError):
    """Действие с контактами, которое не удалось выполнить."""


def person(user: Any) -> dict[str, Any]:
    """Пользователь → плоская запись."""
    name = " ".join(filter(None, [getattr(user, "first_name", None),
                                  getattr(user, "last_name", None)])) or None
    status = type(getattr(user, "status", None)).__name__.replace("UserStatus", "") or "?"
    return {
        "id": getattr(user, "id", None),
        "имя": name,
        "username": getattr(user, "username", None),
        "телефон": getattr(user, "phone", None),
        "бот": bool(getattr(user, "bot", False)),
        "взаимный": bool(getattr(user, "mutual_contact", False)),
        "близкий друг": bool(getattr(user, "close_friend", False)),
        "premium": bool(getattr(user, "premium", False)),
        "был": status,
    }


class Contacts:
    """Адресная книга, чёрный список и поиск."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def all(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self.client(functions.contacts.GetContactsRequest(hash=0))
        return [person(u) for u in (getattr(result, "users", None) or [])]

    async def add(self, user: Any, *, first: str = "", last: str = "", phone: str = "",
                  note: str = "", share_phone: bool = False) -> dict[str, Any]:
        """Добавить в адресную книгу.

        `share_phone` разрешает человеку увидеть ваш номер — по умолчанию нет.
        """
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        known = await self.client.get_entity(user)
        try:
            await self.client(functions.contacts.AddContactRequest(
                id=entity, first_name=first or getattr(known, "first_name", "") or "",
                last_name=last or getattr(known, "last_name", "") or "",
                phone=phone or "", note=note or None,
                add_phone_privacy_exception=share_phone))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"добавлен": getattr(known, "username", None) or getattr(known, "id", None),
                "номер показан": share_phone}

    async def remove(self, users: Sequence[Any]) -> dict[str, Any]:
        """Убрать из адресной книги. Писать вам человек по-прежнему сможет."""
        from telethon.tl import functions

        ids = [await self.client.get_input_entity(u) for u in users]
        await self.client(functions.contacts.DeleteContactsRequest(id=ids))
        return {"удалено контактов": len(ids),
                "примечание": "удаление из книги не запрещает писать — для этого блокировка"}

    async def note(self, user: Any, text: str) -> dict[str, Any]:
        """Личная заметка о человеке. Видна только вам, хранится у Telegram."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        await self.client(functions.contacts.UpdateContactNoteRequest(id=entity, note=text))
        return {"заметка": text or "снята"}

    async def close_friends(self, users: Sequence[Any]) -> dict[str, Any]:
        """Список близких друзей задаётся целиком, а не по одному."""
        from telethon.tl import functions

        ids = []
        for u in users:
            entity = await self.client.get_entity(u)
            ids.append(entity.id)
        await self.client(functions.contacts.EditCloseFriendsRequest(id=ids))
        return {"близких друзей": len(ids)}

    # ── чёрный список ────────────────────────────────────────────────────────
    async def blocked(self, *, stories: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """Кому запрещено писать — или, с `stories`, видеть ваши истории."""
        from telethon.tl import functions

        result = await self.client(functions.contacts.GetBlockedRequest(
            offset=0, limit=int(limit), my_stories_from=stories or None))
        return [person(u) for u in (getattr(result, "users", None) or [])]

    async def block(self, user: Any, *, stories_only: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        try:
            await self.client(functions.contacts.BlockRequest(
                id=entity, my_stories_from=stories_only or None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"заблокирован": str(user),
                "только истории" if stories_only else "полностью": True}

    async def unblock(self, user: Any, *, stories_only: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        await self.client(functions.contacts.UnblockRequest(
            id=entity, my_stories_from=stories_only or None))
        return {"разблокирован": str(user)}

    # ── поиск и разрешение ───────────────────────────────────────────────────
    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Поиск людей и каналов по имени — по всему Telegram, не только у себя."""
        from telethon.tl import functions

        result = await self.client(functions.contacts.SearchRequest(
            q=query, limit=int(limit), broadcasts=None, bots=None))
        out = [person(u) for u in (getattr(result, "users", None) or [])]
        for chat in getattr(result, "chats", None) or []:
            out.append({"id": chat.id, "имя": getattr(chat, "title", None),
                        "username": getattr(chat, "username", None), "бот": False,
                        "взаимный": False, "близкий друг": False,
                        "premium": False, "был": "чат"})
        return out

    async def by_phone(self, phone: str) -> dict[str, Any]:
        """Кто скрывается за номером — если его владелец это разрешил."""
        from telethon.tl import functions

        try:
            result = await self.client(functions.contacts.ResolvePhoneRequest(
                phone="".join(c for c in phone if c.isdigit())))
        except Exception as exc:
            raise self._explain(exc) from exc
        users = getattr(result, "users", None) or []
        if not users:
            raise ContactError("по этому номеру никого не нашлось")
        return person(users[0])

    async def toggle_top_peers(self, enabled: bool) -> dict[str, Any]:
        """Включить или выключить учёт частых собеседников."""
        from telethon.tl import functions

        await self.client(functions.contacts.ToggleTopPeersRequest(enabled=enabled))
        return {"учёт частых собеседников": "включён" if enabled else "выключен"}

    async def birthdays(self) -> list[dict[str, Any]]:
        """У кого из контактов скоро день рождения."""
        from telethon.tl import functions

        result = await self.client(functions.contacts.GetBirthdaysRequest())
        names = {u.id: (u.username or " ".join(filter(None, [u.first_name, u.last_name])))
                 for u in (getattr(result, "users", None) or [])}
        out = []
        for row in getattr(result, "contacts", None) or []:
            birthday = getattr(row, "birthday", None)
            out.append({"кто": names.get(getattr(row, "contact_id", None)),
                        "когда": f"{getattr(birthday, 'day', '?'):02}."
                                 f"{getattr(birthday, 'month', 0):02}"
                        if birthday else None})
        return out

    async def top_peers(self, limit: int = 20) -> list[dict[str, Any]]:
        """С кем вы общаетесь чаще всего — по мнению Telegram."""
        from telethon.tl import functions

        result = await self.client(functions.contacts.GetTopPeersRequest(
            offset=0, limit=int(limit), hash=0, correspondents=True, bots_pm=None,
            bots_inline=None, phone_calls=None, forward_users=None, forward_chats=None,
            groups=None, channels=None, bots_app=None, bots_guestchat=None))
        # Учёт частых собеседников можно выключить в настройках — тогда сервер
        # отвечает TopPeersDisabled, и пустой список без объяснения выглядит
        # как «вы ни с кем не общаетесь».
        if type(result).__name__ == "TopPeersDisabled":
            raise ContactError("учёт частых собеседников выключен в настройках Telegram — "
                               "включите «Частые контакты», и статистика появится")
        names = {u.id: (u.username or " ".join(filter(None, [u.first_name, u.last_name])))
                 for u in (getattr(result, "users", None) or [])}
        out = []
        for category in getattr(result, "categories", None) or []:
            for row in getattr(category, "peers", None) or []:
                who = getattr(getattr(row, "peer", None), "user_id", None)
                out.append({"кто": names.get(who, who), "вес": round(getattr(row, "rating", 0), 2)})
        return out[:limit]

    async def import_token(self, token: str) -> dict[str, Any]:
        """Добавить человека по ссылке-приглашению вида t.me/+токен."""
        from telethon.tl import functions

        clean = token.rstrip("/").split("/")[-1].lstrip("+")
        try:
            user = await self.client(functions.contacts.ImportContactTokenRequest(token=clean))
        except Exception as exc:
            raise self._explain(exc) from exc
        return person(user)

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        hints = {
            "CONTACT_ID_INVALID": "такого пользователя нет",
            "PHONE_NOT_OCCUPIED": "на этом номере нет аккаунта Telegram",
            "PHONE_NUMBER_INVALID": "номер записан неверно",
            "USER_ID_INVALID": "такого пользователя нет",
            "CONTACT_NAME_EMPTY": "у контакта должно быть имя",
            "IMPORT_TOKEN_INVALID": "ссылка-приглашение недействительна",
            "PEER_ID_INVALID": "этого человека не найти — возможно, он ограничил доступ",
        }
        import tgx_net

        return tgx_net.explain(exc, hints, ContactError)
        return exc


# --- остаток: импорт по номерам, свои записи, кто рядом, топ ---

MORE_HINTS = {
    "PHONE_NUMBER_INVALID": "номер записан не так; нужен международный вид, +7…",
    "CONTACT_ID_INVALID": "такого контакта нет",
    "USER_ID_INVALID": "такого пользователя нет",
    "PEER_ID_INVALID": "такого собеседника нет",
    "GEO_POINT_INVALID": "координаты не годятся",
    "CONTACT_NAME_EMPTY": "у контакта должно быть имя",
    "IMPORT_FILE_INVALID": "файл с контактами не разобрался",
    "TAKEOUT_REQUIRED": "эти данные отдают только внутри выгрузки — tgx делает её сам",
    "TAKEOUT_INIT_DELAY": ("Telegram поставил выгрузку на паузу — это защита. Подтвердите "
                           "запрос в другом своём Telegram и повторите"),
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
}


class More:
    """То, что осталось за краем контактов."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        import tgx_net

        try:
            return await self.client(request)
        except Exception as exc:
            raise tgx_net.explain(exc, MORE_HINTS, ContactError) from exc

    async def add_by_phone(self, people: list[tuple[str, str, str]]) -> dict[str, Any]:
        """Добавить по номерам. Пара «есть в Telegram» и «добавлен» — разная.

        Сервер возвращает и тех, кого нашёл, и тех, кого нет; вторых он молча
        пропускает, поэтому мы считаем сами и говорим, сколько не нашлось.
        """
        import secrets

        from telethon.tl import functions, types

        rows = [types.InputPhoneContact(client_id=secrets.randbits(62), phone=phone,
                                        first_name=first, last_name=last)
                for phone, first, last in people]
        result = await self._call(functions.contacts.ImportContactsRequest(contacts=rows))
        added = getattr(result, "imported", None) or []
        return {"просили": len(rows), "добавлено": len(added),
                "не нашлось": len(rows) - len(added),
                "сколько ещё можно сегодня": getattr(result, "retry_contacts", None) and
                                             len(result.retry_contacts) or None}

    async def forget_phones(self, phones: list[str]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.contacts.DeleteByPhonesRequest(phones=phones))
        return {"убрано номеров": len(phones)}

    async def accept(self, who: Any) -> dict[str, Any]:
        """Принять чужую запись о себе — тогда он увидит ваш номер."""
        from telethon.tl import functions

        user = await self.client.get_input_entity(who)
        await self._call(functions.contacts.AcceptContactRequest(id=user))
        return {"кто": str(who), "теперь видит ваш номер": True}

    async def ids(self) -> list[int]:
        from telethon.tl import functions

        return list(await self._call(functions.contacts.GetContactIDsRequest(hash=0)) or [])

    async def statuses(self) -> list[dict[str, Any]]:
        """Кто когда был в сети — одним запросом по всем контактам."""
        from telethon.tl import functions

        result = await self._call(functions.contacts.GetStatusesRequest())
        rows = []
        for item in result or []:
            kind = type(getattr(item, "status", None)).__name__.replace("UserStatus", "")
            rows.append({"кто": getattr(item, "user_id", None),
                         "когда": {"Online": "сейчас", "Offline": "не сейчас",
                                   "Recently": "недавно", "LastWeek": "на неделе",
                                   "LastMonth": "в этом месяце",
                                   "Empty": "скрыто"}.get(kind, kind.lower())})
        return rows

    async def saved(self) -> list[dict[str, Any]]:
        """Что Telegram запомнил из вашей телефонной книги.

        Отдаётся только внутри выгрузки: сервер считает это персональными
        данными, а не текущим состоянием. Открываем её сами и закрываем за
        собой — человек спросил список, а не режим экспорта.
        """
        import tgx_takeout
        from telethon.tl import functions

        tgx_takeout.Takeout(self.client)._tidy()
        async with self.client.takeout(finalize=True, contacts=True) as session:
            result = await session(functions.contacts.GetSavedRequest())
        return [{"телефон": getattr(c, "phone", None), "имя": getattr(c, "first_name", None),
                 "фамилия": getattr(c, "last_name", None), "записано": getattr(c, "date", None)
                 and str(c.date)} for c in result or []]

    async def forget_saved(self) -> dict[str, Any]:
        """Выбросить всё, что Telegram запомнил из вашей телефонной книги."""
        from telethon.tl import functions

        await self._call(functions.contacts.ResetSavedRequest())
        return {"телефонная книга": "забыта на сервере"}

    async def invite_token(self) -> dict[str, Any]:
        """Одноразовая ссылка «добавь меня» — без раскрытия номера."""
        from telethon.tl import functions

        result = await self._call(functions.contacts.ExportContactTokenRequest())
        # сервер отдаёт готовый адрес в url, а не голый токен: собирать ссылку
        # самому значит однажды собрать её неправильно
        return {"ссылка": getattr(result, "url", None),
                "живёт до": getattr(result, "expires", None) and str(result.expires)}

    async def nearby(self, lat: float, lon: float, *, minutes: int = 0) -> list[dict[str, Any]]:
        """Кто рядом. `minutes` — на сколько показать себя другим.

        Ноль означает «только посмотреть»: себя не публикуем. Это важно —
        случайно раздать своё местоположение проще, чем кажется.
        """
        from telethon.tl import functions, types

        result = await self._call(functions.contacts.GetLocatedRequest(
            geo_point=types.InputGeoPoint(lat=lat, long=lon),
            self_expires=minutes * 60 if minutes else 0))
        names = {u.id: getattr(u, "username", None) or getattr(u, "first_name", None)
                 for u in getattr(result, "users", None) or []}
        rows = []
        for update in getattr(result, "updates", None) or []:
            for item in getattr(update, "peers", None) or []:
                who = getattr(getattr(item, "peer", None), "user_id", None)
                rows.append({"кто": names.get(who, who),
                             "метров": getattr(item, "distance", None)})
        return rows

    async def forget_top(self, who: Any, category: str = "correspondents") -> dict[str, Any]:
        """Убрать человека из «часто пишете» — эта подсказка бывает некстати."""
        from telethon.tl import functions, types

        kinds = {"correspondents": "TopPeerCategoryCorrespondents",
                 "bots": "TopPeerCategoryBotsPM", "inline": "TopPeerCategoryBotsInline",
                 "groups": "TopPeerCategoryGroups", "channels": "TopPeerCategoryChannels",
                 "calls": "TopPeerCategoryPhoneCalls", "forwards": "TopPeerCategoryForwardUsers",
                 "apps": "TopPeerCategoryBotsApp"}
        name = kinds.get(category)
        if name is None:
            raise ContactError(f"не знаю разряда «{category}»; есть: {', '.join(sorted(kinds))}")
        peer = await self.client.get_input_entity(who)
        await self._call(functions.contacts.ResetTopPeerRatingRequest(
            category=getattr(types, name)(), peer=peer))
        return {"кто": str(who), "убран из": category}

    async def sponsored(self, query: str) -> list[dict[str, Any]]:
        """Что Telegram подсовывает в поиске как рекламу."""
        from telethon.tl import functions

        result = await self._call(functions.contacts.GetSponsoredPeersRequest(q=query))
        return [{"кто": getattr(getattr(p, "peer", None), "user_id", None)
                       or getattr(getattr(p, "peer", None), "channel_id", None),
                 "почему": getattr(p, "sponsor_info", None)}
                for p in getattr(result, "peers", None) or []]

    async def block_many(self, people: list[Any], *, stories_only: bool = False) -> dict[str, Any]:
        """Заменить список заблокированных целиком — не добавить, а заменить."""
        from telethon.tl import functions

        peers = [await self.client.get_input_entity(p) for p in people]
        await self._call(functions.contacts.SetBlockedRequest(
            id=peers, limit=len(peers), my_stories_from=stories_only or None))
        return {"в списке теперь": len(peers), "что закрыто":
                "истории" if stories_only else "всё"}
