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

    # --- деловые ссылки и место ---

    async def resolve_link(self, slug: str) -> dict[str, Any]:
        """Что стоит за деловой ссылкой t.me/m/… — текст и к кому ведёт."""
        from telethon.tl import functions

        result = await self._call(functions.account.ResolveBusinessChatLinkRequest(
            slug=slug.rstrip("/").split("/")[-1]))
        message = getattr(result, "message", None)
        return {"текст": message, "к кому": getattr(result, "peer", None) and
                type(result.peer).__name__}

    async def business_location(self, *, address: str = "", lat: float | None = None,
                                lon: float | None = None) -> dict[str, Any]:
        """Адрес делового профиля. Пустой адрес убирает место совсем."""
        from telethon.tl import functions, types

        point = (types.InputGeoPoint(lat=lat, long=lon)
                 if lat is not None and lon is not None else None)
        await self._call(functions.account.UpdateBusinessLocationRequest(
            geo_point=point, address=address or None))
        return {"адрес": address or "убран", "точка": [lat, lon] if point else None}

    async def bot_connection(self, connection_id: str) -> dict[str, Any]:
        """Что за деловое подключение бота — по его идентификатору."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetBotBusinessConnectionRequest(
            connection_id=connection_id))
        updates = getattr(result, "updates", None) or []
        found = next((getattr(u, "connection", None) for u in updates
                      if getattr(u, "connection", None)), None)
        return {"подключение": connection_id,
                "бот": getattr(found, "bot_id", None),
                "может писать": bool(getattr(found, "rights", None)),
                "отключено": bool(getattr(found, "disabled", False))}

    # --- статусы каналов ---

    async def channel_statuses(self, *, restricted: bool = False) -> list[dict[str, Any]]:
        """Эмодзи-статусы для каналов: обычные или недоступные без бустов."""
        from telethon.tl import functions

        request = (functions.account.GetChannelRestrictedStatusEmojisRequest if restricted
                   else functions.account.GetChannelDefaultEmojiStatusesRequest)
        result = await self._call(request(hash=0))
        items = getattr(result, "statuses", None) or getattr(result, "documents", None) or []
        return [_emoji_row(s) if hasattr(s, "document_id") else {"эмодзи": getattr(s, "id", s)}
                for s in items]

    async def gift_themes(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Темы чата, которые дают коллекционные подарки."""
        from telethon.tl import functions

        result = await self._call(functions.account.GetUniqueGiftChatThemesRequest(
            offset="", limit=limit, hash=0))
        return [{"тема": getattr(t, "emoticon", None) or getattr(t, "title", None),
                 "id": getattr(t, "id", None)}
                for t in getattr(result, "themes", None) or []]

    # --- отдельные обои и темы по ссылке ---

    async def wallpaper(self, slug: str) -> dict[str, Any]:
        """Подробности одних обоев по адресу-слагу."""
        from telethon.tl import functions, types

        result = await self._call(functions.account.GetWallPaperRequest(
            wallpaper=types.InputWallPaperSlug(slug=slug)))
        return {"обои": slug, "id": getattr(result, "id", None),
                "по умолчанию": bool(getattr(result, "default", False)),
                "узор": bool(getattr(result, "pattern", False))}

    async def theme(self, slug: str) -> dict[str, Any]:
        from telethon.tl import functions, types

        result = await self._call(functions.account.GetThemeRequest(
            format="ios", theme=types.InputThemeSlug(slug=slug)))
        return {"тема": getattr(result, "title", None), "адрес": slug,
                "установок": getattr(result, "installs_count", None)}

    # --- рингтоны, музыка, платные сообщения ---

    async def save_ringtone(self, key: str, *, remove: bool = False) -> dict[str, Any]:
        """Сохранить звук в рингтоны. Ключ — «id:hash», как у стикеров."""
        import tgx_stickers
        from telethon.tl import functions

        await self._call(functions.account.SaveRingtoneRequest(
            id=tgx_stickers.sticker_ref(key), unsave=remove))
        return {"рингтон": key, "сохранён": not remove}

    async def save_music(self, key: str, *, remove: bool = False) -> dict[str, Any]:
        """Прикрепить музыку к профилю или снять."""
        import tgx_stickers
        from telethon.tl import functions

        await self._call(functions.account.SaveMusicRequest(
            id=tgx_stickers.sticker_ref(key), unsave=remove or None))
        return {"музыка": key, "в профиле": not remove}

    async def free_messages_for(self, user: Any, *, free: bool = True,
                                refund: bool = False) -> dict[str, Any]:
        """Пустить человека писать бесплатно, когда у вас платные сообщения."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        await self._call(functions.account.ToggleNoPaidMessagesExceptionRequest(
            user_id=entity, require_payment=None if free else True,
            refund_charged=refund or None))
        return {"кому": str(user), "пишет бесплатно": free,
                "вернули уплаченное": refund}

    async def report_photo(self, peer: Any, photo_id: int, access_hash: int,
                           reason: str = "other", comment: str = "") -> dict[str, Any]:
        """Пожаловаться на аватар — отдельно от жалобы на профиль."""
        from telethon.tl import functions, types

        name = REPORT_REASONS.get(reason)
        if name is None:
            raise AccountError(f"причина: {', '.join(sorted(REPORT_REASONS))}")
        photo = types.InputPhoto(id=photo_id, access_hash=access_hash, file_reference=b"")
        await self._call(functions.account.ReportProfilePhotoRequest(
            peer=peer, photo_id=photo, reason=getattr(types, name)(), message=comment))
        return {"жалоба на аватар": "отправлена", "причина": reason}

    # --- свои обои, темы и рингтоны из файла ---

    async def upload_wallpaper(self, path: Any, *, dark: bool = False,
                               for_chat: bool = False) -> dict[str, Any]:
        """Свои обои из файла. Появляются в списке и становятся доступны везде."""
        import mimetypes
        from pathlib import Path

        from telethon.tl import functions, types

        source = Path(path).expanduser()
        if not source.is_file():
            raise AccountError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        mime = mimetypes.guess_type(str(source))[0] or "image/jpeg"
        result = await self._call(functions.account.UploadWallPaperRequest(
            file=uploaded, mime_type=mime,
            settings=types.WallPaperSettings(dark=dark or None),
            for_chat=for_chat or None))
        return {"обои": source.name, "адрес": getattr(result, "slug", None),
                "id": getattr(result, "id", None)}

    async def upload_ringtone(self, path: Any) -> dict[str, Any]:
        """Свой рингтон из файла — только аудио и небольшого размера."""
        import mimetypes
        from pathlib import Path

        from telethon.tl import functions

        source = Path(path).expanduser()
        if not source.is_file():
            raise AccountError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        mime = mimetypes.guess_type(str(source))[0] or "audio/mpeg"
        result = await self._call(functions.account.UploadRingtoneRequest(
            file=uploaded, file_name=source.name, mime_type=mime))
        return {"рингтон": source.name, "id": getattr(result, "id", None),
                "ключ": f"{getattr(result, 'id', 0)}:{getattr(result, 'access_hash', 0)}"}

    async def create_theme(self, slug: str, title: str, path: Any = None) -> dict[str, Any]:
        """Завести свою тему. Без файла — пустая заготовка под правку."""
        from pathlib import Path

        from telethon.tl import functions

        document = None
        if path:
            source = Path(path).expanduser()
            if not source.is_file():
                raise AccountError(f"файла {source} нет")
            document = await self.client.upload_file(str(source))
        result = await self._call(functions.account.CreateThemeRequest(
            slug=slug, title=title, document=document))
        return {"тема": title, "адрес": getattr(result, "slug", slug),
                "id": getattr(result, "id", None)}

    async def save_theme(self, slug: str, *, remove: bool = False) -> dict[str, Any]:
        from telethon.tl import functions, types

        await self._call(functions.account.SaveThemeRequest(
            theme=types.InputThemeSlug(slug=slug), unsave=remove))
        return {"тема": slug, "сохранена": not remove}

    async def save_wallpaper(self, slug: str, *, remove: bool = False) -> dict[str, Any]:
        from telethon.tl import functions, types

        await self._call(functions.account.SaveWallPaperRequest(
            wallpaper=types.InputWallPaperSlug(slug=slug), unsave=remove,
            settings=types.WallPaperSettings()))
        return {"обои": slug, "сохранены": not remove}

    async def wallpapers_by_slug(self, slugs: list[str]) -> list[dict[str, Any]]:
        """Несколько обоев одним запросом — так делает клиент при листании."""
        from telethon.tl import functions, types

        result = await self._call(functions.account.GetMultiWallPapersRequest(
            wallpapers=[types.InputWallPaperSlug(slug=s) for s in slugs]))
        return [{"адрес": getattr(w, "slug", None), "id": getattr(w, "id", None)}
                for w in result or []]

    async def forget_autosave_exceptions(self) -> dict[str, Any]:
        """Сбросить чаты, для которых автосохранение настроено отдельно."""
        from telethon.tl import functions

        await self._call(functions.account.DeleteAutoSaveExceptionsRequest())
        return {"исключения автосохранения": "сброшены"}

    async def edit_business_link(self, slug: str, *, title: str = "",
                                 text: str = "") -> dict[str, Any]:
        """Поправить деловую ссылку: заголовок и заготовленный текст."""
        from telethon.tl import functions, types

        link = types.InputBusinessChatLink(
            message=text, title=title or None, entities=None)
        result = await self._call(functions.account.EditBusinessChatLinkRequest(
            slug=slug, link=link))
        return {"ссылка": getattr(result, "link", slug),
                "заголовок": getattr(result, "title", title)}

    async def drop_business_link(self, slug: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.account.DeleteBusinessChatLinkRequest(slug=slug))
        return {"деловая ссылка": slug, "удалена": True}

    async def password_settings(self, password: str) -> dict[str, Any]:
        """Что скрыто за паролем двухфакторной защиты: почта и подсказка.

        Пароль не отправляется — отправляется доказательство знания, как и при
        выводе средств. Сам он остаётся здесь.
        """
        from telethon.password import compute_check
        from telethon.tl import functions

        state = await self.client(functions.account.GetPasswordRequest())
        result = await self._call(functions.account.GetPasswordSettingsRequest(
            password=compute_check(state, password)))
        return {"почта восстановления": getattr(result, "email", None) or "не задана",
                "есть скрытые данные": bool(getattr(result, "secure_settings", None))}

    async def confirm_bot_connection(self, connection_id: str) -> dict[str, Any]:
        """Подтвердить деловое подключение бота, которое он запросил."""
        from telethon.tl import functions

        await self._call(functions.account.ConfirmBotConnectionRequest(
            connection_id=connection_id))
        return {"подключение": connection_id, "подтверждено": True}

    async def update_theme(self, slug: str, *, title: str = "",
                           path: Any = None) -> dict[str, Any]:
        """Поправить свою тему: название или файл."""
        from pathlib import Path

        from telethon.tl import functions, types

        document = None
        if path:
            source = Path(path).expanduser()
            if not source.is_file():
                raise AccountError(f"файла {source} нет")
            document = await self.client.upload_file(str(source))
        result = await self._call(functions.account.UpdateThemeRequest(
            format="ios", theme=types.InputThemeSlug(slug=slug),
            title=title or None, document=document))
        return {"тема": getattr(result, "title", title or slug), "адрес": slug}

    async def upload_theme(self, path: Any, *, name: str = "") -> dict[str, Any]:
        """Залить файл темы. Отдельный шаг: сначала файл, потом сама тема."""
        import mimetypes
        from pathlib import Path

        from telethon.tl import functions

        source = Path(path).expanduser()
        if not source.is_file():
            raise AccountError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        mime = mimetypes.guess_type(str(source))[0] or "application/x-tgtheme-ios"
        result = await self._call(functions.account.UploadThemeRequest(
            file=uploaded, file_name=name or source.name, mime_type=mime))
        return {"файл темы": source.name, "id": getattr(result, "id", None),
                "дальше": "завести тему командой create-theme"}

    async def set_password(self, current: str, new: str, *, hint: str = "",
                           email: str = "") -> dict[str, Any]:
        """Сменить пароль двухфакторной защиты.

        Оба пароля остаются здесь: наружу уходит доказательство знания старого и
        проверочные данные для нового. Потерять новый пароль — значит потерять
        доступ, поэтому команда идёт через подтверждение.
        """
        from telethon.password import compute_check, compute_digest
        from telethon.tl import functions, types

        state = await self.client(functions.account.GetPasswordRequest())
        settings = types.account.PasswordInputSettings(
            new_algo=state.new_algo,
            new_password_hash=compute_digest(state.new_algo, new) if new else b"",
            hint=hint or None, email=email or None)
        await self._call(functions.account.UpdatePasswordSettingsRequest(
            password=compute_check(state, current), new_settings=settings))
        return {"пароль": "сменён" if new else "снят",
                "подсказка": hint or None, "почта": email or None}
