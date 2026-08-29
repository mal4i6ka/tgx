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
import tgx_bots
import tgx_business
import tgx_confirm
import tgx_format
import tgx_forum
import tgx_guard
import tgx_net
import tgx_pay
import tgx_poll
import tgx_profile
import tgx_rich
import tgx_transcribe
import tgx_render as render
import tgx_splash

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
        "text": msg.message or "",
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

        if args.paycmd == "saved-info":
            render.emit(await pay.saved_info())
            return

        # ── тратящее и необратимое: только через подтверждение человеком ────
        async def gated(title: str, details: str, danger: str) -> dict[str, Any]:
            if not args.confirm_to:
                raise tgx_pay.PayError(
                    "это действие тратит деньги или необратимо — нужно подтверждение: "
                    "добавьте --confirm-to КОГО --as @бот")
            verdict = await ask_human(client, args.bot, args.confirm_to, title, details,
                                      danger=danger, timeout=args.timeout)
            if verdict["decision"] != "approved":
                render.emit({"ok": False, "действие": "отменено", **verdict})
                raise SystemExit(2)
            return verdict

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
            rows = tgx_bots.parse_buttons(args.button)
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
        bot = await _with_user(lambda c: tgx_bots.BotFather(c).create(args.name, args.username))
        registry.add(bot)
        render.emit({"ok": True, "username": bot.username, "name": bot.name,
                     "token": bot.token if args.reveal else tgx_bots.mask(bot.token),
                     "saved_to": str(registry.path)})
        return

    if command == "secretary":
        on = args.state == "on"
        text = await _with_user(lambda c: tgx_bots.BotFather(c).secretary(args.username, on))
        render.emit({"ok": True, "username": args.username.lstrip("@"),
                     "secretary_mode": "on" if on else "off", "botfather": text})
        return

    if command in {"token", "revoke"}:
        getter = (lambda c: tgx_bots.BotFather(c).token(args.username)) if command == "token" \
            else (lambda c: tgx_bots.BotFather(c).revoke(args.username))
        token = await _with_user(getter)
        stored = registry.load().get(args.username.lstrip("@"))
        bot = tgx_bots.Bot(username=args.username.lstrip("@"),
                           name=stored.name if stored else "", token=token)
        registry.add(bot)
        render.emit({"ok": True, "username": bot.username,
                     "token": token if args.reveal else tgx_bots.mask(token)})
        return

    if command == "mine":
        names = await _with_user(lambda c: tgx_bots.BotFather(c).mine())
        render.emit({"bots": names})
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
            # Файлы для блоков-документов подставляются по имени из --attach.
            attached = dict(pair.partition("=")[::2] for pair in (args.attach or []))
            for block in blocks:
                if block.get("type") == "document":
                    name = str(block.get("document", {}).get("media", "")).removeprefix("attach://")
                    if name in attached:
                        block["_upload"] = Path(attached[name]).expanduser()
        markdown = Path(args.file).expanduser().read_text() if args.file and not blocks else (args.text or "")
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
                buttons=args.button or "", silent=args.silent, protect=args.protect,
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
                args.peer, args.text or "", buttons=args.button or "", parse_mode=args.parse_mode,
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
    ("аккаунт", ["auth", "me", "profile", "profile-get", "profile-edit", "profile-photo-set", "profile-photos", "profile-photo-delete"]),
    ("чаты и папки", ["dialogs", "folders", "folder-upsert"]),
    ("сообщения", ["history", "search", "send", "edit", "delete", "forward", "react", "pin", "pinned", "todo", "todo-check", "todo-add", "format", "export", "message-get", "message-click"]),
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

    bot = sub.add_parser("bot", help="боты: создание через BotFather, токены, посты от их имени")
    bot_sub = bot.add_subparsers(dest="botcmd", required=True)
    bot.set_defaults(func=cmd_bot)

    b_create = bot_sub.add_parser("create", help="создать бота через BotFather")
    b_create.add_argument("name", help="человеческое имя")
    b_create.add_argument("username", help="адрес, обязан заканчиваться на bot")
    b_create.add_argument("--reveal", action="store_true", help="показать токен целиком")

    b_list = bot_sub.add_parser("list", help="сохранённые боты (токены скрыты)")
    b_list.add_argument("--reveal", action="store_true")
    b_list.add_argument("--jsonl", action="store_true")

    for name, help_text in (("token", "получить токен у BotFather и сохранить"),
                            ("revoke", "отозвать токен и сохранить новый")):
        parser = bot_sub.add_parser(name, help=help_text)
        parser.add_argument("username")
        parser.add_argument("--reveal", action="store_true")

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
    b_post.add_argument("--button", help="кнопки: «Текст=https://…, Ещё=webapp:https://…»")
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
    b_rich.add_argument("--button", help="кнопки под сообщением")
    b_rich.add_argument("--media", action="append",
                        help="имя=ссылка для ![](tg://photo?id=имя); можно повторять")
    b_rich.add_argument("--topic", type=int, help="id темы форума")
    b_rich.add_argument("--blocks", help="файл JSON с блоками (Bot API 10.2+); "
                                        "кнопки и файлы внутри документа возможны только так")
    b_rich.add_argument("--attach", action="append", metavar="ИМЯ=ПУТЬ",
                        help="файл для блока-документа с attach://ИМЯ; можно повторять")
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

    y_cancel = gated_parser("cancel-subscription", "отменить или возобновить подписку")
    y_cancel.add_argument("chat")
    y_cancel.add_argument("id", help="id подписки")
    y_cancel.add_argument("--resume", action="store_true", help="возобновить вместо отмены")

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
    rich.add_argument("--button", help="кнопки под сообщением; см. tgx bot buttons")
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
SPOKEN_ERRORS = (PeerError, tgx_article.ArticleError, tgx_bots.BotError, tgx_business.BusinessError, tgx_confirm.ConfirmError,
                 tgx_banner.BannerError, tgx_forum.ForumError, tgx_guard.GuardError, tgx_net.NetError, tgx_pay.PayError, tgx_poll.PollError,
                 tgx_profile.ProfileError,
                 tgx_rich.RichError, tgx_transcribe.TranscribeError)


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
