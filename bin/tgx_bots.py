#!/usr/bin/env python3
"""Bots for tgx: a token registry, the @BotFather conversation, and posting as a bot.

Telegram has no API for creating a bot — it is a chat with @BotFather. So this
module drives that chat with the user's own account, checking what BotFather
answers at every step: it is a conversational interface and its wording drifts,
so a failed step reports what it actually said instead of guessing.

Tokens are credentials: the registry file is written 0600, listings show them
masked, and the full value is printed only when explicitly asked for.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

BOTFATHER = "BotFather"
TOKEN_RE = re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{30,})\b")
TROUBLE = ("sorry", "invalid", "taken", "too many", "error", "can't", "cannot")


class BotError(RuntimeError):
    """Something BotFather refused, with its own wording kept intact."""


def registry_path() -> Path:
    base = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
    return base / "data" / "bots.json"


def mask(token: str) -> str:
    if not token or ":" not in token:
        return ""
    head, _, tail = token.partition(":")
    return f"{head}:{tail[:3]}…{tail[-4:]}" if len(tail) > 10 else f"{head}:…"


@dataclass
class Bot:
    username: str
    name: str = ""
    token: str = ""
    added: str = ""
    note: str = ""

    def public(self, reveal: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["token"] = self.token if reveal else mask(self.token)
        return data


class Registry:
    """`data/bots.json`, kept readable only by the owner."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or registry_path()

    def load(self) -> dict[str, Bot]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise BotError(f"{self.path} повреждён: {exc}") from exc
        return {name: Bot(**data) for name, data in raw.items()}

    def save(self, bots: dict[str, Bot]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: asdict(bot) for name, bot in sorted(bots.items())}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        try:
            os.chmod(self.path, 0o600)      # tokens are credentials
        except OSError:
            pass

    def add(self, bot: Bot) -> Bot:
        bots = self.load()
        bot.username = bot.username.lstrip("@")
        bot.added = bot.added or datetime.now(timezone.utc).isoformat(timespec="seconds")
        bots[bot.username] = bot
        self.save(bots)
        return bot

    def remove(self, username: str) -> bool:
        bots = self.load()
        if bots.pop(username.lstrip("@"), None) is None:
            return False
        self.save(bots)
        return True

    def get(self, username: str) -> Bot:
        bot = self.load().get(username.lstrip("@"))
        if bot is None:
            raise BotError(f"бот @{username.lstrip('@')} не найден — добавьте его через `tgx bot token`")
        return bot


# ── the BotFather conversation ───────────────────────────────────────────────
class BotFather:
    """Every step checks the answer, and failures quote BotFather verbatim."""

    def __init__(self, client: Any, timeout: int = 30) -> None:
        self.client = client
        self.timeout = timeout

    async def _reply(self, conv: Any, sent: str | None = None) -> Any:
        if sent is not None:
            await conv.send_message(sent)
        try:
            return await conv.get_response(timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise BotError("BotFather не ответил — попробуйте ещё раз через минуту") from exc

    @staticmethod
    def _check(message: Any, expect: Sequence[str] = ()) -> str:
        text = (getattr(message, "message", "") or "").strip()
        lowered = text.lower()
        if expect and not any(marker.lower() in lowered for marker in expect):
            if any(word in lowered for word in TROUBLE):
                raise BotError(f"BotFather отказал: {text}")
            raise BotError(f"неожиданный ответ BotFather: {text[:300]}")
        return text

    @staticmethod
    def _token(text: str) -> str:
        found = TOKEN_RE.search(text or "")
        if not found:
            raise BotError(f"в ответе нет токена: {text[:300]}")
        return found.group(1)

    async def _pick(self, conv: Any, message: Any, username: str) -> Any:
        """Click the bot in BotFather's keyboard and take the next reply."""
        handle = "@" + username.lstrip("@")
        rows = getattr(message, "buttons", None) or []
        for row_index, row in enumerate(rows):
            for col_index, button in enumerate(row):
                if (getattr(button, "text", "") or "").strip().lower() == handle.lower():
                    await message.click(row_index, col_index)
                    return await self._reply(conv)
        available = ", ".join(
            (getattr(b, "text", "") or "") for row in rows for b in row
        ) or "кнопок нет"
        raise BotError(f"{handle} нет среди ваших ботов ({available})")

    async def create(self, name: str, username: str) -> Bot:
        """/newbot → name → username → token."""
        username = username.lstrip("@")
        if not username.lower().endswith("bot"):
            raise BotError("имя бота должно заканчиваться на bot, этого требует Telegram")
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            await self._reply(conv, "/newbot")
            self._check(await self._reply(conv, name), ("username", "имя"))
            final = self._check(await self._reply(conv, username), ("token", "use this token", "токен"))
            token = self._token(final)
        return Bot(username=username, name=name, token=token,
                   added=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    async def token(self, username: str) -> str:
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            chooser = await self._reply(conv, "/token")
            answer = await self._pick(conv, chooser, username)
            return self._token(self._check(answer))

    async def revoke(self, username: str) -> str:
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            chooser = await self._reply(conv, "/revoke")
            answer = await self._pick(conv, chooser, username)
            return self._token(self._check(answer))

    async def _set(self, command: str, username: str, value: str, expect: Sequence[str] = ("success",)) -> str:
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            chooser = await self._reply(conv, command)
            await self._pick(conv, chooser, username)
            return self._check(await self._reply(conv, value), expect)

    async def set_name(self, username: str, value: str) -> str:
        return await self._set("/setname", username, value)

    async def set_about(self, username: str, value: str) -> str:
        return await self._set("/setabouttext", username, value)

    async def set_description(self, username: str, value: str) -> str:
        return await self._set("/setdescription", username, value)

    async def set_commands(self, username: str, commands: str) -> str:
        """`commands` is BotFather's own format: one `name - description` per line."""
        return await self._set("/setcommands", username, commands)

    async def _menu(self, message: Any, label: str) -> Any:
        """Click an inline button by label and re-read the message it edits in place.

        BotFather's settings menus edit one message instead of sending new ones, so
        `conv.get_response()` never fires here — the edit has to be waited for.
        """
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for col_index, button in enumerate(row):
                if (getattr(button, "text", "") or "").strip().lower() == label.lower():
                    was = getattr(message, "edit_date", None)
                    await message.click(row_index, col_index)
                    for _ in range(int(self.timeout / 0.4)):
                        await asyncio.sleep(0.4)
                        fresh = await self.client.get_messages(BOTFATHER, ids=message.id)
                        if fresh and fresh.edit_date and (not was or fresh.edit_date > was):
                            return fresh
                    raise BotError(f"BotFather не обновил меню после «{label}»")
        seen = ", ".join((getattr(b, "text", "") or "")
                         for row in (getattr(message, "buttons", None) or []) for b in row)
        raise BotError(f"кнопки «{label}» нет в меню ({seen or 'кнопок нет'})")

    async def secretary(self, username: str, on: bool = True) -> str:
        """Turn the bot's secretary mode on or off.

        Without it `account.updateConnectedBot` fails with BOT_BUSINESS_MISSING.
        BotFather calls the switch "Secretary Mode"; the API calls it business mode.
        """
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            listing = await self._reply(conv, "/mybots")
        page = await self._menu(listing, "@" + username.lstrip("@"))
        page = await self._menu(page, "Bot Settings")
        page = await self._menu(page, "Secretary Mode")
        wanted = "enabled" if on else "disabled"
        text = (getattr(page, "message", "") or "")
        if wanted in text.lower():
            return text.strip()
        page = await self._menu(page, "Turn on" if on else "Turn secretary mode off")
        text = (getattr(page, "message", "") or "").strip()
        if wanted not in text.lower():
            raise BotError(f"не удалось переключить секретарский режим: {text[:200]}")
        return text

    async def mine(self) -> list[str]:
        """Bot usernames BotFather knows about."""
        async with self.client.conversation(BOTFATHER, timeout=self.timeout) as conv:
            listing = await self._reply(conv, "/mybots")
            rows = getattr(listing, "buttons", None) or []
            return [
                (getattr(button, "text", "") or "").strip().lstrip("@")
                for row in rows for button in row
                if (getattr(button, "text", "") or "").strip().startswith("@")
            ]


# ── posting as a bot ─────────────────────────────────────────────────────────
BUTTON_SYNTAX = """Текст=https://…            ссылка
Текст=webapp:https://…     мини-приложение (Web App)
Текст=cb:данные            callback — отвечает работающий бот
Текст=switch:запрос        поделиться инлайн-запросом
Текст=copy:что копировать  кнопка копирования
Текст=user:123456          профиль пользователя
Текст[primary]=…          цветная кнопка: primary, danger, success
Текст[success:5312…]=…    та же кнопка с эмодзи вместо галочки
ряды разделяются «;», кнопки в ряду — запятой"""


STYLES = ("primary", "danger", "success")


def parse_style(label: str) -> tuple[str, Any]:
    """`Скачать[primary]` или `Готово[success:5312…]` → подпись и стиль кнопки.

    Слой 227 разрешил кнопкам свой цвет фона и эмодзи вместо галочки; до него
    кнопка была только серой, поэтому синтаксис добавлен, а не заменён — старые
    строки без скобок работают как работали.
    """
    from telethon.tl.types import KeyboardButtonStyle

    if not (label.endswith("]") and "[" in label):
        return label, None
    head, _, tail = label.rpartition("[")
    spec = tail[:-1].strip().lower()
    name, _, icon = spec.partition(":")
    if name not in STYLES:
        raise BotError(f"стиль «{name}» неизвестен; есть: {', '.join(STYLES)}")
    if icon and not icon.isdigit():
        raise BotError(f"иконке кнопки нужен числовой id эмодзи, а не «{icon}»")
    return head.strip(), KeyboardButtonStyle(
        bg_primary=name == "primary" or None,
        bg_danger=name == "danger" or None,
        bg_success=name == "success" or None,
        icon=int(icon) if icon else None)


def parse_buttons(spec: str) -> list[list[Any]]:
    """`Текст=https://…, Ещё=webapp:https://… ; вторая строка=cb:data` → inline keyboard.

    Bare `https://` targets are link buttons; a `webapp:` prefix opens a Telegram
    Mini App; the rest are named explicitly. Anything unprefixed and non-http is
    treated as callback data, which only a running bot can answer.
    """
    from telethon.tl.types import (KeyboardButtonCallback, KeyboardButtonCopy,
                                   KeyboardButtonSwitchInline, KeyboardButtonUrl,
                                   KeyboardButtonUserProfile, KeyboardButtonWebView)

    rows: list[list[Any]] = []
    for line in (spec or "").split(";"):
        row: list[Any] = []
        for chunk in line.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            label, sep, target = chunk.partition("=")
            if not sep:
                raise BotError(f"кнопка «{chunk}» без адреса — нужно «Текст=https://…»\n{BUTTON_SYNTAX}")
            label, target = label.strip(), target.strip()
            label, style = parse_style(label)
            kind, _, rest = target.partition(":")
            kind = kind.lower()
            if target.startswith(("http://", "https://", "tg://")):
                row.append(KeyboardButtonUrl(label, target, style=style))
            elif kind == "webapp":
                if not rest.startswith("https://"):
                    raise BotError(f"веб-приложению «{label}» нужен https-адрес")
                row.append(KeyboardButtonWebView(label, rest, style=style))
            elif kind == "cb":
                row.append(KeyboardButtonCallback(label, rest.encode(), style=style))
            elif kind == "switch":
                row.append(KeyboardButtonSwitchInline(label, rest, same_peer=False))
            elif kind == "copy":
                row.append(KeyboardButtonCopy(label, rest))
            elif kind == "user":
                if not rest.isdigit():
                    raise BotError(f"кнопке профиля «{label}» нужен числовой id")
                row.append(KeyboardButtonUserProfile(label, int(rest)))
            else:
                row.append(KeyboardButtonCallback(label, target.encode(), style=style))
        if row:
            rows.append(row)
    return rows


# Ошибки Telegram, которые чаще всего встречает бот, — на человеческом языке.
BOT_HINTS = {
    "USER_BOT_TO_BOT_DISABLED": (
        "переписка между ботами выключена у одного из них. Она появилась в Bot API 10.0, "
        "но включается не в меню BotFather, а в его мини-приложении — и должна быть "
        "включена у обоих ботов"),
    "USER_IS_BOT": "боту нельзя писать этому собеседнику",
    "BOT_METHOD_INVALID": "этот метод боту недоступен",
    "PEER_ID_INVALID": "бот не знает этого чата — добавьте его туда",
    "CHAT_WRITE_FORBIDDEN": "боту здесь запрещено писать",
    "CHAT_ADMIN_REQUIRED": "боту нужны права администратора в этом чате",
    "USERNAME_INVALID": "такой адрес Telegram не принимает; он должен заканчиваться на bot",
    "USERNAME_OCCUPIED": "этот адрес занят — придумайте другой",
    "USERNAME_PURCHASE_AVAILABLE": "адрес свободен, но продаётся на Fragment — бесплатно его не занять",
    "BOTS_TOO_MUCH": "у вас уже предельное число ботов; удалите ненужного через BotFather",
    "BOT_INVALID": "это не ваш бот — токен выдают только владельцу",
    "USER_ID_INVALID": "такого пользователя нет",
    "PASSWORD_HASH_INVALID": "неверный пароль двухфакторной защиты",
    "BOT_NOT_FOUND": "бот не найден",
}


def explain_bot_error(exc: Exception) -> Exception:
    """Ответ сервера → объяснение, что делать. Незнакомое отдаём как есть."""
    import tgx_net

    return tgx_net.explain(exc, BOT_HINTS, BotError)


class Direct:
    """Боты без разговора с BotFather — насколько это вообще возможно.

    Слой 227 даёт часть операций обычными вызовами, но не все и не всем:

    * `createBot` создаёт бота **от имени бота-управляющего**, а не от вашего
      имени; управляющему нужно право «управлять ботами» от BotFather. Без него
      сервер отвечает MANAGER_PERMISSION_MISSING.
    * `exportBotToken`, `getBotCommands`, `getBotMenuButton` — вызовы для бота:
      из пользовательского аккаунта приходит USER_BOT_REQUIRED. Им нужна сессия
      самого бота.
    * `getAdminedBots` и `getPreviewMedias` работают из вашего аккаунта.

    Поэтому разговорный путь остаётся основным для создания и токенов.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise explain_bot_error(exc) from exc

    async def create(self, name: str, username: str, manager: Any) -> Bot:
        """Создать бота через бота-управляющего."""
        from telethon.tl import functions

        handle = username.lstrip("@")
        if not handle.lower().endswith("bot"):
            raise BotError("имя бота должно заканчиваться на bot, этого требует Telegram")
        owner = await self.client.get_input_entity(manager)
        user = await self._call(functions.bots.CreateBotRequest(
            name=name.strip(), username=handle, manager_id=owner, via_deeplink=None))
        return Bot(username=getattr(user, "username", handle) or handle,
                   name=name.strip(), token="",
                   added=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    async def mine(self) -> list[dict[str, Any]]:
        """Боты, которыми вы распоряжаетесь. Работает из вашего аккаунта."""
        from telethon.tl import functions

        result = await self._call(functions.bots.GetAdminedBotsRequest())
        return [{"username": getattr(u, "username", None), "имя": getattr(u, "first_name", None),
                 "id": getattr(u, "id", None)} for u in (result or [])]

    async def previews(self, bot: Any) -> list[dict[str, Any]]:
        """Картинки-превью, которые бот показывает до запуска."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(bot)
        result = await self._call(functions.bots.GetPreviewMediasRequest(bot=entity))
        return [{"язык": getattr(m, "lang_code", None) or "по умолчанию",
                 "вид": type(getattr(m, "media", None)).__name__.replace("MessageMedia", "")}
                for m in (result or [])]

    # --- ниже то, что требует сессии самого бота ---

    async def token(self, bot: Any, *, revoke: bool = False) -> str:
        """Токен управляемого бота. Вызывать из сессии бота-управляющего."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(bot)
        result = await self._call(functions.bots.ExportBotTokenRequest(
            bot=entity, revoke=revoke or None))
        token = getattr(result, "token", None)
        if not token:
            raise BotError("сервер не вернул токен")
        return token

    async def commands(self, lang: str = "") -> list[dict[str, Any]]:
        """Свои команды глазами пользователя. Из сессии бота."""
        from telethon.tl import functions, types

        result = await self._call(functions.bots.GetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code=lang))
        return [{"команда": c.command, "описание": c.description} for c in (result or [])]

    async def menu_button(self, user: Any = None) -> dict[str, Any]:
        """Какая сейчас кнопка-меню. Из сессии бота."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(user) if user else types.InputUserSelf()
        result = await self._call(functions.bots.GetBotMenuButtonRequest(user_id=target))
        return {"вид": type(result).__name__.replace("BotMenuButton", "") or "по умолчанию",
                "текст": getattr(result, "text", None), "адрес": getattr(result, "url", None)}

    async def group_rights(self, *, invite: bool = False, pin: bool = False,
                           delete: bool = False, ban: bool = False,
                           info: bool = False, channel: bool = False) -> dict[str, Any]:
        """Права, которые бот просит при добавлении. Из сессии бота."""
        from telethon.tl import functions, types

        rights = types.ChatAdminRights(
            change_info=info, post_messages=channel, edit_messages=channel,
            delete_messages=delete, ban_users=ban, invite_users=invite,
            pin_messages=pin, add_admins=False, anonymous=False, manage_call=False,
            other=True, manage_topics=False)
        request = (functions.bots.SetBotBroadcastDefaultAdminRightsRequest
                   if channel else functions.bots.SetBotGroupDefaultAdminRightsRequest)
        await self._call(request(admin_rights=rights))
        asked = [n for n, v in (("приглашать", invite), ("закреплять", pin),
                                ("удалять", delete), ("банить", ban),
                                ("менять описание", info)) if v]
        return {"где": "канал" if channel else "группа", "просит": asked or ["базовые"]}


class BotSession:
    """A second Telethon client, signed in with a bot token."""

    def __init__(self, bot: Bot, api_id: int, api_hash: str, session_dir: Path | None = None) -> None:
        self.bot = bot
        self.api_id = api_id
        self.api_hash = api_hash
        base = session_dir or registry_path().parent
        self.session = base / f"bot-{bot.username}"
        self.client: Any = None

    async def __aenter__(self) -> "BotSession":
        from telethon import TelegramClient

        if not self.bot.token:
            raise BotError(f"у @{self.bot.username} нет сохранённого токена — `tgx bot token @{self.bot.username}`")
        self.session.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(str(self.session), self.api_id, self.api_hash)
        await self.client.start(bot_token=self.bot.token)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.client is not None:
            await self.client.disconnect()

    async def whoami(self) -> dict[str, Any]:
        me = await self.client.get_me()
        return {"id": me.id, "username": me.username, "name": me.first_name}

    @staticmethod
    def parse_commands(spec: str) -> list[Any]:
        """BotFather's own format — one `команда - описание` per line."""
        from telethon.tl.types import BotCommand

        commands = []
        for line in (spec or "").splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, description = line.partition("-")
            name = name.strip().lstrip("/").lower()
            if not name or not description.strip():
                raise BotError(f"строка «{line}» не разобрана: нужно «команда - описание»")
            if not name.replace("_", "").isalnum():
                raise BotError(f"недопустимое имя команды: {name}")
            commands.append(BotCommand(command=name, description=description.strip()))
        if not commands:
            raise BotError("список команд пуст")
        return commands

    async def set_info(self, name: str | None = None, about: str | None = None,
                       description: str | None = None, lang_code: str = "") -> dict[str, Any]:
        """Name, the «what can this bot do» text and the description — straight through the API.

        BotFather can do this too, but it is a chat: this way there is nothing to
        misparse and no conversation to get stuck in.
        """
        from telethon.tl import functions

        # `bot` identifies which bot to edit when a *user* calls this; called by the
        # bot itself it must be empty, otherwise Telegram answers BOT_INVALID.
        await self.client(functions.bots.SetBotInfoRequest(
            bot=None, lang_code=lang_code, name=name, about=about, description=description,
        ))
        return {"ok": True, "name": name, "about": about, "description": description}

    async def get_info(self, lang_code: str = "") -> dict[str, Any]:
        from telethon.tl import functions

        info = await self.client(functions.bots.GetBotInfoRequest(bot=None, lang_code=lang_code))
        return {"name": getattr(info, "name", ""), "about": getattr(info, "about", ""),
                "description": getattr(info, "description", "")}

    async def set_commands(self, spec: str, lang_code: str = "") -> dict[str, Any]:
        from telethon.tl import functions, types

        commands = self.parse_commands(spec)
        await self.client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code=lang_code, commands=commands))
        return {"ok": True, "commands": [c.command for c in commands]}

    async def set_menu_button(self, text: str = "", url: str = "", reset: bool = False) -> dict[str, Any]:
        """The bot's menu button — the standard way to hang a Mini App on a bot."""
        from telethon.tl import functions, types

        if reset:
            button: Any = types.BotMenuButtonDefault()
        else:
            if not url.startswith("https://"):
                raise BotError("мини-приложению нужен https-адрес")
            button = types.BotMenuButton(text=text or "Открыть", url=url)
        await self.client(functions.bots.SetBotMenuButtonRequest(
            user_id=types.InputUserEmpty(), button=button))
        return {"ok": True, "menu": "по умолчанию" if reset else f"{text or 'Открыть'} → {url}"}

    async def post(self, peer: str, text: str = "", buttons: str = "", parse_mode: str = "md",
                   link_preview: bool = True, silent: bool = False, files: Sequence[str] | None = None,
                   schedule: datetime | None = None) -> Any:
        import tgx_format

        body, entities = tgx_format.parse(text, parse_mode)
        markup = parse_buttons(buttons) if buttons else None
        try:
            return await self._deliver(peer, body, entities, markup, link_preview,
                                       silent, files, schedule)
        except Exception as exc:
            raise explain_bot_error(exc) from exc

    async def _deliver(self, peer: str, body: str, entities: Any, markup: Any,
                       link_preview: bool, silent: bool, files: Sequence[str] | None,
                       schedule: datetime | None) -> Any:
        target = await self.client.get_entity(peer)
        if files:
            import tgx_media

            paths = [str(Path(f).expanduser()) for f in files]
            poster = tgx_media.poster_frame(Path(paths[0])) if len(paths) == 1 else None
            sent = await self.client.send_file(
                target, paths if len(paths) > 1 else paths[0],
                caption=body or None, parse_mode=None, formatting_entities=entities or None,
                buttons=markup, silent=silent or None, schedule=schedule,
                thumb=str(poster) if poster else None,
                supports_streaming=any(tgx_media.is_video_file(p) for p in paths),
            )
            return sent[-1] if isinstance(sent, list) else sent
        if not body.strip():
            raise BotError("пустой пост")
        return await self.client.send_message(
            target, body, parse_mode=None, formatting_entities=entities or None,
            buttons=markup, link_preview=link_preview, silent=silent or None, schedule=schedule,
        )
