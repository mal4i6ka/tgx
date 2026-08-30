"""Обычные группы, ветки обсуждений и признак набора.

Обычная группа — не супергруппа: у неё нет истории для новичков, нет адреса,
нет ролей сложнее «администратор». Telegram её не убрал, и часть переписки у
людей живёт именно в таких. Превратить обычную в супергруппу можно, обратно —
нельзя, поэтому переход идёт через подтверждение.

Здесь же признак набора («печатает…»): без него собеседник видит тишину, пока
вы набираете. В графическом клиенте он ставится сам, в терминале — нет, потому
что терминал не знает, что вы набираете именно сообщение.
"""

from __future__ import annotations

from typing import Any

import tgx_net


class GroupError(RuntimeError):
    """Не вышло."""


HINTS = {
    "CHAT_ID_INVALID": "это не обычная группа; для супергрупп и каналов есть channel-*",
    "PEER_ID_INVALID": "такого чата нет",
    "USER_ID_INVALID": "такого пользователя нет",
    "USERS_TOO_FEW": "группу не завести в одиночку — нужен хотя бы один собеседник",
    "USERS_TOO_MUCH": "в обычной группе столько людей не помещается; нужна супергруппа",
    "USER_NOT_MUTUAL_CONTACT": "человека можно добавить, только если он ваш взаимный контакт",
    "USER_PRIVACY_RESTRICTED": "человек запретил добавлять себя в группы",
    "CHAT_ADMIN_REQUIRED": "нужны права администратора",
    "CHAT_NOT_MODIFIED": "и так уже так",
    "PARTICIPANT_VERSION_OUTDATED": "у собеседника слишком старый клиент для этого",
    "USER_ALREADY_PARTICIPANT": "человек уже в группе",
    "PASSWORD_HASH_INVALID": "неверный пароль двухфакторной защиты",
    "MSG_ID_INVALID": "такого сообщения нет",
    "TTL_PERIOD_INVALID": "срок должен быть 0, 86400 (сутки), 604800 (неделя) или 2678400 (месяц)",
}

# что показывать собеседнику, пока вы заняты
ACTIONS = {
    "typing": "SendMessageTypingAction",
    "cancel": "SendMessageCancelAction",
    "photo": "SendMessageUploadPhotoAction",
    "video": "SendMessageUploadVideoAction",
    "audio": "SendMessageUploadAudioAction",
    "file": "SendMessageUploadDocumentAction",
    "voice": "SendMessageRecordAudioAction",
    "round": "SendMessageRecordRoundAction",
    "record-video": "SendMessageRecordVideoAction",
    "sticker": "SendMessageChooseStickerAction",
    "contact": "SendMessageChooseContactAction",
    "location": "SendMessageGeoLocationAction",
    "game": "SendMessageGamePlayAction",
    "import": "SendMessageHistoryImportAction",
}

# у части действий есть доля выполненного — сервер её показывает
WITH_PROGRESS = {"SendMessageUploadPhotoAction", "SendMessageUploadVideoAction",
                 "SendMessageUploadAudioAction", "SendMessageUploadDocumentAction",
                 "SendMessageHistoryImportAction"}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, GroupError)


def chat_id_of(peer: Any) -> int:
    """Обычная группа адресуется голым числом, а не InputPeer.

    Это единственное место в схеме, где так: у канала InputChannel, у человека
    InputUser, а у обычной группы — просто chat_id. Перепутать легко, и сервер
    отвечает на путаницу невнятным CHAT_ID_INVALID.
    """
    value = getattr(peer, "chat_id", None) or getattr(peer, "id", None)
    if value is None:
        raise GroupError("не понял, какая это группа")
    if getattr(peer, "channel_id", None) or type(peer).__name__.startswith("InputPeerChannel"):
        raise GroupError("это супергруппа или канал — для них команды channel-*")
    return int(value)


class Groups:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def typing(self, peer: Any, what: str = "typing", *, topic: int = 0,
                     progress: int = 0) -> dict[str, Any]:
        """Показать собеседнику, чем вы заняты."""
        from telethon.tl import functions, types

        name = ACTIONS.get(what)
        if name is None:
            raise GroupError(f"не знаю действия «{what}»; есть: {', '.join(sorted(ACTIONS))}")
        maker = getattr(types, name)
        action = maker(progress=progress) if name in WITH_PROGRESS else maker()
        await self._call(functions.messages.SetTypingRequest(
            peer=peer, action=action, top_msg_id=topic or None))
        return {"показано": what}

    async def create(self, title: str, users: list[Any], *, ttl: int = 0) -> dict[str, Any]:
        """Завести обычную группу. В одиночку нельзя — Telegram требует людей."""
        from telethon.tl import functions

        people = [await self.client.get_input_entity(u) for u in users]
        if not people:
            raise GroupError("группу не завести в одиночку — назовите хотя бы одного человека")
        result = await self._call(functions.messages.CreateChatRequest(
            users=people, title=title.strip(), ttl_period=ttl or None))
        chats = getattr(getattr(result, "updates", result), "chats", None) or []
        chat = chats[0] if chats else None
        return {"группа": getattr(chat, "title", title), "id": getattr(chat, "id", None),
                "человек": len(people)}

    async def add(self, peer: Any, who: Any, *, history: int = 0) -> dict[str, Any]:
        """Добавить человека. `history` — сколько прошлых сообщений ему показать."""
        from telethon.tl import functions

        user = await self.client.get_input_entity(who)
        result = await self._call(functions.messages.AddChatUserRequest(
            chat_id=chat_id_of(peer), user_id=user, fwd_limit=history))
        missing = getattr(result, "missing_invitees", None) or []
        row: dict[str, Any] = {"добавлен": str(who), "показано прошлых": history}
        if missing:
            # человек мог закрыть добавление в группы — тогда он не добавлен,
            # но и ошибки нет: сервер отвечает списком неприглашённых
            row["не добавлен"] = [getattr(m, "user_id", m) for m in missing]
        return row

    async def remove(self, peer: Any, who: Any, *, wipe: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        user = await self.client.get_input_entity(who)
        await self._call(functions.messages.DeleteChatUserRequest(
            chat_id=chat_id_of(peer), user_id=user, revoke_history=wipe or None))
        return {"убран": str(who), "его сообщения стёрты": wipe}

    async def rename(self, peer: Any, title: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.EditChatTitleRequest(
            chat_id=chat_id_of(peer), title=title.strip()))
        return {"название": title.strip()}

    async def admin(self, peer: Any, who: Any, *, on: bool = True) -> dict[str, Any]:
        from telethon.tl import functions

        user = await self.client.get_input_entity(who)
        await self._call(functions.messages.EditChatAdminRequest(
            chat_id=chat_id_of(peer), user_id=user, is_admin=on))
        return {"кто": str(who), "администратор": on}

    async def rank(self, peer: Any, who: Any, title: str) -> dict[str, Any]:
        """Звание администратора — подпись вместо слова «админ»."""
        from telethon.tl import functions

        member = await self.client.get_input_entity(who)
        await self._call(functions.messages.EditChatParticipantRankRequest(
            peer=peer, participant=member, rank=title))
        return {"кто": str(who), "звание": title or "без звания"}

    async def hand_over(self, peer: Any, who: Any, password: str) -> dict[str, Any]:
        """Передать группу другому. Требует пароль — как и всё необратимое."""
        from telethon.tl import functions
        from telethon.password import compute_check

        state = await self.client(functions.account.GetPasswordRequest())
        user = await self.client.get_input_entity(who)
        await self._call(functions.messages.EditChatCreatorRequest(
            peer=peer, user_id=user, password=compute_check(state, password)))
        return {"владелец теперь": str(who)}

    async def drop(self, peer: Any) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.DeleteChatRequest(chat_id=chat_id_of(peer)))
        return {"группа": "удалена"}

    async def upgrade(self, peer: Any) -> dict[str, Any]:
        """Превратить в супергруппу. Обратного пути нет."""
        from telethon.tl import functions

        result = await self._call(functions.messages.MigrateChatRequest(
            chat_id=chat_id_of(peer)))
        chats = getattr(result, "chats", None) or []
        new = next((c for c in chats if getattr(c, "megagroup", False)), None)
        return {"стала супергруппой": True, "новый id": getattr(new, "id", None)}

    async def info(self, peer: Any) -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetFullChatRequest(
            chat_id=chat_id_of(peer)))
        full = getattr(result, "full_chat", None)
        chats = getattr(result, "chats", None) or []
        chat = chats[0] if chats else None
        return {"группа": getattr(chat, "title", None),
                "описание": getattr(full, "about", None) or None,
                "участников": len(getattr(getattr(full, "participants", None),
                                          "participants", None) or []),
                "сообщения живут": getattr(full, "ttl_period", None) or "вечно",
                "ссылка": getattr(getattr(full, "exported_invite", None), "link", None)}

    async def ttl(self, peer: Any, seconds: int) -> dict[str, Any]:
        """Через сколько сообщения исчезают в этом чате."""
        from telethon.tl import functions

        await self._call(functions.messages.SetHistoryTTLRequest(peer=peer, period=seconds))
        names = {0: "вечно", 86400: "сутки", 604800: "неделя", 2678400: "месяц"}
        return {"сообщения живут": names.get(seconds, f"{seconds} с")}


class Discussion:
    """Ветки обсуждений: комментарии под постом канала и ответы в группе."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    async def thread(self, peer: Any, message_id: int) -> dict[str, Any]:
        """Куда ведут комментарии под этим постом.

        Пост живёт в канале, а комментарии — в связанной группе, и у них там
        свой номер. Без этого перехода отвечать в ветку не по чему.
        """
        from telethon.tl import functions

        result = await self._call(functions.messages.GetDiscussionMessageRequest(
            peer=peer, msg_id=message_id))
        messages = getattr(result, "messages", None) or []
        head = messages[0] if messages else None
        return {"пост": message_id,
                "в обсуждении id": getattr(head, "id", None),
                "группа обсуждения": getattr(getattr(head, "peer_id", None), "channel_id", None),
                "ответов": getattr(getattr(head, "replies", None), "replies", 0),
                "прочитано до": getattr(result, "read_inbox_max_id", None)}

    async def replies(self, peer: Any, message_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetRepliesRequest(
            peer=peer, msg_id=message_id, offset_id=0, offset_date=None, add_offset=0,
            limit=limit, max_id=0, min_id=0, hash=0))
        rows = []
        for message in getattr(result, "messages", None) or []:
            body = (getattr(message, "message", "") or "").strip().replace("\n", " ")
            rows.append({"id": getattr(message, "id", None),
                         "от": getattr(getattr(message, "from_id", None), "user_id", None),
                         "текст": body[:160] + ("…" if len(body) > 160 else "")})
        return rows

    async def mark_read(self, peer: Any, message_id: int, up_to: int = 0) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ReadDiscussionRequest(
            peer=peer, msg_id=message_id, read_max_id=up_to))
        return {"ветка": message_id, "прочитана до": up_to or "конца"}
