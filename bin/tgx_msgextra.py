"""Остаток пространства messages: то, что не легло в другие модули.

Голосование в опросах, под-чаты избранного, приписки-факт-чеки, предпросмотр
ссылок, тема и обои отдельного чата, недавние геометки, уведомление о снимке
экрана, разбор реакций. По одному эти вещи не тянут на модуль, вместе — тянут.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import tgx_net


class MsgError(RuntimeError):
    """Не вышло."""


HINTS = {
    "PEER_ID_INVALID": "такого чата нет",
    "MSG_ID_INVALID": "такого сообщения нет",
    "POLL_OPTION_INVALID": "такого варианта в опросе нет",
    "POLL_ANSWER_INVALID": "этот вариант выбрать нельзя",
    "REVOTE_NOT_ALLOWED": "в этом опросе переголосовать нельзя",
    "POLL_VOTE_REQUIRED": "сначала надо проголосовать, чтобы увидеть итоги",
    "MESSAGE_POLL_CLOSED": "опрос закрыт",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "FACTCHECK_TOO_LONG": "приписка длиннее допустимого",
    "MESSAGE_NOT_MODIFIED": "и так уже так",
    "URL_INVALID": "ссылка не годится",
    "WALLPAPER_INVALID": "эти обои не годятся",
    "THEME_INVALID": "такой темы нет",
    "REACTION_INVALID": "такой реакции нет",
    "USER_ID_INVALID": "такого пользователя нет",
    "INVITE_HASH_EXPIRED": "ссылка-приглашение больше не действует",
    "INVITE_HASH_INVALID": "такой ссылки-приглашения не существует",
    "RANDOM_ID_INVALID": "ключ рекламы не тот; возьмите свежий из `msgx sponsored`",
    "BUTTON_ID_INVALID": "у этого сообщения нет кнопки с таким номером",
    "URL_AUTH_TOKEN_INVALID": "вход по этой ссылке больше не предлагается",
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
}


FILTERS = {"photo": "InputMessagesFilterPhotos", "video": "InputMessagesFilterVideo",
           "file": "InputMessagesFilterDocument", "music": "InputMessagesFilterMusic",
           "voice": "InputMessagesFilterVoice", "link": "InputMessagesFilterUrl",
           "gif": "InputMessagesFilterGif", "round": "InputMessagesFilterRoundVideo",
           "any": "InputMessagesFilterEmpty"}


def _filter(kind: str) -> Any:
    """Вид вложения — в фильтр поиска. Неизвестное лучше отвергнуть сразу."""
    from telethon.tl import types

    name = FILTERS.get(kind)
    if name is None:
        raise MsgError(f"не знаю вида «{kind}»; есть: {', '.join(sorted(FILTERS))}")
    return getattr(types, name)()


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, MsgError)


def _when(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


class Extra:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    # --- голосование в опросах ---

    async def _poll_options(self, peer: Any, message_id: int) -> list[bytes]:
        """Байты вариантов опроса — их нельзя угадать, надо взять из сообщения."""
        message = await self.client.get_messages(peer, ids=message_id)
        poll = getattr(getattr(message, "media", None), "poll", None)
        if poll is None:
            raise MsgError("в этом сообщении нет опроса")
        return [a.option for a in getattr(poll, "answers", None) or []]

    async def vote(self, peer: Any, message_id: int, choices: list[int]) -> dict[str, Any]:
        """Проголосовать. Варианты задаются номерами, начиная с 1."""
        from telethon.tl import functions

        options = await self._poll_options(peer, message_id)
        picked = []
        for number in choices:
            if not 1 <= number <= len(options):
                raise MsgError(f"вариант {number} вне 1–{len(options)}")
            picked.append(options[number - 1])
        result = await self._call(functions.messages.SendVoteRequest(
            peer=peer, msg_id=message_id, options=picked))
        return {"проголосовано": choices, "обновлено": bool(result)}

    async def retract_vote(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Забрать свой голос — пустой список вариантов."""
        from telethon.tl import functions

        await self._call(functions.messages.SendVoteRequest(
            peer=peer, msg_id=message_id, options=[]))
        return {"голос": "отозван"}

    async def voters(self, peer: Any, message_id: int, *, option: int = 0,
                     limit: int = 50) -> list[dict[str, Any]]:
        """Кто как проголосовал. Работает в открытых опросах."""
        from telethon.tl import functions

        chosen = None
        if option:
            options = await self._poll_options(peer, message_id)
            if not 1 <= option <= len(options):
                raise MsgError(f"вариант {option} вне 1–{len(options)}")
            chosen = options[option - 1]
        result = await self._call(functions.messages.GetPollVotesRequest(
            peer=peer, id=message_id, limit=limit, option=chosen))
        names = {u.id: getattr(u, "username", None) or getattr(u, "first_name", None)
                 for u in getattr(result, "users", None) or []}
        rows = []
        for vote in getattr(result, "votes", None) or []:
            rows.append({"кто": names.get(getattr(vote, "user_id", None),
                                          getattr(vote, "user_id", None))})
        return rows

    # --- факт-чек: приписка от админов под чужим постом ---

    async def fact_check(self, peer: Any, message_id: int, text: str) -> dict[str, Any]:
        """Приписать проверку факта под сообщением. Право даёт государство/платформа."""
        from telethon.tl import functions, types

        await self._call(functions.messages.EditFactCheckRequest(
            peer=peer, msg_id=message_id,
            text=types.TextWithEntities(text=text, entities=[])))
        return {"факт-чек": "добавлен", "сообщение": message_id}

    async def drop_fact_check(self, peer: Any, message_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.DeleteFactCheckRequest(
            peer=peer, msg_id=message_id))
        return {"факт-чек": "убран", "сообщение": message_id}

    # --- предпросмотр ссылки ---

    async def link_preview(self, text: str) -> dict[str, Any]:
        """Что Telegram покажет под ссылкой, не отправляя сообщения."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetWebPagePreviewRequest(message=text))
        media = getattr(result, "media", result)
        page = getattr(media, "webpage", media)
        if page is None or type(page).__name__ in {"WebPageEmpty", "WebPagePending"}:
            return {"предпросмотр": "нет"}
        return {"заголовок": getattr(page, "title", None),
                "описание": getattr(page, "description", None),
                "сайт": getattr(page, "site_name", None),
                "адрес": getattr(page, "url", None),
                "тип": getattr(page, "type", None)}

    # --- тема и обои отдельного чата ---

    async def chat_theme(self, peer: Any, emoticon: str) -> dict[str, Any]:
        """Тема для одного чата задаётся эмодзи. Пустой — сбросить."""
        from telethon.tl import functions, types

        theme = (types.InputChatThemeEmpty() if not emoticon
                 else types.InputChatTheme(emoticon=emoticon))
        await self._call(functions.messages.SetChatThemeRequest(peer=peer, theme=theme))
        return {"тема чата": emoticon or "по умолчанию"}

    async def chat_wallpaper(self, peer: Any, slug: str = "", *,
                             both: bool = False, revert: bool = False) -> dict[str, Any]:
        """Обои для одного чата. `both` — и у собеседника, `revert` — вернуть."""
        from telethon.tl import functions, types

        paper = types.InputWallPaperSlug(slug=slug) if slug else None
        await self._call(functions.messages.SetChatWallPaperRequest(
            peer=peer, wallpaper=paper, for_both=both or None, revert=revert or None))
        return {"обои чата": slug or ("возвращены" if revert else "убраны"),
                "у обоих": both}

    # --- недавние геометки, отправленные в чат ---

    async def recent_locations(self, peer: Any, *, limit: int = 20) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetRecentLocationsRequest(
            peer=peer, limit=limit, hash=0))
        rows = []
        for message in getattr(result, "messages", None) or []:
            geo = getattr(getattr(message, "media", None), "geo", None)
            if geo is not None:
                rows.append({"id": message.id,
                             "широта": getattr(geo, "lat", None),
                             "долгота": getattr(geo, "long", None),
                             "когда": _when(getattr(message, "date", None))})
        return rows

    # --- уведомление о снимке экрана ---

    async def screenshot_notice(self, peer: Any, message_id: int = 0) -> dict[str, Any]:
        """Сообщить собеседнику, что вы сняли экран, — как в исчезающих чатах."""
        from telethon.tl import functions, types

        reply = types.InputReplyToMessage(reply_to_msg_id=message_id) if message_id else None
        await self._call(functions.messages.SendScreenshotNotificationRequest(
            peer=peer, reply_to=reply or types.InputReplyToMessage(reply_to_msg_id=0),
            random_id=secrets.randbits(63)))
        return {"уведомление о снимке экрана": "отправлено"}

    # --- реакции: недавние, для тегов, жалоба ---

    async def recent_reactions(self) -> list[str]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetRecentReactionsRequest(
            limit=50, hash=0))
        return [getattr(r, "emoticon", None) or str(getattr(r, "document_id", ""))
                for r in getattr(result, "reactions", None) or []]

    async def tag_reactions(self) -> list[str]:
        """Реакции, которыми помечают избранное как тегами."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetDefaultTagReactionsRequest(hash=0))
        return [getattr(r, "emoticon", None) or str(getattr(r, "document_id", ""))
                for r in getattr(result, "reactions", None) or []]

    async def clear_recent_reactions(self) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ClearRecentReactionsRequest())
        return {"недавние реакции": "очищены"}

    async def who_reacted(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        """Сводка реакций на сообщения — числами по каждому."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMessagesReactionsRequest(
            peer=peer, id=ids))
        rows = []
        for update in getattr(result, "updates", None) or []:
            reactions = getattr(update, "reactions", None)
            if reactions is None:
                continue
            marks = [(getattr(getattr(r, "reaction", None), "emoticon", None) or "?",
                      getattr(r, "count", 0))
                     for r in getattr(reactions, "results", None) or []]
            rows.append({"сообщение": getattr(update, "msg_id", None),
                         "реакции": {emoji: count for emoji, count in marks}})
        return rows

    async def report_reaction(self, peer: Any, message_id: int, who: Any) -> dict[str, Any]:
        """Пожаловаться на чужую реакцию — если ею оскорбляют."""
        from telethon.tl import functions

        author = await self.client.get_input_entity(who)
        await self._call(functions.messages.ReportReactionRequest(
            peer=peer, id=message_id, reaction_peer=author))
        return {"жалоба на реакцию": "отправлена"}

    # --- под-чаты избранного ---

    async def saved_dialogs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Избранное умеет делиться на под-чаты по автору — вот их список."""
        from telethon.tl import functions, types

        result = await self._call(functions.messages.GetSavedDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(),
            limit=limit, hash=0))
        names = {u.id: getattr(u, "username", None) or getattr(u, "first_name", None)
                 for u in getattr(result, "users", None) or []}
        names.update({c.id: getattr(c, "title", None)
                      for c in getattr(result, "chats", None) or []})
        rows = []
        for dialog in getattr(result, "dialogs", None) or []:
            who = getattr(dialog, "peer", None)
            ident = (getattr(who, "user_id", None) or getattr(who, "channel_id", None)
                     or getattr(who, "chat_id", None))
            rows.append({"от кого": names.get(ident, ident),
                         "сообщений сверху": getattr(dialog, "top_message", None)})
        return rows

    async def saved_history(self, who: Any, *, limit: int = 30) -> list[dict[str, Any]]:
        """Сохранённое от конкретного автора внутри избранного."""
        from telethon.tl import functions

        parent = await self.client.get_input_entity(who)
        result = await self._call(functions.messages.GetSavedHistoryRequest(
            peer=parent, offset_id=0, offset_date=None, add_offset=0, limit=limit,
            max_id=0, min_id=0, hash=0))
        rows = []
        for message in getattr(result, "messages", None) or []:
            body = (getattr(message, "message", "") or "").strip().replace("\n", " ")
            rows.append({"id": message.id, "когда": _when(getattr(message, "date", None)),
                         "текст": body[:160] + ("…" if len(body) > 160 else "")})
        return rows

    # --- реклама в каналах ---

    async def sponsored(self, peer: Any) -> list[dict[str, Any]]:
        """Какую рекламу Telegram показывает в этом канале.

        Ключ отдаём как есть, шестнадцатеричным: он нужен, чтобы отметить
        просмотр, нажатие или пожаловаться, а сам по себе — просто байты.
        """
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSponsoredMessagesRequest(peer=peer))
        rows = []
        for item in getattr(result, "messages", None) or []:
            rows.append({"ключ": getattr(item, "random_id", b"").hex(),
                         "текст": (getattr(item, "message", "") or "")[:160],
                         "кнопка": getattr(item, "button_text", None),
                         "рекламодатель": getattr(item, "sponsor_info", None),
                         "можно скрыть": bool(getattr(item, "can_report", False))})
        return rows

    async def sponsored_seen(self, key: str, *, clicked: bool = False) -> dict[str, Any]:
        """Отметить рекламу просмотренной или нажатой — как делает клиент."""
        from telethon.tl import functions

        raw = bytes.fromhex(key)
        request = (functions.messages.ClickSponsoredMessageRequest(random_id=raw) if clicked
                   else functions.messages.ViewSponsoredMessageRequest(random_id=raw))
        await self._call(request)
        return {"реклама": "нажата" if clicked else "просмотрена"}

    async def report_sponsored(self, key: str, *, option: str = "") -> dict[str, Any]:
        """Жалоба на рекламу — по меню сервера, как и всё остальное."""
        import base64

        from telethon.tl import functions

        picked = base64.urlsafe_b64decode(option + "==") if option else b""
        result = await self._call(functions.messages.ReportSponsoredMessageRequest(
            random_id=bytes.fromhex(key), option=picked))
        if type(result).__name__ == "ChannelsSponsoredMessageReportResultChooseOption":
            return {"шаг": getattr(result, "title", "выберите причину"),
                    "варианты": [{"что": o.text,
                                  "ключ": base64.urlsafe_b64encode(o.option).decode().rstrip("=")}
                                 for o in getattr(result, "options", None) or []]}
        return {"жалоба": "отправлена"}

    # --- вход по ссылке из кнопки ---

    async def url_auth(self, peer: Any, message_id: int, button_id: int) -> dict[str, Any]:
        """Что предлагает кнопка «войти через Telegram», до согласия."""
        from telethon.tl import functions

        result = await self._call(functions.messages.RequestUrlAuthRequest(
            peer=peer, msg_id=message_id, button_id=button_id))
        kind = type(result).__name__
        if kind == "UrlAuthResultRequest":
            return {"сайт": getattr(result, "domain", None),
                    "бот": getattr(getattr(result, "bot", None), "username", None),
                    "просит писать вам": bool(getattr(result, "request_write_access", False)),
                    "дальше": "tgx msgx url-accept"}
        return {"адрес": getattr(result, "url", None) or "согласие не требуется"}

    async def url_accept(self, peer: Any, message_id: int, button_id: int, *,
                         allow_write: bool = False) -> dict[str, Any]:
        """Согласиться войти. Это выдаёт сайту ваш профиль — потому и спрашивают."""
        from telethon.tl import functions

        result = await self._call(functions.messages.AcceptUrlAuthRequest(
            peer=peer, msg_id=message_id, button_id=button_id,
            write_allowed=allow_write or None))
        return {"адрес": getattr(result, "url", None), "разрешили писать": allow_write}

    # --- календарь и позиции найденного ---

    async def search_calendar(self, peer: Any, *, kind: str = "photo") -> list[dict[str, Any]]:
        """По каким дням в чате есть вложения этого вида."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSearchResultsCalendarRequest(
            peer=peer, filter=_filter(kind), offset_id=0, offset_date=None))
        return [{"дата": _when(getattr(p, "date", None)), "сообщений": getattr(p, "count", None),
                 "первое": getattr(p, "min_msg_id", None)}
                for p in getattr(result, "periods", None) or []]

    async def search_positions(self, peer: Any, *, kind: str = "photo",
                               limit: int = 100) -> list[int]:
        """Номера сообщений с вложениями — чтобы прыгать по ленте."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSearchResultsPositionsRequest(
            peer=peer, filter=_filter(kind), offset_id=0, limit=limit))
        return [getattr(p, "msg_id", None) for p in getattr(result, "positions", None) or []]

    async def sent_media(self, query: str, *, kind: str = "photo",
                         limit: int = 30) -> list[dict[str, Any]]:
        """Поиск среди того, что вы сами отправляли."""
        from telethon.tl import functions

        result = await self._call(functions.messages.SearchSentMediaRequest(
            q=query, filter=_filter(kind), limit=limit))
        rows = []
        for message in getattr(result, "messages", None) or []:
            rows.append({"id": message.id, "когда": _when(getattr(message, "date", None)),
                         "текст": (getattr(message, "message", "") or "")[:100]})
        return rows

    # --- приглашения, правка, отложенные ---

    async def check_invite(self, link: str) -> dict[str, Any]:
        """Что за чат по ссылке-приглашению, не вступая в него."""
        from telethon.tl import functions

        code = link.rstrip("/").split("/")[-1].lstrip("+")
        result = await self._call(functions.messages.CheckChatInviteRequest(hash=code))
        kind = type(result).__name__
        if kind == "ChatInviteAlready":
            chat = getattr(result, "chat", None)
            return {"вы уже там": True, "чат": getattr(chat, "title", None)}
        return {"чат": getattr(result, "title", None),
                "участников": getattr(result, "participants_count", None),
                "по заявке": bool(getattr(result, "request_needed", False)),
                "вы уже там": False}

    async def invite_info(self, peer: Any, link: str) -> dict[str, Any]:
        """Подробности выпущенной ссылки: кто выпустил, сколько прошло."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetExportedChatInviteRequest(
            peer=peer, link=link))
        invite = getattr(result, "invite", None)
        return {"ссылка": getattr(invite, "link", link),
                "название": getattr(invite, "title", None),
                "вошло": getattr(invite, "usage", None),
                "предел": getattr(invite, "usage_limit", None),
                "отозвана": bool(getattr(invite, "revoked", False))}

    async def edit_window(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Можно ли ещё править это сообщение — сервер знает точнее часов."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMessageEditDataRequest(
            peer=peer, id=message_id))
        return {"сообщение": message_id,
                "правится": True,
                "нужен предпросмотр ссылки": bool(getattr(result, "caption", False))}

    async def scheduled(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        """Отложенные сообщения по номерам."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetScheduledMessagesRequest(
            peer=peer, id=ids))
        return [{"id": m.id, "когда": _when(getattr(m, "date", None)),
                 "текст": (getattr(m, "message", "") or "")[:120]}
                for m in getattr(result, "messages", None) or []]

    async def default_send_as(self, peer: Any, who: Any) -> dict[str, Any]:
        """От чьего имени писать в этот чат по умолчанию."""
        from telethon.tl import functions

        author = await self.client.get_input_entity(who)
        await self._call(functions.messages.SaveDefaultSendAsRequest(
            peer=peer, send_as=author))
        return {"писать как": str(who)}

    async def personal_channel(self, user: Any, *, limit: int = 20) -> list[dict[str, Any]]:
        """Посты личного канала человека — те, что видны в его профиле."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        result = await self._call(functions.messages.GetPersonalChannelHistoryRequest(
            user_id=entity, limit=limit, max_id=0, min_id=0, hash=0))
        return [{"id": m.id, "когда": _when(getattr(m, "date", None)),
                 "текст": (getattr(m, "message", "") or "")[:120]}
                for m in getattr(result, "messages", None) or []]

    async def suggested_post(self, peer: Any, message_id: int, *, reject: bool = False,
                             comment: str = "") -> dict[str, Any]:
        """Принять или отклонить предложенный в канал пост."""
        from telethon.tl import functions

        await self._call(functions.messages.ToggleSuggestedPostApprovalRequest(
            peer=peer, msg_id=message_id, reject=reject or None,
            reject_comment=comment or None))
        return {"предложенный пост": "отклонён" if reject else "принят"}

    # --- быстрые ответы ---

    async def check_shortcut(self, name: str) -> dict[str, Any]:
        from telethon.tl import functions

        free = await self._call(functions.messages.CheckQuickReplyShortcutRequest(
            shortcut=name))
        return {"имя": name, "свободно": bool(free)}

    async def order_shortcuts(self, ids: list[int]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ReorderQuickRepliesRequest(order=ids))
        return {"порядок быстрых ответов": ids}

    async def drop_shortcut_messages(self, shortcut_id: int,
                                     ids: list[int]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.DeleteQuickReplyMessagesRequest(
            shortcut_id=shortcut_id, id=ids))
        return {"убрано из быстрого ответа": len(ids)}

    # --- перенос переписки из другого мессенджера ---
    #
    # Устроено в четыре шага, и порядок неслучаен: сначала сервер по первым
    # строкам файла узнаёт формат, потом проверяет, годится ли чат, потом
    # заводит перенос, а медиа доливаются по одному и лишь затем запускается
    # сборка. Пропустить шаг нельзя — сервер откажет, но объяснит скупо.

    async def import_check(self, head: str) -> dict[str, Any]:
        """Узнаёт ли Telegram формат по первым строкам выгрузки."""
        from telethon.tl import functions

        ok = await self._call(functions.messages.CheckHistoryImportRequest(
            import_head=head[:1000]))
        return {"формат распознан": bool(ok),
                "откуда": getattr(ok, "title", None) if not isinstance(ok, bool) else None}

    async def import_target(self, peer: Any) -> dict[str, Any]:
        """Годится ли этот чат для переноса и что он скажет пользователю."""
        from telethon.tl import functions

        result = await self._call(functions.messages.CheckHistoryImportPeerRequest(peer=peer))
        return {"чат годится": True, "предупреждение": getattr(result, "confirm_text", None)}

    async def import_start(self, peer: Any, path: Any, *,
                           media_count: int = 0) -> dict[str, Any]:
        """Завести перенос: залить файл выгрузки и получить номер."""
        from pathlib import Path

        from telethon.tl import functions

        source = Path(path).expanduser()
        if not source.is_file():
            raise MsgError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        result = await self._call(functions.messages.InitHistoryImportRequest(
            peer=peer, file=uploaded, media_count=media_count))
        return {"перенос": getattr(result, "id", None),
                "дальше": "долить медиа и запустить `msgx import-run`"}

    async def import_media(self, peer: Any, import_id: int, path: Any) -> dict[str, Any]:
        """Долить одно вложение к переносу."""
        from pathlib import Path

        from telethon.tl import functions, types

        source = Path(path).expanduser()
        if not source.is_file():
            raise MsgError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        await self._call(functions.messages.UploadImportedMediaRequest(
            peer=peer, import_id=import_id, file_name=source.name,
            media=types.InputMediaUploadedDocument(
                file=uploaded, mime_type="application/octet-stream", attributes=[])))
        return {"долито": source.name}

    async def import_run(self, peer: Any, import_id: int) -> dict[str, Any]:
        """Запустить сборку перенесённой переписки."""
        from telethon.tl import functions

        await self._call(functions.messages.StartHistoryImportRequest(
            peer=peer, import_id=import_id))
        return {"перенос": import_id, "запущен": True}

    # --- опросы: свои варианты и непрочитанные голоса ---

    async def add_answer(self, peer: Any, message_id: int, text: str) -> dict[str, Any]:
        """Дописать свой вариант в опрос, где это разрешено."""
        from telethon.tl import functions, types

        options = await self._poll_options(peer, message_id)
        answer = types.PollAnswer(
            text=types.TextWithEntities(text=text, entities=[]),
            option=bytes([len(options)]))
        await self._call(functions.messages.AddPollAnswerRequest(
            peer=peer, msg_id=message_id, answer=answer))
        return {"вариант добавлен": text}

    async def drop_answer(self, peer: Any, message_id: int, option: int) -> dict[str, Any]:
        from telethon.tl import functions

        options = await self._poll_options(peer, message_id)
        if not 1 <= option <= len(options):
            raise MsgError(f"вариант {option} вне 1–{len(options)}")
        await self._call(functions.messages.DeletePollAnswerRequest(
            peer=peer, msg_id=message_id, option=options[option - 1]))
        return {"вариант убран": option}

    async def unread_votes(self, peer: Any, *, limit: int = 30) -> list[dict[str, Any]]:
        """Голоса в ваших опросах, которых вы ещё не видели."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetUnreadPollVotesRequest(
            peer=peer, offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0))
        return [{"опрос": m.id, "текст": (getattr(m, "message", "") or "")[:100]}
                for m in getattr(result, "messages", None) or []]

    async def read_votes(self, peer: Any, *, topic: int = 0) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ReadPollVotesRequest(
            peer=peer, top_msg_id=topic or None))
        return {"голоса": "отмечены прочитанными"}

    # --- закрепления и папки ---

    async def pin_saved(self, who: Any, *, pinned: bool = True) -> dict[str, Any]:
        """Закрепить под-чат избранного."""
        from telethon.tl import functions, types

        peer = await self.client.get_input_entity(who)
        await self._call(functions.messages.ToggleSavedDialogPinRequest(
            peer=types.InputDialogPeer(peer=peer), pinned=pinned or None))
        return {"под-чат": str(who), "закреплён": pinned}

    async def pinned_saved(self) -> list[Any]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetPinnedSavedDialogsRequest())
        return [getattr(getattr(d, "peer", None), "user_id", None)
                or getattr(getattr(d, "peer", None), "channel_id", None)
                for d in getattr(result, "dialogs", None) or []]

    async def order_pinned(self, peers: list[Any], *, folder: int = 0) -> dict[str, Any]:
        """Переставить закреплённые чаты в списке."""
        from telethon.tl import functions, types

        order = [types.InputDialogPeer(peer=await self.client.get_input_entity(p))
                 for p in peers]
        await self._call(functions.messages.ReorderPinnedDialogsRequest(
            folder_id=folder, order=order, force=True))
        return {"закреплённых переставлено": len(order)}

    async def suggested_folders(self) -> list[dict[str, Any]]:
        """Папки, которые Telegram предлагает завести."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSuggestedDialogFiltersRequest())
        rows = []
        for item in result or []:
            # название лежит внутри самой папки, а не на обёртке с описанием
            inner = getattr(item, "filter", None)
            title = getattr(inner, "title", None)
            rows.append({"папка": getattr(title, "text", None) or title,
                         "описание": getattr(item, "description", None)})
        return rows

    async def folder_tags(self, on: bool) -> dict[str, Any]:
        """Цветные метки папок в списке чатов — возможность Premium."""
        from telethon.tl import functions

        await self._call(functions.messages.ToggleDialogFilterTagsRequest(enabled=on))
        return {"метки папок": "включены" if on else "выключены"}

    # --- платные реакции и скрытое медиа ---

    async def paid_privacy(self, peer: Any = None, message_id: int = 0, *,
                           anonymous: bool | None = None) -> dict[str, Any]:
        """Видно ли ваше имя под платной реакцией. Без аргументов — узнать."""
        from telethon.tl import functions, types

        if anonymous is None:
            result = await self._call(functions.messages.GetPaidReactionPrivacyRequest())
            kind = type(result).__name__
            return {"платные реакции": "анонимно" if "Anonymous" in kind else "с именем"}
        choice = (types.PaidReactionPrivacyAnonymous() if anonymous
                  else types.PaidReactionPrivacyDefault())
        await self._call(functions.messages.TogglePaidReactionPrivacyRequest(
            peer=peer, msg_id=message_id, private=choice))
        return {"платные реакции": "анонимно" if anonymous else "с именем"}

    async def extended_media(self, peer: Any, ids: list[int]) -> list[dict[str, Any]]:
        """Медиа, скрытое до оплаты, — что о нём известно заранее."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetExtendedMediaRequest(
            peer=peer, id=ids))
        return [{"обновление": type(u).__name__.replace("Update", "")}
                for u in getattr(result, "updates", None) or []]

    async def rich_message(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Богатое сообщение целиком — то, что рисует `history`."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetRichMessageRequest(
            peer=peer, id=message_id))
        blocks = getattr(getattr(result, "rich_message", None), "blocks", None) or []
        return {"сообщение": message_id, "блоков": len(blocks),
                "виды": sorted({type(b).__name__.replace("PageBlock", "") for b in blocks})}

    async def drop_call_history(self, *, both: bool = False) -> dict[str, Any]:
        """Стереть список звонков."""
        from telethon.tl import functions

        await self._call(functions.messages.DeletePhoneCallHistoryRequest(
            revoke=both or None))
        return {"история звонков": "стёрта", "у обоих": both}

    async def drop_saved(self, who: Any) -> dict[str, Any]:
        """Стереть сохранённое от одного автора внутри избранного."""
        from telethon.tl import functions

        peer = await self.client.get_input_entity(who)
        await self._call(functions.messages.DeleteSavedHistoryRequest(peer=peer, max_id=0))
        return {"сохранённое от": str(who), "стёрто": True}

    async def read_saved(self, who: Any, *, up_to: int = 0) -> dict[str, Any]:
        from telethon.tl import functions, types

        peer = await self.client.get_input_entity(who)
        await self._call(functions.messages.ReadSavedHistoryRequest(
            parent_peer=types.InputPeerSelf(), peer=peer, max_id=up_to))
        return {"под-чат": str(who), "прочитан": True}

    # --- последние: вход по коду, скрытые чаты, приложения ботов, порядок папок ---

    async def url_decline(self, peer: Any, message_id: int,
                          button_id: int) -> dict[str, Any]:
        """Отказаться входить по кнопке — сайт не узнает о вас ничего."""
        from telethon.tl import functions

        await self._call(functions.messages.DeclineUrlAuthRequest(
            peer=peer, msg_id=message_id, button_id=button_id))
        return {"вход": "отклонён"}

    async def url_match_code(self, url: str, code: str) -> dict[str, Any]:
        """Сверить код подтверждения при входе по ссылке."""
        from telethon.tl import functions

        result = await self._call(functions.messages.CheckUrlAuthMatchCodeRequest(
            url=url, match_code=code))
        return {"код подходит": bool(result)}

    async def attach_bot(self, bot: Any) -> dict[str, Any]:
        """Что за бот в меню вложений — до того, как его добавить."""
        from telethon.tl import functions

        who = await self.client.get_input_entity(bot)
        result = await self._call(functions.messages.GetAttachMenuBotRequest(bot=who))
        item = getattr(result, "bot", result)
        return {"бот": getattr(item, "short_name", None),
                "добавлен": not getattr(item, "inactive", True),
                "просит писать вам": bool(getattr(item, "request_write_access", False))}

    async def bot_app(self, bot: Any, short_name: str) -> dict[str, Any]:
        """Мини-приложение бота по короткому имени — до запуска."""
        from telethon.tl import functions, types

        who = await self.client.get_input_entity(bot)
        result = await self._call(functions.messages.GetBotAppRequest(
            app=types.InputBotAppShortName(bot_id=who, short_name=short_name), hash=0))
        app = getattr(result, "app", None)
        return {"приложение": getattr(app, "title", None),
                "короткое имя": getattr(app, "short_name", short_name),
                "описание": getattr(app, "description", None),
                "надо подтвердить запуск": bool(getattr(result, "request_write_access", False))}

    async def game_info(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Сведения об эмодзи-игре в сообщении."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetEmojiGameInfoRequest(
            peer=peer, msg_id=message_id))
        return {"игра": type(result).__name__.replace("EmojiGameInfo", "") or "есть",
                "сообщение": message_id}

    async def future_owner(self, peer: Any) -> dict[str, Any]:
        """Кому достанется чат, если вы из него выйдете."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetFutureChatCreatorAfterLeaveRequest(
            peer=peer))
        user = getattr(result, "user_id", None) or getattr(result, "id", None)
        return {"следующий владелец": user or "никто — чат осиротеет"}

    async def saved_by_id(self, peers: list[Any]) -> list[dict[str, Any]]:
        """Под-чаты избранного по именам — точечно, без листания списка."""
        from telethon.tl import functions

        entities = [await self.client.get_input_entity(p) for p in peers]
        result = await self._call(functions.messages.GetSavedDialogsByIDRequest(
            ids=entities))
        return [{"сверху": getattr(d, "top_message", None)}
                for d in getattr(result, "dialogs", None) or []]

    async def order_pinned_saved(self, peers: list[Any]) -> dict[str, Any]:
        from telethon.tl import functions, types

        order = [types.InputDialogPeer(peer=await self.client.get_input_entity(p))
                 for p in peers]
        await self._call(functions.messages.ReorderPinnedSavedDialogsRequest(
            order=order, force=True))
        return {"под-чатов переставлено": len(order)}

    async def order_folders(self, ids: list[int]) -> dict[str, Any]:
        """Порядок папок в списке чатов."""
        from telethon.tl import functions

        await self._call(functions.messages.UpdateDialogFiltersOrderRequest(order=ids))
        return {"порядок папок": ids}

    async def web_page(self, url: str) -> dict[str, Any]:
        """Мгновенный просмотр страницы, если Telegram его умеет для неё."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetWebPageRequest(url=url, hash=0))
        page = getattr(result, "webpage", result)
        return {"заголовок": getattr(page, "title", None),
                "сайт": getattr(page, "site_name", None),
                "мгновенный просмотр": bool(getattr(page, "cached_page", None))}

    async def app_web_view(self, bot: Any, short_name: str) -> dict[str, Any]:
        """Подписанный адрес приложения по короткому имени — третий способ.

        Первые два — боковое меню и чат — уже умеет `tgx inline run`; этот нужен
        ссылкам вида t.me/бот/имя.
        """
        import json as _json

        from telethon.tl import functions, types

        who = await self.client.get_input_entity(bot)
        result = await self._call(functions.messages.RequestAppWebViewRequest(
            peer=types.InputPeerSelf(),
            app=types.InputBotAppShortName(bot_id=who, short_name=short_name),
            platform="tdesktop",
            theme_params=types.DataJSON(data=_json.dumps({"bg_color": "#121212"}))))
        return {"адрес": getattr(result, "url", None),
                "осторожно": "адрес подписан вашей сессией — не передавайте его никому"}
