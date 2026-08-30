"""Остаток управления каналами и супергруппами.

Здесь то, что в графическом клиенте разбросано по подменю настроек: адреса,
наборы стикеров группы, платные сообщения, автоперевод, поиск по всем публичным
постам, уборка за участником. Часть необратима — удаление канала, превращение
в трансляцию, — и идёт через подтверждение.
"""

from __future__ import annotations

from typing import Any

import tgx_net


class ChanError(RuntimeError):
    """Не вышло."""


HINTS = {
    "CHANNEL_INVALID": "это не канал и не супергруппа",
    "CHANNEL_PRIVATE": "канал закрыт или вас из него удалили",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "CHAT_NOT_MODIFIED": "и так уже так",
    "USERNAME_INVALID": "такой адрес Telegram не принимает",
    "USERNAME_OCCUPIED": "адрес занят",
    "USERNAME_PURCHASE_AVAILABLE": "адрес свободен, но продаётся на Fragment",
    "USERNAMES_ACTIVE_TOO_MUCH": "столько адресов включить нельзя",
    "STICKERSET_INVALID": "такого набора стикеров нет",
    "PARTICIPANTS_TOO_FEW": "в группе слишком мало людей для этого",
    "BOOSTS_REQUIRED": "каналу не хватает бустов",
    "MEGAGROUP_REQUIRED": "так можно только в супергруппе",
    "BROADCAST_REQUIRED": "так можно только в канале",
    "MSG_ID_INVALID": "такого сообщения нет",
    "SEARCH_QUERY_EMPTY": "нечего искать",
    "SEARCH_WITH_LINK_NOT_SUPPORTED": "поиск по ссылке не работает — ищите словами",
    "FLOOD_WAIT": "слишком часто — подождите и повторите",
    "SEND_AS_PEER_INVALID": "от этого имени писать сюда нельзя",
    "SET_MAIN_PROFILE_TAB_INVALID": "такой вкладки в профиле нет",
    "PARTICIPANT_ID_INVALID": "такого участника нет",
    "PEER_ID_INVALID": ("сервер отвечает так и когда чата нет, и когда писать от чужого "
                        "имени тут просто нельзя: выбор появляется у админов канала "
                        "с привязанной группой и у анонимных админов"),
    "TAKEOUT_REQUIRED": "этот список отдают только внутри выгрузки — tgx делает её сам",
    "TAKEOUT_INIT_DELAY": ("Telegram поставил выгрузку на паузу — это защита. Подтвердите "
                           "запрос в другом своём Telegram и повторите"),
}

# что показывать первым в профиле канала
TABS = {"posts": "ProfileTabPosts", "gifts": "ProfileTabGifts",
        "media": "ProfileTabMedia", "files": "ProfileTabFiles",
        "music": "ProfileTabMusic", "voice": "ProfileTabVoice",
        "links": "ProfileTabLinks", "gifs": "ProfileTabGifs",
        "stories": "ProfileTabStories"}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, ChanError)


class ChanAdmin:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    # --- поиск по всем публичным постам ---

    async def search_posts(self, query: str = "", *, hashtag: str = "",
                           limit: int = 30) -> list[dict[str, Any]]:
        """Искать по постам всего Telegram, а не по своим чатам."""
        from telethon.tl import functions, types

        if not query and not hashtag:
            raise ChanError("нечего искать: назовите слова или хештег")
        result = await self._call(functions.channels.SearchPostsRequest(
            offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0,
            limit=limit, hashtag=hashtag.lstrip("#") or None, query=query or None))
        names = {c.id: getattr(c, "title", None) for c in getattr(result, "chats", None) or []}
        rows = []
        for message in getattr(result, "messages", None) or []:
            body = (getattr(message, "message", "") or "").strip().replace("\n", " ")
            where = getattr(getattr(message, "peer_id", None), "channel_id", None)
            rows.append({"канал": names.get(where, where), "id": getattr(message, "id", None),
                         "текст": body[:140] + ("…" if len(body) > 140 else "")})
        return rows

    async def search_quota(self, query: str = "") -> dict[str, Any]:
        """Сколько ещё можно искать до платного — сервер считает это отдельно."""
        from telethon.tl import functions

        result = await self._call(functions.channels.CheckSearchPostsFloodRequest(
            query=query or None))
        return {"поиск бесплатен": not getattr(result, "query_is_free", False) is False,
                "осталось бесплатных": getattr(result, "remains", None),
                "звёзд за поиск": getattr(result, "stars_amount", None),
                "ждать секунд": getattr(result, "wait_till", None)}

    # --- адреса ---

    async def free_name(self, channel: Any, username: str) -> dict[str, Any]:
        from telethon.tl import functions

        try:
            free = await self._call(functions.channels.CheckUsernameRequest(
                channel=channel, username=username.lstrip("@")))
        except ChanError as exc:
            return {"адрес": username.lstrip("@"), "свободен": False, "почему": str(exc)}
        return {"адрес": username.lstrip("@"), "свободен": bool(free)}

    async def username(self, channel: Any, name: str, *, on: bool = True) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.channels.ToggleUsernameRequest(
            channel=channel, username=name.lstrip("@"), active=on))
        return {"адрес": name.lstrip("@"), "включён": on}

    async def order_usernames(self, channel: Any, names: list[str]) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.channels.ReorderUsernamesRequest(
            channel=channel, order=[n.lstrip("@") for n in names]))
        return {"порядок адресов": [n.lstrip("@") for n in names]}

    async def drop_usernames(self, channel: Any) -> dict[str, Any]:
        """Погасить все адреса разом — канал станет закрытым."""
        from telethon.tl import functions

        await self._call(functions.channels.DeactivateAllUsernamesRequest(channel=channel))
        return {"адреса": "все выключены", "канал": "теперь закрытый"}

    # --- вид и содержание ---

    async def autotranslate(self, channel: Any, on: bool) -> dict[str, Any]:
        """Показывать читателям кнопку перевода постов."""
        from telethon.tl import functions

        await self._call(functions.channels.ToggleAutotranslationRequest(
            channel=channel, enabled=on))
        return {"автоперевод": "включён" if on else "выключен"}

    async def main_tab(self, channel: Any, tab: str) -> dict[str, Any]:
        """Что показывать первым в профиле канала."""
        from telethon.tl import functions, types

        name = TABS.get(tab)
        if name is None:
            raise ChanError(f"не знаю вкладки «{tab}»; есть: {', '.join(sorted(TABS))}")
        await self._call(functions.channels.SetMainProfileTabRequest(
            channel=channel, tab=getattr(types, name)()))
        return {"первой в профиле": tab}

    async def stickers(self, channel: Any, name: str, *, emoji: bool = False) -> dict[str, Any]:
        """Набор стикеров или эмодзи, общий для участников группы."""
        import tgx_stickers
        from telethon.tl import functions

        request = (functions.channels.SetEmojiStickersRequest if emoji
                   else functions.channels.SetStickersRequest)
        await self._call(request(channel=channel, stickerset=tgx_stickers.set_ref(name)))
        return {"набор группы": name, "вид": "эмодзи" if emoji else "стикеры"}

    async def location(self, channel: Any, lat: float, lon: float,
                       address: str) -> dict[str, Any]:
        """Привязать группу к месту — так её находят рядом стоящие."""
        from telethon.tl import functions, types

        await self._call(functions.channels.EditLocationRequest(
            channel=channel, geo_point=types.InputGeoPoint(lat=lat, long=lon),
            address=address))
        return {"место": address, "координаты": [lat, lon]}

    async def send_as(self, peer: Any, *, paid_reactions: bool = False) -> list[dict[str, Any]]:
        """От чьего имени можно писать в этот чат."""
        from telethon.tl import functions

        result = await self._call(functions.channels.GetSendAsRequest(
            peer=peer, for_paid_reactions=paid_reactions or None))
        names = {c.id: getattr(c, "title", None) for c in getattr(result, "chats", None) or []}
        names.update({u.id: getattr(u, "username", None) or getattr(u, "first_name", None)
                      for u in getattr(result, "users", None) or []})
        rows = []
        for item in getattr(result, "peers", None) or []:
            who = getattr(item, "peer", item)
            ident = (getattr(who, "channel_id", None) or getattr(who, "user_id", None)
                     or getattr(who, "chat_id", None))
            rows.append({"кто": names.get(ident, ident),
                         "нужен премиум": bool(getattr(item, "premium_required", False))})
        return rows

    async def paid_messages(self, channel: Any, stars: int, *,
                            broadcast: bool = False) -> dict[str, Any]:
        """Сколько звёзд берётся за сообщение в этой группе."""
        from telethon.tl import functions

        await self._call(functions.channels.UpdatePaidMessagesPriceRequest(
            channel=channel, send_paid_messages_stars=stars,
            broadcast_messages_allowed=broadcast or None))
        return {"за сообщение": f"{stars} звёзд" if stars else "бесплатно"}

    async def boost_bypass(self, channel: Any, boosts: int) -> dict[str, Any]:
        """Сколько бустов снимает ограничения с участника."""
        from telethon.tl import functions

        await self._call(functions.channels.SetBoostsToUnblockRestrictionsRequest(
            channel=channel, boosts=boosts))
        return {"бустов для снятия ограничений": boosts or "выключено"}

    async def hide_ads(self, channel: Any, on: bool) -> dict[str, Any]:
        """Убрать рекламу из своего канала — возможность за бусты."""
        from telethon.tl import functions

        await self._call(functions.channels.RestrictSponsoredMessagesRequest(
            channel=channel, restricted=on))
        return {"реклама в канале": "скрыта" if on else "показывается"}

    # --- разбор и уборка ---

    async def author(self, channel: Any, message_id: int) -> dict[str, Any]:
        """Кто из админов написал этот пост — в канале подпись не видна."""
        from telethon.tl import functions

        result = await self._call(functions.channels.GetMessageAuthorRequest(
            channel=channel, id=message_id))
        return {"сообщение": message_id,
                "автор": getattr(result, "username", None) or getattr(result, "first_name", None),
                "id": getattr(result, "id", None)}

    async def wipe_participant(self, channel: Any, who: Any) -> dict[str, Any]:
        """Стереть всё, что человек написал в этой группе."""
        from telethon.tl import functions

        member = await self.client.get_input_entity(who)
        await self._call(functions.channels.DeleteParticipantHistoryRequest(
            channel=channel, participant=member))
        return {"сообщения": "стёрты", "чьи": str(who)}

    async def clear(self, channel: Any, *, up_to: int = 0,
                    everyone: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.channels.DeleteHistoryRequest(
            channel=channel, max_id=up_to, for_everyone=everyone or None))
        return {"история": "стёрта", "у всех": everyone}

    async def report_spam(self, channel: Any, who: Any, ids: list[int]) -> dict[str, Any]:
        from telethon.tl import functions

        member = await self.client.get_input_entity(who)
        await self._call(functions.channels.ReportSpamRequest(
            channel=channel, participant=member, id=ids))
        return {"жалоба": "отправлена", "сообщений": len(ids)}

    async def antispam_mistake(self, channel: Any, message_id: int) -> dict[str, Any]:
        """Сказать Telegram, что антиспам зря удалил это сообщение."""
        from telethon.tl import functions

        await self._call(functions.channels.ReportAntiSpamFalsePositiveRequest(
            channel=channel, msg_id=message_id))
        return {"сообщение": message_id, "помечено": "антиспам ошибся"}

    async def left(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Каналы, из которых вы вышли, но которые за вами числятся.

        Список отдают только внутри выгрузки: сервер считает его историей, а не
        текущим состоянием, и вне режима экспорта отвечает TAKEOUT_REQUIRED.
        Открываем выгрузку сами и закрываем за собой — просить об этом человека
        было бы странно, он спросил всего лишь список.
        """
        import tgx_takeout
        from telethon.tl import functions

        tgx_takeout.Takeout(self.client)._tidy()
        try:
            async with self.client.takeout(finalize=True, channels=True) as session:
                result = await session(functions.channels.GetLeftChannelsRequest(offset=0))
        except Exception as exc:
            raise _explain(exc) from exc
        return [{"канал": getattr(c, "title", None), "адрес": getattr(c, "username", None),
                 "id": getattr(c, "id", None)}
                for c in (getattr(result, "chats", None) or [])[:limit]]

    async def discussable(self) -> list[dict[str, Any]]:
        """Какие ваши группы можно привязать к каналу как обсуждение."""
        from telethon.tl import functions

        result = await self._call(functions.channels.GetGroupsForDiscussionRequest())
        return [{"группа": getattr(c, "title", None), "id": getattr(c, "id", None)}
                for c in getattr(result, "chats", None) or []]

    # --- необратимое ---

    async def to_broadcast(self, channel: Any) -> dict[str, Any]:
        """Супергруппа → трансляция: много читателей, писать могут админы."""
        from telethon.tl import functions

        await self._call(functions.channels.ConvertToGigagroupRequest(channel=channel))
        return {"стала": "трансляцией", "обратно": "нельзя"}

    async def drop(self, channel: Any) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.channels.DeleteChannelRequest(channel=channel))
        return {"канал": "удалён"}
