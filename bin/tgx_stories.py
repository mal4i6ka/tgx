#!/usr/bin/env python3
"""Истории: публикация, чтение, просмотры, альбомы, скрытный режим.

История живёт сутки, если не сказано иначе, и это не сообщение: у неё свои
правила приватности, свой счётчик просмотров и своя лента. Отсюда несколько
особенностей, которые здесь и закодированы.

Приватность задаётся списком правил, а не одним флагом: «всем», «контактам»,
«близким друзьям» или «выбранным». По умолчанию берётся самое узкое из
осмысленных — близкие друзья, — потому что промахнуться в сторону «всем»
дороже, чем в сторону «слишком узко».

Просмотр чужой истории отмечается на сервере: автор видит, что вы смотрели.
Поэтому чтение и *отметка о просмотре* — разные команды, а скрытный режим
существует именно чтобы просмотр не засчитался.
"""
from __future__ import annotations

from typing import Any, Sequence

DAY = 86400
PERIODS = {6: 6 * 3600, 12: 12 * 3600, 24: DAY, 48: 2 * DAY}
AUDIENCES = ("close", "contacts", "everyone", "selected")


class StoryError(RuntimeError):
    """Действие с историей, которое не удалось выполнить."""


def privacy(audience: str, allow: Sequence[Any] = (), deny: Sequence[Any] = ()) -> list[Any]:
    """Кому видна история. Правила складываются, а не выбирается одно.

    По умолчанию — близкие друзья: ошибиться в сторону «всем» дороже.
    """
    from telethon.tl import types

    audience = (audience or "close").lower()
    if audience not in AUDIENCES:
        raise StoryError(f"аудитория «{audience}» неизвестна; есть: {', '.join(AUDIENCES)}")
    rules: list[Any] = []
    if audience == "everyone":
        rules.append(types.InputPrivacyValueAllowAll())
    elif audience == "contacts":
        rules.append(types.InputPrivacyValueAllowContacts())
    elif audience == "close":
        rules.append(types.InputPrivacyValueAllowCloseFriends())
    if allow:
        rules.append(types.InputPrivacyValueAllowUsers(users=list(allow)))
    if deny:
        rules.append(types.InputPrivacyValueDisallowUsers(users=list(deny)))
    if not rules:
        raise StoryError("не указано, кому видна история")
    return rules


def story_row(story: Any) -> dict[str, Any]:
    """Одна история — плоской записью."""
    views = getattr(story, "views", None)
    date = getattr(story, "date", None)
    expires = getattr(story, "expire_date", None)
    return {
        "id": getattr(story, "id", None),
        "подпись": (getattr(story, "caption", None) or "")[:60],
        "опубликована": date.isoformat(timespec="minutes") if date else None,
        "истекает": expires.isoformat(timespec="minutes") if expires else None,
        "просмотров": getattr(views, "views_count", None),
        "реакций": getattr(views, "reactions_count", None),
        "закреплена": bool(getattr(story, "pinned", False)),
        "моя": bool(getattr(story, "out", False)),
        "видео": bool(getattr(getattr(story, "media", None), "document", None)),
    }


class Stories:
    """Всё, что можно сделать с историями."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def feed(self, *, hidden: bool = False) -> list[dict[str, Any]]:
        """Лента историй. `hidden` — те, кого вы убрали из основной ленты."""
        from telethon.tl import functions

        result = await self.client(functions.stories.GetAllStoriesRequest(
            next=None, hidden=hidden or None, state=None))
        out = []
        for group in getattr(result, "peer_stories", None) or []:
            who = getattr(group, "peer", None)
            for story in getattr(group, "stories", None) or []:
                out.append({**story_row(story),
                            "чей": getattr(who, "user_id", None) or getattr(who, "channel_id", None)})
        return out

    async def of(self, peer: Any) -> list[dict[str, Any]]:
        """Активные истории конкретного человека или канала."""
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        result = await self.client(functions.stories.GetPeerStoriesRequest(peer=target))
        group = getattr(result, "stories", None)
        return [story_row(s) for s in (getattr(group, "stories", None) or [])]

    async def pinned(self, peer: Any = None, limit: int = 30) -> list[dict[str, Any]]:
        """Истории, оставленные в профиле после суток."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.stories.GetPinnedStoriesRequest(
            peer=target, offset_id=0, limit=int(limit)))
        return [story_row(s) for s in (getattr(result, "stories", None) or [])]

    async def archive(self, limit: int = 30) -> list[dict[str, Any]]:
        """Свой архив: всё опубликованное, включая истёкшее."""
        from telethon.tl import functions, types

        result = await self.client(functions.stories.GetStoriesArchiveRequest(
            peer=types.InputPeerSelf(), offset_id=0, limit=int(limit)))
        return [story_row(s) for s in (getattr(result, "stories", None) or [])]

    async def publish(self, path: str, *, caption: str = "", audience: str = "close",
                      hours: int = 24, pinned: bool = False, no_forwards: bool = False,
                      allow: Sequence[Any] = (), deny: Sequence[Any] = ()) -> dict[str, Any]:
        """Опубликовать историю. По умолчанию — только близким друзьям."""
        from pathlib import Path

        from telethon import helpers
        from telethon.tl import functions, types

        source = Path(path).expanduser()
        if not source.is_file():
            raise StoryError(f"файла {source} нет")
        period = PERIODS.get(int(hours))
        if period is None:
            raise StoryError(f"срок {hours} ч не поддерживается; можно: "
                             f"{', '.join(str(h) for h in PERIODS)}")

        uploaded = await self.client.upload_file(str(source))
        import tgx_media

        if tgx_media.is_video_file(source):
            size = tgx_media.dimensions(source) or (0, 0, 0)
            media = types.InputMediaUploadedDocument(
                file=uploaded, mime_type="video/mp4",
                attributes=[types.DocumentAttributeVideo(
                    duration=int(size[2]), w=size[0], h=size[1], supports_streaming=True)])
        else:
            media = types.InputMediaUploadedPhoto(file=uploaded)

        allowed = [await self.client.get_input_entity(u) for u in allow]
        denied = [await self.client.get_input_entity(u) for u in deny]
        try:
            result = await self.client(functions.stories.SendStoryRequest(
                peer=types.InputPeerSelf(), media=media,
                privacy_rules=privacy(audience, allowed, denied),
                random_id=helpers.generate_random_long(),
                caption=caption or None, entities=None, period=period,
                pinned=pinned or None, noforwards=no_forwards or None))
        except Exception as exc:
            raise self._explain(exc) from exc
        sent = 0
        for update in getattr(result, "updates", None) or []:
            sent = getattr(getattr(update, "story", None), "id", 0) or sent
        return {"id": sent, "аудитория": audience, "часов": hours, "в профиле": pinned}

    async def delete(self, ids: Sequence[int], peer: Any = None) -> dict[str, Any]:
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.stories.DeleteStoriesRequest(
            peer=target, id=[int(i) for i in ids]))
        return {"удалено": list(result or [])}

    async def pin(self, ids: Sequence[int], pinned: bool = True) -> dict[str, Any]:
        """Оставить историю в профиле после суток — или убрать."""
        from telethon.tl import functions, types

        await self.client(functions.stories.TogglePinnedRequest(
            peer=types.InputPeerSelf(), id=[int(i) for i in ids], pinned=pinned))
        return {"истории": list(ids), "в профиле": pinned}

    async def viewers(self, story_id: int, *, limit: int = 50,
                      contacts_only: bool = False) -> list[dict[str, Any]]:
        """Кто смотрел вашу историю и как отреагировал."""
        from telethon.tl import functions, types

        result = await self.client(functions.stories.GetStoryViewsListRequest(
            peer=types.InputPeerSelf(), id=int(story_id), offset="", limit=int(limit),
            just_contacts=contacts_only or None, reactions_first=True,
            forwards_first=None, q=None))
        names = {u.id: (u.username or " ".join(filter(None, [u.first_name, u.last_name])))
                 for u in (getattr(result, "users", None) or [])}
        out = []
        for view in getattr(result, "views", None) or []:
            reaction = getattr(getattr(view, "reaction", None), "emoticon", None)
            out.append({"кто": names.get(getattr(view, "user_id", None), "?"),
                        "реакция": reaction or "",
                        "когда": str(getattr(view, "date", "") or "")[:16]})
        return out

    async def react(self, peer: Any, story_id: int, emoji: str | None) -> dict[str, Any]:
        """Реакция на чужую историю. Пустой эмодзи снимает её."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer)
        reaction = types.ReactionEmpty() if not emoji else types.ReactionEmoji(emoticon=emoji)
        await self.client(functions.stories.SendReactionRequest(
            peer=target, story_id=int(story_id), reaction=reaction, add_to_recent=True))
        return {"история": int(story_id), "реакция": emoji or "снята"}

    async def mark_read(self, peer: Any, max_id: int) -> dict[str, Any]:
        """Отметить истории прочитанными. Автор увидит, что вы смотрели."""
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        result = await self.client(functions.stories.ReadStoriesRequest(
            peer=target, max_id=int(max_id)))
        return {"отмечено прочитанными": list(result or []),
                "примечание": "автор видит, кто смотрел; чтобы не отмечаться — "
                              "включите скрытный режим"}

    async def stealth(self, *, past: bool = True, future: bool = True) -> dict[str, Any]:
        """Скрытный режим: просмотры не засчитываются.

        `past` прячет просмотры за последние минуты, `future` — на ближайшие.
        Режим доступен только с Telegram Premium.
        """
        from telethon.tl import functions

        try:
            result = await self.client(functions.stories.ActivateStealthModeRequest(
                past=past or None, future=future or None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"скрытный режим": "включён", "назад": past, "вперёд": future,
                "обновления": len(getattr(result, "updates", None) or [])}

    async def link(self, peer: Any, story_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        result = await self.client(functions.stories.ExportStoryLinkRequest(
            peer=target, id=int(story_id)))
        return {"ссылка": getattr(result, "link", None)}

    async def hide_peer(self, peer: Any, hidden: bool = True) -> dict[str, Any]:
        """Убрать чьи-то истории из основной ленты."""
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        await self.client(functions.stories.TogglePeerStoriesHiddenRequest(
            peer=target, hidden=hidden))
        return {"скрыт из ленты": hidden}

    async def can_post(self, peer: Any = None) -> dict[str, Any]:
        """Можно ли публиковать сюда историю прямо сейчас."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        try:
            await self.client(functions.stories.CanSendStoryRequest(peer=target))
        except Exception as exc:
            return {"можно": False, "почему": str(self._explain(exc))}
        return {"можно": True}

    # ── альбомы историй ──────────────────────────────────────────────────────
    async def albums(self, peer: Any = None) -> list[dict[str, Any]]:
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.stories.GetAlbumsRequest(peer=target, hash=0))
        return [{"id": getattr(a, "album_id", None), "название": getattr(a, "title", None),
                 "историй": getattr(a, "stories_count", None)}
                for a in (getattr(result, "albums", None) or [])]

    async def create_album(self, title: str, ids: Sequence[int],
                           peer: Any = None) -> dict[str, Any]:
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.stories.CreateAlbumRequest(
            peer=target, title=title.strip(), stories=[int(i) for i in ids]))
        return {"создан": getattr(result, "title", title),
                "id": getattr(result, "album_id", None)}

    async def delete_album(self, album_id: int, peer: Any = None) -> dict[str, Any]:
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        await self.client(functions.stories.DeleteAlbumRequest(
            peer=target, album_id=int(album_id)))
        return {"удалён": int(album_id)}

    async def search(self, hashtag: str, limit: int = 20) -> list[dict[str, Any]]:
        """Поиск публичных историй по хештегу."""
        from telethon.tl import functions

        result = await self.client(functions.stories.SearchPostsRequest(
            offset="", limit=int(limit), hashtag=hashtag.lstrip("#"), area=None, peer=None))
        return [story_row(s) for s in (getattr(result, "stories", None) or [])]

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        hints = {
            "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
            "STORIES_TOO_MUCH": "лимит историй на сегодня исчерпан",
            "STORY_ID_INVALID": "истории с таким номером нет",
            "STORY_PERIOD_INVALID": "такой срок жизни истории не принимается",
            "BOOSTS_REQUIRED": "каналу не хватает бустов для историй",
            "CHAT_ADMIN_REQUIRED": "нужны права администратора",
            "STORY_NOT_MODIFIED": "ничего не изменилось",
            "MEDIA_EMPTY": "к истории нужно вложение — фото или видео",
        }
        import tgx_net

        return tgx_net.explain(exc, hints, StoryError)


# --- то, чего не хватало: правка, альбомы, эфиры, разбор ---

MORE_HINTS = {
    "STORY_ID_INVALID": "такой истории нет",
    "STORY_NOT_MODIFIED": "и так уже так",
    "ALBUM_ID_INVALID": "такого альбома нет",
    "STORIES_TOO_MUCH": "историй больше, чем можно",
    "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
    "PEER_ID_INVALID": "такого адресата нет",
    "MEDIA_EMPTY": "нечего публиковать",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "BOOSTS_REQUIRED": "каналу не хватает бустов для историй",
}


class More:
    """Остаток историй: правка, альбомы, эфиры, просмотры, жалобы."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        import tgx_net

        try:
            return await self.client(request)
        except Exception as exc:
            raise tgx_net.explain(exc, MORE_HINTS, StoryError) from exc

    async def edit(self, peer: Any, story_id: int, *, caption: str | None = None,
                   media: str = "") -> dict[str, Any]:
        """Поправить опубликованную историю.

        Менять можно подпись, картинку и круг зрителей; поле, которое не
        назвали, остаётся прежним — сервер трактует отсутствие как «не трогать».
        """
        from telethon.tl import functions

        uploaded = None
        if media:
            from pathlib import Path

            path = Path(media).expanduser()
            if not path.is_file():
                raise StoryError(f"файла {path} нет")
            uploaded = await self.client.upload_file(str(path))
        await self._call(functions.stories.EditStoryRequest(
            peer=peer, id=story_id, caption=caption, media=uploaded))
        changed = [n for n, v in (("подпись", caption is not None), ("медиа", bool(media))) if v]
        return {"история": story_id, "изменено": changed or ["ничего"]}

    async def by_id(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.stories.GetStoriesByIDRequest(peer=peer, id=ids))
        return [story_row(s) for s in getattr(result, "stories", None) or []]

    async def views(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.stories.GetStoriesViewsRequest(peer=peer, id=ids))
        rows = []
        for index, item in enumerate(getattr(result, "views", None) or []):
            rows.append({"история": ids[index] if index < len(ids) else None,
                         "просмотров": getattr(item, "views_count", None),
                         "реакций": getattr(item, "reactions_count", None),
                         "переслали": getattr(item, "forwards_count", None)})
        return rows

    async def seen(self, peer: Any, ids: list[int]) -> dict[str, Any]:
        """Отметить чужие истории просмотренными — как если бы вы их открыли."""
        from telethon.tl import functions

        await self._call(functions.stories.IncrementStoryViewsRequest(peer=peer, id=ids))
        return {"отмечено просмотренными": len(ids)}

    async def reactions(self, peer: Any, story_id: int, *,
                        limit: int = 50) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.stories.GetStoryReactionsListRequest(
            peer=peer, id=story_id, limit=limit, offset=None))
        names = {u.id: getattr(u, "username", None) or getattr(u, "first_name", None)
                 for u in getattr(result, "users", None) or []}
        rows = []
        for item in getattr(result, "reactions", None) or []:
            who = getattr(getattr(item, "peer_id", None), "user_id", None)
            rows.append({"кто": names.get(who, who),
                         "реакция": getattr(getattr(item, "reaction", None), "emoticon", None)})
        return rows

    async def latest(self, peers: list[Any]) -> list[dict[str, Any]]:
        """Номер свежей истории у каждого — дёшево проверить, есть ли новое."""
        from telethon.tl import functions

        result = await self._call(functions.stories.GetPeerMaxIDsRequest(id=peers))
        return [{"адресат": index, "последняя": value}
                for index, value in enumerate(result or [])]

    async def read_everywhere(self) -> list[Any]:
        """У кого истории дочитаны до конца."""
        from telethon.tl import functions

        result = await self._call(functions.stories.GetAllReadPeerStoriesRequest())
        return [{"кто": getattr(getattr(p, "peer", None), "user_id", None)
                       or getattr(getattr(p, "peer", None), "channel_id", None),
                 "прочитано до": getattr(p, "max_read_id", None)}
                for p in getattr(result, "peers", None) or []]

    async def where_to_post(self) -> list[dict[str, Any]]:
        """Куда вы вообще можете публиковать истории, кроме своего профиля."""
        from telethon.tl import functions

        result = await self._call(functions.stories.GetChatsToSendRequest())
        return [{"куда": getattr(c, "title", None), "id": getattr(c, "id", None)}
                for c in getattr(result, "chats", None) or []]

    async def hide_all(self, hidden: bool) -> dict[str, Any]:
        """Убрать все чужие истории из ленты разом."""
        from telethon.tl import functions

        await self._call(functions.stories.ToggleAllStoriesHiddenRequest(hidden=hidden))
        return {"чужие истории": "скрыты" if hidden else "показываются"}

    async def pin_top(self, peer: Any, ids: list[int]) -> dict[str, Any]:
        """Поднять истории наверх профиля."""
        from telethon.tl import functions

        await self._call(functions.stories.TogglePinnedToTopRequest(peer=peer, id=ids))
        return {"наверху профиля": ids or "ничего"}

    async def album(self, peer: Any, album_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.stories.GetAlbumStoriesRequest(
            peer=peer, album_id=album_id, offset=0, limit=limit))
        return [story_row(s) for s in getattr(result, "stories", None) or []]

    async def edit_album(self, peer: Any, album_id: int, *, title: str = "",
                         add: list[int] | None = None, drop: list[int] | None = None,
                         order: list[int] | None = None) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.stories.UpdateAlbumRequest(
            peer=peer, album_id=album_id, title=title or None,
            add_stories=add or None, delete_stories=drop or None, order=order or None))
        did = [n for n, v in (("название", title), ("добавлено", add),
                              ("убрано", drop), ("порядок", order)) if v]
        return {"альбом": album_id, "изменено": did or ["ничего"]}

    async def order_albums(self, peer: Any, ids: list[int]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.stories.ReorderAlbumsRequest(peer=peer, order=ids))
        return {"порядок альбомов": ids}

    async def report(self, peer: Any, ids: list[int], *, option: str = "",
                     comment: str = "") -> dict[str, Any]:
        """Жалоба по меню сервера — как и на сообщения."""
        import base64

        from telethon.tl import functions

        picked = base64.urlsafe_b64decode(option + "==") if option else b""
        result = await self._call(functions.stories.ReportRequest(
            peer=peer, id=ids, option=picked, message=comment))
        if type(result).__name__ == "ReportResultChooseOption":
            return {"шаг": getattr(result, "title", "выберите причину"),
                    "варианты": [{"что": o.text,
                                  "ключ": base64.urlsafe_b64encode(o.option).decode().rstrip("=")}
                                 for o in getattr(result, "options", None) or []]}
        return {"жалоба": "отправлена"}

    async def start_live(self, peer: Any, *, caption: str = "",
                         rtmp: bool = False) -> dict[str, Any]:
        """Начать прямой эфир историей. Смотреть его из терминала нечем."""
        import secrets

        from telethon.tl import functions, types

        result = await self._call(functions.stories.StartLiveRequest(
            peer=peer, privacy_rules=[types.InputPrivacyValueAllowAll()],
            caption=caption or None, rtmp_stream=rtmp or None,
            random_id=secrets.randbits(63)))
        return {"эфир": "начат", "id": getattr(result, "id", None),
                "поток": "RTMP" if rtmp else "с устройства",
                "смотреть": "в обычном клиенте — видео терминал не покажет"}
