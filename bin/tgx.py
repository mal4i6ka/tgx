#!/usr/bin/env python3
"""tgx: small Telethon-based Telegram CLI for account automation.

Requires Telegram API credentials from https://my.telegram.org/apps.
The first command that contacts Telegram will prompt for phone/code/password and
store a local session in ~/telegram-cli-tools/data/tgx.session.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from telethon import TelegramClient, helpers
from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions, types
from telethon.tl.types import Channel, Chat, User

import tgx_article
import tgx_banner
import tgx_ai
import tgx_chanadmin
import tgx_chatx
import tgx_bots
import tgx_business
import tgx_calls
import tgx_callweb
import tgx_confirm
import tgx_contacts
import tgx_folders
import tgx_format
import tgx_forum
import tgx_inline
import tgx_groups
import tgx_guard
import tgx_net
import tgx_notify
import tgx_pay
import tgx_pending
import tgx_poll
import tgx_profile
import tgx_rich
import tgx_transcribe
import tgx_render as render
import tgx_safety
import tgx_security
import tgx_splash
import tgx_stats
import tgx_takeout
import tgx_triage
import tgx_stickers
import tgx_stories

VERSION = "1.0.0"

BASE = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
DATA = BASE / "data"
SESSION = DATA / "tgx.session"
CONFIG = DATA / "config.json"


def eprint(*args: Any) -> None:
    render.hint(" ".join(str(a) for a in args))


def load_config() -> dict[str, Any]:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {}


def save_config(config: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def get_credentials() -> tuple[int, str]:
    config = load_config()
    api_id = os.environ.get("TG_API_ID") or config.get("api_id")
    api_hash = os.environ.get("TG_API_HASH") or config.get("api_hash")
    if not api_id or not api_hash:
        eprint("Telegram API credentials are required.")
        eprint("Create them at https://my.telegram.org/apps")
        api_id = input("api_id: ").strip()
        api_hash = getpass.getpass("api_hash: ").strip()
        if not api_id or not api_hash:
            raise SystemExit("missing api_id/api_hash")
        config["api_id"] = int(api_id)
        config["api_hash"] = api_hash
        save_config(config)
        eprint(f"Saved credentials to {CONFIG}")
    return int(api_id), str(api_hash)


def entity_title(entity: Any) -> str:
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        title = " ".join(p for p in parts if p).strip()
        return title or entity.username or str(entity.id)
    return getattr(entity, "title", None) or getattr(entity, "username", None) or str(getattr(entity, "id", ""))


def entity_kind(entity: Any) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        if getattr(entity, "broadcast", False):
            return "channel"
        if getattr(entity, "megagroup", False):
            return "group"
        return "channel"
    if isinstance(entity, Chat):
        return "group"
    return type(entity).__name__.lower()


def _duration(seconds: Any) -> str:
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}" if total else ""


def _size(bytes_count: Any) -> str:
    """Вес файла словами. Нулевой вес не печатаем: это «неизвестно», а не «0 Б»."""
    if not bytes_count:
        return ""
    value = float(bytes_count)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _chip(what: str, detail: str = "") -> str:
    """[что] или [что подробность] — без болтающихся пробелов."""
    detail = (detail or "").strip()
    return f"[{what} {detail}]" if detail else f"[{what}]"


def media_label(msg: Any) -> str:
    """Что во вложении — одной строкой.

    Без этого сообщение без подписи выглядит в ленте пустой строкой: стикер,
    голосовое, фотография без текста — всё одинаково никак. Читать такую ленту
    невозможно, поэтому вложение всегда описывается словами, даже когда его
    нельзя показать.
    """
    from telethon.tl import types

    media = getattr(msg, "media", None)
    if media is None:
        return ""

    if isinstance(media, types.MessageMediaPhoto):
        return "[фото]"
    if isinstance(media, types.MessageMediaGeo):
        return "[геометка]"
    if isinstance(media, types.MessageMediaGeoLive):
        return "[живая геометка]"
    if isinstance(media, types.MessageMediaContact):
        return f"[контакт: {media.first_name} {media.last_name}".strip() + "]"
    if isinstance(media, types.MessageMediaPoll):
        question = getattr(getattr(media.poll, "question", None), "text", None) or ""
        return f"[опрос: {question}]" if question else "[опрос]"
    if isinstance(media, types.MessageMediaDice):
        return f"[{media.emoticon} выпало {media.value}]"
    if isinstance(media, types.MessageMediaVenue):
        return f"[место: {media.title}]"
    if isinstance(media, types.MessageMediaGame):
        return f"[игра: {getattr(media.game, 'title', '')}]"
    if isinstance(media, types.MessageMediaInvoice):
        return f"[счёт: {getattr(media, 'title', '')}]"
    if isinstance(media, types.MessageMediaStory):
        return "[история]"
    if isinstance(media, types.MessageMediaWebPage):
        return ""  # ссылка и так видна в тексте
    if isinstance(media, types.MessageMediaUnsupported):
        return "[вложение, которого этот клиент не знает]"

    document = getattr(media, "document", None)
    if document is None:
        return "[вложение]"

    attributes = {type(a).__name__.replace("DocumentAttribute", ""): a
                  for a in getattr(document, "attributes", None) or []}
    if "Sticker" in attributes or "CustomEmoji" in attributes:
        holder = attributes.get("Sticker") or attributes["CustomEmoji"]
        return _chip("стикер", getattr(holder, "alt", "") or "")
    if "Audio" in attributes:
        audio = attributes["Audio"]
        span = _duration(getattr(audio, "duration", 0))
        if getattr(audio, "voice", False):
            return _chip("голосовое", span)
        title = " — ".join(x for x in (getattr(audio, "performer", None),
                                       getattr(audio, "title", None)) if x)
        return _chip("музыка", f"{title} {span}".strip() if title else span)
    if "Video" in attributes:
        video = attributes["Video"]
        span = _duration(getattr(video, "duration", 0))
        if getattr(video, "round_message", False):
            return _chip("кружок", span)
        return _chip("видео", span)
    if "Animated" in attributes:
        return "[гифка]"

    name = getattr(attributes.get("Filename"), "file_name", None)
    weight = _size(getattr(document, "size", 0))
    return _chip("файл", f"{name} {weight}".strip() if name else weight)


def rich_body(msg: Any) -> str:
    """Богатое сообщение в читаемый текст.

    Оно приходит не вложением, а отдельным полем самого сообщения, поэтому
    обычный разбор его не замечал: tgx умел такие сообщения отправлять, но не
    умел прочесть собственные. Отрисовщик уже был — не хватало вызова здесь.
    """
    rich = getattr(msg, "rich_message", None)
    if rich is None:
        return ""
    try:
        return tgx_rich.render_message(rich).plain.strip()
    except Exception:
        return "[богатое сообщение]"


def msg_to_obj(msg: Any) -> dict[str, Any]:
    sender = getattr(msg, "sender", None)
    dt = msg.date
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {
        "id": msg.id,
        "date": dt.isoformat() if dt else None,
        "sender_id": getattr(msg, "sender_id", None),
        "sender": entity_title(sender) if sender else None,
        "text": msg.message or rich_body(msg) or media_label(msg),
        "media": media_label(msg) or None,
        "rich": bool(getattr(msg, "rich_message", None)) or None,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "reply_to": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
    }


def print_jsonl(items: Iterable[dict[str, Any]]) -> None:
    render.print_jsonl(items)


def print_table(rows: list[dict[str, Any]], fields: list[str], title: str | None = None) -> None:
    render.print_table(rows, fields, title)


async def make_client() -> TelegramClient:
    api_id, api_hash = get_credentials()
    DATA.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(SESSION.with_suffix("")), api_id, api_hash)
    await client.connect()
    return client


async def ensure_login(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        return
    phone = input("phone (+155****4567): ").strip()
    await client.send_code_request(phone)
    code = input("login code: ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = getpass.getpass("2FA password: ")
        await client.sign_in(password=password)


async def cmd_auth(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        me = await client.get_me()
        render.emit({"ok": True, "id": me.id, "username": me.username, "name": entity_title(me)})
    finally:
        await client.disconnect()


async def cmd_me(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        me = await client.get_me()
        render.emit({"id": me.id, "username": me.username, "phone": me.phone, "name": entity_title(me)})
    finally:
        await client.disconnect()


async def cmd_dialogs(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        rows = []
        async for dialog in client.iter_dialogs(limit=args.limit or None):
            ent = dialog.entity
            rows.append({
                "id": getattr(ent, "id", None),
                "kind": entity_kind(ent),
                "name": dialog.name,
                "username": getattr(ent, "username", None) or "",
                "unread": dialog.unread_count,
            })
        if args.jsonl:
            print_jsonl(rows)
        else:
            print_table(rows, ["id", "kind", "name", "username", "unread"], title="чаты")
    finally:
        await client.disconnect()


class PeerError(RuntimeError):
    """Чат не найден — с тем, что искали, и на что это похоже."""


async def resolve_peer(client: TelegramClient, peer: str) -> Any:
    """@имя, телефон, числовой id или название чата.

    Чужую ошибку не прячем. Обрыв связи, FloodWait и отозванная сессия выглядят
    как «чат не найден», только если их проглотить, — и тогда сообщение уводит
    от настоящей причины. «Не найдено» Telethon сообщает через ValueError, всё
    остальное отдаём как есть.
    """
    query = (peer or "").strip()
    if not query:
        raise PeerError("не указан чат")

    # Числовой id надо передать числом: строку Telethon ищет как имя и не находит.
    attempts: list[Any] = [int(query)] if query.lstrip("-").isdigit() else []
    attempts.append(query)

    # Свои чаты важнее чужих совпадений. Название канала может оказаться и чужим
    # адресом: «PLOMBIR» — мой канал и посторонний @Plombir одновременно, и
    # глобальный поиск отдавал постороннего. Написать не туда так проще простого,
    # поэтому точное совпадение с названием своего диалога решает спор. Порядок
    # обратный для @имени и адреса в нижнем регистре: их пишут, когда имеют в
    # виду именно адрес.
    looks_like_handle = query.startswith("@") or (
        query.replace("_", "").isalnum() and query.islower())
    if not looks_like_handle and not query.lstrip("-").isdigit():
        async for dialog in client.iter_dialogs(limit=None):
            if (dialog.name or "").strip().lower() == query.lower():
                return dialog.entity

    missing: Exception | None = None
    for value in attempts:
        try:
            return await client.get_entity(value)
        except (ValueError, TypeError) as exc:
            missing = missing or exc

    needle = query.lower()
    titles: list[str] = []
    async for dialog in client.iter_dialogs(limit=None):
        name = dialog.name or ""
        if name and needle in name.lower():
            return dialog.entity
        if name:
            titles.append(name)

    import difflib

    close = difflib.get_close_matches(query, titles, n=3, cutoff=0.45)
    hint = f"; похоже на: {', '.join(close)}" if close else ""
    raise PeerError(f"чат «{peer}» не найден среди {len(titles)} диалогов{hint}. "
                    f"Telegram ответил: {missing}")


def filter_title_text(title: Any) -> str:
    return getattr(title, "text", None) or str(title or "")


def peer_key(peer: Any) -> tuple[str, Any]:
    return (type(peer).__name__, getattr(peer, "user_id", None) or getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None))


async def get_dialog_filters(client: TelegramClient) -> list[Any]:
    result = await client(functions.messages.GetDialogFiltersRequest())
    return list(getattr(result, "filters", result) or [])


async def cmd_folders(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        rows = []
        for f in await get_dialog_filters(client):
            if not hasattr(f, "id"):
                continue
            rows.append({
                "id": getattr(f, "id", None),
                "title": filter_title_text(getattr(f, "title", "")),
                "include_count": len(getattr(f, "include_peers", []) or []),
                "pinned_count": len(getattr(f, "pinned_peers", []) or []),
                "exclude_count": len(getattr(f, "exclude_peers", []) or []),
                "contacts": bool(getattr(f, "contacts", False)),
                "non_contacts": bool(getattr(f, "non_contacts", False)),
                "groups": bool(getattr(f, "groups", False)),
                "broadcasts": bool(getattr(f, "broadcasts", False)),
                "bots": bool(getattr(f, "bots", False)),
            })
        if args.jsonl:
            print_jsonl(rows)
        else:
            print_table(rows, ["id", "title", "include_count", "pinned_count", "exclude_count"], title="папки")
    finally:
        await client.disconnect()


async def cmd_folder_upsert(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        include_peers = []
        selected_rows = []
        seen = set()

        async def add_dialog(dialog: Any) -> None:
            key = peer_key(dialog.input_entity)
            if key in seen:
                return
            seen.add(key)
            include_peers.append(dialog.input_entity)
            ent = dialog.entity
            selected_rows.append({
                "id": getattr(ent, "id", None),
                "kind": entity_kind(ent),
                "name": dialog.name,
                "username": getattr(ent, "username", None) or "",
            })

        for peer in args.peer or []:
            entity = await resolve_peer(client, peer)
            dialog = await client.get_dialogs(limit=None)
            found = False
            for d in dialog:
                if getattr(d.entity, "id", None) == getattr(entity, "id", None):
                    await add_dialog(d)
                    found = True
                    break
            if not found:
                inp = await client.get_input_entity(entity)
                key = peer_key(inp)
                if key not in seen:
                    seen.add(key)
                    include_peers.append(inp)
                    selected_rows.append({"id": getattr(entity, "id", None), "kind": entity_kind(entity), "name": entity_title(entity), "username": getattr(entity, "username", None) or ""})

        if args.match_regex:
            import re
            pattern = re.compile(args.match_regex, re.I)
            exclusions = {p.lower() for p in (args.exclude or [])}
            async for dialog in client.iter_dialogs(limit=None):
                ent = dialog.entity
                text = f"{dialog.name or ''} {getattr(ent, 'username', '') or ''}"
                ent_id = str(getattr(ent, "id", ""))
                username = (getattr(ent, "username", None) or "").lower()
                name = (dialog.name or "").lower()
                if ent_id in exclusions or username in exclusions or name in exclusions:
                    continue
                if pattern.search(text):
                    await add_dialog(dialog)

        filters = await get_dialog_filters(client)
        existing = None
        used_ids = set()
        for f in filters:
            fid = getattr(f, "id", None)
            if isinstance(fid, int):
                used_ids.add(fid)
            if fid and filter_title_text(getattr(f, "title", "")).strip().lower() == args.title.strip().lower():
                existing = f

        folder_id = args.id or (existing.id if existing else next(i for i in range(2, 256) if i not in used_ids))
        filt = types.DialogFilter(
            id=folder_id,
            title=types.TextWithEntities(text=args.title, entities=[]),
            pinned_peers=list(getattr(existing, "pinned_peers", []) or []) if existing else [],
            include_peers=include_peers,
            exclude_peers=list(getattr(existing, "exclude_peers", []) or []) if existing else [],
            contacts=False,
            non_contacts=False,
            groups=False,
            broadcasts=False,
            bots=False,
            exclude_muted=False,
            exclude_read=False,
            exclude_archived=False,
        )
        await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=filt))
        render.emit({
            "ok": True,
            "action": "updated" if existing else "created",
            "folder_id": folder_id,
            "title": args.title,
            "count": len(selected_rows),
            "chats": selected_rows,
        })
    finally:
        await client.disconnect()


def bool_from_arg(value: str | None) -> bool | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if v in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    raise SystemExit(f"Expected boolean, got {value!r}")


def tl_to_plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, (list, tuple)):
        return [tl_to_plain(x) for x in obj]
    if hasattr(obj, "to_dict"):
        data = obj.to_dict()
        return {k: tl_to_plain(v) for k, v in data.items() if not k.startswith("_")}
    if hasattr(obj, "__dict__"):
        return {k: tl_to_plain(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def user_row(user: Any) -> dict[str, Any]:
    return {
        "id": getattr(user, "id", None),
        "username": getattr(user, "username", None) or "",
        "name": entity_title(user),
        "bot": bool(getattr(user, "bot", False)),
        "scam": bool(getattr(user, "scam", False)),
        "verified": bool(getattr(user, "verified", False)),
    }


def channel_row(channel: Any, full: Any = None) -> dict[str, Any]:
    row = {
        "id": getattr(channel, "id", None),
        "kind": entity_kind(channel),
        "title": entity_title(channel),
        "username": getattr(channel, "username", None) or "",
        "broadcast": bool(getattr(channel, "broadcast", False)),
        "megagroup": bool(getattr(channel, "megagroup", False)),
        "verified": bool(getattr(channel, "verified", False)),
        "scam": bool(getattr(channel, "scam", False)),
    }
    if full is not None:
        row.update({
            "about": getattr(full, "about", None) or "",
            "participants_count": getattr(full, "participants_count", None),
            "admins_count": getattr(full, "admins_count", None),
            "kicked_count": getattr(full, "kicked_count", None),
            "banned_count": getattr(full, "banned_count", None),
            "linked_chat_id": getattr(full, "linked_chat_id", None),
            "can_view_participants": bool(getattr(full, "can_view_participants", False)),
            "can_set_username": bool(getattr(full, "can_set_username", False)),
            "can_set_stickers": bool(getattr(full, "can_set_stickers", False)),
            "hidden_prehistory": bool(getattr(full, "hidden_prehistory", False)),
            "signatures": bool(getattr(full, "signatures", False)),
        })
    return row


def parse_rights_csv(csv_value: str, right_names: list[str], all_value: bool = True) -> dict[str, bool]:
    values = {x.strip().replace("-", "_") for x in (csv_value or "").split(",") if x.strip()}
    if not values or "none" in values:
        return {}
    if "all" in values:
        return {name: all_value for name in right_names}
    unknown = values - set(right_names)
    if unknown:
        raise SystemExit(f"Unknown rights: {', '.join(sorted(unknown))}; valid: all, none, {', '.join(right_names)}")
    return {name: all_value for name in values}


ADMIN_RIGHT_NAMES = [
    "change_info", "post_messages", "edit_messages", "delete_messages", "ban_users",
    "invite_users", "pin_messages", "add_admins", "anonymous", "manage_call", "other",
    "manage_topics", "post_stories", "edit_stories", "delete_stories", "manage_direct_messages", "manage_ranks",
]
BANNED_RIGHT_NAMES = [
    "view_messages", "send_messages", "send_media", "send_stickers", "send_gifs", "send_games",
    "send_inline", "embed_links", "send_polls", "change_info", "invite_users", "pin_messages",
    "manage_topics", "send_photos", "send_videos", "send_roundvideos", "send_audios",
    "send_voices", "send_docs", "send_plain", "edit_rank",
]


def participant_filter(name: str, q: str = "") -> Any:
    name = (name or "recent").lower().replace("-", "_")
    if name == "recent":
        return types.ChannelParticipantsRecent()
    if name == "admins":
        return types.ChannelParticipantsAdmins()
    if name == "bots":
        return types.ChannelParticipantsBots()
    if name == "banned":
        return types.ChannelParticipantsBanned(q=q)
    if name in {"kicked", "removed"}:
        return types.ChannelParticipantsKicked(q=q)
    if name == "search":
        return types.ChannelParticipantsSearch(q=q)
    raise SystemExit("participant filter must be recent/admins/bots/banned/kicked/search")


async def input_channel(client: TelegramClient, peer: str) -> Any:
    entity = await resolve_peer(client, peer)
    return await client.get_input_entity(entity)


async def input_user(client: TelegramClient, peer: str) -> Any:
    entity = await resolve_peer(client, peer)
    return await client.get_input_entity(entity)


async def uploaded_chat_photo(client: TelegramClient, file_path: str) -> Any:
    uploaded = await client.upload_file(str(Path(file_path).expanduser()))
    return types.InputChatUploadedPhoto(file=uploaded)


async def ask_human(client: Any, bot_name: str, who: str, title: str,
                    details: str = "", danger: str = "", timeout: float = 300.0) -> dict[str, Any]:
    """Спросить человека кнопкой в Telegram и дождаться ответа.

    Общая дверь для всего опасного: команда описывает, что собирается сделать,
    и не делает этого, пока не придёт разрешение от нужного человека.
    """
    bot = tgx_bots.Registry().get(bot_name)
    if not bot.token:
        raise tgx_confirm.ConfirmError(f"у @{bot.username} нет токена — "
                                       f"`tgx bot token @{bot.username}`")
    target = await resolve_peer(client, who)
    approver = getattr(target, "id", None)
    approval = tgx_confirm.Approval(bot.token)
    return await asyncio.to_thread(
        lambda: approval.ask(approver, title, details, danger=danger,
                             approver_id=approver, timeout=timeout))


async def cmd_confirm(args: argparse.Namespace) -> None:
    """Спросить человека кнопкой и вернуть его решение."""
    client = await make_client()
    try:
        await ensure_login(client)
        result = await ask_human(client, args.bot, args.to, args.title,
                                 args.details or "", args.danger or "", args.timeout)
        render.emit(result)
        if result["decision"] != "approved":
            raise SystemExit(2)          # чтобы сценарий мог остановиться сам
    finally:
        await client.disconnect()


async def gated_or_die(client: Any, args: argparse.Namespace, title: str,
                       details: str, danger: str) -> dict[str, Any]:
    """Ворота для необратимого: без согласия человека команда не продолжается.

    Отсутствие `--confirm-to` — это отказ, а не предупреждение: иначе опасное
    действие однажды выполнится потому, что флаг забыли.
    """
    if not getattr(args, "confirm_to", None):
        raise tgx_pay.PayError(
            "это действие тратит деньги или необратимо — нужно подтверждение: "
            "добавьте --confirm-to КОГО --as @бот")
    verdict = await ask_human(client, args.bot, args.confirm_to, title, details,
                              danger=danger, timeout=args.timeout)
    if verdict["decision"] != "approved":
        render.emit({"ok": False, "действие": "отменено", **verdict})
        raise SystemExit(2)
    return verdict


def read_secret(prompt: str, env: str = "") -> str:
    """Секрет из скрытой строки — не из аргумента и не из истории оболочки.

    Для автоматизации остаётся переменная окружения: она хотя бы не оседает в
    истории команд. Без терминала и без переменной команда честно отказывается,
    а не зависает в ожидании ввода, которого никто не сделает.
    """
    import getpass

    if env and os.environ.get(env):
        return os.environ[env]
    if not sys.stdin.isatty():
        raise tgx_pay.PayError(
            f"нужен ввод секрета, а терминала нет. Запустите команду в терминале "
            f"или передайте значение через переменную {env or 'окружения'}")
    return getpass.getpass(prompt)


async def cmd_call(args: argparse.Namespace) -> None:
    """Групповые звонки: управление из терминала, звук — в приложении."""
    client = await make_client()
    board = None
    try:
        await ensure_login(client)
        calls = tgx_calls.Calls(client)
        chat = await resolve_peer(client, args.chat)
        cmd = args.callcmd

        if cmd == "participants":
            rows = await calls.participants(chat, args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["кто", "заглушён", "рука поднята", "видео", "громкость"],
                title="в звонке")
            return

        if cmd == "join-as":
            rows = await calls.join_as(chat)
            print_jsonl(rows) if args.jsonl else print_table(rows, ["имя", "id"],
                                                            title="войти можно как")
            return

        if cmd == "watch":
            # Живая картина участников: терминалу такое перерисовывать нечем.
            # Ссылка есть не у всякого звонка — у приватного чата её не бывает,
            # и это не повод не показывать участников.
            try:
                link = (await calls.link(chat)).get("ссылка") or ""
            except tgx_calls.CallError:
                link = "звонок приватный — зовите по одному"
            board = tgx_callweb.Dashboard(
                title=f"Звонок · {entity_title(chat)}", link=link,
                source=lambda: getattr(cmd_call, "_people", []))
            url = board.start()
            render.emit({"страница": url, "звук": link or "ссылку выдаёт админ",
                         "остановить": "Ctrl+C"})
            render.flush()
            if args.open:
                import webbrowser

                webbrowser.open(url)
            while True:
                cmd_call._people = await calls.participants(chat, 100)
                await asyncio.sleep(args.every)

        simple = {
            "info": lambda: calls.info(chat),
            "start": lambda: calls.start(chat, title=args.title or "", rtmp=args.rtmp),
            "link": lambda: calls.link(chat, speaker=args.speaker),
            "invite": lambda: calls.invite(chat, args.user),
            "mute": lambda: calls.mute(chat, args.user, not args.unmute, volume=args.volume),
            "hand": lambda: calls.raise_hand(chat, not args.down),
            "title": lambda: calls.title(chat, args.text),
            "record": lambda: calls.record(chat, start=not args.stop, title=args.title or "",
                                           video=args.video, portrait=args.portrait),
            "settings": lambda: calls.settings(
                chat, join_muted=bool_from_arg(args.join_muted) if args.join_muted else None,
                messages=bool_from_arg(args.messages) if args.messages else None,
                reset_link=args.reset_link),
            "say": lambda: calls.say(chat, args.text),
            "stream-url": lambda: calls.stream_url(chat, revoke=args.revoke),
            "start-scheduled": lambda: calls.start_scheduled(chat),
            "stars": lambda: calls.stars(chat),
        }
        if cmd in simple:
            render.emit({"ok": True, **await simple[cmd]()})
            return

        if cmd == "end":
            v = await gated_or_die(client, args, "Завершить звонок?",
                                   f"Чат: {entity_title(chat)}",
                                   "звонок закончится у всех участников")
            render.emit({"ok": True, **await calls.end(chat), "подтвердил": v["by"]})
            return
    except KeyboardInterrupt:
        render.emit({"наблюдение": "остановлено"})
    finally:
        if board is not None:
            board.stop()
        await client.disconnect()


async def cmd_security(args: argparse.Namespace) -> None:
    """Сессии, приватность и сроки."""
    client = await make_client()
    try:
        await ensure_login(client)
        sec = tgx_security.Security(client)
        cmd = args.seccmd

        tables = {
            "sessions": (lambda: sec.sessions(),
                         ["устройство", "программа", "откуда", "активна", "текущая"]),
            "websites": (lambda: sec.websites(), ["сайт", "через бота", "браузер", "откуда"]),
            "privacy": (lambda: sec.privacy(getattr(args, "topic", None)),
                        ["предмет", "видно"]),
            "notify-exceptions": (lambda: sec.notify_exceptions(args.limit),
                                  ["чат", "заглушён до", "звук"]),
        }
        if cmd in tables:
            getter, fields = tables[cmd]
            rows = await getter()
            print_jsonl(rows) if getattr(args, "jsonl", False) else print_table(
                rows, fields, title=cmd)
            return

        if cmd == "global-privacy":
            if args.archive is None and args.hide_read is None and args.premium_only is None:
                render.emit(await sec.global_privacy())
            else:
                render.emit({"ok": True, **await sec.set_global_privacy(
                    archive_new=bool_from_arg(args.archive) if args.archive else None,
                    hide_read=bool_from_arg(args.hide_read) if args.hide_read else None,
                    premium_only=bool_from_arg(args.premium_only) if args.premium_only else None)})
            return

        if cmd == "set-privacy":
            render.emit({"ok": True, **await sec.set_privacy(
                args.topic, args.audience, allow=args.allow or [], deny=args.deny or [])})
            return

        if cmd == "session-ttl":
            render.emit(await sec.session_ttl(args.days))
            return

        if cmd == "account-ttl":
            render.emit(await sec.account_ttl(args.days))
            return

        if cmd == "session-settings":
            render.emit({"ok": True, **await sec.session_settings(
                args.hash, calls=bool_from_arg(args.calls) if args.calls else None,
                secret_chats=bool_from_arg(args.secret) if args.secret else None)})
            return

        if cmd in {"close-session", "close-website", "close-all-websites"}:
            titles = {"close-session": ("Завершить сессию?", f"Сессия {getattr(args, 'hash', '')}"),
                      "close-website": ("Отозвать доступ у сайта?", f"Сайт {getattr(args, 'hash', '')}"),
                      "close-all-websites": ("Отозвать доступ у всех сайтов?", "все сайты")}
            title, details = titles[cmd]
            v = await gated_or_die(client, args, title, details,
                                   "устройство или сайт потеряет доступ немедленно")
            result = await (sec.close_session(args.hash) if cmd == "close-session"
                            else sec.close_website(getattr(args, "hash", None)
                                                   if cmd == "close-website" else None))
            render.emit({"ok": True, **result, "подтвердил": v["by"]})
            return
    finally:
        await client.disconnect()


async def cmd_pending(args: argparse.Namespace) -> None:
    """Черновики, отложенные, быстрые ответы и закладки."""
    client = await make_client()
    try:
        await ensure_login(client)
        pend = tgx_pending.Pending(client)
        cmd = args.pendcmd

        tables = {
            "drafts": (lambda: pend.drafts(), ["чат", "текст", "изменён"]),
            "shortcuts": (lambda: pend.shortcuts(), ["id", "ярлык", "сообщений"]),
            "saved": (lambda: pend.saved(args.limit), ["от кого", "закреплено"]),
            "tags": (lambda: pend.tags(), ["метка", "название", "сообщений"]),
        }
        if cmd in tables:
            getter, fields = tables[cmd]
            rows = await getter()
            print_jsonl(rows) if getattr(args, "jsonl", False) else print_table(
                rows, fields, title=cmd)
            return

        if cmd == "scheduled":
            rows = await pend.scheduled(await resolve_peer(client, args.chat))
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["id", "уйдёт", "текст", "вложение"], title="отложенные")
            return

        if cmd == "shortcut":
            rows = await pend.shortcut_messages(args.id)
            print_jsonl(rows) if args.jsonl else print_table(rows, ["id", "текст"],
                                                            title=f"заготовка {args.id}")
            return

        peer = await resolve_peer(client, args.chat) if getattr(args, "chat", None) else None
        singles = {
            "draft": lambda: pend.save_draft(peer, args.text or "", reply_to=args.reply_to,
                                             no_preview=args.no_preview),
            "send-now": lambda: pend.send_now(peer, args.id),
            "cancel": lambda: pend.cancel(peer, args.id),
            "send-shortcut": lambda: pend.send_shortcut(peer, args.id),
            "rename-shortcut": lambda: pend.rename_shortcut(args.id, args.name),
            "name-tag": lambda: pend.name_tag(args.emoji, args.title or ""),
            "fact-check": lambda: pend.fact_check(peer, args.id),
        }
        if cmd in singles:
            render.emit({"ok": True, **await singles[cmd]()})
            return

        if cmd in {"clear-drafts", "delete-shortcut"}:
            v = await gated_or_die(
                client, args,
                "Стереть все черновики?" if cmd == "clear-drafts" else "Удалить заготовку?",
                "во всех чатах" if cmd == "clear-drafts" else f"заготовка {args.id}",
                "восстановить нельзя")
            result = await (pend.clear_drafts() if cmd == "clear-drafts"
                            else pend.delete_shortcut(args.id))
            render.emit({"ok": True, **result, "подтвердил": v["by"]})
            return
    finally:
        await client.disconnect()


async def cmd_stats(args: argparse.Namespace) -> None:
    """Статистика каналов, групп, постов и историй."""
    client = await make_client()
    try:
        await ensure_login(client)
        stats = tgx_stats.Stats(client)
        peer = await resolve_peer(client, args.chat)
        cmd = args.statscmd

        if cmd == "forwards":
            rows = await stats.forwards(peer, args.id, args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["куда", "id", "просмотров"], title="публичные пересылки")
            return

        if cmd == "graph":
            render.emit(await stats.graph(peer, args.name, kind=args.kind, msg_id=args.id or 0))
            return

        data = await {
            "channel": lambda: stats.channel(peer),
            "group": lambda: stats.group(peer),
            "message": lambda: stats.message(peer, args.id),
            "story": lambda: stats.story(peer, args.id),
        }[cmd]()
        render.emit(data, title=cmd)
    finally:
        await client.disconnect()


async def cmd_stickers(args: argparse.Namespace) -> None:
    """Свои наборы стикеров. Правку делает бот, владельцем остаётесь вы."""
    client = await make_client()
    session = None
    try:
        await ensure_login(client)
        if getattr(args, "bot", None):
            bot = tgx_bots.Registry().get(args.bot)
            if not bot.token:
                raise tgx_stickers.StickerError(f"у @{bot.username} нет токена — "
                                                f"`tgx bot token @{bot.username}`")
            session = await tgx_bots.BotSession(bot, *get_credentials()).__aenter__()
        packs = tgx_stickers.Stickers(client, session.client if session else None)
        box = tgx_stickers.Box(client)
        cmd = args.stickcmd

        # пользоваться стикерами — своей сессией, без бота
        if cmd == "mine":
            render.emit({"мои наборы": await box.mine(args.limit)})
            return
        if cmd == "installed":
            render.emit({"установлены": await box.installed()})
            return
        if cmd == "find-sets":
            render.emit({"наборы": await box.find_sets(args.query, featured=not args.installed_only)})
            return
        if cmd == "find":
            render.emit({"стикеры": await box.find(
                emoji=args.emoji or "", query=args.query or "", limit=args.limit,
                custom_emoji=args.custom_emoji)})
            return
        if cmd == "faved":
            render.emit({"избранные": await box.faved()})
            return
        if cmd == "recent":
            render.emit({"недавние": await box.recent()})
            return
        if cmd == "fave":
            render.emit(await box.fave(args.key, remove=args.remove))
            return
        if cmd == "install":
            render.emit(await box.install(args.name, remove=args.remove))
            return
        if cmd == "send":
            peer = await resolve_peer(client, args.peer)
            render.emit(await box.send(peer, args.key, reply_to=args.reply_to or 0))
            return

        actions = {
            "show": lambda: packs.show(args.name),
            "check-name": lambda: packs.check_name(args.name),
            "suggest": lambda: packs.suggest_name(args.title),
            "add": lambda: packs.add(args.name, args.file, args.emoji),
            "remove": lambda: packs.remove(args.name, args.position),
            "move": lambda: packs.move(args.name, args.position, args.to),
            "emoji": lambda: packs.set_emoji(args.name, args.position, args.emoji,
                                             args.keywords or ""),
            "rename": lambda: packs.rename(args.name, args.title),
            "thumb": lambda: packs.set_thumb(args.name, args.position),
        }
        if cmd in actions:
            render.emit(await actions[cmd]())
            return

        if cmd == "create":
            pairs = []
            for item in args.sticker:
                path, _, emoji = item.partition("=")
                pairs.append((path, emoji or "🙂"))
            render.emit({"ok": True, **await packs.create(
                args.owner, args.title, args.short_name, pairs,
                masks=args.masks, emojis=args.emojis)})
            return

        if cmd == "delete":
            v = await gated_or_die(client, args, "Удалить набор стикеров?",
                                   args.name, "набор исчезнет у всех, кто его добавил")
            render.emit({"ok": True, **await packs.delete(args.name), "подтвердил": v["by"]})
            return
    finally:
        if session is not None:
            await session.__aexit__()
        await client.disconnect()


async def cmd_share_folder(args: argparse.Namespace) -> None:
    """Общие папки: ссылка на набор чатов и её обновления."""
    client = await make_client()
    try:
        await ensure_login(client)
        shared = tgx_folders.Folders(client)
        cmd = args.sharecmd

        if cmd == "invites":
            rows = await shared.invites(args.id)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["название", "чатов", "ссылка"], title=f"ссылки на папку {args.id}")
            return

        actions = {
            "share": lambda: shared.share(args.id, args.title, args.chat or []),
            "edit": lambda: shared.edit_invite(args.id, args.slug, title=args.title or "",
                                               peers=args.chat or []),
            "revoke": lambda: shared.revoke(args.id, args.slug),
            "check": lambda: shared.check(args.slug),
            "join": lambda: shared.join(args.slug, args.chat or []),
            "updates": lambda: shared.updates(args.id),
            "accept": lambda: shared.accept_updates(args.id, args.chat or []),
            "hide-updates": lambda: shared.hide_updates(args.id),
            "leave-suggestions": lambda: shared.leave_suggestions(args.id),
        }
        if cmd in actions:
            render.emit(await actions[cmd]())
            return

        if cmd == "leave":
            v = await gated_or_die(client, args, "Покинуть общую папку?",
                                   f"Папка {args.id}, чатов: {len(args.chat or [])}",
                                   "выход из чатов необратим без нового приглашения")
            render.emit({"ok": True, **await shared.leave(args.id, args.chat or []),
                         "подтвердил": v["by"]})
            return
    finally:
        await client.disconnect()


async def cmd_stories(args: argparse.Namespace) -> None:
    """Истории: лента, публикация, просмотры, альбомы."""
    client = await make_client()
    try:
        await ensure_login(client)
        st = tgx_stories.Stories(client)
        cmd = args.storycmd
        columns = ["id", "подпись", "просмотров", "реакций", "истекает"]

        if cmd == "feed":
            rows = await st.feed(hidden=args.hidden)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["чей", *columns], title="лента историй")
            return

        if cmd in {"of", "pinned", "archive", "search"}:
            rows = await {
                "of": lambda: st.of(args.chat),
                "pinned": lambda: st.pinned(args.chat, args.limit),
                "archive": lambda: st.archive(args.limit),
                "search": lambda: st.search(args.hashtag, args.limit),
            }[cmd]()
            print_jsonl(rows) if args.jsonl else print_table(rows, columns, title=cmd)
            return

        if cmd == "viewers":
            rows = await st.viewers(args.id, limit=args.limit, contacts_only=args.contacts)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["кто", "реакция", "когда"], title=f"кто смотрел {args.id}")
            return

        if cmd == "albums":
            rows = await st.albums(args.chat)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["id", "название", "историй"], title="альбомы историй")
            return

        singles = {
            "publish": lambda: st.publish(
                args.file, caption=args.caption or "", audience=args.audience,
                hours=args.hours, pinned=args.pin, no_forwards=args.no_forwards,
                allow=args.allow or [], deny=args.deny or []),
            "pin": lambda: st.pin(args.id, not args.off),
            "react": lambda: st.react(args.chat, args.id, None if args.clear else args.emoji),
            "read": lambda: st.mark_read(args.chat, args.id),
            "stealth": lambda: st.stealth(past=not args.future_only, future=not args.past_only),
            "link": lambda: st.link(args.chat, args.id),
            "hide": lambda: st.hide_peer(args.chat, not args.show),
            "can-post": lambda: st.can_post(args.chat),
            "new-album": lambda: st.create_album(args.title, args.id, args.chat),
        }
        if cmd in singles:
            render.emit({"ok": True, **await singles[cmd]()})
            return

        if cmd in {"delete", "delete-album"}:
            v = await gated_or_die(
                client, args,
                "Удалить истории?" if cmd == "delete" else "Удалить альбом историй?",
                f"{'Истории' if cmd == 'delete' else 'Альбом'}: "
                f"{args.id if isinstance(args.id, int) else ', '.join(map(str, args.id))}",
                "восстановить нельзя")
            result = await (st.delete(args.id) if cmd == "delete"
                            else st.delete_album(args.id))
            render.emit({"ok": True, **result, "подтвердил": v["by"]})
            return
    finally:
        await client.disconnect()


async def cmd_contacts(args: argparse.Namespace) -> None:
    """Адресная книга, чёрный список и поиск людей."""
    client = await make_client()
    try:
        await ensure_login(client)
        book = tgx_contacts.Contacts(client)
        cmd = args.contactcmd

        listings = {
            "list": (lambda: book.all(), ["id", "имя", "username", "взаимный", "был"]),
            "blocked": (lambda: book.blocked(stories=args.stories, limit=args.limit),
                        ["id", "имя", "username"]),
            "search": (lambda: book.search(args.query, args.limit),
                       ["id", "имя", "username", "был"]),
            "birthdays": (lambda: book.birthdays(), ["кто", "когда"]),
            "top": (lambda: book.top_peers(args.limit), ["кто", "вес"]),
        }
        if cmd in listings:
            getter, fields = listings[cmd]
            rows = await getter()
            print_jsonl(rows) if getattr(args, "jsonl", False) else print_table(
                rows, fields, title=cmd)
            return

        if cmd == "add":
            render.emit({"ok": True, **await book.add(
                args.user, first=args.first or "", last=args.last or "",
                phone=args.phone or "", note=args.note or "", share_phone=args.share_phone)})
            return

        if cmd == "remove":
            render.emit({"ok": True, **await book.remove(args.user)})
            return

        if cmd == "note":
            render.emit({"ok": True, **await book.note(args.user, args.text or "")})
            return

        if cmd == "close-friends":
            render.emit({"ok": True, **await book.close_friends(args.user)})
            return

        if cmd in {"block", "unblock"}:
            action = book.block if cmd == "block" else book.unblock
            render.emit({"ok": True, **await action(args.user, stories_only=args.stories)})
            return

        if cmd == "top-toggle":
            render.emit({"ok": True, **await book.toggle_top_peers(not args.off)})
            return

        if cmd == "by-phone":
            render.emit(await book.by_phone(args.phone))
            return

        if cmd == "import":
            render.emit(await book.import_token(args.token))
            return
    finally:
        await client.disconnect()


async def cmd_pay(args: argparse.Namespace) -> None:
    """Звёзды, TON и обычные счета. Оплаты здесь нет — только чтение и выписка."""
    client = await make_client()
    try:
        await ensure_login(client)
        pay = tgx_pay.Pay(client)

        if args.paycmd == "balance":
            render.emit(await pay.balance(ton=args.ton))
            return

        if args.paycmd == "history":
            rows = await pay.transactions(limit=args.limit, inbound=args.inbound,
                                          outbound=args.outbound, ton=args.ton)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["дата", "сумма", "направление", "за что"],
                title="TON" if args.ton else "операции со звёздами")
            return

        if args.paycmd == "receipt":
            peer = await resolve_peer(client, args.chat)
            render.emit(await pay.receipt(peer, args.id))
            return

        if args.paycmd == "show":
            data = await pay.form(args.link)
            render.emit({k: v for k, v in data.items() if k != "строки"},
                        title=data.get("название") or "счёт")
            if data.get("строки"):
                print_table(data["строки"], ["за что", "сумма"])
            return

        if args.paycmd == "invoice":
            prices = []
            for item in args.price:
                label, _, value = item.rpartition("=")
                if not label:
                    raise tgx_pay.PayError(f"строка счёта пишется «за что=сумма», а не «{item}»")
                try:
                    prices.append((label, float(value)))
                except ValueError:
                    raise tgx_pay.PayError(f"«{value}» — не сумма") from None
            # Счета выписывает бот: от лица пользователя Telegram отвечает
            # USER_BOT_REQUIRED, поэтому --as обязателен.
            if not args.bot:
                raise tgx_pay.PayError("счёт выписывает бот — добавьте --as @бот")
            bot = tgx_bots.Registry().get(args.bot)
            if not bot.token:
                raise tgx_pay.PayError(f"у @{bot.username} нет токена — "
                                       f"`tgx bot token @{bot.username}`")
            url = await asyncio.to_thread(
                lambda: tgx_pay.Pay.bot_invoice_link(
                    bot.token, title=args.title, description=args.description,
                    currency=args.currency, prices=prices, payload=args.payload,
                    provider=args.provider or "", test=args.test,
                    needs_name=args.need_name, needs_phone=args.need_phone,
                    needs_email=args.need_email, needs_address=args.need_address,
                    subscription_period=args.subscription))
            render.emit({"ok": True, "ссылка": url, "валюта": args.currency.upper(),
                         "итого": sum(v for _, v in prices)})
            return

        if args.paycmd == "send":
            form = await pay.form(args.link)
            total, currency = form.get("итого"), form.get("валюта")
            if currency != "XTR":
                raise tgx_pay.PayError(
                    f"оплатить отсюда можно только звёздами, а счёт в {currency}. "
                    f"Карты требуют платёжных данных — их вводят на своём экране")
            details = (f"{form.get('название') or 'счёт'}\n"
                       f"Сумма: {total} ⭐\n"
                       f"Кому: {args.link}")
            verdict = await ask_human(
                client, args.bot, args.confirm_to, "Оплатить счёт?",
                details, danger=f"со счёта спишется {total} звёзд, вернуть их нельзя",
                timeout=args.timeout)
            if verdict["decision"] != "approved":
                render.emit({"ok": False, "оплата": "не выполнена", **verdict})
                raise SystemExit(2)
            render.emit({"ok": True, **await pay.pay_stars(args.link), "подтвердил": verdict["by"]})
            return

        # Читающие ветки — сразу; тратящие проходят через подтверждение.
        reads = {
            "gifts": lambda: pay.gift_catalogue(args.limit),
            "my-gifts": lambda: pay.my_gifts(args.chat, args.limit),
            "subscriptions": lambda: pay.subscriptions(args.chat),
        }
        if args.paycmd in reads:
            rows = await reads[args.paycmd]()
            fields = {"gifts": ["id", "звёзд", "осталось", "за продажу"],
                      "my-gifts": ["id", "что", "звёзд", "можно продать за"],
                      "subscriptions": ["id", "звёзд", "до", "отменена"]}[args.paycmd]
            print_jsonl(rows) if args.jsonl else print_table(rows, fields, title=args.paycmd)
            return

        if args.paycmd == "revenue":
            render.emit(await pay.revenue(await resolve_peer(client, args.chat), ton=args.ton))
            return

        if args.paycmd == "check-code":
            render.emit(await pay.check_code(args.slug))
            return

        if args.paycmd == "giveaway":
            render.emit(await pay.giveaway(await resolve_peer(client, args.chat), args.id))
            return

        # читающее: без ворот
        simple_reads = {
            "topup": (lambda: pay.topup_options(), ["звёзд", "цена", "валюта"]),
            "auctions": (lambda: pay.auctions(), ["подарок", "до"]),
        }
        if args.paycmd in simple_reads:
            getter, fields = simple_reads[args.paycmd]
            rows = await getter()
            print_jsonl(rows) if args.jsonl else print_table(rows, fields, title=args.paycmd)
            return

        if args.paycmd == "gift":
            render.emit(await pay.gift_details(args.id))
            return

        if args.paycmd == "upgrade-preview":
            render.emit(await pay.upgrade_preview(args.gift_id))
            return

        if args.paycmd == "resale":
            rows = await pay.resale(args.gift_id, args.limit, not args.by_number)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["номер", "название", "звёзд", "TON", "ссылка"], title="вторичный рынок")
            return

        if args.paycmd == "unique":
            render.emit(await pay.unique_gift(args.slug))
            return

        if args.paycmd == "collections":
            rows = await pay.collections(args.chat)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["id", "название", "подарков"], title="коллекции подарков")
            return

        if args.paycmd == "referrals":
            rows = await pay.referral_bots(args.chat, args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["ссылка", "доля", "приведено", "заработано"], title="партнёрские программы")
            return

        if args.paycmd == "can-send-gift":
            render.emit(await pay.can_send_gift(args.gift_id))
            return

        if args.paycmd == "show-gift":
            render.emit({"ok": True, **await pay.show_gift(args.id, not args.hide)})
            return

        if args.paycmd == "pin-gift":
            chat = await resolve_peer(client, args.chat) if args.chat else None
            render.emit({"ok": True, **await pay.pin_gift(chat, args.id)})
            return

        if args.paycmd == "new-collection":
            render.emit({"ok": True, **await pay.create_collection(args.title, args.id)})
            return

        # тратящее: через ворота
        if args.paycmd == "upgrade-gift":
            v = await gated_or_die(client, args, "Улучшить подарок?",
                                   f"Подарок {args.id}",
                                   "улучшение стоит звёзд и необратимо")
            render.emit({"ok": True, **await pay.upgrade_gift(args.id, not args.drop_details),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "sell-gift":
            price = None if args.unlist else args.stars
            v = await gated_or_die(
                client, args, "Выставить подарок на продажу?" if price else "Снять с продажи?",
                f"Подарок {args.id}" + (f" за {price} ⭐" if price else ""),
                "покупатель сможет забрать его сразу")
            render.emit({"ok": True, **await pay.set_price(args.id, price),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "refund":
            v = await gated_or_die(client, args, "Вернуть звёзды за покупку?",
                                   f"{args.user}, платёж {args.charge}",
                                   "возврат отменить нельзя")
            render.emit({"ok": True, **await pay.refund(args.user, args.charge),
                         "подтвердил": v["by"]})
            return

        # ── читающее без ворот ───────────────────────────────────────────────
        plain = {
            "auction-state": (lambda: pay.auction_state(args.id), None),
            "auction-won": (lambda: pay.auction_won(args.gift_id), ["название", "номер"]),
            "craftable": (lambda: pay.craftable(args.gift_id, args.limit),
                          ["название", "номер", "цена"]),
            "suggested-referrals": (lambda: pay.suggested_referrals(args.chat, args.limit),
                                    ["бот", "доля", "срок дней"]),
            "premium-options": (lambda: pay.premium_options(args.chat),
                                ["месяцев", "получателей", "цена", "валюта"]),
            "giveaway-options": (lambda: pay.giveaway_options(),
                                 ["звёзд", "победителей", "цена", "валюта"]),
            "unique-value": (lambda: pay.unique_value(args.slug), None),
            "upgrade-attributes": (lambda: pay.upgrade_attributes(args.gift_id), None),
            "ads-account": (lambda: pay.ads_account(args.chat), None),
        }
        if args.paycmd in plain:
            getter, fields = plain[args.paycmd]
            data = await getter()
            if fields and isinstance(data, list):
                print_jsonl(data) if getattr(args, "jsonl", False) else print_table(
                    data, fields, title=args.paycmd)
            else:
                render.emit(data)
            return

        if args.paycmd == "referral":
            render.emit(await pay.referral_bot(args.bot_name, args.chat))
            return

        if args.paycmd == "transaction":
            rows = await pay.transaction(args.id, ton=args.ton)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["дата", "сумма", "направление", "за что"], title="операции")
            return

        if args.paycmd == "gift-notifications":
            render.emit({"ok": True, **await pay.gift_notifications(
                await resolve_peer(client, args.chat), not args.off)})
            return

        if args.paycmd == "edit-collection":
            render.emit({"ok": True, **await pay.edit_collection(
                args.id, title=args.title or "", add=args.add or [], remove=args.remove or [])})
            return

        if args.paycmd == "reorder-collections":
            render.emit({"ok": True, **await pay.reorder_collections(args.id)})
            return

        if args.paycmd == "validate-info":
            render.emit(await pay.validate_info(args.link, name=args.name or "",
                                                phone=args.phone or "", email=args.email or "",
                                                save=args.save))
            return

        # ── тратящее и необратимое ───────────────────────────────────────────
        if args.paycmd == "craft":
            v = await gated_or_die(client, args, "Собрать новый подарок?",
                                   f"Из подарков: {', '.join(map(str, args.id))}",
                                   "исходные подарки исчезнут")
            render.emit({"ok": True, **await pay.craft(args.id), "подтвердил": v["by"]})
            return

        if args.paycmd == "offer":
            v = await gated_or_die(client, args, "Предложить выкуп подарка?",
                                   f"{args.slug} за {args.stars} ⭐",
                                   "предложение увидит владелец")
            render.emit({"ok": True, **await pay.offer_gift(
                await resolve_peer(client, args.chat), args.slug, args.stars, args.days),
                "подтвердил": v["by"]})
            return

        if args.paycmd == "answer-offer":
            v = await gated_or_die(
                client, args, "Принять предложение?" if not args.decline else "Отклонить?",
                f"Предложение {args.id}", "решение окончательно")
            render.emit({"ok": True, **await pay.answer_offer(args.id, not args.decline),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "delete-collection":
            v = await gated_or_die(client, args, "Удалить коллекцию?",
                                   f"Коллекция {args.id}", "восстановить нельзя")
            render.emit({"ok": True, **await pay.delete_collection(args.id),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "connect-referral":
            v = await gated_or_die(client, args, "Подключить партнёрскую программу?",
                                   f"Бот {args.bot_name}", "условия задаёт бот")
            render.emit({"ok": True, **await pay.connect_referral(args.bot_name),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "revoke-referral":
            v = await gated_or_die(client, args, "Отозвать партнёрскую ссылку?",
                                   args.link, "начисления по ней прекратятся")
            render.emit({"ok": True, **await pay.revoke_referral(args.link),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "fulfil-subscription":
            v = await gated_or_die(client, args, "Доплатить за подписку?",
                                   f"Подписка {args.id}", "звёзды спишутся сразу")
            render.emit({"ok": True, **await pay.fulfil_subscription(
                await resolve_peer(client, args.chat), args.id), "подтвердил": v["by"]})
            return

        if args.paycmd == "gift-to-blockchain":
            v = await gated_or_die(client, args, "Вывести подарок в блокчейн?",
                                   f"Подарок {args.id}", "подарок покинет Telegram")
            secret = read_secret("пароль от аккаунта (не отображается): ", "TGX_PASSWORD")
            render.emit({"ok": True, **await pay.gift_to_blockchain(args.id, secret),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "pay-card":
            form = await pay.form(args.link)
            v = await gated_or_die(client, args, "Оплатить счёт сохранённой картой?",
                                   f"{form.get('название') or 'счёт'}\n"
                                   f"Сумма: {form.get('итого')} {form.get('валюта')}",
                                   "списание пройдёт сразу")
            secret = read_secret("пароль от аккаунта (не отображается): ", "TGX_PASSWORD")
            render.emit({"ok": True, **await pay.pay_card(args.link, secret, tip=args.tip or 0),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "card-bank":
            number = read_secret("номер карты (не отображается): ", "TGX_CARD_NUMBER")
            render.emit(await pay.card_bank(number))
            return

        if args.paycmd == "withdraw":
            chat = await resolve_peer(client, args.chat)
            v = await gated_or_die(client, args, "Вывести средства?",
                                   f"{entity_title(chat)}: {args.amount} "
                                   f"{'TON' if args.ton else '⭐'}",
                                   "ссылка на вывод открывает страницу подтверждения")
            secret = read_secret("пароль от аккаунта (не отображается): ", "TGX_PASSWORD")
            render.emit({"ok": True, **await pay.withdrawal_url(
                chat, args.amount, ton=args.ton, secret=secret), "подтвердил": v["by"]})
            return

        if args.paycmd == "saved-info":
            render.emit(await pay.saved_info())
            return

        # ── тратящее и необратимое: только через подтверждение человеком ────
        async def gated(title: str, details: str, danger: str) -> dict[str, Any]:
            return await gated_or_die(client, args, title, details, danger)

        if args.paycmd == "convert-gift":
            v = await gated("Обменять подарок на звёзды?", f"Подарок {args.id}",
                            "подарок исчезнет навсегда")
            render.emit({"ok": True, **await pay.convert_gift(args.id), "подтвердил": v["by"]})
            return

        if args.paycmd == "transfer-gift":
            v = await gated("Передать подарок?", f"Подарок {args.id} → {args.to}",
                            "вернуть его нельзя")
            render.emit({"ok": True, **await pay.transfer_gift(args.id, args.to),
                         "подтвердил": v["by"]})
            return

        if args.paycmd == "react":
            chat = await resolve_peer(client, args.chat)
            v = await gated("Отправить платную реакцию?",
                            f"{args.count} ⭐ автору сообщения {args.id}",
                            "звёзды спишутся сразу")
            render.emit({"ok": True, **await pay.paid_reaction(
                chat, args.id, args.count, args.anonymous), "подтвердил": v["by"]})
            return

        if args.paycmd == "apply-code":
            v = await gated("Применить подарочный код?", args.slug, "код одноразовый")
            render.emit({"ok": True, **await pay.apply_code(args.slug), "подтвердил": v["by"]})
            return

        if args.paycmd == "clear-saved":
            v = await gated("Стереть сохранённые платёжные данные?",
                            "способ оплаты и контакты", "восстановить их нельзя")
            render.emit({"ok": True, **await pay.clear_saved(), "подтвердил": v["by"]})
            return

        if args.paycmd == "cancel-subscription":
            chat = await resolve_peer(client, args.chat)
            v = await gated("Отменить подписку?" if not args.resume else "Возобновить подписку?",
                            f"Подписка {args.id}", "возобновление — отдельным действием")
            render.emit({"ok": True, **await pay.cancel_subscription(
                chat, args.id, not args.resume), "подтвердил": v["by"]})
            return

        if args.paycmd == "gift-options":
            rows = await pay.gift_options(args.user)
            print_jsonl(rows) if args.jsonl else print_table(rows, ["звёзд", "цена", "валюта"],
                                                            title="подарить звёзды")
            return
    finally:
        await client.disconnect()


async def cmd_poll(args: argparse.Namespace) -> None:
    """Опросы и викторины."""
    client = await make_client()
    try:
        await ensure_login(client)
        polls = tgx_poll.Polls(client)
        peer = await resolve_peer(client, args.chat)

        if args.pollcmd == "create":
            if args.quiz is not None:
                if not args.bot:
                    raise tgx_poll.PollError(tgx_poll.QUIZ_NEEDS_BOT)
                bot = tgx_bots.Registry().get(args.bot)
                if not bot.token:
                    raise tgx_poll.PollError(f"у @{bot.username} нет токена — "
                                             f"`tgx bot token @{bot.username}`")
                username = getattr(peer, "username", None)
                chat_id = f"@{username}" if username else (
                    f"-100{peer.id}" if entity_kind(peer) in {"channel", "group"} else str(peer.id))
                def ask() -> Any:
                    return tgx_poll.send_quiz(
                        bot.token, chat_id, args.question, args.option, correct=args.quiz,
                        explanation=args.explanation or "", topic=args.topic,
                        multiple=args.multiple, anonymous=not args.public,
                        revoting=args.allow_revoting, shuffle=args.shuffle,
                        close_in=args.close_in, silent=args.silent)

                render.emit({"ok": True, "as": bot.username, **await asyncio.to_thread(ask)})
                return
            render.emit({"ok": True, **await polls.create(
                peer, args.question, args.option, quiz_answer=args.quiz,
                multiple=args.multiple, public=args.public, shuffle=args.shuffle,
                hide_until_close=args.hide_results, members_only=args.members_only,
                allow_revoting=args.allow_revoting,
                countries=args.countries, close_in=args.close_in,
                explanation=args.explanation or "", topic=args.topic, silent=args.silent)})
            return

        if args.pollcmd == "vote":
            render.emit({"ok": True, **await polls.vote(peer, args.id, args.choice)})
            return

        if args.pollcmd == "results":
            data = await polls.results(peer, args.id)
            if args.jsonl:
                print_jsonl([data])
            else:
                render.emit({k: v for k, v in data.items() if k != "answers"},
                            title=data["question"])
                print_table(data["answers"], ["n", "text", "voters", "share"])
            return

        if args.pollcmd == "close":
            render.emit({"ok": True, **await polls.close(peer, args.id)})
            return

        if args.pollcmd == "voters":
            rows = await polls.voters(peer, args.id, args.option, args.limit)
            print_jsonl(rows) if args.jsonl else print_table(rows, ["user", "option"],
                                                            title="кто как проголосовал")
            return
    finally:
        await client.disconnect()


async def cmd_guard(args: argparse.Namespace) -> None:
    """Именные одноразовые приглашения и проверка, кто по ним вошёл."""
    client = await make_client()
    try:
        await ensure_login(client)
        guard = tgx_guard.Guard(client)

        if args.guardcmd == "journal":
            rows = guard.journal()
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["for_label", "status", "issued", "link"], title="выписанные приглашения")
            return

        peer = await resolve_peer(client, args.chat)

        if args.guardcmd == "invite":
            row = await guard.issue(peer, args.user, hours=args.hours, note=args.note or "")
            render.emit({"ok": True, "for": row["for_label"], "link": row["link"],
                         "expires": row["expires"], "usage_limit": 1})
            return

        if args.guardcmd == "check":
            if args.confirm_to and not args.no_kick:
                verdict = await ask_human(
                    client, args.bot, args.confirm_to, "Удалить чужих из чата?",
                    f"Чат: {entity_title(peer)}\nБудут удалены те, кто вошёл не по своей ссылке",
                    danger="удаление участника необратимо", timeout=args.timeout)
                if verdict["decision"] != "approved":
                    render.emit({"ok": False, "проверка": "отменена", **verdict})
                    raise SystemExit(2)
            report = await guard.check(peer, kick=not args.no_kick)
            trouble = [r for r in report if r["status"] == "нарушена"]
            if args.jsonl:
                print_jsonl(report)
            else:
                print_table(report, ["for_label", "status", "joined", "kicked"],
                            title="сверка приглашений")
                if trouble:
                    render.fail(f"по {len(trouble)} ссылке(ам) вошёл не тот, кого звали — "
                                f"они удалены, ссылки отозваны")
            return

        if args.guardcmd == "revoke":
            render.emit({"ok": True, **await guard.revoke(peer, args.link)})
            return

        if args.guardcmd == "lock":
            render.emit({"ok": True, **await guard.lock(peer)})
            return
    finally:
        await client.disconnect()


async def cmd_rich(args: argparse.Namespace) -> None:
    """Богатое сообщение от своего имени — MTProto это умеет, ограничение только в Bot API."""
    markdown = Path(args.file).expanduser().read_text() if args.file else (args.text or "")
    if not markdown.strip():
        raise tgx_rich.RichError("нечего отправлять: укажите разметку или --file")
    tgx_rich.check_limits(markdown)
    client = await make_client()
    try:
        await ensure_login(client)
        chat = await resolve_peer(client, args.peer)

        # Картинка живёт внутри самого документа: в разметке на неё ссылаются
        # как tg://photo?id=ИМЯ, а сам файл едет в поле files.
        files = []
        for item in args.media or []:
            name, _, path = item.partition("=")
            if not path:
                raise tgx_rich.RichError(f"--media ждёт ИМЯ=путь, а получил «{item}»")
            source = Path(path).expanduser()
            if not source.is_file():
                raise tgx_rich.RichError(f"файла {source} нет")
            uploaded = await client.upload_file(str(source))
            holder = await client(functions.messages.UploadMediaRequest(
                peer=chat, media=types.InputMediaUploadedPhoto(file=uploaded)))
            photo = holder.photo
            files.append(types.InputRichFilePhoto(
                id=name, photo=types.InputPhoto(id=photo.id, access_hash=photo.access_hash,
                                                file_reference=photo.file_reference)))
            reference = f"tg://photo?id={name}"
            if reference not in markdown:
                markdown = f"![]({reference})\n\n" + markdown
        # Тема форума — тред служебного сообщения, которым её создали: чтобы
        # попасть в неё, отвечаем на её корневое сообщение.
        reply = types.InputReplyToMessage(reply_to_msg_id=args.topic) if args.topic else None
        # Слой 227 разрешил кнопки не только ботам — обычному аккаунту тоже.
        markup = None
        if args.button:
            rows = tgx_bots.parse_buttons(tgx_bots.join_buttons(args.button))
            markup = types.ReplyInlineMarkup(
                rows=[types.KeyboardButtonRow(buttons=r) for r in rows])
        result = await client(functions.messages.SendMessageRequest(
            peer=chat, message="", random_id=helpers.generate_random_long(),
            rich_message=types.InputRichMessageMarkdown(
                markdown=markdown, rtl=False, noautolink=False, files=files or []),
            reply_to=reply, reply_markup=markup, silent=args.silent or None))
        sent_id = 0
        for update in getattr(result, "updates", None) or []:
            sent_id = getattr(update, "id", 0) or getattr(getattr(update, "message", None), "id", 0) or sent_id
        render.emit({"ok": True, "chat": args.peer, "topic": args.topic, "message_id": sent_id})
    finally:
        await client.disconnect()


async def cmd_transcribe(args: argparse.Namespace) -> None:
    """Расшифровка голосовых и кружков."""
    client = await make_client()
    try:
        await ensure_login(client)
        worker = tgx_transcribe.Transcriber(client)

        if args.trcmd == "status":
            render.emit(await worker.status())
            return

        peer = await resolve_peer(client, args.chat)
        if args.trcmd == "rate":
            render.emit({"ok": True, **await worker.rate(
                peer, args.id, args.transcription_id, args.verdict == "good")})
            return

        result = {"chat": args.chat, **await worker.transcribe(peer, args.id, wait=args.wait)}
        if args.jsonl:
            print_jsonl([result])
        elif render.pretty():
            body = result["text"] or "(пусто)"
            if result["pending"]:
                body += "\n\n(ещё не готово — Telegram дописывает; повторите через минуту)"
            if result["free_left"] is not None:
                body += f"\n\nбесплатных расшифровок осталось: {result['free_left']}"
            render.console().print(body)
        else:
            print(result["text"])
        return
    finally:
        await client.disconnect()


async def cmd_forum(args: argparse.Namespace) -> None:
    """Форумы и темы: core.telegram.org/api/forum целиком."""
    command = args.forumcmd
    client = await make_client()
    try:
        await ensure_login(client)
        forum = tgx_forum.Forum(client)
        peer = await resolve_peer(client, args.chat) if getattr(args, "chat", None) else None

        if command == "topics":
            rows = await forum.topics(peer, query=args.search, limit=args.limit)
            if args.jsonl:
                print_jsonl(rows)
            else:
                for row in rows:
                    row["метка"] = " ".join(filter(None, [
                        "закреплена" if row["pinned"] else "",
                        "закрыта" if row["closed"] else "",
                        "скрыта" if row["hidden"] else "",
                        "общая" if row["general"] else ""])) or ""
                print_table(rows, ["id", "title", "unread", "метка"], title="темы форума")
            return

        if command == "show":
            rows = await forum.by_id(peer, args.id)
            print_jsonl(rows) if args.jsonl else render.emit(rows, title="темы по id")
            return

        if command == "icons":
            rows = await forum.icons(args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["emoji", "icon_emoji_id"], title="иконки тем, доступные всем")
            return

        if command == "create":
            render.emit({"ok": True, **await forum.create(
                peer, args.title, color=args.color, icon_emoji_id=args.emoji, send_as=args.send_as)})
            return

        if command == "edit":
            closed = True if args.close else (False if args.open else None)
            hidden = True if args.hide else (False if args.show else None)
            render.emit({"ok": True, **await forum.edit(
                peer, args.id, title=args.title, icon_emoji_id=args.emoji,
                closed=closed, hidden=hidden)})
            return

        if command == "delete":
            if not args.yes:
                raise tgx_forum.ForumError(
                    f"удаление темы {args.id} снесёт всю её переписку и необратимо; "
                    f"повторите с --yes")
            render.emit({"ok": True, **await forum.delete(peer, args.id)})
            return

        if command in {"pin", "unpin"}:
            render.emit({"ok": True, **await forum.pin(peer, args.id, pinned=command == "pin")})
            return

        if command == "reorder":
            render.emit({"ok": True, **await forum.reorder(peer, args.id, force=args.force)})
            return

        if command in {"on", "off"}:
            render.emit({"ok": True, **await forum.toggle(
                peer, command == "on", tabs=bool_from_arg(args.tabs) if args.tabs is not None else None)})
            return

        if command == "tabs":
            render.emit({"ok": True, **await forum.set_tabs(peer, args.state == "on")})
            return

        if command == "as-messages":
            render.emit({"ok": True, **await forum.view_as_messages(peer, args.state == "on")})
            return

        if command == "limit":
            render.emit({"pinned_limit": await forum.pinned_limit()})
            return
    finally:
        await client.disconnect()


async def cmd_profile_banner(args: argparse.Namespace) -> None:
    """Записать заставку из терминала в видео — и поставить её аватаром.

    Цель указывается явно: слишком легко нечаянно сменить собственный аватар.
    """
    targets = [bool(args.bot), bool(args.chat), args.me]
    if not args.save and not any(targets):
        raise tgx_banner.BannerError(
            "не сказано, чей это аватар: --bot @имя, --chat канал, --me — свой, "
            "или --save путь.mp4, чтобы только записать файл")
    if sum(targets) > 1:
        raise tgx_banner.BannerError("выберите одну цель: --bot, --chat или --me")

    out = Path(args.save or args.out or DATA / "banner.mp4").expanduser()
    info = tgx_banner.record(out, effect=args.effect, cols=args.cols, rows=args.rows,
                             fps=args.fps, size=args.size, speed=args.speed,
                             seconds=args.seconds, hold=args.hold)

    if not any(targets):
        render.emit({"ok": True, **info})
        return

    client = await make_client()
    try:
        await ensure_login(client)
        look = tgx_profile.Appearance(client, cache=DATA / "avatars")
        # Обложка — на готовом логотипе: иначе в аватаре виден пустой первый кадр.
        avatar = tgx_profile.parse_avatar(str(out), start=info["cover"])
        where = await resolve_peer(client, args.chat) if args.chat else None
        if where is not None:
            result = await look.set_chat_photo(where, avatar)
        else:
            result = await look.set_photo(avatar, bot=args.bot)
        render.emit({"ok": True, **info, **result})
    finally:
        await client.disconnect()


async def cmd_profile(args: argparse.Namespace) -> None:
    """Оформление: аватары во всех форматах, цвета, статус, дата рождения."""
    command = args.profcmd

    if command == "formats":
        text = tgx_profile.AVATAR_SYNTAX
        render.console().print(text) if render.pretty() else print(text)
        return

    client = await make_client()
    try:
        await ensure_login(client)
        look = tgx_profile.Appearance(client, cache=DATA / "avatars")

        def avatar():
            return tgx_profile.parse_avatar(args.source, colors=getattr(args, "colors", None),
                                            start=getattr(args, "start", None))

        square = getattr(args, "square", False)
        trim = getattr(args, "trim", None)

        if command == "photo":
            where = await resolve_peer(client, args.chat) if args.chat else None
            if args.contact:
                result = await look.set_contact_photo(
                    args.contact, avatar(), suggest=args.suggest, square=square, trim=trim)
            elif where is not None:
                result = await look.set_chat_photo(where, avatar(), square=square, trim=trim)
            else:
                result = await look.set_photo(avatar(), fallback=args.fallback, bot=args.bot,
                                              square=square, trim=trim)
            render.emit({"ok": True, **result})
            return

        if command == "photos":
            rows = await look.photos(args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["id", "kind", "date"], title="ваши аватары")
            return

        if command == "photo-delete":
            render.emit({"ok": True, "deleted": await look.delete_photos(args.id)})
            return

        if command == "unset-contact-photo":
            render.emit({"ok": True, **await look.set_contact_photo(args.contact, None)})
            return

        if command == "color":
            where = await resolve_peer(client, args.chat) if args.chat else None
            render.emit({"ok": True, **await look.set_color(
                args.color, args.emoji, for_profile=args.profile, chat=where)})
            return

        if command == "status":
            where = await resolve_peer(client, args.chat) if args.chat else None
            render.emit({"ok": True, **await look.set_status(
                None if args.off else args.emoji, args.until, chat=where)})
            return

        if command == "birthday":
            render.emit({"ok": True, **await look.set_birthday(None if args.off else args.date)})
            return

        if command == "personal-channel":
            where = None if args.off else await resolve_peer(client, args.chat)
            render.emit({"ok": True, **await look.set_personal_channel(where)})
            return

        if command == "emojis":
            rows = await look.suggested(args.kind, args.limit)
            print_jsonl(rows) if args.jsonl else print_table(
                rows, ["emoji", "emoji_id"], title=f"предложенные эмодзи: {args.kind}")
            return
    finally:
        await client.disconnect()


async def cmd_profile_get(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        me = await client.get_me()
        full = await client(functions.users.GetFullUserRequest(me))
        render.emit({"user": user_row(me), "full": tl_to_plain(getattr(full, "full_user", None))})
    finally:
        await client.disconnect()


async def cmd_profile_edit(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        result: dict[str, Any] = {"ok": True}
        if args.first_name is not None or args.last_name is not None or args.about is not None:
            await client(functions.account.UpdateProfileRequest(first_name=args.first_name, last_name=args.last_name, about=args.about))
            result["profile_updated"] = True
        if args.username is not None:
            ok = await client(functions.account.UpdateUsernameRequest(args.username))
            result["username_updated"] = bool(ok)
        me = await client.get_me()
        result["user"] = user_row(me)
        render.emit(result)
    finally:
        await client.disconnect()


async def cmd_profile_photo_set(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        uploaded = await client.upload_file(str(Path(args.file).expanduser()))
        result = await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_profile_photos(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        me = await client.get_me()
        photos = await client(functions.photos.GetUserPhotosRequest(user_id=me, offset=0, max_id=0, limit=args.limit))
        rows = [{"id": getattr(p, "id", None), "date": getattr(getattr(p, "date", None), "isoformat", lambda: None)(), "type": type(p).__name__} for p in getattr(photos, "photos", [])]
        render.emit({"count": getattr(photos, "count", len(rows)), "photos": rows})
    finally:
        await client.disconnect()


async def cmd_profile_photo_delete(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        me = await client.get_me()
        photos = await client(functions.photos.GetUserPhotosRequest(user_id=me, offset=0, max_id=0, limit=100))
        selected = []
        wanted = {int(x) for x in (args.photo_id or [])}
        for p in getattr(photos, "photos", []):
            if args.all or getattr(p, "id", None) in wanted:
                selected.append(types.InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference))
        result = await client(functions.photos.DeletePhotosRequest(id=selected)) if selected else []
        render.emit({"ok": True, "deleted_requested": len(selected), "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_pin(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        await client(functions.messages.UpdatePinnedMessageRequest(
            peer=peer, id=args.id, silent=not args.notify, unpin=args.unpin))
        render.emit({"ok": True, "message_id": args.id,
                     "action": "unpinned" if args.unpin else "pinned",
                     "notified": bool(args.notify and not args.unpin)})
    finally:
        await client.disconnect()


async def cmd_pinned(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        rows = []
        async for msg in client.iter_messages(peer, limit=args.limit,
                                              filter=types.InputMessagesFilterPinned()):
            rows.append(msg_to_obj(msg))
        rows.reverse()
        if args.jsonl:
            print_jsonl(rows)
        else:
            render.print_messages(rows, title=f"закреплённые · {entity_title(peer)}")
    finally:
        await client.disconnect()


async def cmd_topics(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        result = await client(functions.messages.GetForumTopicsRequest(
            peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=args.limit, q=None))
        rows = [{"id": t.id, "title": t.title, "closed": bool(getattr(t, "closed", False)),
                 "pinned": bool(getattr(t, "pinned", False)), "hidden": bool(getattr(t, "hidden", False)),
                 "unread": int(getattr(t, "unread_count", 0) or 0)}
                for t in getattr(result, "topics", []) if hasattr(t, "title")]
        if args.jsonl:
            print_jsonl(rows)
        else:
            print_table(rows, ["id", "title", "unread", "closed", "pinned"], title="темы")
    finally:
        await client.disconnect()


async def cmd_topic_create(args: argparse.Namespace) -> None:
    from telethon import helpers

    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        await client(functions.messages.CreateForumTopicRequest(
            peer=peer, title=args.title, random_id=helpers.generate_random_long()))
        render.emit({"ok": True, "title": args.title})
    finally:
        await client.disconnect()


async def cmd_topic_edit(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        closed = True if args.close else (False if args.open else None)
        hidden = True if args.hide else (False if args.show else None)
        await client(functions.messages.EditForumTopicRequest(
            peer=peer, topic_id=args.id, title=args.title, closed=closed, hidden=hidden))
        render.emit({"ok": True, "topic_id": args.id, "title": args.title,
                     "closed": closed, "hidden": hidden})
    finally:
        await client.disconnect()


async def cmd_topic_pin(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        await client(functions.messages.UpdatePinnedForumTopicRequest(
            peer=peer, topic_id=args.id, pinned=not args.unpin))
        render.emit({"ok": True, "topic_id": args.id, "pinned": not args.unpin})
    finally:
        await client.disconnect()


async def cmd_channel_create(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        result = await client(functions.channels.CreateChannelRequest(
            title=args.title, about=args.about or "",
            broadcast=args.kind == "channel", megagroup=args.kind != "channel",
            forum=args.kind == "forum",
        ))
        entity = result.chats[0]
        if args.username:
            await client(functions.channels.UpdateUsernameRequest(entity, args.username.lstrip("@")))
        render.emit({"ok": True, "id": entity.id, "title": entity.title, "kind": args.kind,
                     "username": args.username or None})
    finally:
        await client.disconnect()


async def cmd_channel_slowmode(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        await client(functions.channels.ToggleSlowModeRequest(ch, args.seconds))
        render.emit({"ok": True, "slowmode_seconds": args.seconds})
    finally:
        await client.disconnect()


async def cmd_channel_permissions(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        allowed = {name.strip().replace("-", "_") for name in (args.allow or "").split(",") if name.strip()}
        unknown = allowed - set(BANNED_RIGHT_NAMES)
        if unknown:
            raise SystemExit(f"неизвестные права: {', '.join(sorted(unknown))}")
        rights = {name: name not in allowed for name in BANNED_RIGHT_NAMES}
        await client(functions.messages.EditChatDefaultBannedRightsRequest(
            peer=peer, banned_rights=types.ChatBannedRights(until_date=None, **rights)))
        render.emit({"ok": True, "allowed": sorted(allowed)})
    finally:
        await client.disconnect()


async def cmd_channel_discussion(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        broadcast = await input_channel(client, args.peer)
        group = None if args.unlink else await input_channel(client, args.group)
        await client(functions.channels.SetDiscussionGroupRequest(broadcast=broadcast, group=group))
        render.emit({"ok": True, "linked": None if args.unlink else args.group})
    finally:
        await client.disconnect()


async def cmd_chat_join(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        value = args.link.strip()
        if "joinchat" in value or "/+" in value or value.startswith("+"):
            result = await client(functions.messages.ImportChatInviteRequest(
                value.rstrip("/").split("/")[-1].lstrip("+")))
        else:
            result = await client(functions.channels.JoinChannelRequest(value.lstrip("@")))
        chats = getattr(result, "chats", None) or []
        render.emit({"ok": True, "joined": getattr(chats[0], "title", value) if chats else value})
    finally:
        await client.disconnect()


async def cmd_chat_leave(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        if not args.yes:
            raise SystemExit("выход из чата необратим для истории супергруппы: добавьте --yes")
        ch = await input_channel(client, args.peer)
        await client(functions.channels.LeaveChannelRequest(ch))
        render.emit({"ok": True, "left": args.peer})
    finally:
        await client.disconnect()


async def cmd_channel_info(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        entity = await resolve_peer(client, args.peer)
        full = await client(functions.channels.GetFullChannelRequest(entity)) if isinstance(entity, Channel) else None
        render.emit({"channel": channel_row(entity, getattr(full, "full_chat", None)), "raw_full": tl_to_plain(getattr(full, "full_chat", None)) if args.raw else None})
    finally:
        await client.disconnect()


async def cmd_channel_edit(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        changes: dict[str, Any] = {}
        if args.title is not None:
            await client(functions.channels.EditTitleRequest(ch, args.title)); changes["title"] = args.title
        if args.about is not None:
            await client(functions.messages.EditChatAboutRequest(ch, args.about)); changes["about"] = args.about
        if args.username is not None:
            ok = await client(functions.channels.UpdateUsernameRequest(ch, args.username)); changes["username"] = bool(ok)
        if args.signatures is not None or args.profiles is not None:
            await client(functions.channels.ToggleSignaturesRequest(ch, signatures_enabled=bool_from_arg(args.signatures), profiles_enabled=bool_from_arg(args.profiles))); changes["signatures"] = args.signatures; changes["profiles"] = args.profiles
        for attr, req in [("prehistory_hidden", functions.channels.TogglePreHistoryHiddenRequest), ("join_to_send", functions.channels.ToggleJoinToSendRequest), ("join_request", functions.channels.ToggleJoinRequestRequest), ("participants_hidden", functions.channels.ToggleParticipantsHiddenRequest)]:
            value = getattr(args, attr)
            if value is not None:
                await client(req(ch, bool_from_arg(value))); changes[attr] = bool_from_arg(value)
        if args.forum is not None:
            await client(functions.channels.ToggleForumRequest(ch, bool_from_arg(args.forum), bool_from_arg(args.forum_tabs) if args.forum_tabs is not None else False)); changes["forum"] = bool_from_arg(args.forum)
        render.emit({"ok": True, "changes": changes})
    finally:
        await client.disconnect()


async def cmd_channel_photo_set(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        result = await client(functions.channels.EditPhotoRequest(ch, await uploaded_chat_photo(client, args.file)))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_photo_delete(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        result = await client(functions.channels.EditPhotoRequest(ch, types.InputChatPhotoEmpty()))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_participants(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        result = await client(functions.channels.GetParticipantsRequest(ch, participant_filter(args.filter, args.query or ""), offset=args.offset, limit=args.limit, hash=0))
        rows = [user_row(u) for u in getattr(result, "users", [])]
        render.emit({"count": getattr(result, "count", len(rows)), "participants": rows})
    finally:
        await client.disconnect()


async def cmd_channel_admin_set(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        user = await input_user(client, args.user)
        rights = types.ChatAdminRights(**parse_rights_csv(args.rights, ADMIN_RIGHT_NAMES, True))
        result = await client(functions.channels.EditAdminRequest(ch, user, rights, rank=args.rank))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_admin_remove(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        user = await input_user(client, args.user)
        result = await client(functions.channels.EditAdminRequest(ch, user, types.ChatAdminRights(), rank=""))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_ban(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        user = await input_user(client, args.user)
        rights = parse_rights_csv(args.rights or "view_messages", BANNED_RIGHT_NAMES, True)
        result = await client(functions.channels.EditBannedRequest(ch, user, types.ChatBannedRights(until_date=None, **rights)))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_unban(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        user = await input_user(client, args.user)
        rights = {name: False for name in BANNED_RIGHT_NAMES}
        result = await client(functions.channels.EditBannedRequest(ch, user, types.ChatBannedRights(until_date=None, **rights)))
        render.emit({"ok": True, "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_channel_invite_add(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        users = [await input_user(client, u) for u in args.user]
        result = await client(functions.channels.InviteToChannelRequest(ch, users))
        render.emit({"ok": True, "count": len(users), "result": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_invite_export(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await input_channel(client, args.peer)
        result = await client(functions.messages.ExportChatInviteRequest(peer=peer, request_needed=args.request_needed, usage_limit=args.usage_limit, title=args.title))
        render.emit({"ok": True, "invite": tl_to_plain(result)})
    finally:
        await client.disconnect()


async def cmd_invite_list(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await input_channel(client, args.peer)
        me = await client.get_me()
        result = await client(functions.messages.GetExportedChatInvitesRequest(peer=peer, admin_id=me, limit=args.limit, revoked=args.revoked))
        render.emit(tl_to_plain(result))
    finally:
        await client.disconnect()


async def cmd_admin_log(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        ch = await input_channel(client, args.peer)
        result = await client(functions.channels.GetAdminLogRequest(channel=ch, q=args.query or "", max_id=0, min_id=0, limit=args.limit))
        render.emit(tl_to_plain(result))
    finally:
        await client.disconnect()


async def cmd_tl_schema(args: argparse.Namespace) -> None:
    import inspect
    ns = getattr(functions, args.namespace)
    rows = []
    for name in dir(ns):
        if name.endswith("Request") and (not args.query or args.query.lower() in name.lower()):
            cls = getattr(ns, name)
            rows.append({"name": f"{args.namespace}.{name}", "signature": str(inspect.signature(cls))})
    render.emit(rows)


def msg_with_buttons_to_obj(msg: Any) -> dict[str, Any]:
    obj = msg_to_obj(msg)
    obj["buttons"] = [[getattr(b, "text", "") for b in row] for row in (getattr(msg, "buttons", None) or [])]
    obj["media"] = type(getattr(msg, "media", None)).__name__ if getattr(msg, "media", None) else None
    return obj


async def cmd_message_get(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        if args.id:
            msg = await client.get_messages(peer, ids=args.id)
            render.emit(msg_with_buttons_to_obj(msg) if msg else None)
            return
        rows = []
        async for msg in client.iter_messages(peer, limit=args.limit):
            rows.append(msg_with_buttons_to_obj(msg))
        rows.reverse()
        render.emit(rows)
    finally:
        await client.disconnect()


async def cmd_message_click(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        before = None
        async for latest in client.iter_messages(peer, limit=1):
            before = latest.id
        msg = await client.get_messages(peer, ids=args.id)
        if not msg:
            raise SystemExit(f"message not found: {args.id}")
        if args.text:
            result = await msg.click(text=args.text)
        elif args.row is not None and args.col is not None:
            result = await msg.click(args.row, args.col)
        else:
            raise SystemExit("provide --text or both --row and --col")
        if args.wait:
            await asyncio.sleep(args.wait)
        new_rows = []
        async for new_msg in client.iter_messages(peer, limit=args.limit):
            if before is None or new_msg.id > before:
                new_rows.append(msg_with_buttons_to_obj(new_msg))
        new_rows.reverse()
        render.emit({"ok": True, "click_result": tl_to_plain(result), "new_messages": new_rows})
    finally:
        await client.disconnect()


async def cmd_history(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        out = []
        async for msg in client.iter_messages(peer, limit=args.limit, search=args.search):
            if args.with_sender:
                try:
                    msg.sender = await msg.get_sender()
                except Exception:
                    pass
            out.append(msg_to_obj(msg))
        out.reverse()
        if args.jsonl:
            print_jsonl(out)
        else:
            render.print_messages(out, title=entity_title(peer))
    finally:
        await client.disconnect()


async def cmd_search(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        media = None
        if args.kind:
            name = MEDIA_FILTERS.get(args.kind)
            if name is None:
                raise SystemExit(f"неизвестный тип: {args.kind}; доступны {', '.join(sorted(MEDIA_FILTERS))}")
            media = getattr(types, name)()
        since = parse_search_date(args.since) if args.since else None
        until = parse_search_date(args.until) if args.until else None
        if (args.since and since is None) or (args.until and until is None):
            raise SystemExit("дата не разобралась: 2026-08-01, 01.08.2026 или -7d")

        peers: list[Any] = []
        if args.peer:
            peers = [await resolve_peer(client, args.peer)]
        elif args.per_dialog and not args.globally:
            async for dialog in client.iter_dialogs(limit=args.dialog_limit):
                peers.append(dialog.entity)

        sender = None
        if args.sender:
            if not args.peer:
                raise SystemExit("отбор по отправителю работает только с --peer")
            sender = await client.get_input_entity(args.sender)

        results = []

        async def collect(peer: Any, cap: int) -> None:
            kwargs: dict[str, Any] = {"search": args.query or None, "limit": None if since else cap}
            if media is not None:
                kwargs["filter"] = media
            if sender is not None:
                kwargs["from_user"] = sender
            if until is not None:
                kwargs["offset_date"] = until
            async for msg in client.iter_messages(peer, **kwargs):
                when = msg.date
                if since is not None and when is not None:
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < since:
                        break
                obj = msg_to_obj(msg)
                chat = peer if peer is not None else getattr(msg, "chat", None)
                obj["chat"] = entity_title(chat) if chat is not None else ""
                obj["chat_id"] = getattr(chat, "id", None)
                results.append(obj)
                if len(results) >= args.limit:
                    break

        if peers:
            for peer in peers:
                await collect(peer, args.limit if args.peer else args.per_dialog)
                if len(results) >= args.limit:
                    break
        else:
            await collect(None, args.limit)          # global search across every chat

        results.sort(key=lambda x: x.get("date") or "")
        if args.jsonl:
            print_jsonl(results)
        else:
            render.print_messages(results[-args.limit:], title=f"«{args.query or args.kind}»", show_chat=True)
    finally:
        await client.disconnect()


async def cmd_send(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        import tgx_media

        files = [str(Path(f).expanduser()) for f in (args.file or [])]
        body, entities = tgx_format.parse(args.message or "", args.parse_mode)
        schedule = None
        if args.schedule:
            from datetime import datetime

            schedule = datetime.fromisoformat(args.schedule)
        if files and (args.cover or args.start_at is not None):
            # Своя обложка и точка старта — только для одного видео за раз.
            if len(files) > 1:
                raise SystemExit("--cover и --start-at работают с одним файлом")
            media = await tgx_media.video_with_cover(
                client, Path(files[0]),
                Path(args.cover).expanduser() if args.cover else None, args.start_at)
            result = await client(functions.messages.SendMediaRequest(
                peer=peer, media=media, message=body, entities=entities or None,
                random_id=helpers.generate_random_long(),
                reply_to=types.InputReplyToMessage(reply_to_msg_id=args.reply_to)
                if args.reply_to else None,
                silent=args.silent or None))
            sent_id = 0
            for update in getattr(result, "updates", None) or []:
                sent_id = (getattr(update, "id", 0)
                           or getattr(getattr(update, "message", None), "id", 0) or sent_id)
            render.emit({"ok": True, "chat_id": getattr(peer, "id", None),
                         "message_id": sent_id, "cover": bool(args.cover),
                         "start_at": args.start_at})
            return

        if files:
            sent = await client.send_file(
                peer,
                files if len(files) > 1 else files[0],
                caption=body or None,
                parse_mode=None,
                formatting_entities=entities or None,
                schedule=schedule,
                force_document=args.as_document,
                voice_note=args.voice,
                video_note=args.video_note,
                supports_streaming=not args.as_document and any(tgx_media.is_video_file(f) for f in files),
                thumb=str(tgx_media.poster_frame(Path(files[0]))) if len(files) == 1 and not args.as_document
                and tgx_media.poster_frame(Path(files[0])) else None,
                silent=args.silent or None,
                reply_to=args.reply_to,
                comment_to=args.comment_to,
            )
            if isinstance(sent, list):
                sent = sent[-1]
        else:
            if args.effect:
                # Эффект пробрасывается только сырым запросом: send_message его не знает.
                # И работает он лишь в личной переписке — в группе и канале сервер
                # отвечает EFFECT_CHAT_INVALID.
                if entity_kind(peer) in {"channel", "group"}:
                    raise PeerError("эффекты работают только в личных чатах — "
                                    "в группе и канале Telegram их не принимает")
                result = await client(functions.messages.SendMessageRequest(
                    peer=peer, message=body, entities=entities or None,
                    random_id=helpers.generate_random_long(), effect=int(args.effect),
                    no_webpage=args.no_preview or None, silent=args.silent or None,
                    reply_to=types.InputReplyToMessage(reply_to_msg_id=args.reply_to)
                    if args.reply_to else None))
                sent_id = 0
                for update in getattr(result, "updates", None) or []:
                    sent_id = (getattr(update, "id", 0)
                               or getattr(getattr(update, "message", None), "id", 0) or sent_id)
                render.emit({"ok": True, "chat_id": getattr(peer, "id", None),
                             "message_id": sent_id, "effect": args.effect})
                return
            sent = await client.send_message(
                peer, body, parse_mode=None, formatting_entities=entities or None,
                link_preview=not args.no_preview, silent=args.silent or None, schedule=schedule,
                comment_to=args.comment_to, reply_to=None if args.comment_to else args.reply_to,
            )
        render.emit({"ok": True, "chat_id": getattr(peer, "id", None), "message_id": sent.id, "files": len(files)})
    finally:
        await client.disconnect()


async def cmd_export(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        rows = []
        async for msg in client.iter_messages(peer, limit=args.limit):
            if args.with_sender:
                try:
                    msg.sender = await msg.get_sender()
                except Exception:
                    pass
            rows.append(msg_to_obj(msg))
        rows.reverse()
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "jsonl":
            out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows))
        elif args.format == "json":
            out.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str) + "\n")
        else:
            with out.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["id", "date", "sender_id", "sender", "text", "views", "forwards", "reply_to"])
                writer.writeheader()
                writer.writerows(rows)
        render.emit({"ok": True, "output": str(out), "messages": len(rows)})
    finally:
        await client.disconnect()


async def cmd_react(args: argparse.Namespace) -> None:
    """Своя реакция, список чужих и снятие чужих — последнее требует прав админа."""
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)

        if args.who:
            result = await client(functions.messages.GetMessageReactionsListRequest(
                peer=peer, id=args.id, limit=args.limit, reaction=None, offset=None))
            names = {u.id: (u.username or entity_title(u)) for u in result.users}
            rows = [{"кто": names.get(getattr(r.peer_id, "user_id", None),
                                      getattr(r.peer_id, "user_id", "?")),
                     "реакция": getattr(getattr(r, "reaction", None), "emoticon", "?")}
                    for r in result.reactions]
            print_jsonl(rows) if args.jsonl else print_table(rows, ["кто", "реакция"],
                                                             title=f"реакции на {args.id}")
            return

        if args.remove_from:
            # Снять реакции конкретного участника — модерация, а не своя реакция.
            target = await client.get_input_entity(args.remove_from)
            if args.all_messages:
                await client(functions.messages.DeleteParticipantReactionsRequest(
                    peer=peer, participant=target))
                render.emit({"ok": True, "cleared": args.remove_from, "scope": "во всём чате"})
            else:
                await client(functions.messages.DeleteParticipantReactionRequest(
                    peer=peer, msg_id=args.id, participant=target))
                render.emit({"ok": True, "cleared": args.remove_from, "message_id": args.id})
            return

        reaction = [] if args.clear else [types.ReactionEmoji(emoticon=args.emoji)]
        await client(functions.messages.SendReactionRequest(
            peer=peer, msg_id=args.id, reaction=reaction, add_to_recent=not args.clear))
        render.emit({"ok": True, "message_id": args.id, "emoji": None if args.clear else args.emoji})
    finally:
        await client.disconnect()


async def cmd_edit(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        parse_mode = None if args.parse_mode == "none" else args.parse_mode
        edited = await client.edit_message(peer, args.id, args.text, parse_mode=parse_mode,
                                           link_preview=not args.no_preview)
        render.emit({"ok": True, "message_id": edited.id})
    finally:
        await client.disconnect()


async def cmd_forward(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        source = await resolve_peer(client, args.peer)
        target = await resolve_peer(client, args.to)
        await client.forward_messages(target, args.id, from_peer=source, silent=args.silent or None)
        render.emit({"ok": True, "count": len(args.id), "to": entity_title(target)})
    finally:
        await client.disconnect()


async def cmd_effects(args: argparse.Namespace) -> None:
    """Эффекты сообщений (Bot API 7.4): анимация, которая проигрывается при отправке."""
    client = await make_client()
    try:
        await ensure_login(client)
        result = await client(functions.messages.GetAvailableEffectsRequest(hash=0))
        rows = [{"эмодзи": getattr(e, "emoticon", "?"), "id": e.id,
                 "premium": "да" if getattr(e, "premium_required", False) else ""}
                for e in (getattr(result, "effects", None) or [])]
        if args.search:
            rows = [r for r in rows if args.search in r["эмодзи"]]
        rows = rows[: args.limit]
        print_jsonl(rows) if args.jsonl else print_table(rows, ["эмодзи", "id", "premium"],
                                                        title="эффекты сообщений")
    finally:
        await client.disconnect()


async def cmd_copy(args: argparse.Namespace) -> None:
    """Копия без подписи «переслано» — то, что Bot API зовёт copyMessage.

    Тот же метод, что и пересылка, но с `drop_author`: получатель не видит,
    откуда сообщение, и оно не тянет за собой ссылку на источник.
    """
    from telethon import helpers

    client = await make_client()
    try:
        await ensure_login(client)
        source = await resolve_peer(client, args.peer)
        target = await resolve_peer(client, args.to)
        result = await client(functions.messages.ForwardMessagesRequest(
            from_peer=source, to_peer=target, id=list(args.id),
            random_id=[helpers.generate_random_long() for _ in args.id],
            drop_author=True,
            drop_media_captions=args.drop_captions or None,
            top_msg_id=args.topic,
            video_timestamp=int(args.start_at) if args.start_at is not None else None,
            silent=args.silent or None))
        sent = [getattr(u, "id", None) for u in getattr(result, "updates", None) or []
                if getattr(u, "id", None)]
        render.emit({"ok": True, "copied": len(args.id), "to": entity_title(target),
                     "message_ids": sent[:len(args.id)]})
    finally:
        await client.disconnect()


async def cmd_boosts(args: argparse.Namespace) -> None:
    """Бусты канала: сколько их, кто дал и сколько своих осталось."""
    client = await make_client()
    try:
        await ensure_login(client)

        if args.boostcmd == "mine":
            result = await client(functions.premium.GetMyBoostsRequest())
            rows = []
            for slot in getattr(result, "my_boosts", None) or []:
                peer = getattr(slot, "peer", None)
                where = "свободен"
                if peer is not None:
                    # Один недоступный канал не должен ронять весь список.
                    try:
                        where = entity_title(await client.get_entity(peer))
                    except Exception:
                        where = f"канал {getattr(peer, 'channel_id', '?')} (нет доступа)"
                rows.append({"слот": slot.slot, "занят": where,
                             "до": str(getattr(slot, "expires", "") or "")[:10]})
            print_jsonl(rows) if args.jsonl else print_table(rows, ["слот", "занят", "до"],
                                                             title="мои бусты")
            return

        peer = await resolve_peer(client, args.chat)

        if args.boostcmd == "status":
            status = await client(functions.premium.GetBoostsStatusRequest(peer=peer))
            render.emit({
                "level": getattr(status, "level", 0),
                "boosts": getattr(status, "boosts", 0),
                "до следующего уровня": getattr(status, "next_level_boosts", None),
                "мои бусты": getattr(status, "my_boost", False),
                "ссылка": getattr(status, "boost_url", None),
            }, title=entity_title(peer))
            return

        if args.boostcmd == "who":
            result = await client(functions.premium.GetBoostsListRequest(
                peer=peer, offset="", limit=args.limit))
            names = {u.id: (u.username or entity_title(u)) for u in result.users}
            rows = [{"кто": names.get(getattr(b, "user_id", None), "—"),
                     "бустов": getattr(b, "multiplier", 1) or 1,
                     "до": str(getattr(b, "expires", "") or "")[:10]}
                    for b in getattr(result, "boosts", None) or []]
            print_jsonl(rows) if args.jsonl else print_table(rows, ["кто", "бустов", "до"],
                                                             title="кто бустил")
            return

        if args.boostcmd == "give":
            slots = [int(s) for s in (args.slot or [])] or None
            await client(functions.premium.ApplyBoostRequest(peer=peer, slots=slots))
            render.emit({"ok": True, "boosted": entity_title(peer), "slots": slots or "свободные"})
            return
    finally:
        await client.disconnect()


async def cmd_delete(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        if args.confirm_to:
            peer_name = args.peer
            verdict = await ask_human(
                client, args.bot, args.confirm_to, "Удалить сообщения?",
                f"Чат: {peer_name}\nСообщений: {len(args.id)}",
                danger="удаление необратимо" + (" и затронет всех" if not args.only_me else ""),
                timeout=args.timeout)
            if verdict["decision"] != "approved":
                render.emit({"ok": False, "удаление": "отменено", **verdict})
                raise SystemExit(2)
        peer = await resolve_peer(client, args.peer)
        if not args.yes:
            raise SystemExit("удаление необратимо: добавьте --yes, если уверены")
        await client.delete_messages(peer, args.id, revoke=not args.only_me)
        render.emit({"ok": True, "deleted": len(args.id), "for_everyone": not args.only_me})
    finally:
        await client.disconnect()


async def _with_user(action: Any) -> Any:
    """Run something that needs the human account (BotFather talks to people)."""
    client = await make_client()
    try:
        await ensure_login(client)
        return await action(client)
    finally:
        await client.disconnect()


async def _with_bot(username: str, action: Any) -> Any:
    """Run something as the bot itself."""
    api_id, api_hash = get_credentials()
    bot = tgx_bots.Registry().get(username)
    async with tgx_bots.BotSession(bot, api_id, api_hash) as session:
        return await action(session)


async def cmd_ai(args: argparse.Namespace) -> None:
    command = args.aicmd

    if command in {"summarize", "translate", "auto-translate", "digest"}:
        client = await make_client()
        try:
            await ensure_login(client)
            reader = tgx_ai.Reader(client)
            peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

            if command == "summarize":
                render.emit(await reader.summarize(
                    peer, args.id, lang=args.lang or "", tone=args.tone or ""))
            elif command == "translate":
                render.emit({"переводы": await reader.translate(
                    args.lang, peer=peer, ids=args.id or None,
                    text=args.text or "", tone=args.tone or "")})
            elif command == "auto-translate":
                render.emit(await reader.auto_translate(peer, args.state == "on"))
            else:
                messages = []
                async for message in client.iter_messages(peer, limit=args.limit):
                    messages.append(message)
                messages.reverse()  # читаем сверху вниз, как человек
                render.emit(await reader.digest(
                    peer, messages, lang=args.lang or "", long_at=args.long_at))
        finally:
            await client.disconnect()
        return

    if command == "compose":
        text = args.text
        if Path(text).expanduser().is_file():
            text = Path(text).expanduser().read_text()
        result = await _with_user(lambda c: tgx_ai.Compose(c).run(
            text, proofread=args.proofread, emojify=args.emojify,
            translate=args.translate or "", tone=args.tone or ""))
        if args.apply:
            # правку сразу в дело: отправляем то, что вернул сервер
            sent = await _with_user(lambda c: c.send_message(
                args.apply, result["стало"]))
            result["отправлено"] = {"чат": args.apply, "id": sent.id}
        render.emit(result)
        return

    if command == "tones":
        render.emit({"тоны": await _with_user(lambda c: tgx_ai.Tones(c).listing())})
        return

    if command == "tone":
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).show(args.tone)))
        return

    if command == "tone-example":
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).example(args.tone, args.num)))
        return

    if command == "tone-new":
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).create(
            args.title, args.prompt, emoji_id=args.emoji_id, credit=args.credit)))
        return

    if command == "tone-edit":
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).update(
            args.tone, title=args.title or "", prompt=args.prompt or "",
            emoji_id=args.emoji_id, credit=args.credit)))
        return

    if command in {"tone-save", "tone-forget"}:
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).save(
            args.tone, unsave=command == "tone-forget")))
        return

    if command == "tone-delete":
        await gated_or_die(None, args, f"удалить свой тон «{args.tone}»",
                           "тон исчезнет у всех, кто его установил", danger=True)
        render.emit(await _with_user(lambda c: tgx_ai.Tones(c).delete(args.tone)))
        return


async def cmd_inline(args: argparse.Namespace) -> None:
    """Чужие боты: инлайн-запросы, кнопки, мини-приложения."""
    command = args.inlinecmd
    client = await make_client()
    try:
        await ensure_login(client)
        inline = tgx_inline.Inline(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "ask":
            # без чата спрашиваем «для избранного»: результаты те же, а чат не нужен
            from telethon.tl import types as _t
            render.emit(await inline.ask(args.bot, peer or _t.InputPeerSelf(), args.query,
                                         offset=args.offset or ""))
        elif command == "send":
            render.emit(await inline.send(peer, args.query_id, args.result_id,
                                          silent=args.silent, hide_via=args.hide_via,
                                          reply_to=args.reply_to or 0))
        elif command == "start":
            render.emit(await inline.start(args.bot, peer or await resolve_peer(
                client, args.bot), args.param or ""))
        elif command == "press":
            data = args.data.encode() if not args.hex else bytes.fromhex(args.data)
            secret = read_secret("пароль двухфакторной защиты: ",
                                 "TGX_PASSWORD") if args.password else ""
            render.emit(await inline.press(peer, args.id, data, password=secret))
        elif command == "attach-list":
            render.emit({"меню вложений": await inline.attach_menu()})
        elif command == "attach":
            render.emit(await inline.attach_toggle(args.bot, args.state == "on",
                                                   allow_write=args.allow_write))
        elif command == "web-app":
            answer = await inline.web_app(args.bot, peer=peer, url=args.url or "",
                                          param=args.param or "")
            if args.open and answer.get("адрес"):
                import webbrowser

                webbrowser.open(answer["адрес"])
                answer["открыто"] = "в браузере"
            render.emit(answer)
    finally:
        await client.disconnect()


async def cmd_safety(args: argparse.Namespace) -> None:
    """Блокировки, жалобы и уборка — почти всё через подтверждение."""
    command = args.safetycmd
    client = await make_client()
    try:
        await ensure_login(client)
        safety = tgx_safety.Safety(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "block":
            await gated_or_die(client, args, f"заблокировать {args.who}",
                               "человек не сможет вам писать и видеть вас", danger=True)
            render.emit(await safety.block(args.who, stories_only=args.stories_only))
        elif command == "unblock":
            render.emit(await safety.block(args.who, unblock=True,
                                           stories_only=args.stories_only))
        elif command == "blocked":
            render.emit({"заблокированы": await safety.blocked(
                limit=args.limit, stories_only=args.stories_only)})
        elif command == "block-replier":
            await gated_or_die(client, args, f"заблокировать автора сообщения {args.id}",
                               "и, если просили, стереть переписку", danger=True)
            render.emit(await safety.block_replier(
                args.id, delete=args.delete, wipe=args.wipe, spam=args.spam))
        elif command == "peer-settings":
            render.emit(await safety.peer_settings(peer))
        elif command == "hide-bar":
            render.emit(await safety.hide_bar(peer))
        elif command == "report-spam":
            await gated_or_die(client, args, f"пожаловаться на {args.peer} как на спам",
                               "жалобу нельзя отозвать", danger=True)
            render.emit(await safety.report_spam(peer))
        elif command == "report":
            if args.option or args.comment:
                await gated_or_die(client, args, f"отправить жалобу на {args.peer}",
                                   "жалобу нельзя отозвать", danger=True)
            render.emit(await safety.report(peer, args.id, option=args.option or "",
                                            comment=args.comment or ""))
        elif command == "clear-history":
            await gated_or_die(
                client, args, f"стереть переписку с {args.peer}",
                "у собеседника тоже" if args.both_sides else "только у вас", danger=True)
            render.emit(await safety.clear_history(
                peer, both_sides=args.both_sides, keep_chat=not args.drop_chat))
        elif command == "unpin-all":
            await gated_or_die(client, args, f"снять все закрепления в {args.peer}",
                               "вернуть можно только вручную", danger=True)
            render.emit(await safety.unpin_all(peer, topic=args.topic or 0))
        elif command == "sponsored":
            render.emit(await safety.sponsored(args.state == "on"))
    finally:
        await client.disconnect()


async def cmd_chan(args: argparse.Namespace) -> None:
    """Остаток управления каналами: адреса, вид, уборка, необратимое."""
    command = args.chancmd
    client = await make_client()
    try:
        await ensure_login(client)
        admin = tgx_chanadmin.ChanAdmin(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "search-posts":
            render.emit({"посты": await admin.search_posts(
                args.query or "", hashtag=args.hashtag or "", limit=args.limit)})
        elif command == "search-quota":
            render.emit(await admin.search_quota(args.query or ""))
        elif command == "free-name":
            render.emit(await admin.free_name(peer, args.name))
        elif command == "username":
            render.emit(await admin.username(peer, args.name, on=args.state == "on"))
        elif command == "usernames-order":
            render.emit(await admin.order_usernames(peer, args.name))
        elif command == "usernames-off":
            await gated_or_die(client, args, f"погасить все адреса {args.peer}",
                               "канал станет закрытым", danger=True)
            render.emit(await admin.drop_usernames(peer))
        elif command == "autotranslate":
            render.emit(await admin.autotranslate(peer, args.state == "on"))
        elif command == "main-tab":
            render.emit(await admin.main_tab(peer, args.tab))
        elif command == "stickers":
            render.emit(await admin.stickers(peer, args.name, emoji=args.emoji))
        elif command == "location":
            render.emit(await admin.location(peer, args.lat, args.lon, args.address))
        elif command == "send-as":
            render.emit({"можно писать от": await admin.send_as(
                peer, paid_reactions=args.paid_reactions)})
        elif command == "paid-messages":
            render.emit(await admin.paid_messages(peer, args.stars, broadcast=args.broadcast))
        elif command == "boost-bypass":
            render.emit(await admin.boost_bypass(peer, args.boosts))
        elif command == "hide-ads":
            render.emit(await admin.hide_ads(peer, args.state == "on"))
        elif command == "author":
            render.emit(await admin.author(peer, args.id))
        elif command == "wipe-participant":
            await gated_or_die(client, args, f"стереть все сообщения {args.who} в {args.peer}",
                               "вернуть их нельзя", danger=True)
            render.emit(await admin.wipe_participant(peer, args.who))
        elif command == "clear":
            await gated_or_die(client, args, f"стереть историю {args.peer}",
                               "у всех участников" if args.everyone else "только у вас",
                               danger=True)
            render.emit(await admin.clear(peer, up_to=args.up_to or 0, everyone=args.everyone))
        elif command == "report-spam":
            render.emit(await admin.report_spam(peer, args.who, args.id))
        elif command == "antispam-mistake":
            render.emit(await admin.antispam_mistake(peer, args.id))
        elif command == "left":
            render.emit({"покинутые": await admin.left(limit=args.limit)})
        elif command == "discussable":
            render.emit({"годятся в обсуждение": await admin.discussable()})
        elif command == "to-broadcast":
            await gated_or_die(client, args, f"превратить {args.peer} в трансляцию",
                               "писать смогут только администраторы, обратно нельзя",
                               danger=True)
            render.emit(await admin.to_broadcast(peer))
        elif command == "delete":
            await gated_or_die(client, args, f"удалить канал {args.peer}",
                               "вместе со всеми постами и подписчиками", danger=True)
            render.emit(await admin.drop(peer))
    finally:
        await client.disconnect()


async def cmd_group(args: argparse.Namespace) -> None:
    """Обычные группы, ветки обсуждений и признак набора."""
    command = args.groupcmd
    client = await make_client()
    try:
        await ensure_login(client)
        groups = tgx_groups.Groups(client)
        talk = tgx_groups.Discussion(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "typing":
            render.emit(await groups.typing(peer, args.what, topic=args.topic or 0,
                                            progress=args.progress or 0))
        elif command == "new":
            render.emit(await groups.create(args.title, args.who, ttl=args.ttl or 0))
        elif command == "add":
            render.emit(await groups.add(peer, args.who, history=args.history))
        elif command == "remove":
            render.emit(await groups.remove(peer, args.who, wipe=args.wipe))
        elif command == "rename":
            render.emit(await groups.rename(peer, args.title))
        elif command == "admin":
            render.emit(await groups.admin(peer, args.who, on=args.state == "on"))
        elif command == "rank":
            render.emit(await groups.rank(peer, args.who, args.title))
        elif command == "hand-over":
            await gated_or_die(client, args, f"передать группу {args.peer} — {args.who}",
                               "вы перестанете быть владельцем", danger=True)
            render.emit(await groups.hand_over(
                peer, args.who, read_secret("пароль двухфакторной защиты: ", "TGX_PASSWORD")))
        elif command == "delete":
            await gated_or_die(client, args, f"удалить группу {args.peer}",
                               "вместе со всей перепиской", danger=True)
            render.emit(await groups.drop(peer))
        elif command == "upgrade":
            await gated_or_die(client, args, f"превратить {args.peer} в супергруппу",
                               "обратно вернуть нельзя", danger=True)
            render.emit(await groups.upgrade(peer))
        elif command == "info":
            render.emit(await groups.info(peer))
        elif command == "ttl":
            render.emit(await groups.ttl(peer, args.seconds))
        elif command == "thread":
            render.emit(await talk.thread(peer, args.id))
        elif command == "replies":
            render.emit({"ответы": await talk.replies(peer, args.id, limit=args.limit)})
        elif command == "read-thread":
            render.emit(await talk.mark_read(peer, args.id, args.up_to or 0))
    finally:
        await client.disconnect()


async def cmd_notify(args: argparse.Namespace) -> None:
    """Чем вас беспокоят и кого вы впускаете."""
    command = args.notifycmd
    client = await make_client()
    try:
        await ensure_login(client)
        notify = tgx_notify.Notify(client)
        asks = tgx_notify.Requests(client)
        marks = tgx_notify.Reactions(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "show":
            render.emit(await notify.get(peer=peer, kind=args.scope))
        elif command == "mute":
            render.emit(await notify.set(peer=peer, kind=args.scope, span=args.span,
                                         previews=args.previews, stories=args.stories))
        elif command == "reset":
            render.emit(await notify.reset())
        elif command == "reactions":
            render.emit(await notify.reactions(from_whom=args.from_whom or "",
                                               previews=args.previews))
        elif command == "new-contacts":
            render.emit(await notify.new_contacts(args.state == "off"))
        elif command == "requests":
            render.emit({"админы и их ссылки": await asks.admins_with_invites(peer)})
        elif command == "approve":
            render.emit(await asks.decide(peer, args.who, True))
        elif command == "decline":
            render.emit(await asks.decide(peer, args.who, False))
        elif command == "approve-all":
            render.emit(await asks.decide_all(peer, True, link=args.link or ""))
        elif command == "decline-all":
            render.emit(await asks.decide_all(peer, False, link=args.link or ""))
        elif command == "invite-edit":
            render.emit(await asks.edit_link(
                peer, args.link, title=args.title or "", limit=args.limit,
                expires=args.expires or "", request_needed=args.request_needed,
                revoke=args.revoke))
        elif command == "invite-purge":
            render.emit(await asks.purge_revoked(peer, admin=args.admin))
        elif command == "emoji-list":
            render.emit({"доступны": await marks.available()})
        elif command == "emoji-top":
            render.emit({"чаще всего": await marks.top(args.limit)})
        elif command == "emoji-default":
            render.emit(await marks.set_default(args.emoji))
        elif command == "emoji-allow":
            render.emit(await marks.allow(peer, args.emoji, limit=args.limit,
                                          paid=args.paid))
    finally:
        await client.disconnect()


async def cmd_triage(args: argparse.Namespace) -> None:
    """Что ждёт вашего внимания в чате."""
    command = args.func_name
    client = await make_client()
    try:
        await ensure_login(client)
        triage = tgx_triage.Triage(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None
        topic = getattr(args, "topic", 0) or 0

        if command == "mentions":
            render.emit({"упоминания": await triage.mentions(
                peer, limit=args.limit, topic=topic)})
        elif command == "my-reactions":
            render.emit({"реакции": await triage.reactions(
                peer, limit=args.limit, topic=topic)})
        elif command == "triage-clear":
            render.emit(await triage.clear(peer, what=args.what, topic=topic))
        elif command == "read-by":
            render.emit(await triage.read_by(peer, args.id))
        elif command == "read-at":
            render.emit(await triage.read_at(peer, args.id))
        elif command == "views":
            render.emit({"просмотры": await triage.views(peer, args.id)})
        elif command == "chat-counts":
            render.emit(await triage.counts(peer, topic=topic))
        elif command == "online":
            render.emit(await triage.online(peer))
        elif command == "mark-unread":
            render.emit(await triage.mark_unread(peer, args.state == "on"))
        elif command == "marked-unread":
            render.emit({"помечены": await triage.marked()})
        elif command == "pin-chat":
            render.emit(await triage.pin_dialog(peer, args.state == "on"))
        elif command == "no-forwards":
            render.emit(await triage.no_forwards(peer, args.state == "on"))
    finally:
        await client.disconnect()


async def cmd_takeout(args: argparse.Namespace) -> None:
    """Выгрузить аккаунт на диск."""
    client = await make_client()
    try:
        await ensure_login(client)
        render.note("Telegram может попросить подтвердить выгрузку в другом устройстве")
        summary = await tgx_takeout.Takeout(client).run(
            Path(args.out), chats=args.chat or None, limit=args.limit,
            files=args.files, max_file_mb=args.max_file_mb,
            contacts=not args.no_contacts,
            progress=lambda step: render.note(f"выгружаю: {step}"))
        render.emit(summary)
    finally:
        await client.disconnect()


async def cmd_takeout_finish(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        render.emit(await tgx_takeout.Takeout(client).finish(success=args.success))
    finally:
        await client.disconnect()


async def cmd_chatx(args: argparse.Namespace) -> None:
    """Мелочи по чатам и каналам, которых не хватало поодиночке."""
    command = args.func_name
    client = await make_client()
    try:
        await ensure_login(client)
        extras = tgx_chatx.Extras(client)
        peer = await resolve_peer(client, args.peer) if getattr(args, "peer", None) else None

        if command == "message-link":
            render.emit(await extras.message_link(
                peer, args.id, album=args.album, thread=args.thread))
        elif command == "common-chats":
            who = await resolve_peer(client, args.user)
            render.emit({"общие чаты": await extras.common_chats(who, args.limit)})
        elif command == "my-public":
            render.emit({"публичные": await extras.my_public(
                for_location=args.by_location, check_limit=args.check_limit)})
        elif command == "similar":
            render.emit({"похожие": await extras.similar(peer)})
        elif command == "inactive":
            render.emit({"затихшие": await extras.inactive()})
        elif command == "who-is":
            who = await resolve_peer(client, args.who)
            render.emit(await extras.participant(peer, who))
        elif command == "antispam":
            render.emit(await extras.antispam(peer, args.state == "on"))
        elif command == "default-ttl":
            render.emit(await extras.default_ttl(args.seconds))
    finally:
        await client.disconnect()


async def cmd_bot(args: argparse.Namespace) -> None:
    registry = tgx_bots.Registry()
    command = args.botcmd

    if command == "buttons":
        if render.pretty():
            render.console().print(tgx_bots.BUTTON_SYNTAX)
        else:
            print(tgx_bots.BUTTON_SYNTAX)
        return

    if command == "list":
        bots = registry.load()
        rows = [b.public(reveal=args.reveal) for b in bots.values()]
        if args.jsonl:
            print_jsonl(rows)
        else:
            print_table(rows, ["username", "name", "token", "added"], title="боты")
        return

    if command == "forget":
        render.emit({"ok": registry.remove(args.username), "username": args.username.lstrip("@")})
        return

    if command == "create":
        # BotFather — путь по умолчанию: он единственный выдаёт токен владельцу.
        # --manager включает createBot, но там бота заводит бот-управляющий,
        # и токен приходится забирать отдельно, из его же сессии.
        async def make(client):
            if args.manager:
                bot = await tgx_bots.Direct(client).create(args.name, args.username, args.manager)
                return bot, f"управляющий {args.manager.lstrip('@')}"
            return await tgx_bots.BotFather(client).create(args.name, args.username), "botfather"
        bot, path = await _with_user(make)
        registry.add(bot)
        render.emit({"ok": True, "username": bot.username, "name": bot.name,
                     "token": bot.token if args.reveal else tgx_bots.mask(bot.token),
                     "путь": path, "saved_to": str(registry.path)})
        return

    if command == "secretary":
        on = args.state == "on"
        text = await _with_user(lambda c: tgx_bots.BotFather(c).secretary(args.username, on))
        render.emit({"ok": True, "username": args.username.lstrip("@"),
                     "secretary_mode": "on" if on else "off", "botfather": text})
        return

    if command in {"token", "revoke"}:
        revoke = command == "revoke"
        if args.via_manager:
            # exportBotToken — вызов бота: его делает управляющий за подопечного
            token = await _with_bot(args.via_manager, lambda s: tgx_bots.Direct(
                s.client).token(args.username, revoke=revoke))
        else:
            async def fetch(client):
                father = tgx_bots.BotFather(client)
                return await (father.revoke(args.username) if revoke else father.token(args.username))
            token = await _with_user(fetch)
        stored = registry.load().get(args.username.lstrip("@"))
        bot = tgx_bots.Bot(username=args.username.lstrip("@"),
                           name=stored.name if stored else "", token=token)
        registry.add(bot)
        render.emit({"ok": True, "username": bot.username,
                     "token": token if args.reveal else tgx_bots.mask(token)})
        return

    if command == "mine":
        async def listing(client):
            try:
                rows = await tgx_bots.Direct(client).mine()
                if rows:
                    return rows
            except tgx_bots.BotError:
                pass
            return [{"username": name} for name in await tgx_bots.BotFather(client).mine()]
        render.emit({"боты": await _with_user(listing)})
        return

    if command == "commands":
        render.emit({"команды": await _with_bot(
            args.username, lambda s: tgx_bots.Direct(s.client).commands(args.lang))})
        return

    if command == "menu-get":
        render.emit(await _with_bot(
            args.username, lambda s: tgx_bots.Direct(s.client).menu_button(args.user)))
        return

    if command == "previews":
        render.emit({"превью": await _with_user(
            lambda c: tgx_bots.Direct(c).previews(args.username))})
        return

    if command == "access":
        # доступ к боту настраивает сам бот: из аккаунта приходит USER_BOT_REQUIRED
        render.emit(await _with_bot(args.username, lambda s: tgx_bots.Shop(
            s.client).access(args.username, restricted=args.restricted, allow=args.allow)))
        return

    if command in {"previews-info", "preview-add", "preview-remove", "preview-swap",
                   "preview-order", "similar", "popular", "free-name",
                   "username", "usernames-order", "referrals", "emoji-permission",
                   "can-write"}:
        # витрина и настройки спрашиваются от вашего имени, сброс команд — от бота
        shop = lambda c: tgx_bots.Shop(c)
        actions = {
            "previews-info": lambda c: shop(c).previews(args.username, args.lang or ""),
            "preview-add": lambda c: shop(c).add_preview(args.username, args.url, args.lang or ""),
            "preview-remove": lambda c: shop(c).drop_preview(args.username, args.url, args.lang or ""),
            "preview-swap": lambda c: shop(c).swap_preview(args.username, args.old, args.new,
                                                           args.lang or ""),
            "preview-order": lambda c: shop(c).order_previews(args.username, args.url,
                                                              args.lang or ""),
            "similar": lambda c: shop(c).similar(args.username),
            "popular": lambda c: shop(c).popular(args.limit),
            "free-name": lambda c: shop(c).free_name(args.username),
            "username": lambda c: shop(c).username(args.username, args.name,
                                                   on=args.state == "on"),
            "usernames-order": lambda c: shop(c).order_usernames(args.username, args.name),
            "referrals": lambda c: shop(c).referrals(args.username, args.permille,
                                                     months=args.months or 0),
            "emoji-permission": lambda c: shop(c).emoji_permission(args.username,
                                                                   args.state == "on"),
            "can-write": lambda c: shop(c).can_write(args.username, allow=args.allow_write),
        }
        got = await _with_user(actions[command])
        render.emit(got if isinstance(got, dict) else {"найдено": got})
        return

    if command == "reset-commands":
        render.emit(await _with_bot(args.username, lambda s: tgx_bots.Shop(
            s.client).reset_commands(args.lang or "")))
        return

    if command == "group-rights":
        render.emit(await _with_bot(args.username, lambda s: tgx_bots.Direct(
            s.client).group_rights(invite=args.invite, pin=args.pin, delete=args.delete,
                                   ban=args.ban, info=args.info, channel=args.channel)))
        return

    if command in {"setname", "setabout", "setdescription", "setcommands"}:
        value = args.value
        if command == "setcommands" and Path(value).expanduser().is_file():
            value = Path(value).expanduser().read_text().strip()
        if args.via_botfather:
            method = {"setname": "set_name", "setabout": "set_about",
                      "setdescription": "set_description", "setcommands": "set_commands"}[command]
            answer = await _with_user(lambda c: getattr(tgx_bots.BotFather(c), method)(args.username, value))
            render.emit({"ok": True, "botfather": answer[:200]})
            return
        # the plain API path: no BotFather conversation to get stuck in
        field = {"setname": "name", "setabout": "about", "setdescription": "description"}.get(command)
        if command == "setcommands":
            render.emit(await _with_bot(args.username, lambda s: s.set_commands(value)))
        else:
            render.emit(await _with_bot(args.username, lambda s: s.set_info(**{field: value})))
        return

    if command == "info":
        render.emit(await _with_bot(args.username, lambda s: s.get_info()))
        return

    if command == "me":
        render.emit(await _with_bot(args.username, lambda s: s.whoami()))
        return

    if command == "menu":
        result = await _with_bot(args.username, lambda s: s.set_menu_button(
            text=args.text or "Открыть", url=args.url or "", reset=args.reset))
        render.emit(result)
        return

    if command == "rich-syntax":
        if render.pretty():
            render.console().print(tgx_rich.RICH_SYNTAX)
        else:
            print(tgx_rich.RICH_SYNTAX)
        return

    if command == "rich":
        blocks = None
        if getattr(args, "blocks", None):
            source = Path(args.blocks).expanduser()
            if not source.is_file():
                raise SystemExit(f"файла с блоками {source} нет")
            try:
                blocks = json.loads(source.read_text())
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{source}: не разбирается как JSON — {exc}")
            # Файлы подставляются по имени из --attach. Ссылка attach:// лежит
            # в поле, названном по типу блока: у документа в document, у видео
            # в video, у фотографии в photo.
            attached = dict(pair.partition("=")[::2] for pair in (args.attach or []))
            unused = set(attached)
            for block in blocks:
                holder = block.get(block.get("type") or "")
                if not isinstance(holder, dict):
                    continue
                name = str(holder.get("media", "")).removeprefix("attach://")
                if name in attached:
                    block["_upload"] = Path(attached[name]).expanduser()
                    unused.discard(name)
            if unused:
                # молчаливое несовпадение имён — самый частый способ отправить
                # блок без файла и не понять, почему он пустой
                raise SystemExit("в блоках нет ссылок attach:// на: " + ", ".join(sorted(unused)))
        markdown = Path(args.file).expanduser().read_text() if args.file and not blocks else (args.text or "")
        if args.as_blocks and markdown:
            # Сервер разбирает разметку сам, но принимает вложением только
            # фотографию. Свой перевод в блоки — единственный способ вложить видео.
            attached = dict(pair.partition("=")[::2] for pair in (args.attach or []))
            blocks = tgx_rich.blocks_from_markdown(markdown, attachments=attached)
            markdown = ""
        media = []
        for pair in args.media or []:
            name, _, source = pair.partition("=")
            if not source:
                raise SystemExit(f"вложение «{pair}»: нужно «имя=ссылка», имя — то же, что в tg://photo?id=")
            media.append(tgx_rich.photo_media(name.strip(), source.strip()))
        missing = set(tgx_rich.media_ids(markdown)) - {m["id"] for m in media}
        if missing:
            raise SystemExit(f"в тексте есть ссылки на медиа, которых нет в --media: {', '.join(sorted(missing))}")
        # Обратный случай: вложение передано, а ссылки на него в тексте нет —
        # Telegram такое молча игнорирует, поэтому ставим картинку в начало.
        for entry in media:
            reference = f"tg://photo?id={entry['id']}"
            if reference not in markdown:
                markdown = f"![]({reference})\n\n" + markdown
        bot = tgx_bots.Registry().get(args.bot)
        if not bot.token:
            raise SystemExit(f"у @{bot.username} нет токена — `tgx bot token @{bot.username}`")
        client = await make_client()
        try:
            await ensure_login(client)
            peer = await resolve_peer(client, args.peer)
            username = getattr(peer, "username", None)
            chat_id = f"@{username}" if username else (
                f"-100{peer.id}" if entity_kind(peer) in {"channel", "group"} else str(peer.id))
        finally:
            await client.disconnect()
        def publish() -> Any:
            return tgx_rich.send_rich(
                bot.token, chat_id, "" if blocks else markdown, blocks=blocks,
                buttons=tgx_bots.join_buttons(args.button), silent=args.silent,
                protect=args.protect,
                draft=args.draft, media=media, is_rtl=args.rtl, topic=args.topic,
                skip_entity_detection=args.no_autolinks)

        result = await asyncio.to_thread(publish)
        if blocks:
            render.note(tgx_rich.BLOCKS_ARE_WRITE_ONLY)
        render.emit({"ok": True, "message_id": result.get("message_id"), "chat": chat_id,
                     "as": bot.username, "draft": args.draft})
        return

    if command == "post":
        schedule = datetime.fromisoformat(args.schedule) if args.schedule else None

        async def publish(session: Any) -> Any:
            return await session.post(
                args.peer, args.text or "", buttons=tgx_bots.join_buttons(args.button),
                parse_mode=args.parse_mode,
                link_preview=not args.no_preview, silent=args.silent,
                files=args.file or None, schedule=schedule,
            )

        sent = await _with_bot(args.bot, publish)
        render.emit({"ok": True, "message_id": sent.id, "as": args.bot.lstrip("@"),
                     "scheduled_for": schedule.isoformat() if schedule else None})
        return

    raise SystemExit(f"неизвестная команда бота: {command}")


async def cmd_todo(args: argparse.Namespace) -> None:
    from telethon import helpers  # noqa: F401  (kept close to the TL types below)

    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        body_title = types.TextWithEntities(text=args.title, entities=[])
        todo = types.TodoList(
            title=body_title,
            list=[types.TodoItem(id=n + 1, title=types.TextWithEntities(text=text, entities=[]))
                  for n, text in enumerate(args.items)],
            others_can_append=not args.no_append,
            others_can_complete=not args.no_complete,
        )
        sent = await client.send_file(peer, types.InputMediaTodo(todo=todo))
        render.emit({"ok": True, "message_id": sent.id, "items": len(args.items)})
    finally:
        await client.disconnect()


async def cmd_todo_check(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        done = [int(i) for i in (args.done or "").split(",") if i.strip()]
        undone = [int(i) for i in (args.undone or "").split(",") if i.strip()]
        await client(functions.messages.ToggleTodoCompletedRequest(
            peer=peer, msg_id=args.id, completed=done, incompleted=undone))
        render.emit({"ok": True, "done": done, "undone": undone})
    finally:
        await client.disconnect()


async def cmd_todo_add(args: argparse.Namespace) -> None:
    client = await make_client()
    try:
        await ensure_login(client)
        peer = await resolve_peer(client, args.peer)
        current = await client.get_messages(peer, ids=args.id)
        existing = getattr(getattr(getattr(current, "media", None), "todo", None), "list", None) or []
        start = max((item.id for item in existing), default=0)
        await client(functions.messages.AppendTodoListRequest(
            peer=peer, msg_id=args.id,
            list=[types.TodoItem(id=start + n + 1, title=types.TextWithEntities(text=text, entities=[]))
                  for n, text in enumerate(args.items)]))
        render.emit({"ok": True, "added": len(args.items)})
    finally:
        await client.disconnect()


async def cmd_business(args: argparse.Namespace) -> None:
    """Telegram Business: бот-секретарь в личных чатах и всё вокруг него."""
    command = args.bizcmd
    client = await make_client()
    try:
        await ensure_login(client)
        business = tgx_business.Business(client)

        if command == "bots":
            rows = await business.connected_bots()
            for row in rows:
                try:
                    entity = await client.get_entity(row["bot_id"])
                    row["bot"] = f"@{entity.username}" if entity.username else str(row["bot_id"])
                except Exception:
                    row["bot"] = str(row["bot_id"])
                row["rights"] = ", ".join(row["rights"]) or "нет"
                since = row.pop("since", None)
                row["since"] = since.strftime("%Y-%m-%d") if since else ""
            if args.jsonl:
                print_jsonl(rows)
            else:
                print_table(rows, ["bot", "scope", "rights", "since"], title="подключённые боты")
            return

        if command == "connect":
            result = await business.connect(
                args.bot, tgx_business.parse_rights(args.rights),
                tgx_business.parse_scope(args.chats), args.exclude or [],
                replace=args.replace)
            render.emit({"ok": True, **result})
            return

        if command == "restore":
            render.emit({"ok": True, **await business.restore()})
            return

        if command == "disconnect":
            await business.disconnect(args.bot)
            render.emit({"ok": True, "disconnected": args.bot})
            return

        if command in {"pause", "resume"}:
            peer = await resolve_peer(client, args.peer)
            await business.pause(peer, paused=command == "pause")
            render.emit({"ok": True, "peer": args.peer, "paused": command == "pause"})
            return

        if command == "quick-replies":
            rows = await business.quick_replies()
            if args.jsonl:
                print_jsonl(rows)
            else:
                print_table(rows, ["shortcut_id", "shortcut", "messages"], title="быстрые ответы")
            return

        if command == "greeting":
            await business.set_greeting(None if args.off else args.shortcut,
                                        tgx_business.parse_scope(args.chats), args.after_days)
            render.emit({"ok": True, "greeting": None if args.off else args.shortcut})
            return

        if command == "away":
            await business.set_away(None if args.off else args.shortcut, args.schedule,
                                    tgx_business.parse_scope(args.chats), not args.always_send)
            render.emit({"ok": True, "away": None if args.off else args.shortcut})
            return

        if command == "hours":
            ranges = None if args.off else tgx_business.parse_hours(args.schedule)
            await business.set_hours(args.timezone, ranges)
            render.emit({"ok": True, "hours": tgx_business.describe_hours(ranges) if ranges else None})
            return

        if command == "intro":
            await business.set_intro(None if args.off else args.title, None if args.off else args.text)
            render.emit({"ok": True, "intro": None if args.off else {"title": args.title, "text": args.text}})
            return

        if command == "link":
            render.emit({"ok": True, **await business.create_link(args.message, args.title)})
            return

        if command == "links":
            rows = await business.links()
            if args.jsonl:
                print_jsonl(rows)
            else:
                print_table(rows, ["title", "link", "views"], title="деловые ссылки")
            return

        raise SystemExit(f"неизвестная команда: {command}")
    finally:
        await client.disconnect()


async def cmd_article(args: argparse.Namespace) -> None:
    """Publish markdown to telegra.ph — the page gets Instant View in Telegram."""
    command = args.artcmd

    if command == "account":
        account = await asyncio.to_thread(
            tgx_article.create_account, args.name, args.author, args.author_url)
        render.emit({"ok": True, "short_name": account.get("short_name"),
                     "token": tgx_article.mask(account.get("access_token", "")),
                     "saved_to": str(tgx_article.token_path())})
        return

    if command == "list":
        pages = await asyncio.to_thread(tgx_article.page_list, args.limit)
        rows = [{"path": p.get("path"), "title": p.get("title"), "views": p.get("views"),
                 "url": p.get("url")} for p in pages]
        if args.jsonl:
            print_jsonl(rows)
        else:
            print_table(rows, ["title", "views", "url"], title="статьи")
        return

    text = Path(args.file).expanduser().read_text() if args.file else (args.markdown or "")
    if not text.strip():
        raise SystemExit("нет текста: укажите --file или передайте markdown аргументом")

    if command == "edit":
        page = await asyncio.to_thread(tgx_article.edit_page, args.path, args.title, text, args.author)
    else:
        page = await asyncio.to_thread(tgx_article.create_page, args.title, text, args.author, args.author_url)
    result = {"ok": True, "url": page.get("url"), "path": page.get("path"), "title": page.get("title")}

    if getattr(args, "publish", None):
        link = page.get("url", "")
        if args.bot:
            api_id, api_hash = get_credentials()
            bot = tgx_bots.Registry().get(args.bot)
            async with tgx_bots.BotSession(bot, api_id, api_hash) as session:
                sent = await session.post(args.publish, link)
        else:
            client = await make_client()
            try:
                await ensure_login(client)
                peer = await resolve_peer(client, args.publish)
                sent = await client.send_message(peer, link)
            finally:
                await client.disconnect()
        result["published_to"] = args.publish
        result["message_id"] = sent.id
    render.emit(result)


async def cmd_ui(args: argparse.Namespace) -> None:
    """Launch the full-screen client."""
    import tgx_tui

    if not sys.stdout.isatty():
        raise SystemExit("tgx ui needs an interactive terminal (try `tgx dialogs` for piping)")
    if not args.no_splash:
        tgx_splash.play(args.effect)
    # Ask the terminal about image support now: textual-image can only get an
    # answer while stdin still belongs to us, before Textual starts reading it.
    import tgx_media

    detected = tgx_media.probe(args.media)
    api_id = api_hash = None
    if not args.demo:
        api_id, api_hash = get_credentials()
    await tgx_tui.run_async(
        SESSION,
        api_id,
        api_hash,
        demo=args.demo,
        theme=args.theme,
        mark_read=not args.no_mark_read,
        notifications=not args.no_notify,
        dialog_limit=args.limit,
        media=args.media,
        media_detected=detected,
    )


async def cmd_format(args: argparse.Namespace) -> None:
    """Show the markup cheat sheet, or preview how a given text will look."""
    if args.text:
        body, entities = tgx_format.parse(args.text, args.parse_mode)
        if render.pretty():
            render.console().print(tgx_format.render(body, entities, colors=render.PALETTE, reveal_spoilers=True))
        render.emit({"text": body, "entities": [
            {"type": type(e).__name__[len("MessageEntity"):].lower(), "offset": e.offset,
             "length": e.length, "url": getattr(e, "url", None)} for e in entities]})
        return
    if render.pretty():
        render.console().print(tgx_format.SYNTAX)
        render.console().print("\nhtml:  " + tgx_format.HTML_SYNTAX)
    else:
        print(tgx_format.SYNTAX)


async def cmd_banner(args: argparse.Namespace) -> None:
    if not tgx_splash.play(args.effect, force=args.force):
        tgx_splash.static()


GROUPS = [
    ("интерфейс", ["ui", "banner"]),
    ("боты и статьи", ["bot", "article", "business"]),
    ("форумы", ["forum", "guard", "poll", "boosts"]),
    ("платежи", ["pay", "confirm"]),
    ("голос", ["transcribe"]),
    ("люди", ["contacts", "stories"]),
    ("звонки", ["call"]),
    ("безопасность", ["security"]),
    ("аккаунт", ["auth", "me", "profile", "profile-get", "profile-edit", "profile-photo-set", "profile-photos", "profile-photo-delete"]),
    ("чаты и папки", ["dialogs", "folders", "folder-upsert"]),
    ("сообщения", ["history", "search", "send", "edit", "delete", "forward", "react", "pin", "pinned", "todo", "todo-check", "todo-add", "format", "export", "message-get", "message-click"]),
    ("каналы и статистика", ["stats", "stickers"]),
    ("каналы", ["channel-create", "channel-info", "channel-edit", "channel-slowmode",
                "channel-permissions", "channel-discussion", "chat-join", "chat-leave", "topics", "topic-create", "topic-edit", "topic-pin", "channel-photo-set", "channel-photo-delete", "channel-participants", "channel-admin-set", "channel-admin-remove", "channel-ban", "channel-unban", "channel-invite-add"]),
    ("админка", ["invite-export", "invite-list", "admin-log", "tl-schema"]),
]


def subcommand_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {a.dest: (a.help or "") for a in action._choices_actions}
    return {}


def overview(parser: argparse.ArgumentParser) -> None:
    """The no-arguments landing screen: banner plus a grouped command map."""
    helps = subcommand_help(parser)
    if not render.pretty():
        parser.print_help()
        return
    if not tgx_splash.play(os.environ.get("TGX_EFFECT") or "beams"):
        tgx_splash.static()
    from rich.columns import Columns
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = render.console()
    console.print()
    panels = []
    for title, names in GROUPS:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=f"bold {render.ACCENT}", no_wrap=True)
        grid.add_column(style=render.PALETTE["text"])
        for name in names:
            if name in helps:
                text = helps[name]
                grid.add_row(name, Text(text[:49] + "…" if len(text) > 50 else text, style=render.PALETTE["muted"]))
        panels.append(Panel(grid, title=Text(f" {title} ", style=f"bold {render.ACCENT}"), title_align="left", border_style="#2C3E50", padding=(0, 1)))
    console.print(Columns(panels, equal=False, expand=False))
    hint = Text()
    hint.append("  tgx ui", style=f"bold {render.ACCENT}")
    hint.append("            полноэкранный клиент      ", style=render.PALETTE["muted"])
    hint.append("tgx ui --demo", style=f"bold {render.ACCENT}")
    hint.append("   демо без входа в аккаунт", style=render.PALETTE["muted"])
    console.print(hint)
    console.print(Text("  tgx <команда> --help — подробности по любой команде\n", style=render.PALETTE["muted"]))


MEDIA_FILTERS = {
    "photo": "InputMessagesFilterPhotos", "video": "InputMessagesFilterVideo",
    "media": "InputMessagesFilterPhotoVideo", "file": "InputMessagesFilterDocument",
    "link": "InputMessagesFilterUrl", "voice": "InputMessagesFilterVoice",
    "music": "InputMessagesFilterMusic", "gif": "InputMessagesFilterGif",
    "round": "InputMessagesFilterRoundVideo", "mention": "InputMessagesFilterMyMentions",
    "pinned": "InputMessagesFilterPinned", "geo": "InputMessagesFilterGeo",
    "contact": "InputMessagesFilterContacts", "poll": "InputMessagesFilterPoll",
}


def parse_search_date(value: str) -> datetime | None:
    import tgx_tui

    return tgx_tui.parse_date(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Telethon-based Telegram CLI + TUI")
    p.add_argument("--version", action="version", version=f"tgx {VERSION}")
    p.add_argument("--plain", action="store_true", help="disable colour/boxes; emit machine-readable output")
    sub = p.add_subparsers(dest="cmd")

    ui = sub.add_parser("ui", help="полноэкранный TUI-клиент")
    ui.add_argument("--demo", action="store_true", help="демо-данные, без подключения к Telegram")
    ui.add_argument("--theme", default=os.environ.get("TGX_THEME", "tgx-night"), help="tgx-night | tgx-day | textual-dark | nord | gruvbox | catppuccin-mocha")
    ui.add_argument("--no-mark-read", action="store_true", help="не отмечать чаты прочитанными при открытии")
    ui.add_argument("--no-notify", action="store_true", help="без всплывающих уведомлений")
    ui.add_argument("--no-splash", action="store_true", help="без стартовой анимации")
    ui.add_argument("--effect", default=os.environ.get("TGX_EFFECT", "beams"), help="эффект заставки; см. tgx banner --list")
    ui.add_argument("--limit", type=int, default=0, help="сколько чатов грузить; 0 — все (по умолчанию)")
    ui.add_argument("--media", default=os.environ.get("TGX_MEDIA", "auto"),
                    choices=["auto", "tgp", "sixel", "halfcell", "unicode", "off"],
                    help="как рисовать превью: auto (по возможностям терминала), протокол явно, либо off")
    ui.set_defaults(func=cmd_ui)

    il = sub.add_parser("inline", help="чужие боты: инлайн-запросы, кнопки, мини-приложения")
    il_sub = il.add_subparsers(dest="inlinecmd", required=True)
    il.set_defaults(func=cmd_inline)

    i_ask = il_sub.add_parser("ask", help="спросить инлайн-бота, как через @бот запрос")
    i_ask.add_argument("bot")
    i_ask.add_argument("query", nargs="?", default="")
    i_ask.add_argument("--peer", help="для какого чата спрашиваем; по умолчанию избранное")
    i_ask.add_argument("--offset", help="страница из «дальше» прошлого ответа")

    i_snd = il_sub.add_parser("send", help="отправить выбранный результат")
    i_snd.add_argument("peer")
    i_snd.add_argument("query_id", type=int, help="метка из ask")
    i_snd.add_argument("result_id", help="id результата")
    i_snd.add_argument("--silent", action="store_true")
    i_snd.add_argument("--hide-via", action="store_true", help="без подписи «через бота»")
    i_snd.add_argument("--reply-to", type=int)

    i_st = il_sub.add_parser("start", help="запустить бота с параметром из ссылки")
    i_st.add_argument("bot")
    i_st.add_argument("param", nargs="?", default="")
    i_st.add_argument("--peer", help="где запускать; по умолчанию личка с ботом")

    i_pr = il_sub.add_parser("press", help="нажать кнопку и услышать ответ бота")
    i_pr.add_argument("peer")
    i_pr.add_argument("id", type=int, help="id сообщения с кнопками")
    i_pr.add_argument("data", help="данные кнопки")
    i_pr.add_argument("--hex", action="store_true", help="данные шестнадцатеричные")
    i_pr.add_argument("--password", action="store_true",
                      help="кнопка требует пароль двухфакторной защиты")

    il_sub.add_parser("attach-list", help="боты в меню вложений")

    i_at = il_sub.add_parser("attach", help="добавить бота в меню вложений или убрать")
    i_at.add_argument("bot")
    i_at.add_argument("state", choices=["on", "off"])
    i_at.add_argument("--allow-write", action="store_true", help="разрешить ему писать вам")

    i_wa = il_sub.add_parser("web-app", help="подписанный адрес мини-приложения бота")
    i_wa.add_argument("bot")
    i_wa.add_argument("--peer", help="от имени какого чата открывать")
    i_wa.add_argument("--url", help="конкретная страница приложения")
    i_wa.add_argument("--param", help="параметр запуска")
    i_wa.add_argument("--open", action="store_true", help="сразу открыть в браузере")

    sf = sub.add_parser("safety", help="блокировки, жалобы, уборка переписки")
    sf_sub = sf.add_subparsers(dest="safetycmd", required=True)
    sf.set_defaults(func=cmd_safety)

    def confirmable(parser):
        parser.add_argument("--confirm-to", help="кто подтверждает")
        parser.add_argument("--as", dest="bot", help="бот, который спросит")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    s_bl = confirmable(sf_sub.add_parser("block", help="заблокировать (требует подтверждения)"))
    s_bl.add_argument("who")
    s_bl.add_argument("--stories-only", action="store_true", help="закрыть только истории")

    s_ub = sf_sub.add_parser("unblock", help="разблокировать")
    s_ub.add_argument("who")
    s_ub.add_argument("--stories-only", action="store_true")

    s_bd = sf_sub.add_parser("blocked", help="кого вы заблокировали")
    s_bd.add_argument("--limit", type=int, default=100)
    s_bd.add_argument("--stories-only", action="store_true")

    s_br = confirmable(sf_sub.add_parser(
        "block-replier", help="заблокировать автора комментария (требует подтверждения)"))
    s_br.add_argument("id", type=int, help="id сообщения-ответа")
    s_br.add_argument("--delete", action="store_true", help="удалить это сообщение")
    s_br.add_argument("--wipe", action="store_true", help="стереть всю его переписку")
    s_br.add_argument("--spam", action="store_true", help="заодно пожаловаться")

    s_ps = sf_sub.add_parser("peer-settings", help="что Telegram думает о собеседнике")
    s_ps.add_argument("peer")

    s_hb = sf_sub.add_parser("hide-bar", help="убрать полоску над чатом")
    s_hb.add_argument("peer")

    s_rs = confirmable(sf_sub.add_parser(
        "report-spam", help="пожаловаться на спам (требует подтверждения)"))
    s_rs.add_argument("peer")

    s_rp = confirmable(sf_sub.add_parser(
        "report", help="жалоба по меню сервера: без варианта показывает меню"))
    s_rp.add_argument("peer")
    s_rp.add_argument("id", type=int, nargs="+")
    s_rp.add_argument("--option", help="ключ варианта из предыдущего шага")
    s_rp.add_argument("--comment", default="", help="комментарий, если сервер попросил")

    s_ch = confirmable(sf_sub.add_parser(
        "clear-history", help="стереть переписку (требует подтверждения)"))
    s_ch.add_argument("peer")
    s_ch.add_argument("--both-sides", action="store_true", help="и у собеседника тоже")
    s_ch.add_argument("--drop-chat", action="store_true", help="убрать чат из списка")

    s_up = confirmable(sf_sub.add_parser(
        "unpin-all", help="снять все закрепления (требует подтверждения)"))
    s_up.add_argument("peer")
    s_up.add_argument("--topic", type=int, help="только в этой теме форума")

    s_sp = sf_sub.add_parser("sponsored", help="показывать ли рекламу (скрыть — Premium)")
    s_sp.add_argument("state", choices=["on", "off"])

    ch = sub.add_parser("chan", help="каналы: адреса, вид, уборка, поиск по всему Telegram")
    ch_sub = ch.add_subparsers(dest="chancmd", required=True)
    ch.set_defaults(func=cmd_chan)

    def wants(parser):
        parser.add_argument("--confirm-to", help="кто подтверждает")
        parser.add_argument("--as", dest="bot", help="бот, который спросит")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    c_sp = ch_sub.add_parser("search-posts", help="искать по постам всего Telegram")
    c_sp.add_argument("query", nargs="?", default="")
    c_sp.add_argument("--hashtag")
    c_sp.add_argument("--limit", type=int, default=30)

    c_sq = ch_sub.add_parser("search-quota", help="сколько поисков осталось бесплатно")
    c_sq.add_argument("query", nargs="?", default="")

    c_fn = ch_sub.add_parser("free-name", help="свободен ли адрес")
    c_fn.add_argument("peer")
    c_fn.add_argument("name")

    c_un = ch_sub.add_parser("username", help="включить или выключить адрес")
    c_un.add_argument("peer")
    c_un.add_argument("name")
    c_un.add_argument("state", nargs="?", default="on", choices=["on", "off"])

    c_uo = ch_sub.add_parser("usernames-order", help="переставить адреса")
    c_uo.add_argument("peer")
    c_uo.add_argument("name", nargs="+")

    c_uf = wants(ch_sub.add_parser("usernames-off", help="погасить все адреса (подтверждение)"))
    c_uf.add_argument("peer")

    c_at = ch_sub.add_parser("autotranslate", help="кнопка перевода постов у читателей")
    c_at.add_argument("peer")
    c_at.add_argument("state", choices=["on", "off"])

    c_mt = ch_sub.add_parser("main-tab", help="что показывать первым в профиле")
    c_mt.add_argument("peer")
    c_mt.add_argument("tab", choices=sorted(tgx_chanadmin.TABS))

    c_st = ch_sub.add_parser("stickers", help="общий набор стикеров группы")
    c_st.add_argument("peer")
    c_st.add_argument("name", help="короткое имя набора или id:hash")
    c_st.add_argument("--emoji", action="store_true", help="набор эмодзи, а не стикеров")

    c_lo = ch_sub.add_parser("location", help="привязать группу к месту")
    c_lo.add_argument("peer")
    c_lo.add_argument("lat", type=float)
    c_lo.add_argument("lon", type=float)
    c_lo.add_argument("address")

    c_sa = ch_sub.add_parser("send-as", help="от чьего имени можно писать сюда")
    c_sa.add_argument("peer")
    c_sa.add_argument("--paid-reactions", action="store_true")

    c_pm = ch_sub.add_parser("paid-messages", help="звёзд за сообщение в группе")
    c_pm.add_argument("peer")
    c_pm.add_argument("stars", type=int, help="0 — бесплатно")
    c_pm.add_argument("--broadcast", action="store_true", help="разрешить рассылки")

    c_bb = ch_sub.add_parser("boost-bypass", help="сколько бустов снимает ограничения")
    c_bb.add_argument("peer")
    c_bb.add_argument("boosts", type=int)

    c_ha = ch_sub.add_parser("hide-ads", help="убрать рекламу из своего канала (за бусты)")
    c_ha.add_argument("peer")
    c_ha.add_argument("state", choices=["on", "off"])

    c_au = ch_sub.add_parser("author", help="кто из админов написал пост")
    c_au.add_argument("peer")
    c_au.add_argument("id", type=int)

    c_wp = wants(ch_sub.add_parser("wipe-participant",
                                   help="стереть всё, что человек написал (подтверждение)"))
    c_wp.add_argument("peer")
    c_wp.add_argument("who")

    c_cl = wants(ch_sub.add_parser("clear", help="стереть историю (подтверждение)"))
    c_cl.add_argument("peer")
    c_cl.add_argument("--up-to", type=int, help="до этого сообщения")
    c_cl.add_argument("--everyone", action="store_true", help="у всех участников")

    c_rs = ch_sub.add_parser("report-spam", help="пожаловаться на сообщения участника")
    c_rs.add_argument("peer")
    c_rs.add_argument("who")
    c_rs.add_argument("id", type=int, nargs="+")

    c_am = ch_sub.add_parser("antispam-mistake", help="антиспам зря удалил сообщение")
    c_am.add_argument("peer")
    c_am.add_argument("id", type=int)

    c_lf = ch_sub.add_parser("left", help="каналы, из которых вы вышли")
    c_lf.add_argument("--limit", type=int, default=50)

    ch_sub.add_parser("discussable", help="какие группы годятся в обсуждение канала")

    c_tb = wants(ch_sub.add_parser("to-broadcast",
                                   help="супергруппа → трансляция (подтверждение)"))
    c_tb.add_argument("peer")

    c_dl = wants(ch_sub.add_parser("delete", help="удалить канал (подтверждение)"))
    c_dl.add_argument("peer")

    gr = sub.add_parser("group", help="обычные группы, ветки обсуждений, «печатает…»")
    gr_sub = gr.add_subparsers(dest="groupcmd", required=True)
    gr.set_defaults(func=cmd_group)

    def asks(parser):
        parser.add_argument("--confirm-to", help="кто подтверждает")
        parser.add_argument("--as", dest="bot", help="бот, который спросит")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    g_typ = gr_sub.add_parser("typing", help="показать, что вы заняты: печатаете, шлёте файл…")
    g_typ.add_argument("peer")
    g_typ.add_argument("what", nargs="?", default="typing",
                       choices=sorted(tgx_groups.ACTIONS))
    g_typ.add_argument("--topic", type=int, help="в этой теме форума")
    g_typ.add_argument("--progress", type=int, help="доля выполненного для отправки файла")

    g_new = gr_sub.add_parser("new", help="завести обычную группу (не супергруппу)")
    g_new.add_argument("title")
    g_new.add_argument("who", nargs="+", help="кого позвать; в одиночку нельзя")
    g_new.add_argument("--ttl", type=int, help="через сколько секунд исчезают сообщения")

    g_add = gr_sub.add_parser("add", help="добавить человека")
    g_add.add_argument("peer")
    g_add.add_argument("who")
    g_add.add_argument("--history", type=int, default=0, help="сколько прошлых сообщений показать")

    g_rm = gr_sub.add_parser("remove", help="убрать человека")
    g_rm.add_argument("peer")
    g_rm.add_argument("who")
    g_rm.add_argument("--wipe", action="store_true", help="заодно стереть его сообщения")

    g_ren = gr_sub.add_parser("rename", help="сменить название")
    g_ren.add_argument("peer")
    g_ren.add_argument("title")

    g_adm = gr_sub.add_parser("admin", help="выдать или снять права администратора")
    g_adm.add_argument("peer")
    g_adm.add_argument("who")
    g_adm.add_argument("state", nargs="?", default="on", choices=["on", "off"])

    g_rank = gr_sub.add_parser("rank", help="звание администратора вместо слова «админ»")
    g_rank.add_argument("peer")
    g_rank.add_argument("who")
    g_rank.add_argument("title", nargs="?", default="")

    g_ho = asks(gr_sub.add_parser("hand-over", help="передать группу другому (подтверждение)"))
    g_ho.add_argument("peer")
    g_ho.add_argument("who")

    g_del = asks(gr_sub.add_parser("delete", help="удалить группу (подтверждение)"))
    g_del.add_argument("peer")

    g_up = asks(gr_sub.add_parser("upgrade", help="превратить в супергруппу (подтверждение)"))
    g_up.add_argument("peer")

    g_inf = gr_sub.add_parser("info", help="что за группа")
    g_inf.add_argument("peer")

    g_ttl = gr_sub.add_parser("ttl", help="через сколько исчезают сообщения в этом чате")
    g_ttl.add_argument("peer")
    g_ttl.add_argument("seconds", type=int, help="0, 86400, 604800 или 2678400")

    g_th = gr_sub.add_parser("thread", help="куда ведут комментарии под постом")
    g_th.add_argument("peer")
    g_th.add_argument("id", type=int)

    g_rep = gr_sub.add_parser("replies", help="ответы в ветке")
    g_rep.add_argument("peer")
    g_rep.add_argument("id", type=int)
    g_rep.add_argument("--limit", type=int, default=30)

    g_rt = gr_sub.add_parser("read-thread", help="пометить ветку прочитанной")
    g_rt.add_argument("peer")
    g_rt.add_argument("id", type=int)
    g_rt.add_argument("--up-to", type=int)

    nt = sub.add_parser("notify", help="уведомления, заявки на вступление, реакции")
    nt_sub = nt.add_subparsers(dest="notifycmd", required=True)
    nt.set_defaults(func=cmd_notify)

    SCOPES = ["users", "chats", "channels"]
    n_show = nt_sub.add_parser("show", help="как сейчас настроено")
    n_show.add_argument("peer", nargs="?", help="конкретный чат; без него — весь разряд")
    n_show.add_argument("--scope", default="users", choices=SCOPES)

    n_mute = nt_sub.add_parser("mute", help="заглушить: 30m, 2h, 3d, forever, off")
    n_mute.add_argument("span")
    n_mute.add_argument("peer", nargs="?")
    n_mute.add_argument("--scope", default="users", choices=SCOPES)
    n_mute.add_argument("--previews", action=argparse.BooleanOptionalAction,
                        help="показывать ли текст в уведомлении")
    n_mute.add_argument("--stories", action=argparse.BooleanOptionalAction,
                        help="звук у историй")

    nt_sub.add_parser("reset", help="сбросить все уведомления к исходным")

    n_re = nt_sub.add_parser("reactions", help="кто может уведомлять вас реакциями")
    n_re.add_argument("--from", dest="from_whom", choices=["all", "contacts"])
    n_re.add_argument("--previews", action=argparse.BooleanOptionalAction)

    n_nc = nt_sub.add_parser("new-contacts", help="сообщать ли о регистрации контактов")
    n_nc.add_argument("state", choices=["on", "off"])

    n_rq = nt_sub.add_parser("requests", help="кто из админов сколько ссылок наделал")
    n_rq.add_argument("peer")

    for name, help_text in (("approve", "принять заявку"), ("decline", "отклонить заявку")):
        parser = nt_sub.add_parser(name, help=help_text)
        parser.add_argument("peer")
        parser.add_argument("who")

    for name, help_text in (("approve-all", "принять все заявки"),
                            ("decline-all", "отклонить все заявки")):
        parser = nt_sub.add_parser(name, help=help_text)
        parser.add_argument("peer")
        parser.add_argument("--link", help="только по этой ссылке")

    n_ie = nt_sub.add_parser("invite-edit", help="поправить ссылку, не выпуская новую")
    n_ie.add_argument("peer")
    n_ie.add_argument("link")
    n_ie.add_argument("--title")
    n_ie.add_argument("--limit", type=int, help="сколько человек может войти")
    n_ie.add_argument("--expires", help="срок: 2h, 3d, forever")
    n_ie.add_argument("--request-needed", action=argparse.BooleanOptionalAction,
                      help="входить по заявке, а не сразу")
    n_ie.add_argument("--revoke", action="store_true", help="отозвать")

    n_ip = nt_sub.add_parser("invite-purge", help="выбросить отозванные ссылки")
    n_ip.add_argument("peer")
    n_ip.add_argument("--admin", help="чьи; по умолчанию свои")

    nt_sub.add_parser("emoji-list", help="какие реакции вообще есть")
    n_et = nt_sub.add_parser("emoji-top", help="какие ставят чаще всего")
    n_et.add_argument("--limit", type=int, default=20)

    n_ed = nt_sub.add_parser("emoji-default", help="ваша реакция по умолчанию")
    n_ed.add_argument("emoji")

    n_ea = nt_sub.add_parser("emoji-allow", help="что можно ставить в чате")
    n_ea.add_argument("peer")
    n_ea.add_argument("emoji", nargs="*", help="список, `all` — любые, пусто — запретить")
    n_ea.add_argument("--limit", type=int, help="сколько разных на сообщение")
    n_ea.add_argument("--paid", action=argparse.BooleanOptionalAction, help="платные звёздами")

    ai = sub.add_parser("ai", help="правка текста руками Telegram: вычитка, эмодзи, перевод, тон")
    ai_sub = ai.add_subparsers(dest="aicmd", required=True)
    ai.set_defaults(func=cmd_ai)

    a_comp = ai_sub.add_parser("compose", help="поправить текст и показать, что изменилось")
    a_comp.add_argument("text", help="текст или путь к файлу")
    a_comp.add_argument("--proofread", action="store_true", help="вычитать (по умолчанию)")
    a_comp.add_argument("--emojify", action="store_true", help="расставить эмодзи")
    a_comp.add_argument("--translate", help="перевести: код языка, например en")
    a_comp.add_argument("--tone", help=f"тон: {', '.join(tgx_ai.BUILT_IN)} или свой")
    a_comp.add_argument("--apply", help="сразу отправить готовый текст в этот чат")

    a_sum = ai_sub.add_parser("summarize", help="пересказать длинное сообщение")
    a_sum.add_argument("peer")
    a_sum.add_argument("id", type=int)
    a_sum.add_argument("--lang", help="пересказать на этом языке")
    a_sum.add_argument("--tone", help="каким тоном пересказывать")

    a_tr = ai_sub.add_parser("translate", help="перевести свой текст или чужие сообщения")
    a_tr.add_argument("lang", help="на какой язык: en, ru, de…")
    a_tr.add_argument("--text", help="свой текст")
    a_tr.add_argument("--peer", help="чат, если переводим сообщения")
    a_tr.add_argument("--id", type=int, action="append", help="какие сообщения; можно несколько")
    a_tr.add_argument("--tone", help="каким тоном переводить")

    a_at = ai_sub.add_parser("auto-translate", help="полоска «перевести» в чате")
    a_at.add_argument("peer")
    a_at.add_argument("state", choices=["on", "off"])

    a_dg = ai_sub.add_parser("digest", help="сводка по чату: что там было, коротко")
    a_dg.add_argument("peer")
    a_dg.add_argument("--limit", type=int, default=20, help="сколько сообщений просмотреть")
    a_dg.add_argument("--lang", help="сводку на этом языке")
    a_dg.add_argument("--long-at", type=int, default=400,
                      help="с какой длины сообщение считается длинным")

    ai_sub.add_parser("tones", help="какие тоны доступны")

    a_tone = ai_sub.add_parser("tone", help="что за тон")
    a_tone.add_argument("tone")

    a_ex = ai_sub.add_parser("tone-example", help="образец: как этот тон звучит")
    a_ex.add_argument("tone")
    a_ex.add_argument("--num", type=int, default=0, help="какой из образцов")

    a_new = ai_sub.add_parser("tone-new", help="завести свой тон письма")
    a_new.add_argument("title", help="название")
    a_new.add_argument("prompt", help="как писать: указание для сервера")
    a_new.add_argument("--emoji-id", type=int, default=0, help="премиальное эмодзи для значка")
    a_new.add_argument("--credit", action="store_true", help="показывать вас автором")

    a_ted = ai_sub.add_parser("tone-edit", help="поправить свой тон")
    a_ted.add_argument("tone")
    a_ted.add_argument("--title")
    a_ted.add_argument("--prompt")
    a_ted.add_argument("--emoji-id", type=int)
    a_ted.add_argument("--credit", action=argparse.BooleanOptionalAction)

    for name, help_text in (("tone-save", "поставить себе чужой тон"),
                            ("tone-forget", "убрать тон из своих")):
        parser = ai_sub.add_parser(name, help=help_text)
        parser.add_argument("tone")

    a_del = ai_sub.add_parser("tone-delete",
                              help="удалить свой тон насовсем (требует подтверждения)")
    a_del.add_argument("tone")
    a_del.add_argument("--confirm-to", help="кто подтверждает")
    a_del.add_argument("--as", dest="bot", help="бот, который спросит")
    a_del.add_argument("--timeout", type=float, default=300.0)

    bot = sub.add_parser("bot", help="боты: создание через BotFather, токены, посты от их имени")
    bot_sub = bot.add_subparsers(dest="botcmd", required=True)
    bot.set_defaults(func=cmd_bot)

    b_create = bot_sub.add_parser("create", help="создать бота через BotFather")
    b_create.add_argument("name", help="человеческое имя")
    b_create.add_argument("username", help="адрес, обязан заканчиваться на bot")
    b_create.add_argument("--reveal", action="store_true", help="показать токен целиком")
    b_create.add_argument("--manager", help="бот-управляющий: заведёт нового бота вместо "
                                            "BotFather; нужно право «управлять ботами»")

    b_list = bot_sub.add_parser("list", help="сохранённые боты (токены скрыты)")
    b_list.add_argument("--reveal", action="store_true")
    b_list.add_argument("--jsonl", action="store_true")

    for name, help_text in (("token", "получить токен у BotFather и сохранить"),
                            ("revoke", "отозвать токен и сохранить новый")):
        parser = bot_sub.add_parser(name, help=help_text)
        parser.add_argument("username")
        parser.add_argument("--reveal", action="store_true")
        parser.add_argument("--via-manager", help="забрать токен из сессии бота-управляющего")

    b_cmds = bot_sub.add_parser("commands", help="команды бота, как их видит пользователь")
    b_cmds.add_argument("username")
    b_cmds.add_argument("--lang", default="", help="код языка, например ru")

    b_mget = bot_sub.add_parser("menu-get", help="какая сейчас кнопка-меню")
    b_mget.add_argument("username")
    b_mget.add_argument("--user", help="для кого смотреть; по умолчанию общая")

    b_prev = bot_sub.add_parser("previews", help="картинки-превью бота до запуска")
    b_prev.add_argument("username")

    b_rights = bot_sub.add_parser(
        "group-rights", help="какие права бот просит при добавлении в группу")
    b_rights.add_argument("username")
    b_rights.add_argument("--channel", action="store_true", help="права для канала, не группы")
    for flag, help_text in (("invite", "приглашать"), ("pin", "закреплять"),
                            ("delete", "удалять сообщения"), ("ban", "банить"),
                            ("info", "менять описание")):
        b_rights.add_argument(f"--{flag}", action="store_true", help=help_text)

    b_pin = bot_sub.add_parser("previews-info", help="что стоит на витрине бота")
    b_pin.add_argument("username")
    b_pin.add_argument("--lang", help="для этого языка")

    b_pa = bot_sub.add_parser("preview-add", help="добавить превью (публичный адрес)")
    b_pa.add_argument("username")
    b_pa.add_argument("url")
    b_pa.add_argument("--lang")

    b_pr = bot_sub.add_parser("preview-remove", help="убрать превью")
    b_pr.add_argument("username")
    b_pr.add_argument("url", nargs="+")
    b_pr.add_argument("--lang")

    b_pw = bot_sub.add_parser("preview-swap", help="заменить одно превью другим")
    b_pw.add_argument("username")
    b_pw.add_argument("old")
    b_pw.add_argument("new")
    b_pw.add_argument("--lang")

    b_po = bot_sub.add_parser("preview-order", help="переставить превью")
    b_po.add_argument("username")
    b_po.add_argument("url", nargs="+", help="в нужном порядке")
    b_po.add_argument("--lang")

    b_ac = bot_sub.add_parser("access", help="кому бот отвечает; без ключей — показать")
    b_ac.add_argument("username")
    b_ac.add_argument("--restricted", action=argparse.BooleanOptionalAction,
                      help="только по списку")
    b_ac.add_argument("--allow", action="append", help="добавить в список; можно повторять")

    b_sim = bot_sub.add_parser("similar", help="похожие боты")
    b_sim.add_argument("username")

    b_pop = bot_sub.add_parser("popular", help="какие мини-приложения сейчас смотрят")
    b_pop.add_argument("--limit", type=int, default=20)

    b_fn = bot_sub.add_parser("free-name", help="свободен ли адрес для бота")
    b_fn.add_argument("username")

    b_un = bot_sub.add_parser("username", help="включить или выключить один из адресов")
    b_un.add_argument("username")
    b_un.add_argument("name")
    b_un.add_argument("state", nargs="?", default="on", choices=["on", "off"])

    b_uo = bot_sub.add_parser("usernames-order", help="переставить адреса бота")
    b_uo.add_argument("username")
    b_uo.add_argument("name", nargs="+", help="в нужном порядке")

    b_ref = bot_sub.add_parser("referrals", help="партнёрская программа: доля в тысячных")
    b_ref.add_argument("username")
    b_ref.add_argument("permille", type=int, help="0–1000; 150 — это 15%%")
    b_ref.add_argument("--months", type=int, help="срок; без него бессрочно")

    b_rc = bot_sub.add_parser("reset-commands", help="убрать команды бота")
    b_rc.add_argument("username")
    b_rc.add_argument("--lang")

    b_ep = bot_sub.add_parser("emoji-permission", help="пускать ли бота к вашему эмодзи-статусу")
    b_ep.add_argument("username")
    b_ep.add_argument("state", choices=["on", "off"])

    b_cw = bot_sub.add_parser("can-write", help="может ли бот писать вам первым")
    b_cw.add_argument("username")
    b_cw.add_argument("--allow-write", action="store_true", help="разрешить")

    b_secr = bot_sub.add_parser(
        "secretary", help="секретарский режим — без него бота не подключить к личным чатам")
    b_secr.add_argument("username")
    b_secr.add_argument("state", nargs="?", default="on", choices=["on", "off"])

    bot_sub.add_parser("mine", help="каких ботов знает BotFather")
    b_forget = bot_sub.add_parser("forget", help="убрать бота из локального реестра")
    b_forget.add_argument("username")

    for name, help_text in (("setname", "сменить имя"), ("setabout", "текст «о боте»"),
                            ("setdescription", "описание"), ("setcommands", "список команд или путь к файлу")):
        parser = bot_sub.add_parser(name, help=help_text)
        parser.add_argument("username")
        parser.add_argument("value")
        parser.add_argument("--via-botfather", action="store_true",
                            help="через диалог с BotFather вместо прямого API")

    b_info = bot_sub.add_parser("info", help="имя, описание и «о боте» — как их видит API")
    b_info.add_argument("username")

    b_me = bot_sub.add_parser("me", help="кто этот бот (вход по токену)")
    b_me.add_argument("username")

    b_menu = bot_sub.add_parser("menu", help="кнопка-меню бота: мини-приложение")
    b_menu.add_argument("username")
    b_menu.add_argument("--text", default="Открыть")
    b_menu.add_argument("--url", help="https-адрес мини-приложения")
    b_menu.add_argument("--reset", action="store_true", help="вернуть меню по умолчанию")

    b_post = bot_sub.add_parser("post", help="опубликовать от имени бота, с кнопками")
    b_post.add_argument("peer")
    b_post.add_argument("text", nargs="?", default="")
    b_post.add_argument("--as", dest="bot", required=True, help="от имени какого бота")
    b_post.add_argument("--button", action="append",
                        help="кнопки: «Текст=https://…, Ещё=webapp:https://…»; можно повторять — каждый флаг даёт свой ряд")
    b_post.add_argument("--file", action="append")
    b_post.add_argument("--parse-mode", choices=list(tgx_format.MODES), default="md")
    b_post.add_argument("--no-preview", action="store_true")
    b_post.add_argument("--silent", action="store_true")
    b_post.add_argument("--schedule")

    b_rich = bot_sub.add_parser("rich", help="богатое сообщение: заголовки, таблицы, чек-листы, сноски")
    b_rich.add_argument("peer")
    b_rich.add_argument("text", nargs="?", default="", help="разметка; либо --file")
    b_rich.add_argument("--as", dest="bot", required=True)
    b_rich.add_argument("--file", help="файл с разметкой")
    b_rich.add_argument("--button", action="append",
                        help="кнопки под сообщением; можно повторять — каждый флаг даёт свой ряд")
    b_rich.add_argument("--media", action="append",
                        help="имя=ссылка для ![](tg://photo?id=имя); можно повторять")
    b_rich.add_argument("--topic", type=int, help="id темы форума")
    b_rich.add_argument("--blocks", help="файл JSON с блоками (Bot API 10.2+); "
                                        "кнопки и файлы внутри документа возможны только так")
    b_rich.add_argument("--attach", action="append", metavar="ИМЯ=ПУТЬ",
                        help="файл для блока с attach://ИМЯ; можно повторять")
    b_rich.add_argument("--as-blocks", action="store_true",
                        help="перевести разметку в блоки у себя, а не на сервере — "
                             "только так в сообщение кладётся видео")
    b_rich.add_argument("--draft", action="store_true", help="отправить черновиком (стриминг)")
    b_rich.add_argument("--silent", action="store_true")
    b_rich.add_argument("--protect", action="store_true", help="запретить пересылку и копирование")
    b_rich.add_argument("--rtl", action="store_true", help="справа налево")
    b_rich.add_argument("--no-autolinks", action="store_true",
                        help="не превращать ссылки и упоминания в сущности автоматически")

    bot_sub.add_parser("rich-syntax", help="шпаргалка по разметке богатых сообщений")

    bot_sub.add_parser("buttons", help="шпаргалка по кнопкам")

    td = sub.add_parser("todo", help="отправить чек-лист")
    td.add_argument("peer")
    td.add_argument("title")
    td.add_argument("items", nargs="+")
    td.add_argument("--no-append", action="store_true", help="запретить другим дописывать")
    td.add_argument("--no-complete", action="store_true", help="запретить другим отмечать")
    td.set_defaults(func=cmd_todo)

    tdc = sub.add_parser("todo-check", help="отметить или снять пункты чек-листа")
    tdc.add_argument("peer")
    tdc.add_argument("id", type=int)
    tdc.add_argument("--done", help="номера пунктов через запятую")
    tdc.add_argument("--undone", help="номера пунктов через запятую")
    tdc.set_defaults(func=cmd_todo_check)

    tda = sub.add_parser("todo-add", help="дописать пункты в чек-лист")
    tda.add_argument("peer")
    tda.add_argument("id", type=int)
    tda.add_argument("items", nargs="+")
    tda.set_defaults(func=cmd_todo_add)

    biz = sub.add_parser("business", help="бизнес-режим: бот-секретарь, часы работы, автоответы")
    biz_sub = biz.add_subparsers(dest="bizcmd", required=True)
    biz.set_defaults(func=cmd_business)

    z_bots = biz_sub.add_parser("bots", help="какие боты подключены к личным чатам")
    z_bots.add_argument("--jsonl", action="store_true")

    z_conn = biz_sub.add_parser("connect", help="подключить бота к своим личным чатам")
    z_conn.add_argument("bot")
    z_conn.add_argument("--rights", default="reply,read_messages",
                        help="через запятую или all/none; см. tgx business rights")
    z_conn.add_argument("--chats", default="all", choices=list(tgx_business.SCOPES))
    z_conn.add_argument("--exclude", action="append", help="кого не трогать; можно повторять")
    z_conn.add_argument("--replace", action="store_true",
                        help="отключить уже подключённого бота — Telegram держит только одного")

    biz_sub.add_parser(
        "restore", help="вернуть бота, которого вытеснил --replace, с прежними настройками")

    z_disc = biz_sub.add_parser("disconnect", help="отключить бота")
    z_disc.add_argument("bot")

    for name, help_text in (("pause", "приостановить бота в конкретном чате"),
                            ("resume", "вернуть бота в чат")):
        parser = biz_sub.add_parser(name, help=help_text)
        parser.add_argument("peer")

    z_qr = biz_sub.add_parser("quick-replies", help="быстрые ответы и их id")
    z_qr.add_argument("--jsonl", action="store_true")

    z_greet = biz_sub.add_parser("greeting", help="приветствие новым собеседникам")
    z_greet.add_argument("--shortcut", type=int, help="id быстрого ответа")
    z_greet.add_argument("--chats", default="all", choices=list(tgx_business.SCOPES))
    z_greet.add_argument("--after-days", type=int, default=7, help="после скольких дней тишины считать чат новым")
    z_greet.add_argument("--off", action="store_true")

    z_away = biz_sub.add_parser("away", help="автоответ в нерабочее время")
    z_away.add_argument("--shortcut", type=int)
    z_away.add_argument("--schedule", default="outside", choices=["outside", "always"])
    z_away.add_argument("--chats", default="all", choices=list(tgx_business.SCOPES))
    z_away.add_argument("--always-send", action="store_true", help="отвечать даже когда вы онлайн")
    z_away.add_argument("--off", action="store_true")

    z_hours = biz_sub.add_parser("hours", help="часы работы: «пн-пт 9:00-18:00; сб 10:00-14:00»")
    z_hours.add_argument("schedule", nargs="?", default="")
    z_hours.add_argument("--timezone", default="Europe/Moscow")
    z_hours.add_argument("--off", action="store_true")

    z_intro = biz_sub.add_parser("intro", help="приветственный экран профиля")
    z_intro.add_argument("--title")
    z_intro.add_argument("--text")
    z_intro.add_argument("--off", action="store_true")

    z_link = biz_sub.add_parser("link", help="деловая ссылка с заготовленным текстом")
    z_link.add_argument("message")
    z_link.add_argument("--title")

    z_links = biz_sub.add_parser("links", help="список деловых ссылок")
    z_links.add_argument("--jsonl", action="store_true")

    art = sub.add_parser("article", help="статьи на telegra.ph из маркдауна (Instant View)")
    art_sub = art.add_subparsers(dest="artcmd", required=True)
    art.set_defaults(func=cmd_article)

    a_acc = art_sub.add_parser("account", help="создать аккаунт telegra.ph и сохранить токен")
    a_acc.add_argument("--name", required=True, help="короткое имя, видно только вам")
    a_acc.add_argument("--author", help="подпись автора под статьями")
    a_acc.add_argument("--author-url", help="ссылка с подписи автора")

    a_new = art_sub.add_parser("new", help="опубликовать статью")
    a_new.add_argument("title")
    a_new.add_argument("markdown", nargs="?", help="текст; либо --file")
    a_new.add_argument("--file", help="файл с маркдауном")
    a_new.add_argument("--author")
    a_new.add_argument("--author-url")
    a_new.add_argument("--publish", help="сразу отправить ссылку в этот чат")
    a_new.add_argument("--as", dest="bot", help="отправить ссылку от имени бота")

    a_edit = art_sub.add_parser("edit", help="переписать статью")
    a_edit.add_argument("path", help="путь статьи, например Zagolovok-08-28")
    a_edit.add_argument("title")
    a_edit.add_argument("markdown", nargs="?")
    a_edit.add_argument("--file")
    a_edit.add_argument("--author")
    a_edit.add_argument("--publish")
    a_edit.add_argument("--as", dest="bot")

    a_list = art_sub.add_parser("list", help="ваши статьи")
    a_list.add_argument("--limit", type=int, default=50)
    a_list.add_argument("--jsonl", action="store_true")

    fm = sub.add_parser("format", help="шпаргалка по разметке или разбор текста в сущности")
    fm.add_argument("text", nargs="?", help="текст для разбора; без него — шпаргалка")
    fm.add_argument("--parse-mode", choices=list(tgx_format.MODES), default="md")
    fm.set_defaults(func=cmd_format)

    bn = sub.add_parser("banner", help="проиграть анимированную заставку")
    bn.add_argument("--effect", default="random", help="имя эффекта или random")
    bn.add_argument("--force", action="store_true", help="играть даже без TTY")
    bn.add_argument("--list", action="store_true", help="показать доступные эффекты")
    bn.set_defaults(func=cmd_banner)

    sub.add_parser("auth", help="log in and save local Telegram session").set_defaults(func=cmd_auth)
    sub.add_parser("me", help="show the logged-in account").set_defaults(func=cmd_me)

    d = sub.add_parser("dialogs", help="list chats, channels, groups, and users")
    d.add_argument("--limit", type=int, default=50, help="0 — все чаты")
    d.add_argument("--jsonl", action="store_true")
    d.set_defaults(func=cmd_dialogs)

    f = sub.add_parser("folders", help="list Telegram chat folders/dialog filters")
    f.add_argument("--jsonl", action="store_true")
    f.set_defaults(func=cmd_folders)

    fu = sub.add_parser("folder-upsert", help="create or replace a Telegram chat folder/dialog filter")
    fu.add_argument("title", help="folder title, e.g. AI")
    fu.add_argument("--id", type=int, help="optional folder id; otherwise existing title or first free id is used")
    fu.add_argument("--peer", action="append", help="peer to include; can be repeated")
    fu.add_argument("--match-regex", help="include dialogs whose title or username matches this regex")
    fu.add_argument("--exclude", action="append", help="id, username, or exact title to exclude from --match-regex; can be repeated")
    fu.set_defaults(func=cmd_folder_upsert)

    cf = sub.add_parser("confirm", help="спросить человека кнопкой в Telegram и дождаться ответа")
    cf.add_argument("title", help="что собираемся сделать")
    cf.add_argument("--details", help="подробности: суммы, имена, количество")
    cf.add_argument("--danger", help="чем это необратимо")
    cf.add_argument("--to", required=True, help="кого спрашиваем")
    cf.add_argument("--as", dest="bot", required=True, help="бот, который спросит")
    cf.add_argument("--timeout", type=float, default=tgx_confirm.DEFAULT_TIMEOUT,
                    help="сколько ждать ответа, секунд")
    cf.set_defaults(func=cmd_confirm)

    cl = sub.add_parser("call", help="групповые звонки: управление и живая страница")
    cl_sub = cl.add_subparsers(dest="callcmd", required=True)
    cl.set_defaults(func=cmd_call)

    def call_cmd(name: str, help_text: str) -> Any:
        parser = cl_sub.add_parser(name, help=help_text)
        parser.add_argument("chat")
        return parser

    c_st = call_cmd("start", "начать голосовой чат")
    c_st.add_argument("--title")
    c_st.add_argument("--rtmp", action="store_true", help="под трансляцию")

    call_cmd("info", "что происходит в звонке")
    call_cmd("start-scheduled", "начать назначенный звонок")
    call_cmd("stars", "сколько звёзд собрал звонок")

    c_pt = call_cmd("participants", "кто сейчас в звонке")
    c_pt.add_argument("--limit", type=int, default=50)
    c_pt.add_argument("--jsonl", action="store_true")

    c_ja = call_cmd("join-as", "от чьего имени можно войти")
    c_ja.add_argument("--jsonl", action="store_true")

    c_ln = call_cmd("link", "ссылка на звонок")
    c_ln.add_argument("--speaker", action="store_true", help="с правом говорить")

    c_iv = call_cmd("invite", "позвать в звонок")
    c_iv.add_argument("user", nargs="+")

    c_mt = call_cmd("mute", "заглушить участника")
    c_mt.add_argument("user")
    c_mt.add_argument("--unmute", action="store_true", help="вернуть слово")
    c_mt.add_argument("--volume", type=int, help="громкость, 0–200")

    c_hd = call_cmd("hand", "поднять руку")
    c_hd.add_argument("--down", action="store_true", help="опустить")

    c_ti = call_cmd("title", "переименовать звонок")
    c_ti.add_argument("text")

    c_rc = call_cmd("record", "запись звонка")
    c_rc.add_argument("--stop", action="store_true", help="остановить запись")
    c_rc.add_argument("--title")
    c_rc.add_argument("--video", action="store_true")
    c_rc.add_argument("--portrait", action="store_true", help="вертикальное видео")

    c_se = call_cmd("settings", "настройки звонка")
    c_se.add_argument("--join-muted", help="входят заглушёнными: on|off")
    c_se.add_argument("--messages", help="чат внутри звонка: on|off")
    c_se.add_argument("--reset-link", action="store_true", help="обновить ссылку")

    c_sy = call_cmd("say", "написать в чат звонка")
    c_sy.add_argument("text")

    c_su = call_cmd("stream-url", "адрес и ключ для трансляции")
    c_su.add_argument("--revoke", action="store_true", help="сменить ключ")

    c_wt = call_cmd("watch", "живая страница участников (Ctrl+C — остановить)")
    c_wt.add_argument("--every", type=float, default=2.0, help="как часто опрашивать, секунд")
    c_wt.add_argument("--open", action="store_true", help="открыть в браузере сразу")

    c_en = call_cmd("end", "завершить звонок (требует подтверждения)")
    c_en.add_argument("--confirm-to")
    c_en.add_argument("--as", dest="bot")
    c_en.add_argument("--timeout", type=float, default=300.0)

    sec = sub.add_parser("security", help="сессии, приватность, сроки")
    sec_sub = sec.add_subparsers(dest="seccmd", required=True)
    sec.set_defaults(func=cmd_security)

    for name, help_text in (("sessions", "устройства, вошедшие в аккаунт"),
                            ("websites", "сайты, куда входили через Telegram")):
        parser = sec_sub.add_parser(name, help=help_text)
        parser.add_argument("--jsonl", action="store_true")

    e_pr = sec_sub.add_parser("privacy", help="кто что о вас видит")
    e_pr.add_argument("topic", nargs="?", choices=sorted(tgx_security.TOPICS),
                      help="без предмета — сводка по всем")
    e_pr.add_argument("--jsonl", action="store_true")

    e_sp = sec_sub.add_parser("set-privacy", help="изменить приватность предмета")
    e_sp.add_argument("topic", choices=sorted(tgx_security.TOPICS))
    e_sp.add_argument("audience", choices=list(tgx_security.AUDIENCES))
    e_sp.add_argument("--allow", action="append", help="плюс эти люди")
    e_sp.add_argument("--deny", action="append", help="кроме этих")

    e_gp = sec_sub.add_parser("global-privacy", help="настройки на весь аккаунт")
    e_gp.add_argument("--archive", help="архивировать новые чаты: on|off")
    e_gp.add_argument("--hide-read", help="скрывать статус прочтения: on|off")
    e_gp.add_argument("--premium-only", help="писать могут только Premium и контакты: on|off")

    e_st = sec_sub.add_parser("session-ttl", help="дней бездействия до выхода")
    e_st.add_argument("days", nargs="?", type=int, help="без числа — показать текущее")

    e_at = sec_sub.add_parser("account-ttl", help="дней бездействия до удаления аккаунта")
    e_at.add_argument("days", nargs="?", type=int)

    e_ss = sec_sub.add_parser("session-settings", help="что разрешено сессии")
    e_ss.add_argument("hash", type=int)
    e_ss.add_argument("--calls", help="разрешить звонки: on|off")
    e_ss.add_argument("--secret", help="разрешить секретные чаты: on|off")

    e_ne = sec_sub.add_parser("notify-exceptions", help="чаты с отдельными уведомлениями")
    e_ne.add_argument("--limit", type=int, default=30)
    e_ne.add_argument("--jsonl", action="store_true")

    def sec_gate(name: str, help_text: str) -> Any:
        parser = sec_sub.add_parser(name, help=help_text + " (требует подтверждения)")
        parser.add_argument("--confirm-to")
        parser.add_argument("--as", dest="bot")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    e_cs = sec_gate("close-session", "завершить сессию")
    e_cs.add_argument("hash", type=int)
    e_cw = sec_gate("close-website", "отозвать доступ у сайта")
    e_cw.add_argument("hash", type=int)
    sec_gate("close-all-websites", "отозвать доступ у всех сайтов")

    pd = sub.add_parser("pending", help="черновики, отложенные, заготовки, закладки")
    pd_sub = pd.add_subparsers(dest="pendcmd", required=True)
    pd.set_defaults(func=cmd_pending)

    for name, help_text in (("drafts", "все черновики"), ("shortcuts", "быстрые ответы"),
                            ("tags", "метки в избранном")):
        parser = pd_sub.add_parser(name, help=help_text)
        parser.add_argument("--jsonl", action="store_true")

    d_sv = pd_sub.add_parser("saved", help="избранное по авторам")
    d_sv.add_argument("--limit", type=int, default=30)
    d_sv.add_argument("--jsonl", action="store_true")

    d_dr = pd_sub.add_parser("draft", help="сохранить черновик; пустой текст стирает")
    d_dr.add_argument("chat")
    d_dr.add_argument("text", nargs="?", default="")
    d_dr.add_argument("--reply-to", type=int)
    d_dr.add_argument("--no-preview", action="store_true")

    d_sc = pd_sub.add_parser("scheduled", help="что уйдёт само и когда")
    d_sc.add_argument("chat")
    d_sc.add_argument("--jsonl", action="store_true")

    for name, help_text in (("send-now", "отправить отложенное немедленно"),
                            ("cancel", "отменить отложенное")):
        parser = pd_sub.add_parser(name, help=help_text)
        parser.add_argument("chat")
        parser.add_argument("id", nargs="+", type=int)

    d_sh = pd_sub.add_parser("shortcut", help="сообщения внутри заготовки")
    d_sh.add_argument("id", type=int)
    d_sh.add_argument("--jsonl", action="store_true")

    d_ss = pd_sub.add_parser("send-shortcut", help="отправить заготовку в чат")
    d_ss.add_argument("chat")
    d_ss.add_argument("id", type=int)

    d_rs = pd_sub.add_parser("rename-shortcut", help="переименовать заготовку")
    d_rs.add_argument("id", type=int)
    d_rs.add_argument("name")

    d_nt = pd_sub.add_parser("name-tag", help="назвать метку в избранном")
    d_nt.add_argument("emoji")
    d_nt.add_argument("title", nargs="?")

    d_fc = pd_sub.add_parser("fact-check", help="проверка фактов на сообщении")
    d_fc.add_argument("chat")
    d_fc.add_argument("id", type=int)

    d_cd = pd_sub.add_parser("clear-drafts", help="стереть все черновики (требует подтверждения)")
    d_cd.add_argument("--confirm-to")
    d_cd.add_argument("--as", dest="bot")
    d_cd.add_argument("--timeout", type=float, default=300.0)

    d_ds = pd_sub.add_parser("delete-shortcut", help="удалить заготовку (требует подтверждения)")
    d_ds.add_argument("id", type=int)
    d_ds.add_argument("--confirm-to")
    d_ds.add_argument("--as", dest="bot")
    d_ds.add_argument("--timeout", type=float, default=300.0)

    stt = sub.add_parser("stats", help="статистика каналов, групп, постов и историй")
    stt_sub = stt.add_subparsers(dest="statscmd", required=True)
    stt.set_defaults(func=cmd_stats)
    for name, help_text in (("channel", "сводка по каналу"), ("group", "сводка по группе")):
        parser = stt_sub.add_parser(name, help=help_text)
        parser.add_argument("chat")
    for name, help_text in (("message", "статистика поста"), ("story", "статистика истории")):
        parser = stt_sub.add_parser(name, help=help_text)
        parser.add_argument("chat")
        parser.add_argument("id", type=int)
    t_fw = stt_sub.add_parser("forwards", help="кто публично переслал пост")
    t_fw.add_argument("chat")
    t_fw.add_argument("id", type=int)
    t_fw.add_argument("--limit", type=int, default=20)
    t_fw.add_argument("--jsonl", action="store_true")
    t_gr = stt_sub.add_parser("graph", help="догрузить один график по имени")
    t_gr.add_argument("chat")
    t_gr.add_argument("name", help="имя из списка «графики»")
    t_gr.add_argument("--kind", default="channel", choices=["channel", "group", "message"])
    t_gr.add_argument("--id", type=int, help="номер сообщения для kind=message")

    sk = sub.add_parser("stickers", help="свои наборы стикеров")
    sk_sub = sk.add_subparsers(dest="stickcmd", required=True)
    sk.set_defaults(func=cmd_stickers)

    k_mine = sk_sub.add_parser("mine", help="наборы, которые вы сделали")
    k_mine.add_argument("--limit", type=int, default=50)

    sk_sub.add_parser("installed", help="какие наборы у вас установлены")

    k_fs = sk_sub.add_parser("find-sets", help="найти набор по названию")
    k_fs.add_argument("query")
    k_fs.add_argument("--installed-only", action="store_true", help="без рекомендованных")

    k_find = sk_sub.add_parser("find", help="найти отдельные стикеры")
    k_find.add_argument("query", nargs="?", default="", help="слова")
    k_find.add_argument("--emoji", help="по эмодзи")
    k_find.add_argument("--custom-emoji", action="store_true", help="искать эмодзи, а не стикеры")
    k_find.add_argument("--limit", type=int, default=20)

    sk_sub.add_parser("faved", help="избранные стикеры")
    sk_sub.add_parser("recent", help="недавние стикеры")

    k_fav = sk_sub.add_parser("fave", help="в избранное или обратно")
    k_fav.add_argument("key", help="ключ вида «число:число» из find")
    k_fav.add_argument("--remove", action="store_true")

    k_ins = sk_sub.add_parser("install", help="поставить набор себе или убрать")
    k_ins.add_argument("name", help="короткое имя или ссылка t.me/addstickers/…")
    k_ins.add_argument("--remove", action="store_true")

    k_snd = sk_sub.add_parser("send", help="отправить существующий стикер")
    k_snd.add_argument("peer")
    k_snd.add_argument("key", help="ключ вида «число:число» из find")
    k_snd.add_argument("--reply-to", type=int)

    k_show = sk_sub.add_parser("show", help="что внутри набора")
    k_show.add_argument("name", help="короткое имя или ссылка t.me/addstickers/…")

    k_chk = sk_sub.add_parser("check-name", help="свободно ли короткое имя")
    k_chk.add_argument("name")
    k_chk.add_argument("--as", dest="bot", required=True)

    k_sug = sk_sub.add_parser("suggest", help="подобрать свободное короткое имя")
    k_sug.add_argument("title")
    k_sug.add_argument("--as", dest="bot", required=True)

    k_new = sk_sub.add_parser("create", help="создать набор")
    k_new.add_argument("owner", help="владелец набора — обычно вы")
    k_new.add_argument("title")
    k_new.add_argument("short_name")
    k_new.add_argument("sticker", nargs="+", metavar="ФАЙЛ=ЭМОДЗИ")
    k_new.add_argument("--as", dest="bot", required=True)
    k_new.add_argument("--masks", action="store_true")
    k_new.add_argument("--emojis", action="store_true", help="набор эмодзи, а не стикеров")

    k_add = sk_sub.add_parser("add", help="добавить стикер в набор")
    k_add.add_argument("name")
    k_add.add_argument("file")
    k_add.add_argument("emoji", nargs="?", default="🙂")
    k_add.add_argument("--as", dest="bot", required=True)

    k_rm = sk_sub.add_parser("remove", help="убрать стикер по номеру")
    k_rm.add_argument("name")
    k_rm.add_argument("position", type=int)
    k_rm.add_argument("--as", dest="bot", required=True)

    k_mv = sk_sub.add_parser("move", help="переставить стикер")
    k_mv.add_argument("name")
    k_mv.add_argument("position", type=int)
    k_mv.add_argument("to", type=int)
    k_mv.add_argument("--as", dest="bot", required=True)

    k_em = sk_sub.add_parser("emoji", help="сменить эмодзи стикера")
    k_em.add_argument("name")
    k_em.add_argument("position", type=int)
    k_em.add_argument("emoji")
    k_em.add_argument("--keywords", help="ключевые слова для поиска")
    k_em.add_argument("--as", dest="bot", required=True)

    k_rn = sk_sub.add_parser("rename", help="переименовать набор")
    k_rn.add_argument("name")
    k_rn.add_argument("title")
    k_rn.add_argument("--as", dest="bot", required=True)

    k_th = sk_sub.add_parser("thumb", help="сделать стикер обложкой набора")
    k_th.add_argument("name")
    k_th.add_argument("position", type=int)
    k_th.add_argument("--as", dest="bot", required=True)

    k_del = sk_sub.add_parser("delete", help="удалить набор (требует подтверждения)")
    k_del.add_argument("name")
    k_del.add_argument("--as", dest="bot", required=True)
    k_del.add_argument("--confirm-to", help="кто подтверждает")
    k_del.add_argument("--timeout", type=float, default=300.0)

    sh = sub.add_parser("share-folder", help="общие папки: ссылка на набор чатов")
    sh_sub = sh.add_subparsers(dest="sharecmd", required=True)
    sh.set_defaults(func=cmd_share_folder)

    h_inv = sh_sub.add_parser("invites", help="какие ссылки выписаны на папку")
    h_inv.add_argument("id", type=int, help="номер папки; список — tgx folders")
    h_inv.add_argument("--jsonl", action="store_true")

    h_sh = sh_sub.add_parser("share", help="выписать ссылку на папку")
    h_sh.add_argument("id", type=int)
    h_sh.add_argument("title")
    h_sh.add_argument("--chat", action="append", help="только эти чаты; без него — все делимые")

    h_ed = sh_sub.add_parser("edit", help="поменять название ссылки или набор чатов")
    h_ed.add_argument("id", type=int)
    h_ed.add_argument("slug")
    h_ed.add_argument("--title")
    h_ed.add_argument("--chat", action="append")

    h_rv = sh_sub.add_parser("revoke", help="отозвать ссылку")
    h_rv.add_argument("id", type=int)
    h_rv.add_argument("slug")

    h_ck = sh_sub.add_parser("check", help="что внутри чужой ссылки — до принятия")
    h_ck.add_argument("slug")

    h_jn = sh_sub.add_parser("join", help="принять папку по ссылке")
    h_jn.add_argument("slug")
    h_jn.add_argument("--chat", action="append", help="только эти чаты")

    h_up = sh_sub.add_parser("updates", help="что автор добавил в папку")
    h_up.add_argument("id", type=int)

    h_ac = sh_sub.add_parser("accept", help="принять новые чаты папки")
    h_ac.add_argument("id", type=int)
    h_ac.add_argument("--chat", action="append")

    h_hd = sh_sub.add_parser("hide-updates", help="больше не предлагать обновления папки")
    h_hd.add_argument("id", type=int)

    h_ls = sh_sub.add_parser("leave-suggestions", help="что советуют покинуть вместе с папкой")
    h_ls.add_argument("id", type=int)

    h_lv = sh_sub.add_parser("leave", help="покинуть папку и чаты (требует подтверждения)")
    h_lv.add_argument("id", type=int)
    h_lv.add_argument("--chat", action="append")
    h_lv.add_argument("--confirm-to", help="кто подтверждает")
    h_lv.add_argument("--as", dest="bot", help="бот, который спросит")
    h_lv.add_argument("--timeout", type=float, default=300.0)

    stz = sub.add_parser("stories", help="истории: лента, публикация, просмотры, альбомы")
    stz_sub = stz.add_subparsers(dest="storycmd", required=True)
    stz.set_defaults(func=cmd_stories)

    s_feed = stz_sub.add_parser("feed", help="лента историй")
    s_feed.add_argument("--hidden", action="store_true", help="скрытые из ленты")
    s_feed.add_argument("--jsonl", action="store_true")

    s_of = stz_sub.add_parser("of", help="активные истории конкретного человека или канала")
    s_of.add_argument("chat")
    s_of.add_argument("--jsonl", action="store_true")

    s_pin = stz_sub.add_parser("pinned", help="истории, оставленные в профиле")
    s_pin.add_argument("chat", nargs="?")
    s_pin.add_argument("--limit", type=int, default=30)
    s_pin.add_argument("--jsonl", action="store_true")

    s_ar = stz_sub.add_parser("archive", help="свой архив историй")
    s_ar.add_argument("--limit", type=int, default=30)
    s_ar.add_argument("--jsonl", action="store_true")

    s_pub = stz_sub.add_parser("publish", help="опубликовать историю")
    s_pub.add_argument("file", help="фото или видео")
    s_pub.add_argument("--caption")
    s_pub.add_argument("--audience", default="close", choices=list(tgx_stories.AUDIENCES),
                       help="кому видно; по умолчанию близким друзьям")
    s_pub.add_argument("--hours", type=int, default=24,
                       choices=list(tgx_stories.PERIODS), help="сколько живёт")
    s_pub.add_argument("--pin", action="store_true", help="оставить в профиле после срока")
    s_pub.add_argument("--no-forwards", action="store_true", help="запретить пересылку")
    s_pub.add_argument("--allow", action="append", help="добавить конкретных людей")
    s_pub.add_argument("--deny", action="append", help="исключить конкретных людей")

    s_pn = stz_sub.add_parser("pin", help="оставить историю в профиле или убрать")
    s_pn.add_argument("id", nargs="+", type=int)
    s_pn.add_argument("--off", action="store_true", help="убрать из профиля")

    s_v = stz_sub.add_parser("viewers", help="кто смотрел вашу историю")
    s_v.add_argument("id", type=int)
    s_v.add_argument("--limit", type=int, default=50)
    s_v.add_argument("--contacts", action="store_true", help="только контакты")
    s_v.add_argument("--jsonl", action="store_true")

    s_re = stz_sub.add_parser("react", help="реакция на чужую историю")
    s_re.add_argument("chat")
    s_re.add_argument("id", type=int)
    s_re.add_argument("emoji", nargs="?", default="❤")
    s_re.add_argument("--clear", action="store_true", help="снять реакцию")

    s_rd = stz_sub.add_parser("read", help="отметить истории прочитанными")
    s_rd.add_argument("chat")
    s_rd.add_argument("id", type=int, help="до какого номера включительно")

    s_stl = stz_sub.add_parser("stealth", help="скрытный режим: просмотры не засчитываются")
    s_stl.add_argument("--past-only", action="store_true", help="только за прошедшие минуты")
    s_stl.add_argument("--future-only", action="store_true", help="только на ближайшие")

    s_lk = stz_sub.add_parser("link", help="ссылка на историю")
    s_lk.add_argument("chat")
    s_lk.add_argument("id", type=int)

    s_hd = stz_sub.add_parser("hide", help="убрать чьи-то истории из ленты")
    s_hd.add_argument("chat")
    s_hd.add_argument("--show", action="store_true", help="вернуть в ленту")

    s_cp = stz_sub.add_parser("can-post", help="можно ли публиковать сюда историю")
    s_cp.add_argument("chat", nargs="?")

    s_sr = stz_sub.add_parser("search", help="публичные истории по хештегу")
    s_sr.add_argument("hashtag")
    s_sr.add_argument("--limit", type=int, default=20)
    s_sr.add_argument("--jsonl", action="store_true")

    s_al = stz_sub.add_parser("albums", help="альбомы историй")
    s_al.add_argument("chat", nargs="?")
    s_al.add_argument("--jsonl", action="store_true")

    s_na = stz_sub.add_parser("new-album", help="создать альбом историй")
    s_na.add_argument("title")
    s_na.add_argument("id", nargs="+", type=int)
    s_na.add_argument("--chat")

    def story_gate(name: str, help_text: str) -> Any:
        parser = stz_sub.add_parser(name, help=help_text + " (требует подтверждения)")
        parser.add_argument("--confirm-to", help="кто подтверждает")
        parser.add_argument("--as", dest="bot", help="бот, который спросит")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    s_del = story_gate("delete", "удалить истории")
    s_del.add_argument("id", nargs="+", type=int)

    s_da = story_gate("delete-album", "удалить альбом историй")
    s_da.add_argument("id", type=int)

    ct = sub.add_parser("contacts", help="адресная книга, чёрный список, поиск людей")
    ct_sub = ct.add_subparsers(dest="contactcmd", required=True)
    ct.set_defaults(func=cmd_contacts)

    c_list = ct_sub.add_parser("list", help="все контакты")
    c_list.add_argument("--jsonl", action="store_true")

    c_add = ct_sub.add_parser("add", help="добавить в адресную книгу")
    c_add.add_argument("user")
    c_add.add_argument("--first", help="имя; по умолчанию — как в профиле")
    c_add.add_argument("--last")
    c_add.add_argument("--phone")
    c_add.add_argument("--note", help="личная заметка, видна только вам")
    c_add.add_argument("--share-phone", action="store_true",
                       help="разрешить ему увидеть ваш номер")

    c_rm = ct_sub.add_parser("remove", help="убрать из книги (писать он всё равно сможет)")
    c_rm.add_argument("user", nargs="+")

    c_note = ct_sub.add_parser("note", help="заметка о человеке")
    c_note.add_argument("user")
    c_note.add_argument("text", nargs="?", help="пусто — снять заметку")

    c_cf = ct_sub.add_parser("close-friends", help="задать список близких друзей целиком")
    c_cf.add_argument("user", nargs="+")

    c_bl = ct_sub.add_parser("blocked", help="чёрный список")
    c_bl.add_argument("--stories", action="store_true", help="кому запрещены истории")
    c_bl.add_argument("--limit", type=int, default=100)
    c_bl.add_argument("--jsonl", action="store_true")

    for name, help_text in (("block", "запретить писать"), ("unblock", "снять запрет")):
        parser = ct_sub.add_parser(name, help=help_text)
        parser.add_argument("user")
        parser.add_argument("--stories", action="store_true", help="только истории")

    c_s = ct_sub.add_parser("search", help="поиск людей и каналов по всему Telegram")
    c_s.add_argument("query")
    c_s.add_argument("--limit", type=int, default=20)
    c_s.add_argument("--jsonl", action="store_true")

    c_ph = ct_sub.add_parser("by-phone", help="кто за номером, если он это разрешил")
    c_ph.add_argument("phone")

    c_bd = ct_sub.add_parser("birthdays", help="у кого скоро день рождения")
    c_bd.add_argument("--jsonl", action="store_true")

    c_top = ct_sub.add_parser("top", help="с кем общаетесь чаще всего")
    c_top.add_argument("--limit", type=int, default=20)
    c_top.add_argument("--jsonl", action="store_true")

    c_tt = ct_sub.add_parser("top-toggle", help="включить или выключить учёт частых собеседников")
    c_tt.add_argument("--off", action="store_true")

    c_im = ct_sub.add_parser("import", help="добавить по ссылке-приглашению")
    c_im.add_argument("token")

    pay = sub.add_parser("pay", help="звёзды, TON и счета; оплата — только с подтверждением")
    pay_sub = pay.add_subparsers(dest="paycmd", required=True)
    pay.set_defaults(func=cmd_pay)

    y_bal = pay_sub.add_parser("balance", help="баланс звёзд или TON")
    y_bal.add_argument("--ton", action="store_true", help="криптовалюта вместо звёзд")

    y_hist = pay_sub.add_parser("history", help="история операций")
    y_hist.add_argument("--limit", type=int, default=30)
    y_hist.add_argument("--inbound", action="store_true", help="только приход")
    y_hist.add_argument("--outbound", action="store_true", help="только расход")
    y_hist.add_argument("--ton", action="store_true")
    y_hist.add_argument("--jsonl", action="store_true")

    y_rec = pay_sub.add_parser("receipt", help="чек по оплаченному сообщению")
    y_rec.add_argument("chat")
    y_rec.add_argument("id", type=int)

    y_show = pay_sub.add_parser("show", help="что просит счёт по ссылке (без оплаты)")
    y_show.add_argument("link")

    y_inv = pay_sub.add_parser("invoice", help="выписать ссылку-счёт")
    y_inv.add_argument("title")
    y_inv.add_argument("description")
    y_inv.add_argument("--currency", default="XTR", help="XTR — звёзды, иначе код валюты")
    y_inv.add_argument("--price", action="append", required=True, metavar="ЗА-ЧТО=СУММА",
                       help="строка счёта; можно повторять")
    y_inv.add_argument("--as", dest="bot", help="бот, который выписывает счёт (обязателен)")
    y_inv.add_argument("--provider", help="токен платёжного провайдера; звёздам не нужен")
    y_inv.add_argument("--subscription", type=int, metavar="СЕК",
                       help="продавать подписку: период в секундах (только звёзды)")
    y_inv.add_argument("--payload", default="tgx", help="служебная метка счёта")
    y_inv.add_argument("--test", action="store_true", help="тестовый платёж")
    y_inv.add_argument("--need-name", action="store_true")
    y_inv.add_argument("--need-phone", action="store_true")
    y_inv.add_argument("--need-email", action="store_true")
    y_inv.add_argument("--need-address", action="store_true")

    y_send = pay_sub.add_parser("send", help="оплатить счёт звёздами — только с подтверждением")
    y_send.add_argument("link", help="ссылка-счёт")
    y_send.add_argument("--as", dest="bot", required=True, help="бот, который спросит разрешение")
    y_send.add_argument("--confirm-to", required=True, help="кто подтверждает списание")
    y_send.add_argument("--timeout", type=float, default=300.0, help="сколько ждать согласия")

    # читающие
    y_cat = pay_sub.add_parser("gifts", help="каталог подарков Telegram и цены")
    y_cat.add_argument("--limit", type=int, default=40)
    y_cat.add_argument("--jsonl", action="store_true")

    y_mine = pay_sub.add_parser("my-gifts", help="полученные подарки")
    y_mine.add_argument("chat", nargs="?", help="чей: по умолчанию свои")
    y_mine.add_argument("--limit", type=int, default=30)
    y_mine.add_argument("--jsonl", action="store_true")

    y_subs = pay_sub.add_parser("subscriptions", help="подписки за звёзды")
    y_subs.add_argument("chat", nargs="?")
    y_subs.add_argument("--jsonl", action="store_true")

    y_rev = pay_sub.add_parser("revenue", help="сколько заработал канал или бот")
    y_rev.add_argument("chat")
    y_rev.add_argument("--ton", action="store_true")

    y_code = pay_sub.add_parser("check-code", help="что даёт подарочный код")
    y_code.add_argument("slug")

    y_give = pay_sub.add_parser("giveaway", help="сведения о розыгрыше")
    y_give.add_argument("chat")
    y_give.add_argument("id", type=int)

    pay_sub.add_parser("saved-info", help="что Telegram хранит из платёжных данных")

    pay_sub.add_parser("card-bank", help="какой банк выпустил карту; номер спросим скрытно")

    # ── подарки: витрина, рынок, коллекции ──────────────────────────────────
    y_tp = pay_sub.add_parser("topup", help="пакеты звёзд и их цена")
    y_tp.add_argument("--jsonl", action="store_true")

    y_auc = pay_sub.add_parser("auctions", help="идущие аукционы уникальных подарков")
    y_auc.add_argument("--jsonl", action="store_true")

    y_g1 = pay_sub.add_parser("gift", help="подробности своего подарка")
    y_g1.add_argument("id", type=int, help="id сообщения с подарком")

    y_upv = pay_sub.add_parser("upgrade-preview", help="что даст улучшение — до оплаты")
    y_upv.add_argument("gift_id", type=int)

    y_res = pay_sub.add_parser("resale", help="вторичный рынок: кто что продаёт")
    y_res.add_argument("gift_id", type=int)
    y_res.add_argument("--limit", type=int, default=20)
    y_res.add_argument("--by-number", action="store_true", help="сортировать по номеру, не по цене")
    y_res.add_argument("--jsonl", action="store_true")

    y_uni = pay_sub.add_parser("unique", help="уникальный подарок по ссылке")
    y_uni.add_argument("slug")

    y_col = pay_sub.add_parser("collections", help="коллекции подарков")
    y_col.add_argument("chat", nargs="?")
    y_col.add_argument("--jsonl", action="store_true")

    y_ref = pay_sub.add_parser("referrals", help="партнёрские программы ботов")
    y_ref.add_argument("chat", nargs="?")
    y_ref.add_argument("--limit", type=int, default=20)
    y_ref.add_argument("--jsonl", action="store_true")

    y_can = pay_sub.add_parser("can-send-gift", help="можно ли отправить этот подарок")
    y_can.add_argument("gift_id", type=int)

    y_show = pay_sub.add_parser("show-gift", help="показать подарок в профиле или спрятать")
    y_show.add_argument("id", type=int)
    y_show.add_argument("--hide", action="store_true", help="спрятать вместо показа")

    y_pin = pay_sub.add_parser("pin-gift", help="закрепить подарки наверху витрины")
    y_pin.add_argument("id", nargs="+", type=int)
    y_pin.add_argument("--chat", help="витрина канала вместо своей")

    y_nc = pay_sub.add_parser("new-collection", help="создать коллекцию подарков")
    y_nc.add_argument("title")
    y_nc.add_argument("id", nargs="+", type=int)


    # тратящие и необратимые — у всех общие ключи подтверждения
    def gated_parser(name: str, help_text: str) -> Any:
        parser = pay_sub.add_parser(name, help=help_text + " (требует подтверждения)")
        parser.add_argument("--confirm-to", help="кто подтверждает")
        parser.add_argument("--as", dest="bot", help="бот, который спросит")
        parser.add_argument("--timeout", type=float, default=300.0)
        return parser

    y_conv = gated_parser("convert-gift", "обменять подарок на звёзды")
    y_conv.add_argument("id", type=int, help="id сообщения с подарком")

    y_tr = gated_parser("transfer-gift", "передать подарок другому")
    y_tr.add_argument("id", type=int)
    y_tr.add_argument("to")

    y_react = gated_parser("react", "платная реакция звёздами")
    y_react.add_argument("chat")
    y_react.add_argument("id", type=int)
    y_react.add_argument("count", type=int, help="сколько звёзд")
    y_react.add_argument("--anonymous", action="store_true")

    y_apply = gated_parser("apply-code", "применить подарочный код")
    y_apply.add_argument("slug")

    gated_parser("clear-saved", "стереть сохранённые платёжные данные")

    y_wd = gated_parser("withdraw", "вывести средства: вернёт ссылку на страницу вывода")
    y_wd.add_argument("chat", help="канал или бот, чей доход выводим")
    y_wd.add_argument("amount", type=float, help="сколько выводим")
    y_wd.add_argument("--ton", action="store_true", help="в TON вместо звёзд")

    y_cancel = gated_parser("cancel-subscription", "отменить или возобновить подписку")
    y_cancel.add_argument("chat")
    y_cancel.add_argument("id", help="id подписки")
    y_cancel.add_argument("--resume", action="store_true", help="возобновить вместо отмены")

    y_up = gated_parser("upgrade-gift", "улучшить подарок до уникального")
    y_up.add_argument("id", type=int)
    y_up.add_argument("--drop-details", action="store_true", help="не сохранять историю подарка")

    y_sell = gated_parser("sell-gift", "выставить подарок на продажу или снять")
    y_sell.add_argument("id", type=int)
    y_sell.add_argument("stars", nargs="?", type=int, help="цена в звёздах")
    y_sell.add_argument("--unlist", action="store_true", help="снять с продажи")

    y_rf = gated_parser("refund", "вернуть звёзды за покупку")
    y_rf.add_argument("user")
    y_rf.add_argument("charge", help="идентификатор платежа")

    # ── аукционы, крафт, партнёрские программы ──────────────────────────────
    y_as = pay_sub.add_parser("auction-state", help="состояние аукциона")
    y_as.add_argument("id", type=int)

    y_aw = pay_sub.add_parser("auction-won", help="выигранные на аукционе подарки")
    y_aw.add_argument("gift_id", type=int)
    y_aw.add_argument("--jsonl", action="store_true")

    y_cr = pay_sub.add_parser("craftable", help="из чего можно собрать подарок")
    y_cr.add_argument("gift_id", type=int)
    y_cr.add_argument("--limit", type=int, default=20)
    y_cr.add_argument("--jsonl", action="store_true")

    y_sr = pay_sub.add_parser("suggested-referrals", help="какие боты предлагают партнёрство")
    y_sr.add_argument("chat", nargs="?")
    y_sr.add_argument("--limit", type=int, default=20)
    y_sr.add_argument("--jsonl", action="store_true")

    y_r1 = pay_sub.add_parser("referral", help="одна партнёрская программа")
    y_r1.add_argument("bot_name", metavar="бот")
    y_r1.add_argument("chat", nargs="?")

    y_po = pay_sub.add_parser("premium-options", help="почём подарить Premium")
    y_po.add_argument("chat", nargs="?")
    y_po.add_argument("--jsonl", action="store_true")

    y_go = pay_sub.add_parser("giveaway-options", help="варианты розыгрышей звёзд")
    y_go.add_argument("--jsonl", action="store_true")

    y_uv = pay_sub.add_parser("unique-value", help="во что оценивается уникальный подарок")
    y_uv.add_argument("slug")

    y_ua = pay_sub.add_parser("upgrade-attributes", help="варианты оформления и их редкость")
    y_ua.add_argument("gift_id", type=int)

    y_ads = pay_sub.add_parser("ads-account", help="ссылка на рекламный кабинет")
    y_ads.add_argument("chat")

    y_tx = pay_sub.add_parser("transaction", help="операции по их идентификаторам")
    y_tx.add_argument("id", nargs="+")
    y_tx.add_argument("--ton", action="store_true")
    y_tx.add_argument("--jsonl", action="store_true")

    y_gn = pay_sub.add_parser("gift-notifications", help="уведомления о подарках в чате")
    y_gn.add_argument("chat")
    y_gn.add_argument("--off", action="store_true")

    y_ec = pay_sub.add_parser("edit-collection", help="править коллекцию подарков")
    y_ec.add_argument("id", type=int)
    y_ec.add_argument("--title")
    y_ec.add_argument("--add", action="append", type=int)
    y_ec.add_argument("--remove", action="append", type=int)

    y_rc = pay_sub.add_parser("reorder-collections", help="порядок коллекций")
    y_rc.add_argument("id", nargs="+", type=int)

    y_vi = pay_sub.add_parser("validate-info", help="проверить контактные данные до оплаты")
    y_vi.add_argument("link")
    y_vi.add_argument("--name")
    y_vi.add_argument("--phone")
    y_vi.add_argument("--email")
    y_vi.add_argument("--save", action="store_true")

    y_cf = gated_parser("craft", "собрать новый подарок из имеющихся")
    y_cf.add_argument("id", nargs="+", type=int)

    y_of = gated_parser("offer", "предложить владельцу выкуп подарка")
    y_of.add_argument("chat")
    y_of.add_argument("slug")
    y_of.add_argument("stars", type=int)
    y_of.add_argument("--days", type=int, help="срок предложения")

    y_ao = gated_parser("answer-offer", "принять или отклонить предложение")
    y_ao.add_argument("id", type=int)
    y_ao.add_argument("--decline", action="store_true")

    y_dc = gated_parser("delete-collection", "удалить коллекцию")
    y_dc.add_argument("id", type=int)

    y_cn = gated_parser("connect-referral", "подключить партнёрскую программу")
    y_cn.add_argument("bot_name", metavar="бот")

    y_rv = gated_parser("revoke-referral", "отозвать партнёрскую ссылку")
    y_rv.add_argument("link")

    y_fs = gated_parser("fulfil-subscription", "доплатить за подписку")
    y_fs.add_argument("chat")
    y_fs.add_argument("id")

    y_gb = gated_parser("gift-to-blockchain", "вывести подарок в блокчейн")
    y_gb.add_argument("id", type=int)

    y_pc = gated_parser("pay-card", "оплатить счёт сохранённой картой")
    y_pc.add_argument("link")
    y_pc.add_argument("--tip", type=int, help="чаевые")

    y_gift = pay_sub.add_parser("gift-options", help="во что обойдётся подарить звёзды")
    y_gift.add_argument("user")
    y_gift.add_argument("--jsonl", action="store_true")

    pl = sub.add_parser("poll", help="опросы и викторины")
    pl_sub = pl.add_subparsers(dest="pollcmd", required=True)
    pl.set_defaults(func=cmd_poll)

    p_new = pl_sub.add_parser("create", help="создать опрос или викторину")
    p_new.add_argument("chat")
    p_new.add_argument("question")
    p_new.add_argument("option", nargs="+", help="варианты ответа; с 10.0 хватает одного")
    p_new.add_argument("--quiz", type=int, nargs="+", metavar="N",
                       help="викторина: номера правильных ответов с 0; требует --as. "
                            "С Bot API 9.6 их может быть несколько")
    p_new.add_argument("--allow-revoting", action="store_true", help="разрешить переголосовать")
    p_new.add_argument("--as", dest="bot", help="бот, от имени которого уйдёт викторина")
    p_new.add_argument("--explanation", help="пояснение к правильному ответу (только викторина)")
    p_new.add_argument("--multiple", action="store_true", help="можно выбрать несколько")
    p_new.add_argument("--public", action="store_true", help="видно, кто как проголосовал")
    p_new.add_argument("--shuffle", action="store_true", help="перемешивать варианты")
    p_new.add_argument("--hide-results", action="store_true", help="результаты только после закрытия")
    p_new.add_argument("--members-only", action="store_true", help="только для подписчиков")
    p_new.add_argument("--countries", help="ограничить странами: RU,DE")
    p_new.add_argument("--close-in", type=int, metavar="СЕК", help="закрыть автоматически")
    p_new.add_argument("--topic", type=int, help="id темы форума")
    p_new.add_argument("--silent", action="store_true")

    p_vote = pl_sub.add_parser("vote", help="проголосовать; без вариантов — снять голос")
    p_vote.add_argument("chat")
    p_vote.add_argument("id", type=int)
    p_vote.add_argument("choice", nargs="*", type=int, help="номера вариантов с 0")

    p_res = pl_sub.add_parser("results", help="результаты опроса")
    p_res.add_argument("chat")
    p_res.add_argument("id", type=int)
    p_res.add_argument("--jsonl", action="store_true")

    p_close = pl_sub.add_parser("close", help="закрыть опрос")
    p_close.add_argument("chat")
    p_close.add_argument("id", type=int)

    p_who = pl_sub.add_parser("voters", help="кто как проголосовал (если опрос не анонимный)")
    p_who.add_argument("chat")
    p_who.add_argument("id", type=int)
    p_who.add_argument("--option", type=int, help="только за этот вариант")
    p_who.add_argument("--limit", type=int, default=50)
    p_who.add_argument("--jsonl", action="store_true")

    gd = sub.add_parser("guard", help="именные одноразовые приглашения: вошёл не тот — удаляем")
    gd_sub = gd.add_subparsers(dest="guardcmd", required=True)
    gd.set_defaults(func=cmd_guard)

    g_inv = gd_sub.add_parser("invite", help="выписать ссылку на одного человека")
    g_inv.add_argument("chat")
    g_inv.add_argument("user", help="@имя или id того, кого зовём")
    g_inv.add_argument("--hours", type=int, default=tgx_guard.DEFAULT_HOURS, help="срок жизни ссылки")
    g_inv.add_argument("--note", help="пометка в журнал: зачем звали")

    g_chk = gd_sub.add_parser("check", help="сверить, кто вошёл, и удалить чужих")
    g_chk.add_argument("chat")
    g_chk.add_argument("--no-kick", action="store_true", help="только показать, никого не трогать")
    g_chk.add_argument("--jsonl", action="store_true")
    g_chk.add_argument("--confirm-to", help="спросить человека перед удалением")
    g_chk.add_argument("--as", dest="bot", help="бот, который спросит")
    g_chk.add_argument("--timeout", type=float, default=300.0)

    g_rev = gd_sub.add_parser("revoke", help="отозвать ссылку")
    g_rev.add_argument("chat")
    g_rev.add_argument("link")

    g_lock = gd_sub.add_parser("lock", help="запретить обычным участникам звать людей")
    g_lock.add_argument("chat")

    g_jrn = gd_sub.add_parser("journal", help="кому какие ссылки выписаны и чем кончилось")
    g_jrn.add_argument("--jsonl", action="store_true")

    rich = sub.add_parser("rich", help="богатое сообщение от своего имени: заголовки, таблицы, цитаты")
    rich.add_argument("peer")
    rich.add_argument("text", nargs="?", default="", help="разметка; либо --file")
    rich.add_argument("--file", help="файл с разметкой")
    rich.add_argument("--topic", type=int, help="id темы форума")
    rich.add_argument("--button", action="append",
                      help="кнопки под сообщением; можно повторять — каждый флаг даёт свой ряд; см. tgx bot buttons")
    rich.add_argument("--media", action="append", metavar="ИМЯ=ПУТЬ",
                      help="картинка внутрь документа; ссылка в тексте — tg://photo?id=ИМЯ. "
                           "Если ссылки нет, картинка встаёт в начало")
    rich.add_argument("--silent", action="store_true")
    rich.set_defaults(func=cmd_rich)

    tr = sub.add_parser("transcribe", help="расшифровать голосовое или кружок в текст")
    tr_sub = tr.add_subparsers(dest="trcmd", required=True)
    tr.set_defaults(func=cmd_transcribe)

    t_get = tr_sub.add_parser("get", help="расшифровать сообщение")
    t_get.add_argument("chat")
    t_get.add_argument("id", type=int, help="id голосового сообщения")
    t_get.add_argument("--wait", type=float, default=tgx_transcribe.WAIT_SECONDS,
                       help="сколько секунд ждать готовый текст")
    t_get.add_argument("--jsonl", action="store_true")

    t_rate = tr_sub.add_parser("rate", help="оценить расшифровку — это учат распознавание")
    t_rate.add_argument("chat")
    t_rate.add_argument("id", type=int)
    t_rate.add_argument("transcription_id", type=int)
    t_rate.add_argument("verdict", choices=["good", "bad"])

    tr_sub.add_parser("status", help="доступна ли расшифровка и сколько бесплатных осталось")

    fr = sub.add_parser("forum", help="форумы и темы: создание, иконки, закрепление, порядок")
    fr_sub = fr.add_subparsers(dest="forumcmd", required=True)
    fr.set_defaults(func=cmd_forum)

    f_list = fr_sub.add_parser("topics", help="темы форума; --search ищет по названию")
    f_list.add_argument("chat")
    f_list.add_argument("--search", help="искать по названию")
    f_list.add_argument("--limit", type=int, default=100)
    f_list.add_argument("--jsonl", action="store_true")

    f_show = fr_sub.add_parser("show", help="темы по id — так же узнают, что тему удалили")
    f_show.add_argument("chat")
    f_show.add_argument("id", nargs="+", type=int)
    f_show.add_argument("--jsonl", action="store_true")

    f_icons = fr_sub.add_parser("icons", help="иконки тем, доступные без Premium")
    f_icons.add_argument("--limit", type=int, default=40)
    f_icons.add_argument("--jsonl", action="store_true")

    f_new = fr_sub.add_parser("create", help="создать тему")
    f_new.add_argument("chat")
    f_new.add_argument("title")
    f_new.add_argument("--color", help=f"цвет стандартной иконки: {', '.join(tgx_forum.ICON_COLORS)}")
    f_new.add_argument("--emoji", type=int, help="id эмодзи-иконки; список — tgx forum icons")
    f_new.add_argument("--send-as", help="создать от имени канала")

    f_edit = fr_sub.add_parser("edit", help="переименовать, сменить иконку, закрыть или скрыть")
    f_edit.add_argument("chat")
    f_edit.add_argument("id", type=int)
    f_edit.add_argument("--title", help="новое название")
    f_edit.add_argument("--emoji", type=int, help="новая иконка; 0 убирает эмодзи")
    f_edit.add_argument("--close", action="store_true", help="закрыть тему для новых сообщений")
    f_edit.add_argument("--open", action="store_true", help="открыть обратно")
    f_edit.add_argument("--hide", action="store_true", help="скрыть — только «Общую» тему")
    f_edit.add_argument("--show", action="store_true", help="показать «Общую» обратно")

    f_del = fr_sub.add_parser("delete", help="удалить тему со всей перепиской (необратимо)")
    f_del.add_argument("chat")
    f_del.add_argument("id", type=int)
    f_del.add_argument("--yes", action="store_true", help="подтвердить удаление")

    for name, help_text in (("pin", "закрепить тему"), ("unpin", "открепить тему")):
        parser = fr_sub.add_parser(name, help=help_text)
        parser.add_argument("chat")
        parser.add_argument("id", type=int)

    f_ord = fr_sub.add_parser("reorder", help="порядок закреплённых тем, сверху вниз")
    f_ord.add_argument("chat")
    f_ord.add_argument("id", nargs="+", type=int)
    f_ord.add_argument("--force", action="store_true",
                       help="открепить всё, чего нет в списке — иначе порядок будет не тот")

    for name, help_text in (("on", "включить форум в супергруппе"),
                            ("off", "вернуть супергруппу без тем")):
        parser = fr_sub.add_parser(name, help=help_text + " (только владелец)")
        parser.add_argument("chat")
        parser.add_argument("--tabs", help="вкладки вместо списка тем: on|off")

    f_tabs = fr_sub.add_parser("tabs", help="вкладки вместо списка тем")
    f_tabs.add_argument("chat")
    f_tabs.add_argument("state", choices=["on", "off"])

    f_asm = fr_sub.add_parser("as-messages", help="показывать форум сплошной лентой")
    f_asm.add_argument("chat")
    f_asm.add_argument("state", choices=["on", "off"])

    fr_sub.add_parser("limit", help="сколько тем разрешено закрепить")

    prof = sub.add_parser("profile", help="оформление: аватары, цвета, статус, дата рождения")
    prof_sub = prof.add_subparsers(dest="profcmd", required=True)
    prof.set_defaults(func=cmd_profile)

    prof_sub.add_parser("formats", help="какие бывают аватары и как их указать")

    p_ban = prof_sub.add_parser(
        "banner", help="записать заставку из терминала в видео и поставить аватаром")
    p_ban.set_defaults(func=cmd_profile_banner)
    p_ban.add_argument("--bot", help="аватар своего бота")
    p_ban.add_argument("--chat", help="аватар канала или группы")
    p_ban.add_argument("--me", action="store_true", help="свой собственный аватар")
    p_ban.add_argument("--save", help="только записать файл, никуда не ставить")
    p_ban.add_argument("--out", help="куда положить mp4 (по умолчанию data/banner.mp4)")
    p_ban.add_argument("--effect", default="beams", help="эффект заставки; список — tgx banner --list")
    p_ban.add_argument("--cols", type=int, default=40)
    p_ban.add_argument("--rows", type=int, default=16)
    p_ban.add_argument("--fps", type=int, default=30, help="кадров в секунду в файле")
    p_ban.add_argument("--speed", type=int, default=60, help="скорость анимации; меньше — медленнее")
    p_ban.add_argument("--seconds", type=float, default=12.0, help="сколько ждать анимацию")
    p_ban.add_argument("--hold", type=float, default=1.5, help="задержать готовый логотип в конце")
    p_ban.add_argument("--size", type=int, default=512)

    p_photo = prof_sub.add_parser("photo", help="поставить аватар: фото, видео, эмодзи или стикер")
    p_photo.add_argument("source", help="файл, emoji:id или sticker:набор:id — см. profile formats")
    p_photo.add_argument("--chat", help="аватар канала или группы, а не свой")
    p_photo.add_argument("--bot", help="аватар своего бота")
    p_photo.add_argument("--contact", help="фотография для человека в вашей адресной книге")
    p_photo.add_argument("--suggest", action="store_true", help="не ставить у себя, а предложить ему")
    p_photo.add_argument("--fallback", action="store_true",
                         help="публичный запасной аватар — его видят те, кому закрыт основной")
    p_photo.add_argument("--start", type=float, help="секунда, с которой начинается видеоаватар")
    p_photo.add_argument("--colors", help="градиент под эмодзи или стикером: #e8a4ff,#ff00aa")
    p_photo.add_argument("--square", action="store_true", help="обрезать по центру в квадрат")
    p_photo.add_argument("--trim", type=float, help="укоротить видео до N секунд")

    p_list = prof_sub.add_parser("photos", help="ваши аватары")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--jsonl", action="store_true")

    p_del = prof_sub.add_parser("photo-delete", help="убрать аватары по id")
    p_del.add_argument("id", nargs="+", type=int)

    p_unset = prof_sub.add_parser("unset-contact-photo", help="убрать фотографию, поставленную контакту")
    p_unset.add_argument("contact")

    p_color = prof_sub.add_parser("color", help="цвет имени и узор фона")
    p_color.add_argument("--color", type=int, help="номер цвета из палитры Telegram")
    p_color.add_argument("--emoji", type=int, help="id эмодзи для узора фона")
    p_color.add_argument("--profile", action="store_true", help="цвет шапки профиля, а не имени")
    p_color.add_argument("--chat", help="канал или группа вместо своего профиля")

    p_status = prof_sub.add_parser("status", help="эмодзи-статус рядом с именем")
    p_status.add_argument("--emoji", type=int, help="id эмодзи; список — profile emojis --kind status")
    p_status.add_argument("--until", type=int, help="до какого времени, unix-время")
    p_status.add_argument("--off", action="store_true", help="снять статус")
    p_status.add_argument("--chat", help="статус канала вместо своего")

    p_bd = prof_sub.add_parser("birthday", help="дата рождения: 14.03, 14.03.1990 или 1990-03-14")
    p_bd.add_argument("date", nargs="?")
    p_bd.add_argument("--off", action="store_true", help="убрать дату")

    p_pc = prof_sub.add_parser("personal-channel", help="канал, который виден прямо в профиле")
    p_pc.add_argument("chat", nargs="?")
    p_pc.add_argument("--off", action="store_true", help="убрать канал из профиля")

    p_em = prof_sub.add_parser("emojis", help="эмодзи, которые Telegram предлагает")
    p_em.add_argument("--kind", default="profile", choices=list(tgx_profile.STATUS_KINDS))
    p_em.add_argument("--limit", type=int, default=40)
    p_em.add_argument("--jsonl", action="store_true")

    pg = sub.add_parser("profile-get", help="show your Telegram profile and full user info")
    pg.set_defaults(func=cmd_profile_get)

    pe = sub.add_parser("profile-edit", help="edit your Telegram profile name/about/username")
    pe.add_argument("--first-name")
    pe.add_argument("--last-name")
    pe.add_argument("--about")
    pe.add_argument("--username")
    pe.set_defaults(func=cmd_profile_edit)

    pps = sub.add_parser("profile-photo-set", help="set your profile avatar from an image file")
    pps.add_argument("file")
    pps.set_defaults(func=cmd_profile_photo_set)

    pp = sub.add_parser("profile-photos", help="list your profile avatars/photos")
    pp.add_argument("--limit", type=int, default=20)
    pp.set_defaults(func=cmd_profile_photos)

    ppd = sub.add_parser("profile-photo-delete", help="delete your profile avatars/photos by id or --all")
    ppd.add_argument("--photo-id", action="append")
    ppd.add_argument("--all", action="store_true")
    ppd.set_defaults(func=cmd_profile_photo_delete)

    pn = sub.add_parser("pin", help="закрепить или открепить сообщение")
    pn.add_argument("peer")
    pn.add_argument("id", type=int)
    pn.add_argument("--notify", action="store_true", help="с уведомлением участников")
    pn.add_argument("--unpin", action="store_true")
    pn.set_defaults(func=cmd_pin)

    pl = sub.add_parser("pinned", help="закреплённые сообщения чата")
    pl.add_argument("peer")
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--jsonl", action="store_true")
    pl.set_defaults(func=cmd_pinned)

    tp = sub.add_parser("topics", help="темы форума")
    tp.add_argument("peer")
    tp.add_argument("--limit", type=int, default=100)
    tp.add_argument("--jsonl", action="store_true")
    tp.set_defaults(func=cmd_topics)

    tc = sub.add_parser("topic-create", help="создать тему форума")
    tc.add_argument("peer")
    tc.add_argument("title")
    tc.set_defaults(func=cmd_topic_create)

    te = sub.add_parser("topic-edit", help="переименовать, закрыть или скрыть тему")
    te.add_argument("peer")
    te.add_argument("id", type=int)
    te.add_argument("--title")
    te.add_argument("--close", action="store_true")
    te.add_argument("--open", action="store_true")
    te.add_argument("--hide", action="store_true")
    te.add_argument("--show", action="store_true")
    te.set_defaults(func=cmd_topic_edit)

    tpn = sub.add_parser("topic-pin", help="закрепить тему в списке")
    tpn.add_argument("peer")
    tpn.add_argument("id", type=int)
    tpn.add_argument("--unpin", action="store_true")
    tpn.set_defaults(func=cmd_topic_pin)

    cc = sub.add_parser("channel-create", help="создать канал, группу или группу с темами")
    cc.add_argument("title")
    cc.add_argument("--kind", choices=["channel", "group", "forum"], default="channel")
    cc.add_argument("--about", default="")
    cc.add_argument("--username", help="публичный адрес без @")
    cc.set_defaults(func=cmd_channel_create)

    csm = sub.add_parser("channel-slowmode", help="медленный режим в группе, секунды (0 — выключить)")
    csm.add_argument("peer")
    csm.add_argument("seconds", type=int)
    csm.set_defaults(func=cmd_channel_slowmode)

    cpm = sub.add_parser("channel-permissions", help="права участников группы по умолчанию")
    cpm.add_argument("peer")
    cpm.add_argument("--allow", default="", help="что разрешено, через запятую: send_messages,send_media,…")
    cpm.set_defaults(func=cmd_channel_permissions)

    cd = sub.add_parser("channel-discussion", help="привязать или отвязать группу обсуждения")
    cd.add_argument("peer")
    cd.add_argument("--group", help="группа обсуждения")
    cd.add_argument("--unlink", action="store_true")
    cd.set_defaults(func=cmd_channel_discussion)

    cj = sub.add_parser("chat-join", help="вступить по ссылке или @имени")
    cj.add_argument("link")
    cj.set_defaults(func=cmd_chat_join)

    cl = sub.add_parser("chat-leave", help="выйти из канала или группы (нужен --yes)")
    cl.add_argument("peer")
    cl.add_argument("--yes", action="store_true")
    cl.set_defaults(func=cmd_chat_leave)

    for name, help_text in (("mentions", "где вас звали и вы ещё не видели"),
                            ("my-reactions", "на что вам отреагировали")):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("peer")
        parser.add_argument("--limit", type=int, default=30)
        parser.add_argument("--topic", type=int, help="только в этой теме форума")
        parser.set_defaults(func=cmd_triage, func_name=name)

    g_clr = sub.add_parser("triage-clear", help="пометить упоминания и реакции просмотренными")
    g_clr.add_argument("peer")
    g_clr.add_argument("--what", default="both", choices=["both", "mentions", "reactions"])
    g_clr.add_argument("--topic", type=int)
    g_clr.set_defaults(func=cmd_triage, func_name="triage-clear")

    g_rb = sub.add_parser("read-by", help="кто прочитал сообщение в группе")
    g_rb.add_argument("peer")
    g_rb.add_argument("id", type=int)
    g_rb.set_defaults(func=cmd_triage, func_name="read-by")

    g_ra = sub.add_parser("read-at", help="когда прочитали ваше сообщение в личке")
    g_ra.add_argument("peer")
    g_ra.add_argument("id", type=int)
    g_ra.set_defaults(func=cmd_triage, func_name="read-at")

    g_vw = sub.add_parser("views", help="просмотры и пересылки постов")
    g_vw.add_argument("peer")
    g_vw.add_argument("id", type=int, nargs="+")
    g_vw.set_defaults(func=cmd_triage, func_name="views")

    g_cc = sub.add_parser("chat-counts", help="чем набит чат: фото, видео, ссылки, файлы")
    g_cc.add_argument("peer")
    g_cc.add_argument("--topic", type=int)
    g_cc.set_defaults(func=cmd_triage, func_name="chat-counts")

    g_on = sub.add_parser("online", help="сколько человек сейчас в чате")
    g_on.add_argument("peer")
    g_on.set_defaults(func=cmd_triage, func_name="online")

    g_mu = sub.add_parser("mark-unread", help="вернуть чату жирную точку")
    g_mu.add_argument("peer")
    g_mu.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    g_mu.set_defaults(func=cmd_triage, func_name="mark-unread")

    g_mk = sub.add_parser("marked-unread", help="какие чаты помечены непрочитанными")
    g_mk.set_defaults(func=cmd_triage, func_name="marked-unread")

    g_pc = sub.add_parser("pin-chat", help="закрепить чат в списке (не сообщение)")
    g_pc.add_argument("peer")
    g_pc.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    g_pc.set_defaults(func=cmd_triage, func_name="pin-chat")

    g_nf = sub.add_parser("no-forwards", help="запретить пересылку и копирование из чата")
    g_nf.add_argument("peer")
    g_nf.add_argument("state", choices=["on", "off"])
    g_nf.set_defaults(func=cmd_triage, func_name="no-forwards")

    tk = sub.add_parser("takeout", help="выгрузить аккаунт на диск: контакты, чаты, история")
    tk.add_argument("out", help="папка для выгрузки")
    tk.add_argument("--chat", action="append",
                    help="выгрузить историю этого чата; можно несколько раз")
    tk.add_argument("--limit", type=int, default=0, help="сколько сообщений на чат; 0 — все")
    tk.add_argument("--files", action="store_true", help="скачивать вложения")
    tk.add_argument("--max-file-mb", type=int, default=20, help="крупнее — пропускать")
    tk.add_argument("--no-contacts", action="store_true")
    tk.set_defaults(func=cmd_takeout)

    tkf = sub.add_parser("takeout-finish", help="закрыть висящую выгрузку")
    tkf.add_argument("--success", action="store_true", help="пометить как удавшуюся")
    tkf.set_defaults(func=cmd_takeout_finish)

    x_link = sub.add_parser("message-link", help="постоянная ссылка на сообщение")
    x_link.add_argument("peer")
    x_link.add_argument("id", type=int)
    x_link.add_argument("--album", action="store_true", help="ссылка на весь альбом")
    x_link.add_argument("--thread", action="store_true", help="ссылка внутрь обсуждения")
    x_link.set_defaults(func=cmd_chatx, func_name="message-link")

    x_com = sub.add_parser("common-chats", help="где вы состоите вместе с человеком")
    x_com.add_argument("user")
    x_com.add_argument("--limit", type=int, default=100)
    x_com.set_defaults(func=cmd_chatx, func_name="common-chats")

    x_pub = sub.add_parser("my-public", help="ваши публичные каналы и группы")
    x_pub.add_argument("--by-location", action="store_true", help="привязанные к месту")
    x_pub.add_argument("--check-limit", action="store_true",
                       help="только проверить, не упёрлись ли в предел")
    x_pub.set_defaults(func=cmd_chatx, func_name="my-public")

    x_sim = sub.add_parser("similar", help="похожие каналы")
    x_sim.add_argument("peer", nargs="?", help="без него — что Telegram советует вам")
    x_sim.set_defaults(func=cmd_chatx, func_name="similar")

    x_ina = sub.add_parser("inactive", help="чаты, где давно тихо — кандидаты на уборку")
    x_ina.set_defaults(func=cmd_chatx, func_name="inactive")

    x_who = sub.add_parser("who-is", help="кто этот человек в этом чате")
    x_who.add_argument("peer")
    x_who.add_argument("who")
    x_who.set_defaults(func=cmd_chatx, func_name="who-is")

    x_spam = sub.add_parser("antispam", help="жёсткий антиспам в супергруппе (нужны бусты)")
    x_spam.add_argument("peer")
    x_spam.add_argument("state", choices=["on", "off"])
    x_spam.set_defaults(func=cmd_chatx, func_name="antispam")

    x_ttl = sub.add_parser("default-ttl", help="через сколько сообщения исчезают в новых чатах")
    x_ttl.add_argument("seconds", nargs="?", type=int,
                       help="0, 86400 (сутки), 604800 (неделя), 2678400 (месяц); без него — показать")
    x_ttl.set_defaults(func=cmd_chatx, func_name="default-ttl")

    ci = sub.add_parser("channel-info", help="show channel/supergroup profile/admin metadata")
    ci.add_argument("peer")
    ci.add_argument("--raw", action="store_true")
    ci.set_defaults(func=cmd_channel_info)

    ce = sub.add_parser("channel-edit", help="edit channel/supergroup title/about/username/toggles")
    ce.add_argument("peer")
    ce.add_argument("--title")
    ce.add_argument("--about")
    ce.add_argument("--username")
    ce.add_argument("--signatures")
    ce.add_argument("--profiles")
    ce.add_argument("--prehistory-hidden")
    ce.add_argument("--join-to-send")
    ce.add_argument("--join-request")
    ce.add_argument("--participants-hidden")
    ce.add_argument("--forum")
    ce.add_argument("--forum-tabs")
    ce.set_defaults(func=cmd_channel_edit)

    cps = sub.add_parser("channel-photo-set", help="set channel/supergroup avatar from an image file")
    cps.add_argument("peer")
    cps.add_argument("file")
    cps.set_defaults(func=cmd_channel_photo_set)

    cpd = sub.add_parser("channel-photo-delete", help="remove channel/supergroup avatar")
    cpd.add_argument("peer")
    cpd.set_defaults(func=cmd_channel_photo_delete)

    cp = sub.add_parser("channel-participants", help="list channel/supergroup participants/admins/bots/banned users")
    cp.add_argument("peer")
    cp.add_argument("--filter", choices=["recent", "admins", "bots", "banned", "kicked", "search"], default="recent")
    cp.add_argument("--query")
    cp.add_argument("--offset", type=int, default=0)
    cp.add_argument("--limit", type=int, default=100)
    cp.set_defaults(func=cmd_channel_participants)

    cas = sub.add_parser("channel-admin-set", help="grant/edit channel admin rights")
    cas.add_argument("peer")
    cas.add_argument("user")
    cas.add_argument("--rights", default="all", help="comma list or all/none")
    cas.add_argument("--rank", default="")
    cas.set_defaults(func=cmd_channel_admin_set)

    car = sub.add_parser("channel-admin-remove", help="remove channel admin rights")
    car.add_argument("peer")
    car.add_argument("user")
    car.set_defaults(func=cmd_channel_admin_remove)

    cb = sub.add_parser("channel-ban", help="ban/restrict a user in a channel/supergroup")
    cb.add_argument("peer")
    cb.add_argument("user")
    cb.add_argument("--rights", default="view_messages", help="comma list or all; default bans viewing")
    cb.set_defaults(func=cmd_channel_ban)

    cu = sub.add_parser("channel-unban", help="clear restrictions for a user in a channel/supergroup")
    cu.add_argument("peer")
    cu.add_argument("user")
    cu.set_defaults(func=cmd_channel_unban)

    cia = sub.add_parser("channel-invite-add", help="add users to a channel/supergroup")
    cia.add_argument("peer")
    cia.add_argument("user", action="append")
    cia.set_defaults(func=cmd_channel_invite_add)

    ie = sub.add_parser("invite-export", help="create an invite link for a channel/supergroup")
    ie.add_argument("peer")
    ie.add_argument("--title")
    ie.add_argument("--usage-limit", type=int)
    ie.add_argument("--request-needed", action="store_true")
    ie.set_defaults(func=cmd_invite_export)

    il = sub.add_parser("invite-list", help="list invite links created by this account")
    il.add_argument("peer")
    il.add_argument("--limit", type=int, default=50)
    il.add_argument("--revoked", action="store_true")
    il.set_defaults(func=cmd_invite_list)

    al = sub.add_parser("admin-log", help="read channel/supergroup admin log")
    al.add_argument("peer")
    al.add_argument("--query")
    al.add_argument("--limit", type=int, default=50)
    al.set_defaults(func=cmd_admin_log)

    ts = sub.add_parser("tl-schema", help="inspect Telethon TL request signatures by namespace")
    ts.add_argument("namespace", choices=["channels", "messages", "account", "photos", "users"])
    ts.add_argument("--query")
    ts.set_defaults(func=cmd_tl_schema)

    mg = sub.add_parser("message-get", help="get messages with inline/reply button metadata")
    mg.add_argument("peer", help="@username, phone, id, or part of dialog title")
    mg.add_argument("--id", type=int, help="specific message id")
    mg.add_argument("--limit", type=int, default=10)
    mg.set_defaults(func=cmd_message_get)

    mc = sub.add_parser("message-click", help="click an inline/reply button on a message")
    mc.add_argument("peer", help="@username, phone, id, or part of dialog title")
    mc.add_argument("id", type=int, help="message id containing the button")
    mc.add_argument("--text", help="button text to click")
    mc.add_argument("--row", type=int, help="zero-based button row index")
    mc.add_argument("--col", type=int, help="zero-based button column index")
    mc.add_argument("--wait", type=int, default=5, help="seconds to wait for new messages after click")
    mc.add_argument("--limit", type=int, default=10, help="max new messages to return")
    mc.set_defaults(func=cmd_message_click)

    rc = sub.add_parser("react", help="поставить или убрать реакцию на сообщение")
    rc.add_argument("peer")
    rc.add_argument("id", type=int)
    rc.add_argument("emoji", nargs="?", default="👍")
    rc.add_argument("--clear", action="store_true", help="убрать свою реакцию")
    rc.add_argument("--who", action="store_true", help="показать, кто и чем отреагировал")
    rc.add_argument("--remove-from", metavar="КТО",
                    help="снять реакции участника — нужны права администратора")
    rc.add_argument("--all-messages", action="store_true",
                    help="вместе с --remove-from: снять его реакции во всём чате")
    rc.add_argument("--limit", type=int, default=50)
    rc.add_argument("--jsonl", action="store_true")
    rc.set_defaults(func=cmd_react)

    ed = sub.add_parser("edit", help="изменить своё сообщение")
    ed.add_argument("peer")
    ed.add_argument("id", type=int)
    ed.add_argument("text")
    ed.add_argument("--parse-mode", choices=["md", "html", "none"], default="md")
    ed.add_argument("--no-preview", action="store_true", help="без превью ссылок")
    ed.set_defaults(func=cmd_edit)

    fw = sub.add_parser("forward", help="переслать сообщения в другой чат")
    fw.add_argument("peer")
    fw.add_argument("id", type=int, nargs="+")
    fw.add_argument("--to", required=True)
    fw.add_argument("--silent", action="store_true")
    fw.set_defaults(func=cmd_forward)

    dl = sub.add_parser("delete", help="удалить сообщения (необратимо, нужен --yes)")
    dl.add_argument("peer")
    dl.add_argument("id", type=int, nargs="+")
    dl.add_argument("--yes", action="store_true", help="подтвердить удаление")
    dl.add_argument("--only-me", action="store_true", help="удалить только у себя")
    dl.add_argument("--confirm-to", help="спросить человека кнопкой перед удалением")
    dl.add_argument("--as", dest="bot", help="бот, который спросит")
    dl.add_argument("--timeout", type=float, default=300.0)
    dl.set_defaults(func=cmd_delete)

    h = sub.add_parser("history", help="show messages from a peer")
    h.add_argument("peer", help="@username, phone, id, or part of dialog title")
    h.add_argument("--limit", type=int, default=20)
    h.add_argument("--search")
    h.add_argument("--with-sender", action="store_true")
    h.add_argument("--jsonl", action="store_true")
    h.set_defaults(func=cmd_history)

    s = sub.add_parser("search", help="search messages in one peer or recent dialogs")
    s.add_argument("query", nargs="?", default="", help="текст; можно опустить, если задан --kind")
    s.add_argument("--peer")
    s.add_argument("--kind", choices=sorted(MEDIA_FILTERS), help="тип сообщений: photo, video, file, link…")
    s.add_argument("--sender", help="только от этого отправителя (работает с --peer)")
    s.add_argument("--since", help="не раньше: 2026-08-01, 01.08.2026 или -7d")
    s.add_argument("--until", help="не позже: та же запись")
    s.add_argument("--global", dest="globally", action="store_true",
                   help="искать по всем чатам сразу, а не по последним диалогам")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--per-dialog", type=int, default=5)
    s.add_argument("--dialog-limit", type=int, default=30)
    s.add_argument("--jsonl", action="store_true")
    s.set_defaults(func=cmd_search)

    snd = sub.add_parser("send", help="send a message or file")
    snd.add_argument("peer", help="@username, phone, id, or part of dialog title")
    snd.add_argument("message", nargs="?", help="message text or file caption")
    snd.add_argument("--file", action="append", help="файл для отправки; можно повторять — уйдёт альбомом")
    snd.add_argument("--as-document", action="store_true", help="отправить как файл, без сжатия")
    snd.add_argument("--voice", action="store_true", help="голосовое сообщение (ogg/opus)")
    snd.add_argument("--video-note", action="store_true", help="видео-кружок")
    snd.add_argument("--cover", help="своя обложка видео вместо кадра из него (Bot API 8.3); "
                                     "у ролика без звука Telegram её отбросит — он считает такой "
                                     "файл гифкой")
    snd.add_argument("--start-at", type=float, metavar="СЕК",
                     help="с какой секунды начинать проигрывание")
    snd.add_argument("--silent", action="store_true", help="без звука уведомления")
    snd.add_argument("--reply-to", type=int, help="id сообщения, на которое отвечаем")
    snd.add_argument("--comment-to", type=int, help="id поста канала — отправить комментарием")
    snd.add_argument("--parse-mode", choices=list(tgx_format.MODES), default="md",
                     help="разметка текста: md (по умолчанию), html или none")
    snd.add_argument("--no-preview", action="store_true", help="без превью ссылок")
    snd.add_argument("--schedule", help="отложить: ISO-время, например 2026-08-29T10:00")
    snd.add_argument("--effect", help="id эффекта при отправке; список — tgx effects")
    snd.set_defaults(func=cmd_send)

    ef = sub.add_parser("effects", help="эффекты сообщений: анимация при отправке")
    ef.add_argument("--search", help="фильтр по эмодзи")
    ef.add_argument("--limit", type=int, default=40)
    ef.add_argument("--jsonl", action="store_true")
    ef.set_defaults(func=cmd_effects)

    cp = sub.add_parser("copy", help="скопировать сообщения без подписи «переслано»")
    cp.add_argument("peer", help="откуда")
    cp.add_argument("to", help="куда")
    cp.add_argument("id", nargs="+", type=int)
    cp.add_argument("--drop-captions", action="store_true", help="без подписей к вложениям")
    cp.add_argument("--topic", type=int, help="id темы форума в получателе")
    cp.add_argument("--start-at", type=float, metavar="СЕК", help="сдвинуть точку старта видео")
    cp.add_argument("--silent", action="store_true")
    cp.set_defaults(func=cmd_copy)

    bs = sub.add_parser("boosts", help="бусты канала: уровень, кто дал, свои слоты")
    bs_sub = bs.add_subparsers(dest="boostcmd", required=True)
    bs.set_defaults(func=cmd_boosts)
    b_st = bs_sub.add_parser("status", help="уровень канала и сколько до следующего")
    b_st.add_argument("chat")
    b_who = bs_sub.add_parser("who", help="кто бустил канал")
    b_who.add_argument("chat")
    b_who.add_argument("--limit", type=int, default=50)
    b_who.add_argument("--jsonl", action="store_true")
    b_my = bs_sub.add_parser("mine", help="мои слоты бустов")
    b_my.add_argument("--jsonl", action="store_true")
    b_gv = bs_sub.add_parser("give", help="забустить канал")
    b_gv.add_argument("chat")
    b_gv.add_argument("--slot", action="append", help="конкретные слоты; можно повторять")

    ex = sub.add_parser("export", help="export recent messages from a peer")
    ex.add_argument("peer")
    ex.add_argument("--limit", type=int, default=1000)
    ex.add_argument("--output", required=True)
    ex.add_argument("--format", choices=["jsonl", "json", "csv"], default="jsonl")
    ex.add_argument("--with-sender", action="store_true")
    ex.set_defaults(func=cmd_export)
    return p


async def amain() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "plain", False):
        render.set_plain(True)
    if getattr(args, "list", False):
        print(" ".join(tgx_splash.available()))
        return
    if not getattr(args, "cmd", None):
        overview(parser)
        return
    await args.func(args)


# Errors these modules raise are already written for a person to read; a stack
# trace on top of them only hides the sentence that explains what to do.
SPOKEN_ERRORS = (PeerError, tgx_article.ArticleError, tgx_bots.BotError, tgx_business.BusinessError, tgx_calls.CallError, tgx_confirm.ConfirmError, tgx_contacts.ContactError,
                 tgx_banner.BannerError, tgx_folders.FolderError, tgx_forum.ForumError, tgx_guard.GuardError, tgx_net.NetError, tgx_pay.PayError, tgx_pending.PendingError, tgx_poll.PollError,
                 tgx_profile.ProfileError,
                 tgx_ai.AIError, tgx_chatx.ChatXError, tgx_takeout.TakeoutError, tgx_triage.TriageError, tgx_notify.NotifyError, tgx_safety.SafetyError, tgx_groups.GroupError, tgx_chanadmin.ChanError, tgx_inline.InlineError, tgx_rich.RichError, tgx_security.SecurityError, tgx_stats.StatsError, tgx_stickers.StickerError,
                 tgx_stories.StoryError,
                 tgx_transcribe.TranscribeError)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except SPOKEN_ERRORS as exc:
        render.fail(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
