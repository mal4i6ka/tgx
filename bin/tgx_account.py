"""Настройки и оформление аккаунта — остаток пространства account.

Большая часть здесь в терминале не рисуется: обои, темы, наборы эмодзи-статусов.
Но их можно ставить, и это влияет на все ваши остальные клиенты — телефон,
десктоп. Поэтому команды делятся надвое: посмотреть (что доступно, что стоит) и
поставить (тогда меняется везде).

Тонкое место — дополнительные адреса. У аккаунта их бывает несколько, и
включённые показываются в профиле как ссылки. Список приходит один, а
управляют им по одному имени: включить, выключить, переставить. Перепутать
основной и дополнительный легко, и тогда профиль остаётся без адреса вовсе.
"""

from __future__ import annotations

from typing import Any

import tgx_net


class AccountError(RuntimeError):
    """Не вышло."""


HINTS = {
    "USERNAME_INVALID": "такой адрес Telegram не принимает",
    "USERNAME_OCCUPIED": "адрес занят",
    "USERNAME_NOT_MODIFIED": "и так уже так",
    "USERNAMES_ACTIVE_TOO_MUCH": "столько адресов включить нельзя",
    "USERNAME_PURCHASE_AVAILABLE": "адрес свободен, но продаётся на Fragment",
    "WALLPAPER_INVALID": "эти обои не годятся",
    "WALLPAPER_NOT_FOUND": "таких обоев нет",
    "THEME_INVALID": "такой темы нет",
    "THEME_FORMAT_INVALID": "неизвестный формат темы",
    "RINGTONE_INVALID": "звук не годится в рингтоны",
    "RINGTONE_MIME_INVALID": "рингтон должен быть аудио",
    "AUDIO_TITLE_EMPTY": "у музыки должно быть название",
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
    "PEER_ID_INVALID": "такого собеседника нет",
    "MSG_ID_INVALID": "такого сообщения нет",
    "PASSWORD_HASH_INVALID": "неверный пароль двухфакторной защиты",
    "USER_ID_INVALID": "такого пользователя нет",
}

# что показывать первым в профиле — те же вкладки, что у канала
TABS = {"posts": "ProfileTabPosts", "gifts": "ProfileTabGifts",
        "media": "ProfileTabMedia", "files": "ProfileTabFiles",
        "music": "ProfileTabMusic", "voice": "ProfileTabVoice",
        "links": "ProfileTabLinks", "gifs": "ProfileTabGifs",
        "stories": "ProfileTabStories"}

REPORT_REASONS = {
    "spam": "InputReportReasonSpam", "violence": "InputReportReasonViolence",
    "porn": "InputReportReasonPornography", "child-abuse": "InputReportReasonChildAbuse",
    "drugs": "InputReportReasonIllegalDrugs", "personal": "InputReportReasonPersonalDetails",
    "copyright": "InputReportReasonCopyright", "fake": "InputReportReasonFake",
    "geo": "InputReportReasonGeoIrrelevant", "other": "InputReportReasonOther"}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, AccountError)


def _emoji_row(status: Any) -> dict[str, Any]:
    return {"эмодзи": getattr(status, "document_id", None),
            "срок": getattr(status, "until", None)}


class Account:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    # --- взрослый контент и уведомление о новых контактах ---

    async def sensitive(self, allow: bool | None = None) -> dict[str, Any]:
        """Показывать ли отмеченное как деликатное. Без аргумента — узнать."""
        from telethon.tl import functions

        if allow is None:
            result = await self._call(functions.account.GetContentSettingsRequest())
            return {"деликатный контент": "показывать"
                    if getattr(result, "sensitive_enabled", False) else "прятать",
                    "можно менять": bool(getattr(result, "sensitive_can_change", False))}
        await self._call(functions.account.SetContentSettingsRequest(sensitive_enabled=allow))
        return {"деликатный контент": "показывать" if allow else "прятать"}

    async def new_contact_notice(self) -> dict[str, Any]:
        """Приходит ли отбивка, когда контакт зарегистрировался."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetContactSignUpNotificationRequest())
        return {"о новых контактах": "молчит" if result else "сообщает"}

    # --- дополнительные адреса ---

    async def free_name(self, username: str) -> dict[str, Any]:
        from telethon.tl import functions

        try:
            free = await self._call(functions.account.CheckUsernameRequest(
                username=username.lstrip("@")))
        except AccountError as exc:
            return {"адрес": username.lstrip("@"), "свободен": False, "почему": str(exc)}
        return {"адрес": username.lstrip("@"), "свободен": bool(free)}

    async def username(self, name: str, *, on: bool = True) -> dict[str, Any]:
        """Включить или выключить один из ваших дополнительных адресов."""
        from telethon.tl import functions

        await self._call(functions.account.ToggleUsernameRequest(
            username=name.lstrip("@"), active=on))
        return {"адрес": name.lstrip("@"), "включён": on}

    async def order_usernames(self, names: list[str]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.account.ReorderUsernamesRequest(
            order=[n.lstrip("@") for n in names]))
        return {"порядок адресов": [n.lstrip("@") for n in names]}

    async def main_tab(self, tab: str) -> dict[str, Any]:
        """Что показывать первым в вашем профиле."""
        from telethon.tl import functions, types

        name = TABS.get(tab)
        if name is None:
            raise AccountError(f"не знаю вкладки «{tab}»; есть: {', '.join(sorted(TABS))}")
        await self._call(functions.account.SetMainProfileTabRequest(tab=getattr(types, name)()))
        return {"первой в профиле": tab}

    # --- обои ---

    async def wallpapers(self) -> list[dict[str, Any]]:
        """Обои, доступные к установке."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetWallPapersRequest(hash=0))
        rows = []
        for paper in getattr(result, "wallpapers", None) or []:
            rows.append({"id": getattr(paper, "id", None),
                         "адрес": getattr(paper, "slug", None),
                         "по умолчанию": bool(getattr(paper, "default", False))})
        return rows

    async def set_wallpaper(self, slug: str, *, dark: bool = False) -> dict[str, Any]:
        """Поставить обои по адресу-слагу. Меняется на всех клиентах."""
        from telethon.tl import functions, types

        settings = types.WallPaperSettings(dark=dark or None)
        await self._call(functions.account.InstallWallPaperRequest(
            wallpaper=types.InputWallPaperSlug(slug=slug), settings=settings))
        return {"обои": slug, "тёмные": dark}

    async def reset_wallpapers(self) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.account.ResetWallPapersRequest())
        return {"обои": "сброшены к исходным"}

    # --- темы ---

    async def themes(self, *, chat: bool = False) -> list[dict[str, Any]]:
        """Темы: свои созданные или готовые для чатов."""
        from telethon.tl import functions

        request = (functions.account.GetChatThemesRequest(hash=0) if chat
                   else functions.account.GetThemesRequest(format="ios", hash=0))
        result = await self._call(request)
        items = getattr(result, "themes", None) or []
        return [{"тема": getattr(t, "title", None) or getattr(t, "emoticon", None),
                 "адрес": getattr(t, "slug", None), "id": getattr(t, "id", None)}
                for t in items]

    async def install_theme(self, slug: str, *, dark: bool = False) -> dict[str, Any]:
        from telethon.tl import functions, types

        await self._call(functions.account.InstallThemeRequest(
            dark=dark or None, format="ios",
            theme=types.InputThemeSlug(slug=slug)))
        return {"тема": slug, "тёмная": dark}

    # --- эмодзи-статусы: что доступно (сам статус ставит `tgx profile status`) ---

    async def emoji_statuses(self, *, kind: str = "default") -> list[dict[str, Any]]:
        """Наборы эмодзи-статусов: рекомендованные, недавние, коллекционные."""
        from telethon.tl import functions

        request = {"default": functions.account.GetDefaultEmojiStatusesRequest,
                   "recent": functions.account.GetRecentEmojiStatusesRequest,
                   "collectible": functions.account.GetCollectibleEmojiStatusesRequest,
                   }.get(kind)
        if request is None:
            raise AccountError("вид статусов: default, recent или collectible")
        result = await self._call(request(hash=0))
        return [_emoji_row(s) for s in getattr(result, "statuses", None) or []]

    async def clear_recent_statuses(self) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.account.ClearRecentEmojiStatusesRequest())
        return {"недавние статусы": "очищены"}

    async def status_emojis(self, *, kind: str = "profile") -> list[int]:
        """Какие эмодзи годятся: на аватар, на фон профиля, для групп."""
        from telethon.tl import functions

        request = {"profile": functions.account.GetDefaultProfilePhotoEmojisRequest,
                   "group": functions.account.GetDefaultGroupPhotoEmojisRequest,
                   "background": functions.account.GetDefaultBackgroundEmojisRequest,
                   }.get(kind)
        if request is None:
            raise AccountError("вид: profile, group или background")
        result = await self._call(request(hash=0))
        return [getattr(d, "id", d) for d in getattr(result, "documents", None) or []]

    # --- рингтоны и музыка профиля ---

    async def ringtones(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.account.GetSavedRingtonesRequest(hash=0))
        return [{"id": getattr(r, "id", None), "имя": getattr(r, "file_name", None)}
                for r in getattr(result, "ringtones", None) or []]

    async def profile_music(self) -> list[int]:
        """Музыка, прикреплённая к вашему профилю."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetSavedMusicIdsRequest(hash=0))
        return list(getattr(result, "ids", None) or [])

    # --- автосохранение медиа ---

    async def autosave(self) -> dict[str, Any]:
        """Что скачивается само: фото, видео, откуда."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetAutoSaveSettingsRequest())

        def brief(settings: Any) -> dict[str, Any]:
            return {"фото": bool(getattr(settings, "photos", False)),
                    "видео": bool(getattr(settings, "videos", False)),
                    "видео до, МБ": (getattr(settings, "video_max_size", 0) or 0) // 1048576 or None}

        return {"личные": brief(getattr(result, "users_settings", None)),
                "группы": brief(getattr(result, "chats_settings", None)),
                "каналы": brief(getattr(result, "broadcasts_settings", None))}

    async def set_autosave(self, *, where: str, photos: bool = False,
                           videos: bool = False, max_mb: int = 0) -> dict[str, Any]:
        from telethon.tl import functions, types

        settings = types.AutoSaveSettings(
            photos=photos, videos=videos,
            video_max_size=max_mb * 1048576 if max_mb else None)
        flags = {"users": where == "users", "chats": where == "chats",
                 "broadcasts": where == "channels"}
        if not any(flags.values()):
            raise AccountError("куда: users, chats или channels")
        await self._call(functions.account.SaveAutoSaveSettingsRequest(
            settings=settings, **flags))
        return {"где": where, "фото": photos, "видео": videos}

    # --- деньги за сообщения ---

    async def paid_revenue(self, user: Any) -> dict[str, Any]:
        """Сколько звёзд принесли платные сообщения от этого человека."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        result = await self._call(functions.account.GetPaidMessagesRevenueRequest(
            user_id=entity))
        return {"от кого": str(user), "звёзд": getattr(result, "stars_amount", None)}

    # --- жалоба на собеседника целиком ---

    async def report(self, peer: Any, reason: str = "spam",
                     comment: str = "") -> dict[str, Any]:
        """Пожаловаться на весь профиль — не на сообщение, а на человека."""
        from telethon.tl import functions, types

        name = REPORT_REASONS.get(reason)
        if name is None:
            raise AccountError(f"причина: {', '.join(sorted(REPORT_REASONS))}")
        await self._call(functions.account.ReportPeerRequest(
            peer=peer, reason=getattr(types, name)(), message=comment))
        return {"жалоба": "отправлена", "причина": reason}
