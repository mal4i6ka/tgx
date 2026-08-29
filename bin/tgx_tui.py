#!/usr/bin/env python3
"""tgx ui — a full-screen Telegram client for the terminal, built on Textual.

Two backends sit behind the same interface: the real Telethon client, and a
self-contained demo backend (`tgx ui --demo`) that needs no account and is what
the UI tests drive.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ItemGrid, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Checkbox, DirectoryTree, Footer, Input, OptionList, Select, Static, Tab, Tabs, TextArea
from textual.widgets.option_list import Option

import tgx_bots
import tgx_format
import tgx_rich
import tgx_media
from tgx_render import name_color

# ── themes ───────────────────────────────────────────────────────────────────
NIGHT = Theme(
    name="tgx-night",
    primary="#2AABEE",
    secondary="#229ED9",
    accent="#7BC862",
    foreground="#E4EDF5",
    background="#0E1621",
    surface="#17212B",
    panel="#22303C",
    boost="#182533",
    success="#4FCE5D",
    warning="#E5CA77",
    error="#E9576B",
    dark=True,
    variables={
        "footer-key-foreground": "#2AABEE",
        "footer-description-foreground": "#8FA6B8",
        "input-cursor-background": "#2AABEE",
        "input-selection-background": "#2AABEE 35%",
        "block-cursor-background": "#2AABEE",
        "block-cursor-foreground": "#0E1621",
    },
)

DAY = Theme(
    name="tgx-day",
    primary="#229ED9",
    secondary="#2AABEE",
    accent="#3FA34D",
    foreground="#101E29",
    background="#FFFFFF",
    surface="#F2F5F8",
    panel="#DDE5EC",
    boost="#EFF3F7",
    success="#3FA34D",
    warning="#B07D18",
    error="#D64550",
    dark=False,
    variables={
        "footer-key-foreground": "#0F7EB5",
        "input-selection-background": "#229ED9 30%",
    },
)

CONNECT_TIMEOUT = 25.0
READ_DWELL = 1.2      # seconds a chat must stay open before it counts as read
FIRST_PAGE = 200      # shown immediately; the rest streams in behind it

THEMES = ("tgx-night", "tgx-day", "textual-dark", "nord", "gruvbox", "catppuccin-mocha")

KIND_GLYPH = {"channel": "📣", "group": "👥", "user": "👤", "bot": "🤖", "chat": "💬"}


# ── data model ───────────────────────────────────────────────────────────────
@dataclass
class Chat:
    id: int
    name: str
    kind: str = "chat"
    username: str = ""
    unread: int = 0
    muted: bool = False
    pinned: bool = False
    date: datetime | None = None
    preview: str = ""
    entity: Any = None
    input_entity: Any = None
    folders: tuple[int, ...] = ()
    subtitle: str = ""
    can_post: bool = True            # False for channels we may only comment in
    archived: bool = False
    contact: bool = False
    forum: bool = False              # supergroup split into topics

    @property
    def glyph(self) -> str:
        return KIND_GLYPH.get(self.kind, "💬")


@dataclass
class Msg:
    id: int
    date: datetime | None = None
    text: str = ""
    out: bool = False
    sender_id: int | None = None
    sender: str = ""
    views: int | None = None
    reply_to: int | None = None
    media: str = ""
    edited: bool = False
    service: bool = False
    comments: int | None = None      # None = no discussion thread on this post
    reactions: tuple[tuple[str, int, bool], ...] = ()   # (emoji, count, mine)
    buttons: tuple[tuple[str, ...], ...] = ()           # inline keyboard rows
    entities: tuple[Any, ...] = ()                      # Telegram formatting spans
    pinned: bool = False
    rich: Any = None                                    # RichMessage: документ из блоков
    checklist_title: str = ""
    checklist: tuple[tuple[int, str, bool], ...] = ()   # (id, текст, выполнен)
    transcript: str = ""                                # расшифровка голосового


@dataclass
class Topic:
    """A thread in a forum-style supergroup."""

    id: int
    title: str
    closed: bool = False
    pinned: bool = False
    hidden: bool = False
    unread: int = 0

    @property
    def label(self) -> str:
        mark = "📌" if self.pinned else ("🔒" if self.closed else "")
        return f"{mark}{self.title}" + (f" {self.unread}" if self.unread else "")


@dataclass
class Folder:
    """A Telegram chat folder — explicit peers *plus* the category rules.

    Most folders are built from categories ("all groups", "unread only"), so
    filtering on `include_peers` alone hides almost everything they contain.
    """

    id: int
    title: str
    include: frozenset[int] = frozenset()
    exclude: frozenset[int] = frozenset()
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False

    def matches(self, chat: "Chat") -> bool:
        if chat.id in self.exclude:
            return False
        if self.exclude_muted and chat.muted:
            return False
        if self.exclude_read and not chat.unread:
            return False
        if self.exclude_archived and chat.archived:
            return False
        if chat.id in self.include:
            return True
        if chat.kind == "bot":
            return self.bots
        if chat.kind == "user":
            return self.contacts if chat.contact else self.non_contacts
        if chat.kind == "group":
            return self.groups
        if chat.kind == "channel":
            return self.broadcasts
        return False


def _local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def clock(dt: datetime | None) -> str:
    dt = _local(dt)
    return dt.strftime("%H:%M") if dt else "--:--"


def day_label(dt: datetime | None) -> str:
    dt = _local(dt)
    if dt is None:
        return "—"
    today = datetime.now().astimezone().date()
    delta = (today - dt.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return dt.strftime("%A").lower()
    return dt.strftime("%d %b %Y").lower()


def relative(dt: datetime | None) -> str:
    dt = _local(dt)
    if dt is None:
        return ""
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (now.date() - dt.date()).days == 1:
        return "yest"
    if (now - dt) < timedelta(days=7):
        return dt.strftime("%a").lower()
    return dt.strftime("%d.%m")


def one_line(text: str, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


# ── telegram backend ─────────────────────────────────────────────────────────
class TelegramBackend:
    """Thin async facade over Telethon, shaped for the UI."""

    demo = False

    def __init__(self, session: Path, api_id: int, api_hash: str) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Any = None
        self.me: Any = None
        self._names: dict[int, str] = {}
        self._raw: dict[tuple[int, int], Any] = {}   # (chat id, msg id) -> Telethon message
        self._paths: dict[tuple[int, int, bool], Path | None] = {}  # resolved previews; None = nothing to show

    async def connect(self) -> bool:
        from telethon import TelegramClient

        plain_task_factory()
        self.client = TelegramClient(str(self.session.with_suffix("")), self.api_id, self.api_hash)
        await self.client.connect()
        if await self.client.is_user_authorized():
            self.me = await self.client.get_me()
            return True
        return False

    # --- login steps (driven by LoginScreen) ---
    async def send_code(self, phone: str) -> None:
        await self.client.send_code_request(phone)

    async def sign_in(self, phone: str, code: str) -> bool:
        """True when signed in, False when a 2FA password is still needed."""
        from telethon.errors import SessionPasswordNeededError

        try:
            await self.client.sign_in(phone, code)
        except SessionPasswordNeededError:
            return False
        self.me = await self.client.get_me()
        return True

    async def sign_in_password(self, password: str) -> None:
        await self.client.sign_in(password=password)
        self.me = await self.client.get_me()

    async def whoami(self) -> str:
        me = self.me or await self.client.get_me()
        self.me = me
        handle = f"@{me.username}" if getattr(me, "username", None) else (getattr(me, "phone", "") or "")
        name = " ".join(p for p in [me.first_name or "", me.last_name or ""] if p).strip()
        return f"{name} {handle}".strip()

    @staticmethod
    def _can_post(entity: Any) -> bool:
        """Whether plain messages are allowed — channels usually only take comments."""
        from telethon.tl.types import Channel

        if not isinstance(entity, Channel):
            return True
        if getattr(entity, "creator", False):
            return True
        rights = getattr(entity, "admin_rights", None)
        if rights is not None and (getattr(rights, "post_messages", False) or getattr(rights, "other", False)):
            return True
        if getattr(entity, "broadcast", False):
            return False
        banned = getattr(entity, "banned_rights", None) or getattr(entity, "default_banned_rights", None)
        return not (banned is not None and getattr(banned, "send_messages", False))

    def _kind(self, entity: Any) -> str:
        from telethon.tl.types import Channel, Chat as TgChat, User

        if isinstance(entity, User):
            return "bot" if getattr(entity, "bot", False) else "user"
        if isinstance(entity, Channel):
            return "group" if getattr(entity, "megagroup", False) else "channel"
        if isinstance(entity, TgChat):
            return "group"
        return "chat"

    async def dialogs(self, limit: int | None = None) -> list[Chat]:
        rows: list[Chat] = []
        async for d in self.client.iter_dialogs(limit=limit or None):
            entity = d.entity
            msg = d.message
            preview = ""
            if msg is not None:
                preview = one_line(msg.message or media_label(msg) or "")
                if getattr(msg, "out", False):
                    preview = f"you: {preview}" if preview else "you: …"
            rows.append(
                Chat(
                    id=int(getattr(entity, "id", 0) or 0),
                    name=d.name or "(no title)",
                    kind=self._kind(entity),
                    username=getattr(entity, "username", None) or "",
                    unread=int(d.unread_count or 0),
                    muted=is_muted(d),
                    pinned=bool(getattr(d, "pinned", False)),
                    date=getattr(msg, "date", None),
                    preview=preview,
                    entity=entity,
                    input_entity=d.input_entity,
                    can_post=self._can_post(entity),
                    archived=bool(getattr(d, "archived", False)),
                    forum=bool(getattr(entity, "forum", False)),
                    contact=bool(getattr(entity, "contact", False)),
                )
            )
        return rows

    async def folders(self) -> list[Folder]:
        from telethon.tl import functions

        try:
            result = await self.client(functions.messages.GetDialogFiltersRequest())
        except Exception:
            return []
        out: list[Folder] = []
        for f in list(getattr(result, "filters", result) or []):
            fid = getattr(f, "id", None)
            if not isinstance(fid, int):
                continue
            title = getattr(f, "title", "")
            title = getattr(title, "text", None) or str(title or f"folder {fid}")
            def ids(*buckets: str) -> frozenset[int]:
                found = set()
                for bucket in buckets:
                    for peer in getattr(f, bucket, None) or []:
                        pid = getattr(peer, "user_id", None) or getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
                        if pid:
                            found.add(int(pid))
                return frozenset(found)

            out.append(Folder(
                id=fid,
                title=title,
                include=ids("pinned_peers", "include_peers"),
                exclude=ids("exclude_peers"),
                contacts=bool(getattr(f, "contacts", False)),
                non_contacts=bool(getattr(f, "non_contacts", False)),
                groups=bool(getattr(f, "groups", False)),
                broadcasts=bool(getattr(f, "broadcasts", False)),
                bots=bool(getattr(f, "bots", False)),
                exclude_muted=bool(getattr(f, "exclude_muted", False)),
                exclude_read=bool(getattr(f, "exclude_read", False)),
                exclude_archived=bool(getattr(f, "exclude_archived", False)),
            ))
        return out

    async def _sender_name(self, msg: Any) -> str:
        sid = getattr(msg, "sender_id", None)
        if getattr(msg, "out", False):
            return "you"
        if sid in self._names:
            return self._names[sid]
        sender = getattr(msg, "sender", None)
        if sender is None:
            try:
                sender = await msg.get_sender()
            except Exception:
                sender = None
        name = ""
        if sender is not None:
            parts = [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""]
            name = " ".join(p for p in parts if p).strip() or getattr(sender, "title", "") or getattr(sender, "username", "") or ""
        name = name or (getattr(msg, "post_author", None) or "")
        if not name and sid:
            name = str(sid)
        if sid:
            self._names[sid] = name
        return name

    def _remember(self, msg: Any) -> None:
        if getattr(msg, "media", None) is None:
            return
        if len(self._raw) > 400:                # bounded: previews only need recent messages
            for key in list(self._raw)[:200]:
                self._raw.pop(key, None)
        # Message ids repeat across chats, so the chat has to be part of the key.
        self._raw[(peer_id(getattr(msg, "chat_id", 0)), msg.id)] = msg

    @staticmethod
    def _pick_thumb(msg: Any, full: bool = False) -> Any | None:
        """What to hand to `download_media(thumb=...)`.

        Telethon's `_get_thumb` takes a PhotoSize / Cached / Stripped / VideoSize
        object, an int index, or None — but *not* a `PhotoSizeProgressive`, which
        is exactly what the largest size of a modern photo usually is; passing one
        makes the download silently return None.  So for the full-size view we ask
        for the biggest plain size, and fall back to asking by index.
        """
        from telethon.tl import types

        media = getattr(msg, "media", None)
        if media is None:
            return None
        photo = getattr(media, "photo", None)
        document = getattr(media, "document", None)
        if photo is not None:
            sizes = list(getattr(photo, "sizes", None) or [])
        elif document is not None:
            sizes = list(getattr(document, "thumbs", None) or [])
        else:
            return None
        usable = [size for size in sizes if getattr(size, "w", None)]
        if not usable:
            return None
        usable.sort(key=lambda size: size.w)
        if not full:
            return next((size for size in usable if size.w >= 200), usable[-1])

        biggest = usable[-1]
        if not isinstance(biggest, types.PhotoSizeProgressive):
            return biggest
        plain = [size for size in usable if not isinstance(size, types.PhotoSizeProgressive)]
        if plain and plain[-1].w >= 800:
            return plain[-1]
        # Ask by index: Telethon sorts the sizes itself and -1 is the largest.
        # Only safe when there are no video sizes, which would sort after them.
        return -1 if not getattr(photo, "video_sizes", None) else (plain[-1] if plain else None)

    async def thumbnail(self, chat: Chat, msg_id: int, cache: Path, full: bool = False) -> Path | None:
        """Cached path of a preview file, downloading only when it is really missing.

        Three layers, cheapest first: an in-process index (so re-opening a chat
        costs nothing), the files on disk, and only then the network.  A `None`
        is remembered too — a voice message never grows a preview, and asking
        Telegram again on every open would be pure waste.
        """
        key = (chat.id, msg_id, full)
        if key in self._paths:
            known = self._paths[key]
            if known is None or known.exists():
                return known

        stem = f"{chat.id}_{msg_id}{'_full' if full else ''}"
        cache.mkdir(parents=True, exist_ok=True)
        # Sweep leftovers first: a valid file sorting before them must not hide the junk.
        for leftover in sorted(cache.glob(f"{stem}.*")):
            if leftover.suffix in {".part", ".download"} or not leftover.stat().st_size:
                leftover.unlink(missing_ok=True)
        for existing in sorted(cache.glob(f"{stem}.*")):
            if existing.suffix == ".thumb":
                existing = tgx_media.with_real_suffix(existing)
            if existing.suffix == ".bin":
                self._paths[key] = None               # not something we can draw
                return None
            self._paths[key] = existing
            return existing

        msg = self._raw.get((chat.id, msg_id))
        if msg is None:
            msg = await self.client.get_messages(chat.entity, ids=msg_id)
        if msg is None:
            return None
        size = self._pick_thumb(msg, full=full)
        if size is None:
            self._paths[key] = None
            return None

        part = cache / f"{stem}.part"
        try:
            result = await self.client.download_media(msg, file=str(part), thumb=size)
        except BaseException:
            part.unlink(missing_ok=True)              # never leave half a file behind
            raise
        if not result:
            part.unlink(missing_ok=True)
            self._paths[key] = None
            return None
        final = tgx_media.with_real_suffix(Path(result))
        if final.suffix == ".bin":
            self._paths[key] = None
            return None
        self._paths[key] = final
        return final

    async def to_msg(self, msg: Any) -> Msg:
        self._remember(msg)
        action = getattr(msg, "action", None)
        if action is not None:
            return Msg(
                id=msg.id,
                date=getattr(msg, "date", None),
                text=service_label(action),
                service=True,
                out=bool(getattr(msg, "out", False)),
            )
        return Msg(
            id=msg.id,
            date=getattr(msg, "date", None),
            text=msg.message or "",
            out=bool(getattr(msg, "out", False)),
            sender_id=getattr(msg, "sender_id", None),
            sender=await self._sender_name(msg),
            views=getattr(msg, "views", None),
            reply_to=getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
            media=media_label(msg),
            edited=bool(getattr(msg, "edit_date", None)),
            comments=comment_count(msg),
            reactions=read_reactions(msg),
            buttons=read_buttons(msg),
            entities=tuple(getattr(msg, "entities", None) or ()),
            pinned=bool(getattr(msg, "pinned", False)),
            rich=getattr(msg, "rich_message", None),
            checklist_title=read_checklist(msg)[0],
            checklist=read_checklist(msg)[1],
        )

    async def history(self, chat: Chat, limit: int = 60, before_id: int | None = None,
                      topic_id: int | None = None) -> list[Msg]:
        kwargs: dict[str, Any] = {"limit": limit}
        if before_id:
            kwargs["offset_id"] = before_id
        if topic_id:
            kwargs["reply_to"] = topic_id          # a forum topic reads like a thread
        out = [await self.to_msg(m) async for m in self.client.iter_messages(chat.entity, **kwargs)]
        out.reverse()
        return out

    MEDIA_FILTERS = {
        "photo": "InputMessagesFilterPhotos",
        "video": "InputMessagesFilterVideo",
        "media": "InputMessagesFilterPhotoVideo",
        "file": "InputMessagesFilterDocument",
        "link": "InputMessagesFilterUrl",
        "voice": "InputMessagesFilterVoice",
        "music": "InputMessagesFilterMusic",
        "gif": "InputMessagesFilterGif",
        "round": "InputMessagesFilterRoundVideo",
        "mention": "InputMessagesFilterMyMentions",
        "pinned": "InputMessagesFilterPinned",
        "geo": "InputMessagesFilterGeo",
        "contact": "InputMessagesFilterContacts",
        "poll": "InputMessagesFilterPoll",
    }

    def _filter(self, kind: str | None) -> Any:
        from telethon.tl import types

        if not kind:
            return None
        name = self.MEDIA_FILTERS.get(kind.strip().lower())
        if name is None:
            raise ValueError(f"неизвестный тип «{kind}»; доступны: {', '.join(sorted(self.MEDIA_FILTERS))}")
        return getattr(types, name)()

    def _chat_of(self, msg: Any) -> Chat | None:
        """Which chat a globally found message belongs to."""
        entity = getattr(msg, "chat", None)
        if entity is None:
            return None
        name = getattr(entity, "title", None) or " ".join(
            p for p in [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""] if p
        ).strip() or str(getattr(entity, "id", ""))
        return Chat(id=int(getattr(entity, "id", 0) or 0), name=name, kind=self._kind(entity),
                    username=getattr(entity, "username", None) or "", entity=entity)

    async def search(self, chat: Chat | None, query: str, limit: int = 40, kind: str | None = None,
                     from_user: str | None = None, since: datetime | None = None,
                     until: datetime | None = None) -> list[tuple[Chat | None, Msg]]:
        """Search one chat or every chat, narrowed by media type, sender and dates.

        Telegram's global search has no sender field and ignores a lower date bound,
        so `from_user` is refused outside a chat and `since` is applied while reading
        (results arrive newest first, so it simply stops early).
        """
        sender = None
        if from_user:
            if chat is None:
                raise ValueError("отбор по отправителю работает только внутри чата")
            sender = await self.client.get_input_entity(from_user)
        kwargs: dict[str, Any] = {"search": query, "limit": None if since else int(limit)}
        media = self._filter(kind)
        if media is not None:
            kwargs["filter"] = media
        if sender is not None:
            kwargs["from_user"] = sender
        if until is not None:
            kwargs["offset_date"] = until

        hits: list[tuple[Chat | None, Msg]] = []
        async for msg in self.client.iter_messages(chat.entity if chat else None, **kwargs):
            when = getattr(msg, "date", None)
            if since is not None and when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < since:
                    break                       # newest first: everything after this is older
            hits.append((chat or self._chat_of(msg), await self.to_msg(msg)))
            if len(hits) >= int(limit):
                break
        return hits

    async def send(self, chat: Chat, text: str, reply_to: int | None = None,
                   parse_mode: str = "md", link_preview: bool = True,
                   topic_id: int | None = None) -> Msg:
        body, entities = tgx_format.parse(text, parse_mode)
        reply_to = reply_to or topic_id            # posting into a topic means replying to it
        sent = await self.client.send_message(
            chat.entity, body, reply_to=reply_to, parse_mode=None,
            formatting_entities=entities or None, link_preview=link_preview,
        )
        return await self.to_msg(sent)

    VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

    async def send_file(
        self,
        chat: Chat,
        paths: Sequence[str],
        caption: str = "",
        reply_to: int | None = None,
        comment_to: int | None = None,
        as_document: bool = False,
        voice: bool = False,
        video_note: bool = False,
        silent: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> Msg:
        """Send one file, or several as an album. Telegram decides photo/video/audio
        from the content; the flags force a document, a voice note or a round video."""
        files = [str(Path(p).expanduser()) for p in paths]
        if not files:
            raise ValueError("нечего отправлять")
        streaming = not as_document and any(tgx_media.is_video_file(f) for f in files)
        poster = None
        if streaming and len(files) == 1:
            poster = tgx_media.poster_frame(Path(files[0]))
        sent = await self.client.send_file(
            chat.entity,
            files if len(files) > 1 else files[0],
            caption=caption or None,
            thumb=str(poster) if poster else None,
            force_document=as_document,
            voice_note=voice,
            video_note=video_note,
            supports_streaming=streaming,
            silent=silent or None,
            reply_to=reply_to,
            comment_to=comment_to,
            progress_callback=progress,
        )
        if isinstance(sent, list):
            sent = sent[-1]
        return await self.to_msg(sent)

    async def react(self, chat: Chat, msg_id: int, emoji: str | None) -> None:
        """Set (or with emoji=None clear) your reaction on a message."""
        from telethon.tl import functions, types

        reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
        await self.client(functions.messages.SendReactionRequest(
            peer=chat.entity, msg_id=int(msg_id), reaction=reaction, add_to_recent=bool(emoji),
        ))

    async def edit(self, chat: Chat, msg_id: int, text: str, parse_mode: str | None = "md",
                   link_preview: bool = True) -> Msg:
        body, entities = tgx_format.parse(text, parse_mode or "none")
        edited = await self.client.edit_message(
            chat.entity, int(msg_id), body, parse_mode=None,
            formatting_entities=entities or None, link_preview=link_preview,
        )
        return await self.to_msg(edited)

    async def delete(self, chat: Chat, msg_ids: Sequence[int], revoke: bool = True) -> int:
        """Delete your own messages. `revoke` removes them for everyone."""
        await self.client.delete_messages(chat.entity, [int(i) for i in msg_ids], revoke=revoke)
        return len(msg_ids)

    async def forward(self, source: Chat, msg_ids: Sequence[int], target: Chat, silent: bool = False) -> int:
        await self.client.forward_messages(
            target.entity, [int(i) for i in msg_ids], from_peer=source.entity, silent=silent or None
        )
        return len(msg_ids)

    async def press_button(self, chat: Chat, msg_id: int, row: int, col: int) -> str:
        """Press an inline button and return whatever the bot answered."""
        msg = await self.client.get_messages(chat.entity, ids=int(msg_id))
        if msg is None or not getattr(msg, "buttons", None):
            raise ValueError("в этом сообщении нет кнопок")
        result = await msg.click(row, col)
        for attr in ("message", "text"):
            answer = getattr(result, attr, None)
            if isinstance(answer, str) and answer:
                return answer
        return "нажато"

    # ── channels and groups ──────────────────────────────────────────
    async def create_chat(self, title: str, kind: str = "channel", about: str = "",
                          username: str | None = None) -> Chat:
        """kind: channel (broadcast), group (supergroup) or forum (group with topics)."""
        from telethon.tl import functions

        if not title.strip():
            raise ValueError("нужно название")
        result = await self.client(functions.channels.CreateChannelRequest(
            title=title.strip(),
            about=(about or "").strip(),
            broadcast=kind == "channel",
            megagroup=kind != "channel",
            forum=kind == "forum",
        ))
        entity = result.chats[0]
        if username:
            await self.client(functions.channels.UpdateUsernameRequest(entity, username.lstrip("@")))
            entity = await self.client.get_entity(entity)
        return Chat(
            id=int(getattr(entity, "id", 0) or 0),
            name=getattr(entity, "title", title),
            kind=self._kind(entity),
            username=getattr(entity, "username", None) or "",
            entity=entity,
            can_post=True,
        )

    async def chat_details(self, chat: Chat) -> dict[str, Any]:
        """Everything the manage screen shows: counts, about, link, slow mode, rights."""
        from telethon.tl import functions
        from telethon.tl.types import Channel

        info: dict[str, Any] = {
            "title": chat.name, "username": chat.username, "kind": chat.kind,
            "about": "", "participants": None, "admins": None, "linked_chat_id": None,
            "slowmode": 0, "banned_rights": None, "invite": None, "creator": False, "can_edit": False,
        }
        if not isinstance(chat.entity, Channel):
            return info
        full = await self.client(functions.channels.GetFullChannelRequest(chat.entity))
        detail = full.full_chat
        info.update({
            "about": getattr(detail, "about", "") or "",
            "participants": getattr(detail, "participants_count", None),
            "admins": getattr(detail, "admins_count", None),
            "linked_chat_id": getattr(detail, "linked_chat_id", None),
            "slowmode": int(getattr(detail, "slowmode_seconds", 0) or 0),
            "banned_rights": getattr(chat.entity, "default_banned_rights", None),
            "invite": getattr(getattr(detail, "exported_invite", None), "link", None),
            "creator": bool(getattr(chat.entity, "creator", False)),
            "can_edit": bool(getattr(chat.entity, "creator", False) or getattr(chat.entity, "admin_rights", None)),
        })
        return info

    async def edit_chat(self, chat: Chat, title: str | None = None, about: str | None = None,
                        username: str | None = None) -> dict[str, Any]:
        from telethon.tl import functions

        changed: dict[str, Any] = {}
        if title is not None and title.strip() and title.strip() != chat.name:
            await self.client(functions.channels.EditTitleRequest(chat.entity, title.strip()))
            chat.name = title.strip()
            changed["title"] = chat.name
        if about is not None:
            await self.client(functions.messages.EditChatAboutRequest(chat.entity, about.strip()))
            changed["about"] = about.strip()
        if username is not None and username.strip().lstrip("@") != (chat.username or ""):
            handle = username.strip().lstrip("@")
            await self.client(functions.channels.UpdateUsernameRequest(chat.entity, handle))
            chat.username = handle
            changed["username"] = handle
        return changed

    async def set_slowmode(self, chat: Chat, seconds: int) -> int:
        from telethon.tl import functions

        await self.client(functions.channels.ToggleSlowModeRequest(chat.entity, int(seconds)))
        return int(seconds)

    async def set_permissions(self, chat: Chat, allowed: dict[str, bool]) -> dict[str, bool]:
        """Telegram stores *bans*, the UI speaks in allowances — invert here."""
        from telethon.tl import functions, types

        banned = {name: not value for name, value in allowed.items()}
        await self.client(functions.messages.EditChatDefaultBannedRightsRequest(
            peer=chat.entity, banned_rights=types.ChatBannedRights(until_date=None, **banned)
        ))
        return allowed

    async def set_discussion(self, channel: Chat, group: Chat | None) -> None:
        from telethon.tl import functions

        await self.client(functions.channels.SetDiscussionGroupRequest(
            broadcast=channel.entity, group=group.entity if group else None
        ))

    async def invite_link(self, chat: Chat, title: str | None = None, usage_limit: int | None = None,
                          request_needed: bool = False) -> str:
        from telethon.tl import functions

        result = await self.client(functions.messages.ExportChatInviteRequest(
            peer=chat.entity, title=title, usage_limit=usage_limit, request_needed=request_needed
        ))
        return getattr(result, "link", "")

    async def members(self, chat: Chat, kind: str = "recent", limit: int = 100) -> list[dict[str, Any]]:
        from telethon.tl import functions, types

        filters = {
            "recent": types.ChannelParticipantsRecent(),
            "admins": types.ChannelParticipantsAdmins(),
            "bots": types.ChannelParticipantsBots(),
            "banned": types.ChannelParticipantsKicked(q=""),
        }
        result = await self.client(functions.channels.GetParticipantsRequest(
            chat.entity, filters.get(kind, filters["recent"]), offset=0, limit=int(limit), hash=0
        ))
        rows = []
        for user in getattr(result, "users", []):
            name = " ".join(p for p in [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""] if p)
            rows.append({"id": user.id, "name": name.strip() or str(user.id),
                         "username": getattr(user, "username", None) or "",
                         "bot": bool(getattr(user, "bot", False))})
        return rows

    async def join(self, link_or_username: str) -> str:
        from telethon.tl import functions

        value = link_or_username.strip()
        if "joinchat" in value or value.startswith("+") or "/+" in value:
            invite_hash = value.rstrip("/").split("/")[-1].lstrip("+")
            result = await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
        else:
            result = await self.client(functions.channels.JoinChannelRequest(value.lstrip("@")))
        chats = getattr(result, "chats", None) or []
        return getattr(chats[0], "title", value) if chats else value

    async def leave(self, chat: Chat) -> None:
        from telethon.tl import functions

        await self.client(functions.channels.LeaveChannelRequest(chat.entity))

    async def publish(
        self,
        chat: Chat,
        text: str = "",
        *,
        parse_mode: str = "md",
        link_preview: bool = True,
        silent: bool = False,
        schedule: datetime | None = None,
        files: Sequence[str] | None = None,
        reply_to: int | None = None,
        comment_to: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> Msg:
        """One entry point for a post: formatted text, optional files, optional schedule."""
        body, entities = tgx_format.parse(text, parse_mode)
        if files:
            paths = [str(Path(p).expanduser()) for p in files]
            streaming = any(tgx_media.is_video_file(p) for p in paths)
            poster = tgx_media.poster_frame(Path(paths[0])) if streaming and len(paths) == 1 else None
            sent = await self.client.send_file(
                chat.entity,
                paths if len(paths) > 1 else paths[0],
                caption=body or None,
                thumb=str(poster) if poster else None,
                parse_mode=None,
                formatting_entities=entities or None,
                supports_streaming=streaming,
                silent=silent or None,
                schedule=schedule,
                reply_to=reply_to,
                comment_to=comment_to,
                progress_callback=progress,
            )
            if isinstance(sent, list):
                sent = sent[-1]
        else:
            if not body.strip():
                raise ValueError("пустой пост")
            sent = await self.client.send_message(
                chat.entity,
                body,
                parse_mode=None,
                formatting_entities=entities or None,
                link_preview=link_preview,
                silent=silent or None,
                schedule=schedule,
                reply_to=reply_to,
                comment_to=comment_to,
            )
        return await self.to_msg(sent)

    @staticmethod
    def _todo_text(value: str) -> Any:
        """Checklist titles carry entities, so the items can be formatted too."""
        from telethon.tl.types import TextWithEntities

        body, entities = tgx_format.parse(value, "md")
        return TextWithEntities(text=body, entities=entities or [])

    async def send_checklist(self, chat: Chat, title: str, items: Sequence[str],
                             others_can_append: bool = True, others_can_complete: bool = True,
                             reply_to: int | None = None) -> Msg:
        from telethon.tl import types

        cleaned = [i.strip() for i in items if i.strip()]
        if not title.strip() or not cleaned:
            raise ValueError("нужны заголовок и хотя бы один пункт")
        todo = types.TodoList(
            title=self._todo_text(title),
            list=[types.TodoItem(id=index + 1, title=self._todo_text(text))
                  for index, text in enumerate(cleaned)],
            others_can_append=others_can_append,
            others_can_complete=others_can_complete,
        )
        sent = await self.client.send_file(chat.entity, types.InputMediaTodo(todo=todo), reply_to=reply_to)
        return await self.to_msg(sent)

    async def append_checklist(self, chat: Chat, msg_id: int,
                               items: Sequence[str]) -> tuple[tuple[int, str, bool], ...]:
        """Returns the list as it now stands on the server, so callers never guess."""
        from telethon.tl import functions, types

        current = await self.client.get_messages(chat.entity, ids=int(msg_id))
        _, existing = read_checklist(current) if current is not None else ("", ())
        start = max((i for i, _, _ in existing), default=0)
        cleaned = [i.strip() for i in items if i.strip()]
        if not cleaned:
            raise ValueError("нечего добавлять")
        await self.client(functions.messages.AppendTodoListRequest(
            peer=chat.entity, msg_id=int(msg_id),
            list=[types.TodoItem(id=start + n + 1, title=self._todo_text(text))
                  for n, text in enumerate(cleaned)],
        ))
        return await self._reread_checklist(chat, msg_id)

    async def toggle_checklist(self, chat: Chat, msg_id: int, done: Sequence[int] = (),
                               undone: Sequence[int] = ()) -> tuple[tuple[int, str, bool], ...]:
        from telethon.tl import functions

        await self.client(functions.messages.ToggleTodoCompletedRequest(
            peer=chat.entity, msg_id=int(msg_id),
            completed=[int(i) for i in done], incompleted=[int(i) for i in undone],
        ))
        return await self._reread_checklist(chat, msg_id)

    async def _reread_checklist(self, chat: Chat, msg_id: int) -> tuple[tuple[int, str, bool], ...]:
        fresh = await self.client.get_messages(chat.entity, ids=int(msg_id))
        return read_checklist(fresh)[1] if fresh is not None else ()

    async def topics(self, chat: Chat, limit: int = 100) -> list[Topic]:
        from telethon.tl import functions

        if not chat.forum:
            return []
        result = await self.client(functions.messages.GetForumTopicsRequest(
            peer=chat.entity, offset_date=None, offset_id=0, offset_topic=0, limit=int(limit), q=None
        ))
        found = []
        for topic in getattr(result, "topics", []) or []:
            if not hasattr(topic, "title"):
                continue                                   # ForumTopicDeleted
            found.append(Topic(
                id=int(topic.id), title=str(topic.title),
                closed=bool(getattr(topic, "closed", False)),
                pinned=bool(getattr(topic, "pinned", False)),
                hidden=bool(getattr(topic, "hidden", False)),
                unread=int(getattr(topic, "unread_count", 0) or 0),
            ))
        found.sort(key=lambda t: (not t.pinned, t.title.lower()))
        return found

    async def create_topic(self, chat: Chat, title: str, icon_color: int | None = None) -> Topic:
        from telethon.tl import functions

        if not title.strip():
            raise ValueError("нужно название темы")
        from telethon import helpers

        result = await self.client(functions.messages.CreateForumTopicRequest(
            peer=chat.entity, title=title.strip(), icon_color=icon_color,
            random_id=helpers.generate_random_long(),
        ))
        topic_id = 0
        for update in getattr(result, "updates", []) or []:
            topic_id = getattr(update, "id", 0) or getattr(getattr(update, "message", None), "id", 0) or topic_id
        return Topic(id=int(topic_id), title=title.strip())

    async def edit_topic(self, chat: Chat, topic_id: int, title: str | None = None,
                         closed: bool | None = None, hidden: bool | None = None) -> None:
        """Через общий модуль форумов: там же лежат правила, которые ставит сервер
        (переименование и закрытие идут разными запросами, скрывать можно только
        «Общую»), и незачем держать их в двух местах."""
        import tgx_forum

        await tgx_forum.Forum(self.client).edit(
            chat.entity, topic_id, title=title, closed=closed, hidden=hidden)

    async def delete_topic(self, chat: Chat, topic_id: int) -> int:
        """Удалить тему вместе с перепиской. «Общую» модуль не отдаст."""
        import tgx_forum

        result = await tgx_forum.Forum(self.client).delete(chat.entity, topic_id)
        return int(result.get("messages", 0))

    async def transcribe(self, chat: Chat, msg_id: int) -> dict[str, Any]:
        """Расшифровать голосовое. Текст приходит не сразу — модуль его дожидается."""
        import tgx_transcribe

        return await tgx_transcribe.Transcriber(self.client).transcribe(chat.entity, msg_id)

    async def pin_topic(self, chat: Chat, topic_id: int, pinned: bool = True) -> None:
        from telethon.tl import functions

        await self.client(functions.messages.UpdatePinnedForumTopicRequest(
            peer=chat.entity, topic_id=int(topic_id), pinned=bool(pinned)
        ))

    async def pin(self, chat: Chat, msg_id: int, silent: bool = True, unpin: bool = False) -> None:
        """Pin quietly by default — a loud pin notifies everyone in the chat."""
        from telethon.tl import functions

        await self.client(functions.messages.UpdatePinnedMessageRequest(
            peer=chat.entity, id=int(msg_id), silent=silent, unpin=unpin
        ))

    async def pinned(self, chat: Chat, limit: int = 20) -> list[Msg]:
        from telethon.tl import types

        rows = [await self.to_msg(m) async for m in self.client.iter_messages(
            chat.entity, limit=int(limit), filter=types.InputMessagesFilterPinned())]
        rows.reverse()
        return rows

    def list_bots(self) -> list[dict[str, str]]:
        """Bots whose tokens are stored locally — tokens never leave the registry."""
        try:
            return [{"username": bot.username, "name": bot.name} for bot in tgx_bots.Registry().load().values()]
        except Exception:
            return []

    async def publish_rich(self, bot_username: str, chat: Chat, markdown: str, *, buttons: str = "",
                           silent: bool = False, draft: bool = False,
                           media: Sequence[dict[str, Any]] = (), topic: int | None = None) -> Msg:
        """A Bot API 10.1 rich message — headings, tables, task lists, footnotes.

        MTProto has no method for these, so this goes out over HTTP with the bot's
        own token; everything else in tgx still speaks MTProto. The reply carries
        only an id, so the message is reconstructed locally for the conversation.
        """
        if not bot_username:
            # A user account can send these straight over MTProto; only the Bot API
            # restricts rich messages to bots.
            from telethon import helpers
            from telethon.tl import functions, types

            tgx_rich.check_limits(markdown, media)
            # Тема форума — это тред служебного сообщения, которым её создали:
            # чтобы попасть в неё, сообщение отвечает на её корневое сообщение.
            reply = types.InputReplyToMessage(reply_to_msg_id=int(topic)) if topic else None
            await self.client(functions.messages.SendMessageRequest(
                peer=chat.entity, message="", random_id=helpers.generate_random_long(),
                rich_message=types.InputRichMessageMarkdown(
                    markdown=markdown, rtl=False, noautolink=False, files=[]),
                reply_to=reply, silent=silent or None,
            ))
            fresh = await self.client.get_messages(chat.entity, limit=1)
            return await self.to_msg(fresh[0]) if fresh else Msg(id=0, date=datetime.now(timezone.utc), out=True)

        bot = tgx_bots.Registry().get(bot_username)
        if not bot.token:
            raise ValueError(f"у @{bot.username} нет токена — `tgx bot token @{bot.username}`")
        result = await asyncio.to_thread(
            tgx_rich.send_rich, bot.token, tgx_rich.bot_chat_id(chat), markdown,
            buttons=buttons, silent=silent, draft=draft, media=list(media),
        )
        headline = next((line for line in markdown.splitlines() if line.strip()), "")
        return Msg(id=int(result.get("message_id", 0) or 0), date=datetime.now(timezone.utc), out=True,
                   sender=f"@{bot.username}", sender_id=999, media="📄 rich-сообщение",
                   text=one_line(headline.lstrip("# ").strip(), 80))

    async def publish_as(self, bot_username: str, chat: Chat, text: str = "", *, buttons: str = "",
                         parse_mode: str = "md", link_preview: bool = True, silent: bool = False,
                         schedule: datetime | None = None, files: Sequence[str] | None = None) -> Msg:
        """Post from a bot's name — the only way to hang inline buttons on a post."""
        bot = tgx_bots.Registry().get(bot_username)
        target = f"@{chat.username}" if chat.username else chat.id
        async with tgx_bots.BotSession(bot, self.api_id, self.api_hash) as session:
            sent = await session.post(target, text, buttons=buttons, parse_mode=parse_mode,
                                      link_preview=link_preview, silent=silent, files=files,
                                      schedule=schedule)
        return await self.to_msg(sent)

    async def comments(self, chat: Chat, post_id: int, limit: int = 60) -> list[Msg]:
        """The discussion thread of a channel post, oldest first."""
        out = [await self.to_msg(m) async for m in self.client.iter_messages(chat.entity, reply_to=post_id, limit=limit)]
        out.reverse()
        return out

    async def send_comment(self, chat: Chat, post_id: int, text: str, parse_mode: str = "md") -> Msg:
        """Post into the channel's linked discussion group — allowed without admin rights."""
        body, entities = tgx_format.parse(text, parse_mode)
        sent = await self.client.send_message(
            chat.entity, body, comment_to=post_id, parse_mode=None, formatting_entities=entities or None,
        )
        return await self.to_msg(sent)

    async def mark_read(self, chat: Chat) -> None:
        await self.client.send_read_acknowledge(chat.entity)

    async def download(self, chat: Chat, msg_id: int, dest_dir: Path, progress: Callable[[int, int], None] | None = None) -> str | None:
        msg = self._raw.get((chat.id, msg_id))
        if msg is None:
            msg = await self.client.get_messages(chat.entity, ids=msg_id)
        if msg is None or not getattr(msg, "media", None):
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        return await self.client.download_media(msg, file=str(dest_dir), progress_callback=progress)

    def watch(self, on_message: Callable[[int, Msg], None], on_typing: Callable[[int, str], None] | None = None) -> None:
        from telethon import events

        async def handler(event: Any) -> None:
            try:
                on_message(peer_id(event.chat_id), await self.to_msg(event.message))
            except Exception:
                pass

        self.client.add_event_handler(handler, events.NewMessage())

        if on_typing is not None:
            async def typing_handler(event: Any) -> None:
                try:
                    if not getattr(event, "typing", False):
                        return
                    sender = await event.get_sender()
                    who = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "кто-то"
                    on_typing(peer_id(event.chat_id), who)
                except Exception:
                    pass

            self.client.add_event_handler(typing_handler, events.UserUpdate())

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()


def plain_task_factory() -> None:
    """Turn off asyncio's eager task factory for this loop.

    Textual enables `asyncio.eager_task_factory`, which runs a coroutine
    synchronously at `create_task` time.  Telethon's `MTProtoSender._connect`
    starts its send/receive loops *before* `connect()` flips `_user_connected`
    to True, so under eager tasks both loops observe the flag as False, exit at
    once, and every later request waits forever on a future nobody resolves —
    the UI just sits at "connecting…".  Reconnects take the same path, so this
    has to stay off for the whole session, not only around the first connect.
    """
    try:
        asyncio.get_running_loop().set_task_factory(None)
    except Exception:
        pass


def is_muted(dialog: Any) -> bool:
    """Telegram mutes by storing a future `mute_until`; past values mean unmuted."""
    settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    until = getattr(settings, "mute_until", None)
    if until is None:
        return False
    try:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)
    except Exception:
        return bool(until)


def peer_id(chat_id: Any) -> int:
    """Telethon reports channels as -100…; the UI keys on the bare entity id."""
    value = int(abs(chat_id or 0))
    raw = str(value)
    if raw.startswith("100") and len(raw) > 10:
        return int(raw[3:])
    return value


def merge_chats(existing: Sequence[Chat], fresh: Sequence[Chat]) -> list[Chat]:
    """Fold a freshly fetched list into the old one, keeping object identity.

    The open chat is compared by identity in a few places, so replacing its
    object wholesale would quietly break previews and read tracking.
    """
    by_id = {chat.id: chat for chat in existing}
    merged: list[Chat] = []
    for chat in fresh:
        old = by_id.get(chat.id)
        if old is None:
            merged.append(chat)
            continue
        for field in ("name", "kind", "username", "unread", "muted", "pinned", "date",
                      "preview", "entity", "input_entity", "can_post", "archived", "contact"):
            setattr(old, field, getattr(chat, field))
        merged.append(old)
    return merged


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def read_checklist(msg: Any) -> tuple[str, tuple[tuple[int, str, bool], ...]]:
    """Title and items of a checklist message, with what is already ticked."""
    media = getattr(msg, "media", None)
    todo = getattr(media, "todo", None)
    if todo is None:
        return "", ()
    done = {int(getattr(c, "id", 0)) for c in (getattr(media, "completions", None) or [])}
    title = getattr(getattr(todo, "title", None), "text", "") or ""
    items = tuple(
        (int(item.id), getattr(getattr(item, "title", None), "text", "") or "", int(item.id) in done)
        for item in (getattr(todo, "list", None) or [])
    )
    return title, items


def read_reactions(msg: Any) -> tuple[tuple[str, int, bool], ...]:
    """(emoji, count, is mine) for every reaction on the message."""
    block = getattr(msg, "reactions", None)
    if block is None:
        return ()
    out = []
    for item in getattr(block, "results", None) or []:
        reaction = getattr(item, "reaction", None)
        emoji = getattr(reaction, "emoticon", None)
        if emoji is None:                       # custom (premium) emoji have an id, not a char
            custom = getattr(reaction, "document_id", None)
            emoji = "⭐" if custom else "?"
        out.append((str(emoji), int(getattr(item, "count", 0) or 0), getattr(item, "chosen_order", None) is not None))
    return tuple(out)


def read_buttons(msg: Any) -> tuple[tuple[str, ...], ...]:
    """Inline keyboard labels, row by row."""
    try:
        rows = getattr(msg, "buttons", None) or []
    except Exception:
        return ()
    return tuple(tuple(str(getattr(button, "text", "") or "") for button in row) for row in rows)


def comment_count(msg: Any) -> int | None:
    """Number of comments on a channel post, or None when it has no thread."""
    replies = getattr(msg, "replies", None)
    if replies is None or not getattr(replies, "comments", False):
        return None
    return int(getattr(replies, "replies", 0) or 0)


def is_video(label: str) -> bool:
    return any(word in (label or "").lower() for word in ("video", "gif", "animation"))


def media_hint(label: str) -> str:
    """What the keys do for this kind of attachment — shown right in the bubble."""
    if is_video(label):
        return "v — кадр · o — плеер"
    if tgx_media.wants_preview(label):
        return "v — открыть"
    return "o — открыть"


def media_label(msg: Any) -> str:
    media = getattr(msg, "media", None)
    if media is None:
        return ""
    kind = type(media).__name__
    if "Unsupported" in kind:
        # Telegram sends this when the client's API layer predates the feature —
        # a rich message (Bot API 10.1) looks like this to Telethon 1.43.
        return "📄 сообщение нового типа — этот слой Telethon его не читает"
    if "Photo" in kind:
        return "🖼 photo"
    if "WebPage" in kind:
        return "🔗 link preview"
    if "ToDo" in kind or "Todo" in kind:
        return "☑ чек-лист"
    if "Poll" in kind:
        return "📊 poll"
    if "Geo" in kind:
        return "📍 location"
    if "Contact" in kind:
        return "📇 contact"
    doc = getattr(media, "document", None)
    if doc is not None:
        name = ""
        seconds = None
        for attr in getattr(doc, "attributes", []) or []:
            name = getattr(attr, "file_name", None) or name
            seconds = getattr(attr, "duration", None) or seconds
            if type(attr).__name__.endswith("Audio") and getattr(attr, "voice", False):
                return f"🎤 voice {_dur(seconds)}"
            if type(attr).__name__.endswith("Video"):
                if getattr(attr, "round_message", False):
                    return f"📹 video note {_dur(seconds)}"      # кружок, его тоже расшифровывают
                return f"🎬 video {_dur(seconds)}"
            if type(attr).__name__.endswith("Sticker"):
                return f"🩹 sticker {getattr(attr, 'alt', '')}".strip()
        if str(getattr(doc, "mime_type", "")).startswith("audio"):
            return f"🎵 audio {_dur(seconds)}".strip()
        return f"📎 {name or 'file'}"
    return "📦 media"


def _dur(seconds: Any) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def service_label(action: Any) -> str:
    name = type(action).__name__.replace("MessageAction", "")
    words = "".join(f" {c.lower()}" if c.isupper() else c for c in name).strip()
    return f"— {words or 'service message'} —"


# ── demo backend ─────────────────────────────────────────────────────────────
DEMO_CHATS = [
    ("Anthropic Releases", "channel", "anthropic_news", 3, True, "Claude Opus 5 is live for all API customers"),
    ("tgx · dev", "group", "", 12, False, "Аня: залил ветку с TUI, глянь бабблы"),
    ("Мария Кузнецова", "user", "mkuznetsova", 2, False, "ок, созвон в 16:00?"),
    ("Terminal Aesthetics", "channel", "tui_daily", 0, False, "37 эффектов терминальных анимаций в одном пакете"),
    ("deploy-bot", "bot", "deploy_bot", 1, False, "✅ build #4821 passed in 2m 14s"),
    ("Saved Messages", "user", "", 0, True, "ссылка на доку по Telethon"),
    ("Design Guild", "group", "design_guild", 0, True, "Ilya: bubbles > tables, всегда"),
    ("Артём Смирнов", "user", "artem", 0, False, "you: скинул конфиг"),
]

DEMO_SCRIPT = [
    ("Мария Кузнецова", False, "Слушай, а терминальный клиент уже умеет показывать историю?"),
    ("you", True, "Умеет. И баблами, как в десктопе — с датами, ответами и медиа-чипами."),
    ("Мария Кузнецова", False, "А непрочитанные видно?"),
    ("you", True, "Справа от чата бейдж, плюс превью последнего сообщения."),
    ("Мария Кузнецова", False, "🖼 photo"),
    ("Мария Кузнецова", False, "вот так это выглядит у меня в ghostty"),
    ("you", True, "Красиво. Осталось прикрутить поиск по чату — ctrl+f."),
    ("Мария Кузнецова", False, "ок, созвон в 16:00?"),
]


class DemoBackend:
    """Fully offline stand-in so the UI can be tried (and tested) without an account."""

    demo = True

    def __init__(self) -> None:
        self._chats: list[Chat] = []
        self._history: dict[int, list[Msg]] = {}
        self._comments: dict[tuple[int, int], list[Msg]] = {}
        self._details: dict[int, dict[str, Any]] = {}
        self._topics: dict[int, list[Topic]] = {}
        self.downloads = 0                            # counted so tests can assert cache hits
        self._next_id = 1000
        self._watcher: Callable[[int, Msg], None] | None = None
        self._typing_cb: Callable[[int, str], None] | None = None
        self._task: asyncio.Task | None = None

    async def connect(self) -> bool:
        now = datetime.now(timezone.utc)
        for i, (name, kind, username, unread, muted_flag, preview) in enumerate(DEMO_CHATS):
            chat = Chat(
                id=100 + i,
                name=name,
                kind=kind,
                username=username,
                unread=unread,
                muted=muted_flag,
                pinned=i < 2,
                date=now - timedelta(minutes=7 * i + 3),
                preview=preview,
            )
            chat.can_post = kind != "channel"       # channels take comments, not posts
            self._chats.append(chat)
            self._history[chat.id] = self._make_history(chat, now)
            if name == "Design Guild":              # a forum-style group for the topic UI
                chat.forum = True
                self._topics[chat.id] = [
                    Topic(id=1, title="Общее", pinned=True),
                    Topic(id=2, title="Дизайн-ревью", unread=4),
                    Topic(id=3, title="Архив", closed=True),
                ]
                self._history[chat.id].extend([
                    Msg(id=201, date=now - timedelta(hours=2), text="привет из общей темы",
                        sender="Ilya", sender_id=61, reply_to=1),
                    Msg(id=202, date=now - timedelta(hours=1), text="макеты на ревью",
                        sender="Аня", sender_id=62, reply_to=2, pinned=True),
                    Msg(id=203, date=now - timedelta(minutes=30), text="ещё один на ревью",
                        sender="Ilya", sender_id=61, reply_to=2),
                ])
            if kind == "channel":
                for message in self._history[chat.id]:
                    if not message.service:
                        message.comments = (message.id * 3) % 7
                        message.reactions = (("👍", 12, False), ("🔥", 3, True))
            if kind == "bot":
                self._history[chat.id].append(Msg(
                    id=777, date=now - timedelta(minutes=4), sender="deploy-bot", sender_id=chat.id,
                    text="Сборка #4821 готова. Что делаем?",
                    buttons=(("🚀 Выкатить", "📋 Логи"), ("❌ Отменить",)),
                ))
        return True

    def _make_history(self, chat: Chat, now: datetime) -> list[Msg]:
        msgs: list[Msg] = []
        base = now - timedelta(days=1, hours=3)
        msgs.append(Msg(id=1, date=base - timedelta(days=2), text=f"— {chat.name} —", service=True))
        for n, (sender, out, text) in enumerate(DEMO_SCRIPT):
            msgs.append(
                Msg(
                    id=10 + n,
                    date=base + timedelta(minutes=11 * n),
                    text="" if text.startswith("🖼") else text,
                    media=text if text.startswith("🖼") else "",
                    out=out,
                    sender=sender,
                    sender_id=1 if out else 42,
                    views=1200 + n * 37 if chat.kind == "channel" else None,
                    reply_to=10 + n - 1 if n in (3, 6) else None,
                )
            )
        return msgs

    VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

    async def send_file(
        self,
        chat: Chat,
        paths: Sequence[str],
        caption: str = "",
        reply_to: int | None = None,
        comment_to: int | None = None,
        as_document: bool = False,
        voice: bool = False,
        video_note: bool = False,
        silent: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> Msg:
        """Send one file, or several as an album. Telegram decides photo/video/audio
        from the content; the flags force a document, a voice note or a round video."""
        files = [str(Path(p).expanduser()) for p in paths]
        if not files:
            raise ValueError("нечего отправлять")
        streaming = not as_document and any(tgx_media.is_video_file(f) for f in files)
        poster = None
        if streaming and len(files) == 1:
            poster = tgx_media.poster_frame(Path(files[0]))
        sent = await self.client.send_file(
            chat.entity,
            files if len(files) > 1 else files[0],
            caption=caption or None,
            thumb=str(poster) if poster else None,
            force_document=as_document,
            voice_note=voice,
            video_note=video_note,
            supports_streaming=streaming,
            silent=silent or None,
            reply_to=reply_to,
            comment_to=comment_to,
            progress_callback=progress,
        )
        if isinstance(sent, list):
            sent = sent[-1]
        return await self.to_msg(sent)

    async def react(self, chat: Chat, msg_id: int, emoji: str | None) -> None:
        """Set (or with emoji=None clear) your reaction on a message."""
        from telethon.tl import functions, types

        reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
        await self.client(functions.messages.SendReactionRequest(
            peer=chat.entity, msg_id=int(msg_id), reaction=reaction, add_to_recent=bool(emoji),
        ))

    async def edit(self, chat: Chat, msg_id: int, text: str, parse_mode: str | None = "md",
                   link_preview: bool = True) -> Msg:
        body, entities = tgx_format.parse(text, parse_mode or "none")
        edited = await self.client.edit_message(
            chat.entity, int(msg_id), body, parse_mode=None,
            formatting_entities=entities or None, link_preview=link_preview,
        )
        return await self.to_msg(edited)

    async def delete(self, chat: Chat, msg_ids: Sequence[int], revoke: bool = True) -> int:
        """Delete your own messages. `revoke` removes them for everyone."""
        await self.client.delete_messages(chat.entity, [int(i) for i in msg_ids], revoke=revoke)
        return len(msg_ids)

    async def forward(self, source: Chat, msg_ids: Sequence[int], target: Chat, silent: bool = False) -> int:
        await self.client.forward_messages(
            target.entity, [int(i) for i in msg_ids], from_peer=source.entity, silent=silent or None
        )
        return len(msg_ids)

    async def press_button(self, chat: Chat, msg_id: int, row: int, col: int) -> str:
        """Press an inline button and return whatever the bot answered."""
        msg = await self.client.get_messages(chat.entity, ids=int(msg_id))
        if msg is None or not getattr(msg, "buttons", None):
            raise ValueError("в этом сообщении нет кнопок")
        result = await msg.click(row, col)
        for attr in ("message", "text"):
            answer = getattr(result, attr, None)
            if isinstance(answer, str) and answer:
                return answer
        return "нажато"

    async def comments(self, chat: Chat, post_id: int, limit: int = 60) -> list[Msg]:
        await asyncio.sleep(0.05)
        return list(self._comments.setdefault((chat.id, post_id), [
            Msg(id=90001, date=datetime.now(timezone.utc) - timedelta(minutes=42), text="первый!", sender="Игорь", sender_id=51),
            Msg(id=90002, date=datetime.now(timezone.utc) - timedelta(minutes=17), text="а можно подробнее про горячие клавиши?", sender="Лена", sender_id=52),
        ]))

    async def send_comment(self, chat: Chat, post_id: int, text: str, parse_mode: str = "md") -> Msg:
        await asyncio.sleep(0.05)
        self._next_id += 1
        msg = Msg(id=self._next_id, date=datetime.now(timezone.utc), text=text, out=True, sender="you", sender_id=1)
        self._comments.setdefault((chat.id, post_id), []).append(msg)
        for message in self._history.get(chat.id, []):
            if message.id == post_id and message.comments is not None:
                message.comments += 1
        return msg

    async def whoami(self) -> str:
        return "Demo Account @tgx_demo"

    async def dialogs(self, limit: int | None = None) -> list[Chat]:
        return list(self._chats)

    async def folders(self) -> list[Folder]:
        return [
            Folder(id=2, title="AI", include=frozenset({100, 103})),
            Folder(id=3, title="Группы", groups=True),
            Folder(id=4, title="Каналы", broadcasts=True, exclude=frozenset({100})),
            Folder(id=5, title="Непрочитанные", groups=True, broadcasts=True, contacts=True,
                   non_contacts=True, bots=True, exclude_read=True),
        ]

    async def history(self, chat: Chat, limit: int = 60, before_id: int | None = None,
                      topic_id: int | None = None) -> list[Msg]:
        await asyncio.sleep(0.05)
        rows = self._history.get(chat.id, [])
        if topic_id:
            rows = [m for m in rows if m.reply_to == topic_id or m.id == topic_id]
        return [] if before_id else list(rows)

    async def search(self, chat: Chat | None, query: str, limit: int = 40, kind: str | None = None,
                     from_user: str | None = None, since: datetime | None = None,
                     until: datetime | None = None) -> list[tuple[Chat | None, Msg]]:
        if from_user and chat is None:
            raise ValueError("отбор по отправителю работает только внутри чата")
        q = query.lower()
        pool = [chat] if chat else self._chats
        hits: list[tuple[Chat | None, Msg]] = []
        for c in pool:
            if c is None:
                continue
            for m in self._history.get(c.id, []):
                if q and q not in (m.text or "").lower():
                    continue
                if kind in {"photo", "media"} and "photo" not in (m.media or ""):
                    continue
                if kind == "pinned" and not m.pinned:
                    continue
                if from_user and from_user.lstrip("@").lower() not in (m.sender or "").lower():
                    continue
                when = m.date
                if since is not None and when is not None and when < since:
                    continue
                if until is not None and when is not None and when > until:
                    continue
                hits.append((c, m))
        return hits[:limit]

    async def send(self, chat: Chat, text: str, reply_to: int | None = None,
                   parse_mode: str = "md", link_preview: bool = True,
                   topic_id: int | None = None) -> Msg:
        await asyncio.sleep(0.08)
        reply_to = reply_to or topic_id
        text, entities = tgx_format.parse(text, parse_mode)
        self._next_id += 1
        msg = Msg(id=self._next_id, date=datetime.now(timezone.utc), text=text, out=True,
                  sender="you", sender_id=1, reply_to=reply_to, entities=tuple(entities))
        self._history.setdefault(chat.id, []).append(msg)
        return msg

    async def send_file(
        self,
        chat: Chat,
        paths: Sequence[str],
        caption: str = "",
        reply_to: int | None = None,
        comment_to: int | None = None,
        as_document: bool = False,
        voice: bool = False,
        video_note: bool = False,
        silent: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> Msg:
        await asyncio.sleep(0.05)
        if progress:
            progress(1, 1)
        self._next_id += 1
        names = ", ".join(Path(p).name for p in paths)
        if voice:
            label = f"🎤 voice {names}"
        elif video_note:
            label = f"⭕ кружок {names}"
        elif as_document:
            label = f"📎 {names}"
        elif len(paths) > 1:
            label = f"🖼 альбом · {len(paths)} файла"
        else:
            label = f"🖼 {names}"
        msg = Msg(id=self._next_id, date=datetime.now(timezone.utc), text=caption, out=True,
                  sender="you", sender_id=1, media=label, reply_to=reply_to)
        target = self._comments.setdefault((chat.id, comment_to), []) if comment_to else self._history.setdefault(chat.id, [])
        target.append(msg)
        return msg

    async def publish(self, chat: Chat, text: str = "", *, parse_mode: str = "md",
                      link_preview: bool = True, silent: bool = False, schedule: datetime | None = None,
                      files: Sequence[str] | None = None, reply_to: int | None = None,
                      comment_to: int | None = None,
                      progress: Callable[[int, int], None] | None = None) -> Msg:
        await asyncio.sleep(0.05)
        body, entities = tgx_format.parse(text, parse_mode)
        self._next_id += 1
        media = ""
        if files:
            media = f"🖼 альбом · {len(files)} файла" if len(files) > 1 else f"🖼 {Path(files[0]).name}"
        msg = Msg(id=self._next_id, date=schedule or datetime.now(timezone.utc), text=body, out=True,
                  sender="you", sender_id=1, media=media, entities=tuple(entities), reply_to=reply_to)
        target = self._comments.setdefault((chat.id, comment_to), []) if comment_to else self._history.setdefault(chat.id, [])
        target.append(msg)
        return msg

    def list_bots(self) -> list[dict[str, str]]:
        return [{"username": "tgx_demo_bot", "name": "Демо-бот"}]

    async def send_checklist(self, chat: Chat, title: str, items: Sequence[str],
                             others_can_append: bool = True, others_can_complete: bool = True,
                             reply_to: int | None = None) -> Msg:
        await asyncio.sleep(0.03)
        cleaned = [i.strip() for i in items if i.strip()]
        if not title.strip() or not cleaned:
            raise ValueError("нужны заголовок и хотя бы один пункт")
        self._next_id += 1
        msg = Msg(id=self._next_id, date=datetime.now(timezone.utc), out=True, sender="you", sender_id=1,
                  media="☑ чек-лист", checklist_title=title,
                  checklist=tuple((n + 1, text, False) for n, text in enumerate(cleaned)))
        self._history.setdefault(chat.id, []).append(msg)
        return msg

    async def append_checklist(self, chat: Chat, msg_id: int,
                               items: Sequence[str]) -> tuple[tuple[int, str, bool], ...]:
        for message in self._history.get(chat.id, []):
            if message.id == msg_id:
                start = max((i for i, _, _ in message.checklist), default=0)
                message.checklist = message.checklist + tuple(
                    (start + n + 1, text.strip(), False) for n, text in enumerate(items) if text.strip())
                return message.checklist
        return ()

    async def toggle_checklist(self, chat: Chat, msg_id: int, done: Sequence[int] = (),
                               undone: Sequence[int] = ()) -> tuple[tuple[int, str, bool], ...]:
        for message in self._history.get(chat.id, []):
            if message.id == msg_id:
                message.checklist = tuple(
                    (i, text, True if i in set(done) else (False if i in set(undone) else was))
                    for i, text, was in message.checklist)
                return message.checklist
        return ()

    async def topics(self, chat: Chat, limit: int = 100) -> list[Topic]:
        await asyncio.sleep(0.02)
        return list(self._topics.get(chat.id, []))

    async def create_topic(self, chat: Chat, title: str, icon_color: int | None = None) -> Topic:
        await asyncio.sleep(0.02)
        rows = self._topics.setdefault(chat.id, [])
        topic = Topic(id=1000 + len(rows), title=title.strip())
        rows.append(topic)
        return topic

    async def edit_topic(self, chat: Chat, topic_id: int, title: str | None = None,
                         closed: bool | None = None, hidden: bool | None = None) -> None:
        for topic in self._topics.get(chat.id, []):
            if topic.id == topic_id:
                if title:
                    topic.title = title
                if closed is not None:
                    topic.closed = closed
                if hidden is not None:
                    topic.hidden = hidden

    async def transcribe(self, chat: Chat, msg_id: int) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        for message in self._history.get(chat.id, []):
            if message.id == msg_id and (message.media or "").startswith(("🎤", "📹")):
                return {"message_id": msg_id, "transcription_id": 1,
                        "text": "привет, это расшифровка голосового", "pending": False,
                        "free_left": None, "free_reset": None}
        raise ValueError("расшифровать можно голосовое или кружок")

    async def delete_topic(self, chat: Chat, topic_id: int) -> int:
        if int(topic_id) == 1:
            raise ValueError("«Общую» тему удалить нельзя — её можно только скрыть")
        rows = self._topics.get(chat.id, [])
        kept = [t for t in rows if t.id != int(topic_id)]
        self._topics[chat.id] = kept
        return len(rows) - len(kept)

    async def pin_topic(self, chat: Chat, topic_id: int, pinned: bool = True) -> None:
        for topic in self._topics.get(chat.id, []):
            if topic.id == topic_id:
                topic.pinned = pinned

    async def pin(self, chat: Chat, msg_id: int, silent: bool = True, unpin: bool = False) -> None:
        await asyncio.sleep(0.02)
        for message in self._history.get(chat.id, []):
            if message.id == msg_id:
                message.pinned = not unpin

    async def pinned(self, chat: Chat, limit: int = 20) -> list[Msg]:
        return [m for m in self._history.get(chat.id, []) if m.pinned][:limit]

    async def publish_rich(self, bot_username: str, chat: Chat, markdown: str, *, buttons: str = "",
                           silent: bool = False, draft: bool = False,
                           media: Sequence[dict[str, Any]] = (), topic: int | None = None) -> Msg:
        await asyncio.sleep(0.05)
        tgx_rich.check_limits(markdown, media)
        tgx_rich.buttons_json(buttons)
        self._next_id += 1
        bot_username = bot_username or "you"
        headline = next((line for line in markdown.splitlines() if line.strip()), "")
        msg = Msg(id=self._next_id, date=datetime.now(timezone.utc), out=True, sender=f"@{bot_username}",
                  sender_id=999, media="📄 rich-сообщение", text=headline.lstrip("# ").strip()[:80])
        self._history.setdefault(chat.id, []).append(msg)
        return msg

    async def publish_as(self, bot_username: str, chat: Chat, text: str = "", *, buttons: str = "",
                         parse_mode: str = "md", link_preview: bool = True, silent: bool = False,
                         schedule: datetime | None = None, files: Sequence[str] | None = None) -> Msg:
        await asyncio.sleep(0.05)
        body, entities = tgx_format.parse(text, parse_mode)
        rows = tgx_bots.parse_buttons(buttons) if buttons else []
        self._next_id += 1
        msg = Msg(id=self._next_id, date=schedule or datetime.now(timezone.utc), text=body, out=True,
                  sender=f"@{bot_username}", sender_id=999, entities=tuple(entities),
                  buttons=tuple(tuple(b.text for b in row) for row in rows))
        self._history.setdefault(chat.id, []).append(msg)
        return msg

    async def create_chat(self, title: str, kind: str = "channel", about: str = "",
                          username: str | None = None) -> Chat:
        await asyncio.sleep(0.05)
        chat = Chat(id=900 + len(self._chats), name=title, kind="channel" if kind == "channel" else "group",
                    username=(username or "").lstrip("@"), date=datetime.now(timezone.utc),
                    preview="создан только что", can_post=True)
        self._chats.append(chat)
        self._history[chat.id] = []
        self._details[chat.id] = {"about": about, "participants": 1, "admins": 1, "slowmode": 0}
        return chat

    async def chat_details(self, chat: Chat) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        stored = self._details.setdefault(chat.id, {"about": "демо-описание", "participants": 42,
                                                    "admins": 2, "slowmode": 0})
        return {"title": chat.name, "username": chat.username, "kind": chat.kind,
                "about": stored["about"], "participants": stored["participants"],
                "admins": stored["admins"], "linked_chat_id": None, "slowmode": stored["slowmode"],
                "banned_rights": None, "invite": f"https://t.me/+demo{chat.id}", "creator": True,
                "can_edit": True}

    async def edit_chat(self, chat: Chat, title: str | None = None, about: str | None = None,
                        username: str | None = None) -> dict[str, Any]:
        changed = {}
        if title:
            chat.name, changed["title"] = title, title
        if about is not None:
            self._details.setdefault(chat.id, {})["about"] = about
            changed["about"] = about
        if username is not None:
            chat.username = username.lstrip("@")
            changed["username"] = chat.username
        return changed

    async def set_slowmode(self, chat: Chat, seconds: int) -> int:
        self._details.setdefault(chat.id, {})["slowmode"] = int(seconds)
        return int(seconds)

    async def set_permissions(self, chat: Chat, allowed: dict[str, bool]) -> dict[str, bool]:
        self._details.setdefault(chat.id, {})["allowed"] = dict(allowed)
        return allowed

    async def set_discussion(self, channel: Chat, group: Chat | None) -> None:
        self._details.setdefault(channel.id, {})["linked"] = group.id if group else None

    async def invite_link(self, chat: Chat, title: str | None = None, usage_limit: int | None = None,
                          request_needed: bool = False) -> str:
        return f"https://t.me/+demo{chat.id}{'x' if request_needed else ''}"

    async def members(self, chat: Chat, kind: str = "recent", limit: int = 100) -> list[dict[str, Any]]:
        return [{"id": 51, "name": "Игорь", "username": "igor", "bot": False},
                {"id": 52, "name": "Лена", "username": "", "bot": False}]

    async def join(self, link_or_username: str) -> str:
        return link_or_username

    async def leave(self, chat: Chat) -> None:
        self._chats = [c for c in self._chats if c.id != chat.id]

    async def react(self, chat: Chat, msg_id: int, emoji: str | None) -> None:
        for message in self._history.get(chat.id, []):
            if message.id != msg_id:
                continue
            others = [r for r in message.reactions if not r[2]]
            mine = [(emoji, 1, True)] if emoji else []
            message.reactions = tuple(others + mine)

    async def edit(self, chat: Chat, msg_id: int, text: str, parse_mode: str | None = "md",
                   link_preview: bool = True) -> Msg:
        for message in self._history.get(chat.id, []):
            if message.id == msg_id:
                message.text, message.edited = text, True
                return message
        raise ValueError("сообщение не найдено")

    async def delete(self, chat: Chat, msg_ids: Sequence[int], revoke: bool = True) -> int:
        rows = self._history.get(chat.id, [])
        self._history[chat.id] = [m for m in rows if m.id not in set(msg_ids)]
        return len(rows) - len(self._history[chat.id])

    async def forward(self, source: Chat, msg_ids: Sequence[int], target: Chat, silent: bool = False) -> int:
        moved = [m for m in self._history.get(source.id, []) if m.id in set(msg_ids)]
        for message in moved:
            self._next_id += 1
            self._history.setdefault(target.id, []).append(
                Msg(id=self._next_id, date=datetime.now(timezone.utc), text=message.text,
                    out=True, sender="you", sender_id=1, media=message.media)
            )
        return len(moved)

    async def press_button(self, chat: Chat, msg_id: int, row: int, col: int) -> str:
        for message in self._history.get(chat.id, []):
            if message.id == msg_id and message.buttons:
                return f"нажата «{message.buttons[row][col]}»"
        raise ValueError("в этом сообщении нет кнопок")

    async def mark_read(self, chat: Chat) -> None:
        chat.unread = 0

    async def download(self, chat: Chat, msg_id: int, dest_dir: Path, progress: Callable[[int, int], None] | None = None) -> str | None:
        return await self.thumbnail(chat, msg_id, dest_dir)

    async def thumbnail(self, chat: Chat, msg_id: int, cache: Path, full: bool = False) -> Path | None:
        """Draw a placeholder photo once so previews can be seen without an account."""
        dest = cache / "demo-photo.png"
        if dest.exists():
            return dest
        self.downloads += 1
        try:
            from PIL import Image, ImageDraw
        except ModuleNotFoundError:
            return None
        cache.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (480, 320))
        draw = ImageDraw.Draw(img)
        for y in range(320):
            shade = y / 320
            draw.line([(0, y), (480, y)], fill=(int(14 + shade * 28), int(22 + shade * 120), int(33 + shade * 190)))
        draw.ellipse([150, 60, 330, 240], fill=(255, 255, 255))
        draw.ellipse([196, 106, 284, 194], fill=(42, 171, 238))
        draw.rectangle([0, 280, 480, 320], fill=(14, 22, 33))
        draw.text((16, 296), "tgx demo photo", fill=(226, 237, 245))
        img.save(dest)
        return dest

    def watch(self, on_message: Callable[[int, Msg], None], on_typing: Callable[[int, str], None] | None = None) -> None:
        self._watcher = on_message
        self._typing_cb = on_typing
        self._task = asyncio.create_task(self._chatter())

    async def _chatter(self) -> None:
        lines = [
            "кстати, анимации в списке чатов — огонь",
            "✅ build #4822 passed in 1m 52s",
            "новый пост вышел, глянешь?",
            "ping",
            "и ещё: тема переключается на ctrl+t",
        ]
        try:
            while True:
                await asyncio.sleep(9)
                if not self._watcher:
                    return
                chat = random.choice(self._chats)
                if self._typing_cb is not None:
                    self._typing_cb(chat.id, chat.name.split()[0])
                    await asyncio.sleep(2)
                self._next_id += 1
                msg = Msg(
                    id=self._next_id,
                    date=datetime.now(timezone.utc),
                    text=random.choice(lines),
                    sender=chat.name.split()[0],
                    sender_id=chat.id,
                )
                self._history.setdefault(chat.id, []).append(msg)
                self._watcher(chat.id, msg)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._task:
            self._task.cancel()


# ── palette plumbing ─────────────────────────────────────────────────────────
PAL: dict[str, str] = {
    "primary": "#2AABEE",
    "accent": "#7BC862",
    "foreground": "#E4EDF5",
    "text-muted": "#7E93A5",
    "panel": "#22303C",
    "surface": "#17212B",
    "background": "#0E1621",
    "success": "#4FCE5D",
    "warning": "#E5CA77",
    "error": "#E9576B",
}


def pal(key: str) -> str:
    return PAL.get(key, "#7E93A5")


def muted() -> str:
    return pal("text-muted")


# ── widgets ──────────────────────────────────────────────────────────────────
class TopBar(Static):
    account = reactive("connecting…", layout=False)
    detail = reactive("", layout=False)
    live = reactive(False, layout=False)

    def render(self) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("▌", style=f"bold {pal('primary')}")
        t.append("tgx", style=f"bold {pal('primary')}")
        t.append("  ")
        t.append(self.account, style=f"bold {pal('foreground')}")
        if self.detail:
            t.append(f"  {self.detail}", style=muted())
        dot, style = ("●", pal("success")) if self.live else ("○", muted())
        pad = max(1, self.size.width - t.cell_len - 10)
        t.append(" " * pad)
        t.append(f"{dot} ", style=style)
        t.append("live" if self.live else "offline", style=muted())
        return t


def chat_row(chat: Chat) -> Table:
    grid = Table.grid(expand=True, padding=(0, 0))
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(justify="right", width=6, no_wrap=True)

    title = Text(no_wrap=True, overflow="ellipsis")
    title.append("▎" if chat.pinned else " ", style=pal("primary"))
    title.append(f"{chat.glyph} ")
    title.append(chat.name, style=f"bold {pal('foreground')}")
    if chat.muted:
        title.append(" 🔕")
    grid.add_row(title, Text(relative(chat.date), style=muted()))

    sub_text = chat.preview or (f"@{chat.username}" if chat.username else chat.subtitle)
    sub = Text(f"  {sub_text}", style=muted(), no_wrap=True, overflow="ellipsis")
    badge = Text()
    if chat.unread:
        count = "99+" if chat.unread > 99 else str(chat.unread)
        bg = muted() if chat.muted else pal("primary")
        badge.append(f" {count} ", style=f"bold {pal('background')} on {bg}")
    grid.add_row(sub, badge)
    return grid


class ChatList(OptionList):
    """Sidebar chat list: two-line rows with unread badges and previews."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.chats: list[Chat] = []
        self.visible_chats: list[Chat] = []
        self.query_text = ""
        self.folder: Folder | None = None

    def set_chats(self, chats: list[Chat]) -> None:
        self.chats = chats
        self.rebuild()

    def rebuild(self, keep: int | None = None) -> None:
        keep = keep if keep is not None else self.current_chat_id()
        needle = self.query_text.strip().lower()
        rows = []
        for c in self.chats:
            if self.folder is not None and not self.folder.matches(c):
                continue
            if needle and needle not in f"{c.name} {c.username}".lower():
                continue
            rows.append(c)
        rows.sort(key=lambda c: (not c.pinned, -(c.date.timestamp() if c.date else 0)))
        self.visible_chats = rows
        with self.prevent(OptionList.OptionHighlighted):
            self.clear_options()
            self.add_options([Option(chat_row(c), id=str(c.id)) for c in rows])
            if rows:
                self.highlighted = next((i for i, c in enumerate(rows) if c.id == keep), 0)

    def current_chat_id(self) -> int | None:
        if self.highlighted is None or not self.visible_chats:
            return None
        if self.highlighted >= len(self.visible_chats):
            return None
        return self.visible_chats[self.highlighted].id

    def chat_at(self, index: int | None) -> Chat | None:
        if index is None or index >= len(self.visible_chats):
            return None
        return self.visible_chats[index]

    def refresh_chat(self, chat: Chat) -> None:
        for i, c in enumerate(self.visible_chats):
            if c.id == chat.id:
                self.replace_option_prompt_at_index(i, chat_row(chat))
                return

    def next_unread(self) -> int | None:
        if not self.visible_chats:
            return None
        start = (self.highlighted or 0) + 1
        order = list(range(start, len(self.visible_chats))) + list(range(0, start))
        for i in order:
            if self.visible_chats[i].unread:
                return i
        return None


class DaySeparator(Static):
    def __init__(self, label: str) -> None:
        super().__init__(Text(f"──  {label}  ──", style=muted()))


class Bubble(Vertical):
    """A message bubble: header + text, plus an inline picture when one exists."""

    def __init__(self, msg: Msg, fallback_name: str = "") -> None:
        super().__init__()
        self.msg = msg
        self.fallback_name = fallback_name
        self.has_image = False
        self.preview_tried = False      # don't keep retrying what cannot be drawn
        self.spoilers_shown = False
        self._body = Static(id=None)
        if msg.service:
            self.add_class("service")
        elif msg.out:
            self.add_class("own")
        if msg.rich is not None:
            self.add_class("document")      # a document needs more room than a chat bubble

    class Selected(TextualMessage):
        """Posted when the bubble is clicked, so the list can highlight it."""

        def __init__(self, bubble: "Bubble", open_media: bool = False) -> None:
            self.bubble = bubble
            self.open_media = open_media
            super().__init__()

    def compose(self) -> ComposeResult:
        if self.msg.rich is not None:
            # a document-shaped message needs a column to wrap into; an auto-width
            # bubble would hug the longest line and clip the rest
            self._body.styles.width = 72
        self._body.update(self.text())
        yield self._body

    def on_click(self, event: Any = None) -> None:
        double = getattr(event, "chain", 1) >= 2
        self.post_message(self.Selected(self, open_media=double))

    def refresh_text(self) -> None:
        self._body.update(self.text())

    async def attach_image(self, path: Path, backend: str = "auto", max_cols: int = 36) -> bool:
        if self.has_image:
            return False
        widget = tgx_media.make_widget(path, max_cols=max_cols, backend=backend)
        if widget is None:
            return False
        self.has_image = True
        await self.mount(widget)
        fade_in(widget, duration=0.25)
        return True

    def text(self) -> Text:
        m = self.msg
        if m.service:
            return Text(m.text or "—", style=f"italic {muted()}")
        t = Text()
        who = "you" if m.out else (m.sender or self.fallback_name or "unknown")
        t.append(who, style=f"bold {pal('accent')}" if m.out else f"bold {name_color(m.sender_id or who)}")
        t.append(f"  {clock(m.date)}", style=muted())
        if m.pinned:
            t.append(" 📌", style=pal("primary"))
        if m.edited:
            t.append(" · edited", style=muted())
        if m.views:
            t.append(f" · 👁 {m.views}", style=muted())
        if m.reply_to:
            t.append(f"\n↩ in reply to #{m.reply_to}", style=f"italic {muted()}")
        if m.rich is not None:
            t.append("\n📄 богатое сообщение\n", style=pal("primary"))
            t.append_text(tgx_rich.render_message(m.rich, colors=PAL))
        elif m.checklist:
            t.append(f"\n☑ {m.checklist_title}", style=f"bold {pal('primary')}")
            for _, text, done in m.checklist:
                mark = "☑" if done else "☐"
                style = muted() if done else pal("foreground")
                t.append(f"\n  {mark} {text}", style=style)
            ready = sum(1 for _, _, done in m.checklist if done)
            t.append(f"\n  {ready} из {len(m.checklist)}   l — отметить", style=muted())
        elif m.media:
            t.append(f"\n{m.media}", style=pal("primary"))
            t.append(f"   {media_hint(m.media)}", style=muted())
            if m.transcript:
                t.append(f"\n  «{m.transcript}»", style=f"italic {pal('text')}")
            elif m.media.startswith(("🎤", "📹")):        # голосовое или кружок
                t.append("   a — расшифровать", style=muted())
        if m.text:
            t.append("\n")
            t.append_text(tgx_format.render(m.text, m.entities, colors=PAL,
                                            reveal_spoilers=self.spoilers_shown))
            if any(type(e).__name__ == "MessageEntitySpoiler" for e in m.entities) and not self.spoilers_shown:
                t.append("   s — показать спойлер", style=muted())
        for row in m.buttons:
            t.append("\n" + "  ".join(f"[ {label} ]" for label in row), style=f"bold {pal('primary')}")
        if m.buttons:
            t.append("   b — нажать", style=muted())
        if m.reactions:
            t.append("\n")
            for emoji, count, mine in m.reactions:
                t.append(f" {emoji}{count} ", style=f"bold {pal('accent')}" if mine else muted())
            t.append("  + — реакция", style=muted())
        if m.comments is not None:
            label = f"💬 {m.comments} " + plural(m.comments, "комментарий", "комментария", "комментариев") if m.comments else "💬 комментариев нет"
            t.append(f"\n{label}   c — открыть", style=muted())
        return t


class MessageRow(Horizontal):
    def __init__(self, bubble: Bubble) -> None:
        super().__init__(bubble)
        self.bubble = bubble
        if bubble.msg.service:
            self.add_class("service")
        else:
            self.add_class("own" if bubble.msg.out else "other")


class MessageList(VerticalScroll):
    """The conversation pane: day separators, bubbles, keyboard selection."""

    can_focus = True

    BINDINGS = [
        Binding("up,k", "move(-1)", "prev message", show=False),
        Binding("down,j", "move(1)", "next message", show=False),
        Binding("home,g", "jump(0)", "top", show=False),
        Binding("end,G", "jump(1)", "bottom", show=False),
    ]

    class ReplyRequested(TextualMessage):
        def __init__(self, msg: Msg) -> None:
            self.msg = msg
            super().__init__()

    class LoadOlder(TextualMessage):
        pass

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.chat: Chat | None = None
        self.msgs: list[Msg] = []
        self.rows: list[MessageRow] = []
        self.selected: int | None = None
        self._oldest_id: int | None = None

    # --- rendering -------------------------------------------------------
    def _build(self, msgs: Sequence[Msg], start_day: str | None = None) -> tuple[list[Any], str | None]:
        widgets: list[Any] = []
        day = start_day
        for m in msgs:
            label = day_label(m.date)
            if label != day:
                widgets.append(DaySeparator(label))
                day = label
            widgets.append(MessageRow(Bubble(m, self.chat.name if self.chat else "")))
        return widgets, day

    async def show(self, chat: Chat, msgs: Sequence[Msg]) -> None:
        self.chat = chat
        self.msgs = list(msgs)
        self.selected = None
        self._oldest_id = msgs[0].id if msgs else None
        await self.remove_children()
        if not msgs:
            await self.mount(Static(Text("no messages yet — say something", style=f"italic {muted()}"), id="empty-state"))
            return
        widgets, _ = self._build(self.msgs)
        await self.mount_all(widgets)
        self.rows = [w for w in widgets if isinstance(w, MessageRow)]
        tail = widgets[-14:]
        for i, w in enumerate(tail):
            fade_in(w, delay=0.02 * i, duration=0.18)
        self.scroll_end(animate=False)

    async def append(self, msg: Msg, flash: bool = True) -> None:
        if any(m.id == msg.id for m in self.msgs[-30:]):
            return
        at_bottom = self.is_vertical_scroll_end
        last_day = day_label(self.msgs[-1].date) if self.msgs else None
        if not self.msgs:
            await self.remove_children()
        widgets, _ = self._build([msg], start_day=last_day)
        for w in widgets:
            fade_in(w, duration=0.2)
        await self.mount_all(widgets)
        self.msgs.append(msg)
        self.rows.extend(w for w in widgets if isinstance(w, MessageRow))
        if flash and self.rows:
            bubble = self.rows[-1].bubble
            bubble.add_class("flash")
            self.set_timer(0.9, lambda: bubble.remove_class("flash"))
        if at_bottom or msg.out:
            self.call_after_refresh(self.scroll_end, animate=True, duration=0.25)

    async def prepend(self, msgs: Sequence[Msg]) -> None:
        if not msgs:
            return
        anchor = self.rows[0] if self.rows else None
        widgets, _ = self._build(msgs)
        await self.mount_all(widgets, before=0)
        self.msgs = list(msgs) + self.msgs
        self.rows = [w for w in widgets if isinstance(w, MessageRow)] + self.rows
        self._oldest_id = self.msgs[0].id if self.msgs else None
        if anchor is not None:
            self.call_after_refresh(self.scroll_to_widget, anchor, animate=False, top=True)

    @property
    def oldest_id(self) -> int | None:
        return self._oldest_id

    # --- selection -------------------------------------------------------
    def selected_msg(self) -> Msg | None:
        if self.selected is None or self.selected >= len(self.rows):
            return None
        return self.rows[self.selected].bubble.msg

    def _paint_selection(self, previous: int | None) -> None:
        if previous is not None and previous < len(self.rows):
            self.rows[previous].bubble.remove_class("selected")
        if self.selected is not None and self.selected < len(self.rows):
            bubble = self.rows[self.selected].bubble
            bubble.add_class("selected")
            self.scroll_to_widget(bubble, animate=True, duration=0.15)

    def action_move(self, delta: int) -> None:
        if not self.rows:
            return
        previous = self.selected
        if self.selected is None:
            self.selected = len(self.rows) - 1 if delta < 0 else 0
        else:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))
        self._paint_selection(previous)
        if self.selected == 0 and delta < 0:
            self.post_message(self.LoadOlder())

    def action_jump(self, where: int) -> None:
        if not self.rows:
            return
        previous = self.selected
        self.selected = len(self.rows) - 1 if where else 0
        self._paint_selection(previous)
        (self.scroll_end if where else self.scroll_home)(animate=True, duration=0.25)

    @on(Bubble.Selected)
    def _bubble_clicked(self, event: Bubble.Selected) -> None:
        event.stop()
        for index, row in enumerate(self.rows):
            if row.bubble is event.bubble:
                previous = self.selected
                self.selected = index
                self._paint_selection(previous)
                break
        self.focus()
        if event.open_media and event.bubble.msg.media:
            self.app.action_view_media()      # double click opens the picture

    def remove_message(self, msg_id: int) -> None:
        for index, row in enumerate(list(self.rows)):
            if row.bubble.msg.id == msg_id:
                self.rows.remove(row)
                row.remove()
                if self.selected is not None and self.selected >= index:
                    self.selected = max(0, self.selected - 1) if self.rows else None
                break
        self.msgs = [m for m in self.msgs if m.id != msg_id]

    def focus_message(self, msg_id: int) -> bool:
        for i, row in enumerate(self.rows):
            if row.bubble.msg.id == msg_id:
                previous = self.selected
                self.selected = i
                self._paint_selection(previous)
                return True
        return False


def fade_in(widget: Any, delay: float = 0.0, duration: float = 0.2) -> None:
    """Opacity fade that quietly no-ops when the user disabled animations."""
    try:
        if getattr(widget.app, "animation_level", "full") == "none":
            return
    except Exception:
        return
    widget.styles.opacity = 0.0
    widget.styles.animate("opacity", value=1.0, duration=duration, delay=delay, easing="out_cubic")


class TypingBar(Static):
    """Animated `… is typing` line above the composer."""

    FRAMES = ("·  ", "·· ", "···", " ··", "  ·", "   ")

    def __init__(self) -> None:
        super().__init__(id="typing")
        self._who = ""
        self._plain = ""
        self._frame = 0
        self._timer: Any = None
        self._hide_timer: Any = None

    def status(self, text: str, seconds: float = 8.0) -> None:
        """Borrow the line for progress messages (downloads, opening files)."""
        self._plain = text
        self.add_class("visible")
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._hide_timer is not None:
            self._hide_timer.stop()
        self._hide_timer = self.set_timer(seconds, self.hide)
        self.refresh()

    def show(self, who: str, seconds: float = 5.0) -> None:
        self._who = who
        self._plain = ""
        self.add_class("visible")
        if self._timer is None:
            self._timer = self.set_interval(0.28, self._tick)
        if self._hide_timer is not None:
            self._hide_timer.stop()
        self._hide_timer = self.set_timer(seconds, self.hide)

    def hide(self) -> None:
        self._plain = ""
        self.remove_class("visible")
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self.refresh()

    def render(self) -> Text:
        if not self.has_class("visible"):
            return Text("")
        if self._plain:
            return Text(self._plain, style=muted())
        return Text(f"{self._who} печатает {self.FRAMES[self._frame]}", style=f"italic {pal('primary')}")


class Composer(Input):
    """Message box. Enter sends; ctrl+e opens the multi-line editor."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(placeholder="  написать сообщение…   (enter — отправить, ctrl+e — многострочно)", **kwargs)


# ── modal screens ────────────────────────────────────────────────────────────
HELP_KEYS = [
    ("навигация", [
        ("/", "поиск по списку чатов"),
        ("ctrl+k", "фокус на список чатов"),
        ("↑ ↓ / j k", "перемещение по списку и сообщениям"),
        ("enter", "открыть чат · отправить сообщение"),
        ("ctrl+n", "следующий непрочитанный чат"),
        ("ctrl+b", "свернуть/развернуть боковую панель"),
        ("g / G", "начало · конец переписки"),
    ]),
    ("сообщения", [
        ("c", "комментарии к посту канала (чтение и отправка)"),
        ("ctrl+f", "поиск внутри чата"),
        ("ctrl+r", "ответить на выбранное сообщение"),
        ("e / x", "правка · удаление своего сообщения"),
        ("f", "переслать сообщение в другой чат"),
        ("+ / −", "поставить · убрать реакцию"),
        ("b", "нажать инлайн-кнопку бота"),
        ("s", "показать/скрыть спойлер в сообщении"),
        ("ctrl+y", "скопировать текст сообщения"),
        ("v", "картинка на весь экран (у видео — кадр)"),
        ("o", "открыть вложение в системном просмотрщике"),
        ("ctrl+d", "скачать вложение в data/downloads"),
        ("n / i", "создать канал или группу · управление текущим чатом"),
        ("t", "темы форума: переключение, создание, закрытие, закрепление"),
        ("l", "чек-лист: создать, отмечать пункты, дописывать"),
        ("shift+p", "закрепить или открепить сообщение (тихо)"),
        ("p", "редактор поста: разметка, превью, файл, отложенная публикация"),
        ("ctrl+s", "прикрепить файл: фото, видео, кружок, голосовое, документ, альбом"),
        ("ctrl+e", "многострочный редактор"),
        ("shift+r", "отметить чат прочитанным вручную"),
        ("клик", "выделить сообщение · двойной клик — открыть картинку"),
        ("escape", "снять выделение · отменить ответ"),
    ]),
    ("оформление", [
        ("ctrl+t", "следующая тема"),
        ("ctrl+p", "палитра команд"),
        ("f1 / ?", "эта справка"),
        ("f12", "сохранить SVG-скриншот"),
        ("ctrl+q", "выход"),
    ]),
]


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q,question_mark,f1", "dismiss", "закрыть"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text("tgx · горячие клавиши", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Static(self._table(), id="help-grid")
            yield Static(Text("escape — закрыть", style=muted()), classes="dialog-hint")

    def _table(self) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="right", style=f"bold {pal('primary')}", no_wrap=True)
        grid.add_column(style=pal("foreground"))
        for section, rows in HELP_KEYS:
            grid.add_row("", Text(section.upper(), style=f"bold {muted()}"))
            for key, text in rows:
                grid.add_row(key, text)
            grid.add_row("", "")
        return grid


class ComposeScreen(ModalScreen[str | None]):
    """Multi-line editor for longer messages."""

    BINDINGS = [
        Binding("escape", "cancel", "отмена"),
        Binding("ctrl+s", "send", "отправить"),
    ]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text("многострочное сообщение", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield TextArea(self.initial, id="long-text", soft_wrap=True)
            yield Static(Text("ctrl+s — отправить · escape — отмена", style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def action_send(self) -> None:
        self.dismiss(self.query_one(TextArea).text.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


SEARCH_KINDS = (
    ("всё", ""),
    ("фото", "photo"),
    ("видео", "video"),
    ("фото и видео", "media"),
    ("файлы", "file"),
    ("ссылки", "link"),
    ("голосовые", "voice"),
    ("музыка", "music"),
    ("гифки", "gif"),
    ("кружки", "round"),
    ("упоминания меня", "mention"),
    ("закреплённые", "pinned"),
    ("геометки", "geo"),
    ("контакты", "contact"),
    ("опросы", "poll"),
)


def parse_date(value: str) -> datetime | None:
    """`2026-08-01`, `01.08.2026`, `-7d`, `-12h` → an aware datetime, or None."""
    text = (value or "").strip().lower()
    if not text:
        return None
    now = datetime.now().astimezone()
    if text.startswith("-"):
        try:
            amount = int(text[1:-1])
        except ValueError:
            return None
        unit = text[-1]
        span = {"d": timedelta(days=amount), "h": timedelta(hours=amount),
                "w": timedelta(weeks=amount), "m": timedelta(minutes=amount)}.get(unit)
        return now - span if span else None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y", "%d.%m"):
        try:
            stamp = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%d.%m":
            stamp = stamp.replace(year=now.year)
        return stamp.replace(tzinfo=now.tzinfo)
    return None


def parse_when(value: str) -> datetime | None:
    """`2026-08-29 10:00`, `29.08 10:00` or `+30m` / `+2h` into a local datetime."""
    text = value.strip().lower()
    now = datetime.now().astimezone()
    if text.startswith("+"):
        try:
            amount = int(text[1:-1])
        except ValueError:
            return None
        unit = text[-1]
        if unit == "m":
            return now + timedelta(minutes=amount)
        if unit == "h":
            return now + timedelta(hours=amount)
        if unit == "d":
            return now + timedelta(days=amount)
        return None
    for pattern in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            stamp = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%H:%M":
            stamp = stamp.replace(hour=stamp.hour, minute=stamp.minute, second=0, microsecond=0)
            stamp = now.replace(hour=stamp.hour, minute=stamp.minute, second=0, microsecond=0)
            return stamp + timedelta(days=1) if stamp <= now else stamp
        if pattern == "%d.%m %H:%M":
            stamp = stamp.replace(year=now.year)
        return stamp.replace(tzinfo=now.tzinfo)
    return None


def human_size(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def comment_error(exc: Exception) -> str:
    name = type(exc).__name__
    if "MsgIdInvalid" in name:
        return "у этого поста нет комментариев"
    if "ChatWriteForbidden" in name or "Forbidden" in name:
        return "комментарии закрыты"
    if "Banned" in name or "UserBanned" in name:
        return "вам ограничили доступ в обсуждение"
    return f"не получилось: {exc}"


PERMISSIONS = (
    ("send_messages", "писать"),
    ("send_media", "медиа"),
    ("send_stickers", "стикеры"),
    ("send_gifs", "гифки"),
    ("send_polls", "опросы"),
    ("embed_links", "ссылки"),
    ("invite_users", "приглашать"),
    ("pin_messages", "закреплять"),
    ("change_info", "менять инфо"),
)


class TopicsScreen(ModalScreen[Topic | None]):
    """Threads of a forum group: switch, create, rename, close, pin."""

    BINDINGS = [
        Binding("escape", "dismiss", "закрыть"),
        Binding("n", "create", "создать"),
        Binding("r", "rename", "переименовать"),
        Binding("c", "toggle_closed", "закрыть/открыть"),
        Binding("p", "toggle_pinned", "закрепить"),
        Binding("d", "delete", "удалить"),
    ]

    def __init__(self, backend: Any, chat: Chat) -> None:
        super().__init__()
        self.backend = backend
        self.chat = chat
        self.topics: list[Topic] = []
        self._pending_delete: int | None = None      # d подтверждается вторым нажатием

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text(f"темы «{self.chat.name}»", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield OptionList(id="topic-list")
            yield Input(placeholder="название новой темы — enter создаст", id="topic-title")
            yield Static(Text("enter в списке — открыть · n — создать · r — переименовать · "
                              "c — закрыть/открыть · p — закрепить · d — удалить · escape — выход",
                              style=muted()),
                         classes="dialog-hint")

    def on_mount(self) -> None:
        self.load()

    @work(exclusive=True)
    async def load(self) -> None:
        listing = self.query_one("#topic-list", OptionList)
        listing.loading = True
        try:
            self.topics = await self.backend.topics(self.chat)
        except Exception as exc:
            listing.loading = False
            self.notify(f"темы не прочитались: {exc}", severity="error", timeout=8)
            return
        listing.loading = False
        listing.clear_options()
        for index, topic in enumerate(self.topics):
            line = Text()
            line.append("📌 " if topic.pinned else "   ", style=pal("primary"))
            line.append(topic.title, style=f"bold {pal('foreground')}")
            marks = []
            if topic.closed:
                marks.append("закрыта")
            if topic.hidden:
                marks.append("скрыта")
            if topic.unread:
                marks.append(f"{topic.unread} непрочитанных")
            if marks:
                line.append("   " + " · ".join(marks), style=muted())
            listing.add_option(Option(line, id=str(index)))
        listing.focus()

    def _current(self) -> Topic | None:
        listing = self.query_one("#topic-list", OptionList)
        if listing.highlighted is None or listing.highlighted >= len(self.topics):
            return None
        return self.topics[listing.highlighted]

    @on(OptionList.OptionSelected, "#topic-list")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.dismiss(self.topics[int(event.option_id)])

    @on(Input.Submitted, "#topic-title")
    def submitted(self) -> None:
        self.action_create()

    @work(group="topics")
    async def action_create(self) -> None:
        field = self.query_one("#topic-title", Input)
        title = field.value.strip()
        if not title:
            field.focus()
            self.notify("введите название темы", timeout=3)
            return
        try:
            await self.backend.create_topic(self.chat, title)
        except Exception as exc:
            self.notify(f"тема не создалась: {exc}", severity="error", timeout=8)
            return
        field.value = ""
        self.notify(f"тема «{title}» создана", timeout=4)
        self.load()

    @work(group="topics")
    async def action_rename(self) -> None:
        topic = self._current()
        title = self.query_one("#topic-title", Input).value.strip()
        if topic is None or not title:
            self.notify("выберите тему и впишите новое название в поле ниже", timeout=5)
            return
        await self._apply(topic, title=title)

    @work(group="topics")
    async def action_delete(self) -> None:
        """Удаление уносит всю переписку темы, поэтому спрашиваем — как и `x` в чате."""
        topic = self._current()
        if topic is None:
            self.notify("выберите тему", timeout=3)
            return
        if self._pending_delete != topic.id:
            self._pending_delete = topic.id
            self.notify(f"«{topic.title}» удалится со всей перепиской — нажмите d ещё раз",
                        severity="warning", timeout=6)
            return
        self._pending_delete = None
        try:
            removed = await self.backend.delete_topic(self.chat, topic.id)
        except Exception as exc:
            self.notify(f"не получилось: {exc}", severity="error", timeout=8)
            return
        self.notify(f"тема «{topic.title}» удалена ({removed} сообщений)", timeout=5)
        self.load()

    @work(group="topics")
    async def action_toggle_closed(self) -> None:
        topic = self._current()
        if topic is not None:
            await self._apply(topic, closed=not topic.closed)

    @work(group="topics")
    async def action_toggle_pinned(self) -> None:
        topic = self._current()
        if topic is None:
            return
        try:
            await self.backend.pin_topic(self.chat, topic.id, not topic.pinned)
        except Exception as exc:
            self.notify(f"не получилось: {exc}", severity="error", timeout=8)
            return
        self.load()

    async def _apply(self, topic: Topic, **changes: Any) -> None:
        try:
            await self.backend.edit_topic(self.chat, topic.id, **changes)
        except Exception as exc:
            self.notify(f"не получилось: {exc}", severity="error", timeout=8)
            return
        self.query_one("#topic-title", Input).value = ""
        self.load()


class NewChecklistScreen(ModalScreen[dict | None]):
    """Compose a checklist: a title and one item per line."""

    BINDINGS = [
        Binding("escape", "cancel", "отмена"),
        Binding("ctrl+s", "create", "отправить"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text("чек-лист", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Input(placeholder="заголовок", id="todo-title")
            yield TextArea(id="todo-items", soft_wrap=True)
            with Horizontal(id="todo-options"):
                yield Checkbox("другие могут отмечать", value=True, id="todo-complete")
                yield Checkbox("другие могут дописывать", value=True, id="todo-append")
            yield Static(Text("по пункту на строку · ctrl+s — отправить · escape — отмена", style=muted()),
                         classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#todo-title", Input).focus()

    def action_create(self) -> None:
        title = self.query_one("#todo-title", Input).value.strip()
        items = [line.strip() for line in self.query_one("#todo-items", TextArea).text.splitlines() if line.strip()]
        if not title or not items:
            self.notify("нужны заголовок и хотя бы один пункт", severity="warning", timeout=4)
            return
        self.dismiss({
            "title": title,
            "items": items,
            "others_can_complete": self.query_one("#todo-complete", Checkbox).value,
            "others_can_append": self.query_one("#todo-append", Checkbox).value,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChecklistScreen(ModalScreen[None]):
    """Tick items off a checklist, or add new ones."""

    BINDINGS = [Binding("escape", "dismiss", "закрыть")]

    def __init__(self, backend: Any, chat: Chat, msg: Msg) -> None:
        super().__init__()
        self.backend = backend
        self.chat = chat
        self.msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(f"☑ {self.msg.checklist_title}", style=f"bold {pal('primary')}"),
                         classes="dialog-title")
            yield OptionList(id="todo-list")
            yield Input(placeholder="дописать пункт — enter добавит", id="todo-new")
            yield Static(Text("enter в списке — отметить или снять · escape — закрыть", style=muted()),
                         classes="dialog-hint")

    def on_mount(self) -> None:
        self.repaint()
        self.query_one("#todo-list", OptionList).focus()

    def repaint(self) -> None:
        listing = self.query_one("#todo-list", OptionList)
        keep = listing.highlighted
        listing.clear_options()
        for index, (item_id, text, done) in enumerate(self.msg.checklist):
            line = Text()
            line.append("☑ " if done else "☐ ", style=pal("accent") if done else pal("foreground"))
            line.append(text, style=muted() if done else pal("foreground"))
            listing.add_option(Option(line, id=str(item_id)))
        if keep is not None and self.msg.checklist:
            listing.highlighted = min(keep, len(self.msg.checklist) - 1)

    @on(OptionList.OptionSelected, "#todo-list")
    def toggled(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.flip(int(event.option_id))

    @work(group="todo")
    async def flip(self, item_id: int) -> None:
        was = next((done for i, _, done in self.msg.checklist if i == item_id), False)
        try:
            updated = await self.backend.toggle_checklist(
                self.chat, self.msg.id, done=[] if was else [item_id], undone=[item_id] if was else [])
        except Exception as exc:
            self.notify(f"не отметилось: {exc}", severity="error", timeout=8)
            return
        if updated:
            self.msg.checklist = tuple(updated)
        self.repaint()

    @on(Input.Submitted, "#todo-new")
    def add(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.append_item(text)

    @work(group="todo")
    async def append_item(self, text: str) -> None:
        try:
            updated = await self.backend.append_checklist(self.chat, self.msg.id, [text])
        except Exception as exc:
            self.notify(f"не добавилось: {exc}", severity="error", timeout=8)
            return
        if updated:
            self.msg.checklist = tuple(updated)
        self.repaint()


class NewChatScreen(ModalScreen[dict | None]):
    """Create a channel, a supergroup, or a group with topics."""

    BINDINGS = [
        Binding("escape", "cancel", "отмена"),
        Binding("ctrl+s", "create", "создать"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text("новый чат", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Select([("канал", "channel"), ("группа", "group"), ("группа с темами", "forum")],
                         value="channel", allow_blank=False, id="new-kind")
            yield Input(placeholder="название", id="new-title")
            yield Input(placeholder="описание (необязательно)", id="new-about")
            yield Input(placeholder="публичный адрес без @ (необязательно)", id="new-username")
            yield Static(Text("без адреса чат останется приватным — ссылку можно выпустить позже",
                              style=muted()), classes="dialog-hint")
            yield Static(Text("ctrl+s — создать · escape — отмена", style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#new-title", Input).focus()

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_create()

    def action_create(self) -> None:
        title = self.query_one("#new-title", Input).value.strip()
        if not title:
            self.notify("нужно название", severity="warning", timeout=3)
            return
        self.dismiss({
            "title": title,
            "kind": str(self.query_one("#new-kind", Select).value or "channel"),
            "about": self.query_one("#new-about", Input).value.strip(),
            "username": self.query_one("#new-username", Input).value.strip() or None,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class ManageScreen(ModalScreen[None]):
    """Read and change a channel or group: title, description, link, rights, slow mode."""

    BINDINGS = [
        Binding("escape", "dismiss", "закрыть"),
        Binding("ctrl+s", "save", "сохранить"),
        Binding("ctrl+l", "invite", "ссылка"),
        Binding("ctrl+d", "discussion", "обсуждение"),
        Binding("ctrl+u", "members", "участники"),
    ]

    def __init__(self, backend: Any, chat: Chat, chats: Sequence[Chat]) -> None:
        super().__init__()
        self.backend = backend
        self.chat = chat
        self.chats = list(chats)
        self.details: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text(f"управление «{self.chat.name}»", style=f"bold {pal('primary')}"),
                         classes="dialog-title")
            yield Static(id="manage-summary")
            yield Input(placeholder="название", id="manage-title")
            yield Input(placeholder="описание", id="manage-about")
            yield Input(placeholder="публичный адрес без @", id="manage-username")
            yield Static(Text("права участников по умолчанию", style=muted()), id="manage-rights-label")
            with ItemGrid(id="manage-rights", min_column_width=18):
                for name, label in PERMISSIONS:
                    yield Checkbox(label, value=True, id=f"perm-{name}")
            with Horizontal(id="manage-extras"):
                yield Static(Text("слоу-мод, секунд (0 — выключен):", style=muted()), id="manage-slow-label")
                yield Input(placeholder="0", id="manage-slowmode")
            yield Static(Text("ctrl+s — сохранить · ctrl+l — ссылка-приглашение · "
                              "ctrl+d — группа обсуждения · ctrl+u — участники · escape — закрыть",
                              style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.load()

    @work(exclusive=True)
    async def load(self) -> None:
        summary = self.query_one("#manage-summary", Static)
        summary.update(Text("читаю…", style=muted()))
        try:
            self.details = await self.backend.chat_details(self.chat)
        except Exception as exc:
            summary.update(Text(f"не прочиталось: {exc}", style=pal("error")))
            return
        info = Text()
        info.append(f"{self.chat.kind}", style=muted())
        for label, key in (("участников", "participants"), ("админов", "admins")):
            if self.details.get(key) is not None:
                info.append(f" · {self.details[key]} {label}", style=muted())
        if self.details.get("linked_chat_id"):
            info.append(f" · обсуждение #{self.details['linked_chat_id']}", style=muted())
        if not self.details.get("can_edit", False):
            info.append("  — вы не администратор, правки не пройдут", style=pal("warning"))
        summary.update(info)
        self.query_one("#manage-title", Input).value = self.details.get("title") or self.chat.name
        self.query_one("#manage-about", Input).value = self.details.get("about") or ""
        self.query_one("#manage-username", Input).value = self.details.get("username") or ""
        self.query_one("#manage-slowmode", Input).value = str(self.details.get("slowmode") or 0)
        # default rights and slow mode are a group thing; a channel has neither
        group_only = self.chat.kind != "channel"
        self.query_one("#manage-rights").display = group_only
        self.query_one("#manage-rights-label").display = group_only
        self.query_one("#manage-extras").display = group_only
        banned = self.details.get("banned_rights")
        for name, _ in PERMISSIONS:
            allowed = not bool(getattr(banned, name, False)) if banned is not None else True
            self.query_one(f"#perm-{name}", Checkbox).value = allowed
        self.query_one("#manage-title", Input).focus()

    @work(group="manage")
    async def action_save(self) -> None:
        changes: list[str] = []
        try:
            edited = await self.backend.edit_chat(
                self.chat,
                title=self.query_one("#manage-title", Input).value,
                about=self.query_one("#manage-about", Input).value,
                username=self.query_one("#manage-username", Input).value or None,
            )
            changes += list(edited)
            wanted = int(self.query_one("#manage-slowmode", Input).value or 0) if self.chat.kind != "channel" else 0
            if wanted != int(self.details.get("slowmode") or 0):
                await self.backend.set_slowmode(self.chat, wanted)
                changes.append("слоу-мод")
            if self.chat.kind != "channel":
                allowed = {name: self.query_one(f"#perm-{name}", Checkbox).value for name, _ in PERMISSIONS}
                await self.backend.set_permissions(self.chat, allowed)
                changes.append("права")
        except Exception as exc:
            self.notify(f"не сохранилось: {exc}", severity="error", timeout=10)
            return
        self.notify("сохранено: " + (", ".join(changes) if changes else "без изменений"), timeout=5)
        self.load()

    @work(group="manage")
    async def action_invite(self) -> None:
        try:
            link = await self.backend.invite_link(self.chat)
        except Exception as exc:
            self.notify(f"ссылка не выпустилась: {exc}", severity="error", timeout=8)
            return
        self.app.copy_to_clipboard(link)
        self.notify(f"{link}  — скопировано в буфер", timeout=10)

    @work(group="manage")
    async def action_discussion(self) -> None:
        if self.chat.kind != "channel":
            self.notify("группу обсуждения привязывают к каналу", timeout=4)
            return
        groups = [c for c in self.chats if c.kind == "group"]
        target = await self.app.push_screen_wait(ChatPickScreen(groups, "группа обсуждения"))
        if target is None:
            return
        try:
            await self.backend.set_discussion(self.chat, target)
        except Exception as exc:
            self.notify(f"не привязалось: {exc}", severity="error", timeout=10)
            return
        self.notify(f"обсуждение: «{target.name}»", timeout=5)
        self.load()

    @work(group="manage")
    async def action_members(self) -> None:
        try:
            rows = await self.backend.members(self.chat, limit=50)
        except Exception as exc:
            self.notify(f"участники недоступны: {exc}", severity="error", timeout=8)
            return
        listing = ", ".join(r["name"] for r in rows[:12]) or "никого не видно"
        self.notify(f"{len(rows)}: {listing}", timeout=10)


class PostScreen(ModalScreen[dict | None]):
    """A post editor: markup on the left, exactly what Telegram will show on the right."""

    BINDINGS = [
        Binding("escape", "cancel", "отмена"),
        Binding("ctrl+s", "publish", "опубликовать"),
    ]

    def __init__(self, chat_name: str, draft: str = "", bots: Sequence[dict[str, str]] = ()) -> None:
        super().__init__()
        self.chat_name = chat_name
        self.draft = draft
        self.bots = list(bots)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text(f"пост в «{self.chat_name}»", style=f"bold {pal('primary')}"), classes="dialog-title")
            with Horizontal(id="post-options"):
                yield Select([("markdown", "md"), ("html", "html"), ("без разметки", "none")],
                             value="md", allow_blank=False, id="post-mode")
                yield Checkbox("превью ссылок", value=True, id="post-preview")
                yield Checkbox("без звука", id="post-silent")
                yield Select([("от себя", "")] + [(f"@{b['username']}", b["username"]) for b in self.bots],
                             value="", allow_blank=False, id="post-as")
                yield Checkbox("богатое", id="post-rich")
            with Horizontal(id="post-body"):
                yield TextArea(self.draft, id="post-text", soft_wrap=True)
                yield Static(id="post-render")
            with Horizontal(id="post-extras"):
                yield Input(placeholder="файл (необязательно), несколько через запятую", id="post-files")
                yield Input(placeholder="отложить: 2026-08-29 10:00", id="post-when")
            yield Input(placeholder="кнопки: Текст=https://…, Приложение=webapp:https://… — только от имени бота",
                        id="post-buttons")
            yield Static(Text(tgx_format.SYNTAX, style=muted()), id="post-syntax")
            yield Static(Text("ctrl+s — опубликовать · escape — отмена", style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#post-text", TextArea).focus()
        self.repaint()

    @on(TextArea.Changed, "#post-text")
    def typed(self) -> None:
        self.repaint()

    @on(Select.Changed, "#post-mode")
    def mode_changed(self) -> None:
        self.repaint()

    def mode(self) -> str:
        value = self.query_one("#post-mode", Select).value
        return str(value) if value else "md"

    def repaint(self) -> None:
        source = self.query_one("#post-text", TextArea).text
        target = self.query_one("#post-render", Static)
        if not source.strip():
            target.update(Text("здесь будет виден готовый пост", style=f"italic {muted()}"))
            return
        try:
            target.update(tgx_format.preview(source, self.mode(), colors=PAL))
        except Exception as exc:
            target.update(Text(f"разметка не разобралась: {exc}", style=pal("error")))

    def action_publish(self) -> None:
        text = self.query_one("#post-text", TextArea).text.strip()
        files = [f.strip() for f in self.query_one("#post-files", Input).value.split(",") if f.strip()]
        if not text and not files:
            self.notify("пустой пост", severity="warning", timeout=3)
            return
        when = self.query_one("#post-when", Input).value.strip()
        schedule = None
        if when:
            schedule = parse_when(when)
            if schedule is None:
                self.notify("не понял дату: нужен формат 2026-08-29 10:00", severity="warning", timeout=5)
                return
        as_bot = str(self.query_one("#post-as", Select).value or "")
        rich = self.query_one("#post-rich", Checkbox).value
        if rich:
            try:
                tgx_rich.check_limits(text)
            except Exception as exc:
                self.notify(str(exc), severity="warning", timeout=8)
                return
        buttons = self.query_one("#post-buttons", Input).value.strip()
        if buttons and not as_bot:
            self.notify("кнопки под постом умеет вешать только бот — выберите его в «от себя»",
                        severity="warning", timeout=6)
            return
        if buttons:
            try:
                tgx_bots.parse_buttons(buttons)
            except Exception as exc:
                self.notify(str(exc), severity="warning", timeout=8)
                return
        self.dismiss({
            "text": text,
            "as_bot": as_bot,
            "rich": rich,
            "buttons": buttons,
            "parse_mode": self.mode(),
            "link_preview": self.query_one("#post-preview", Checkbox).value,
            "silent": self.query_one("#post-silent", Checkbox).value,
            "files": files,
            "schedule": schedule,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question — used before anything that cannot be undone."""

    BINDINGS = [
        Binding("escape,n", "refuse", "нет"),
        Binding("y,enter", "accept", "да"),
    ]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.question, style=f"bold {pal('warning')}"), classes="dialog-title")
            if self.detail:
                yield Static(Text(self.detail, style=muted()))
            yield Static(Text("y — да · escape — нет", style=muted()), classes="dialog-hint")

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_refuse(self) -> None:
        self.dismiss(False)


class ChatPickScreen(ModalScreen[Chat | None]):
    """Choose a chat — where to forward to."""

    BINDINGS = [Binding("escape", "dismiss", "закрыть")]

    def __init__(self, chats: Sequence[Chat], title: str) -> None:
        super().__init__()
        self.chats = list(chats)
        self.title_text = title
        self.shown: list[Chat] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text(self.title_text, style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Input(placeholder="фильтр по названию", id="pick-filter")
            yield OptionList(id="pick-list")
            yield Static(Text("↑↓ + enter — выбрать · escape — отмена", style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.repaint("")
        self.query_one("#pick-filter", Input).focus()

    def repaint(self, needle: str) -> None:
        needle = needle.strip().lower()
        self.shown = [c for c in self.chats if not needle or needle in f"{c.name} {c.username}".lower()][:100]
        options = self.query_one("#pick-list", OptionList)
        options.clear_options()
        options.add_options([Option(chat_row(c), id=str(i)) for i, c in enumerate(self.shown)])

    @on(Input.Changed, "#pick-filter")
    def filtered(self, event: Input.Changed) -> None:
        self.repaint(event.value)

    @on(Input.Submitted, "#pick-filter")
    def take_first(self) -> None:
        if self.shown:
            self.dismiss(self.shown[0])

    @on(OptionList.OptionSelected, "#pick-list")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.dismiss(self.shown[int(event.option_id)])


class ReactionScreen(ModalScreen[str | None]):
    """Pick a reaction. Empty string means "remove mine"."""

    QUICK = ("👍", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "😢", "🎉", "💯", "🤝", "👀")
    BINDINGS = [
        Binding("escape", "dismiss", "закрыть"),
        Binding("minus,delete,backspace", "clear", "убрать"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text("реакция", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield OptionList(*[Option(f" {e}   {i + 1}", id=e) for i, e in enumerate(self.QUICK)], id="reaction-list")
            yield Input(placeholder="или свой эмодзи", id="reaction-custom")
            yield Static(Text("enter — поставить · минус — убрать свою · escape — отмена", style=muted()),
                         classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#reaction-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#reaction-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    @on(Input.Submitted, "#reaction-custom")
    def custom(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_clear(self) -> None:
        self.dismiss("")


class ButtonScreen(ModalScreen[tuple[int, int] | None]):
    """Press one of a bot's inline buttons."""

    BINDINGS = [Binding("escape", "dismiss", "закрыть")]

    def __init__(self, rows: Sequence[Sequence[str]]) -> None:
        super().__init__()
        self.rows = [list(row) for row in rows]

    def compose(self) -> ComposeResult:
        options = []
        for r, row in enumerate(self.rows):
            for c, label in enumerate(row):
                options.append(Option(Text(f"  {label}", style=pal("foreground")), id=f"{r},{c}"))
        with Vertical(id="dialog"):
            yield Static(Text("кнопки бота", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield OptionList(*options, id="button-list")
            yield Static(Text("enter — нажать · escape — отмена", style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#button-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#button-list")
    def pressed(self, event: OptionList.OptionSelected) -> None:
        row, col = (event.option_id or "0,0").split(",")
        self.dismiss((int(row), int(col)))


class AttachScreen(ModalScreen[dict | None]):
    """Pick file(s), choose how they go out, add a caption."""

    BINDINGS = [
        Binding("escape", "cancel", "отмена"),
        Binding("ctrl+s", "send", "отправить"),
    ]

    def __init__(self, caption: str = "", start: Path | None = None) -> None:
        super().__init__()
        self.caption = caption
        self.start = start or Path.home()

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(Text("прикрепить файл", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Input(placeholder="путь к файлу — несколько через запятую уйдут альбомом", id="attach-path")
            with Horizontal(id="attach-body"):
                yield DirectoryTree(str(self.start), id="attach-tree")
                yield Vertical(id="attach-preview")
            with Horizontal(id="attach-options"):
                yield Checkbox("как файл", id="opt-document")
                yield Checkbox("голосовое", id="opt-voice")
                yield Checkbox("кружок", id="opt-note")
                yield Checkbox("без звука", id="opt-silent")
            yield Input(value=self.caption, placeholder="подпись…", id="attach-caption")
            yield Static(Text("enter — отправить · ctrl+s — тоже · escape — отмена", style=muted()),
                         classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#attach-path", Input).focus()

    # --- picking ---------------------------------------------------------
    @on(DirectoryTree.FileSelected, "#attach-tree")
    def picked_in_tree(self, event: DirectoryTree.FileSelected) -> None:
        self.query_one("#attach-path", Input).value = str(event.path)

    @on(Input.Changed, "#attach-path")
    def path_changed(self) -> None:
        self.refresh_preview()

    def paths(self) -> list[Path]:
        raw = self.query_one("#attach-path", Input).value
        out = []
        for chunk in raw.split(","):
            chunk = chunk.strip().strip("'\"")
            if chunk:
                out.append(Path(chunk).expanduser())
        return out

    @work(exclusive=True, group="attach-preview")
    async def refresh_preview(self) -> None:
        holder = self.query_one("#attach-preview", Vertical)
        await holder.remove_children()
        files = self.paths()
        if not files:
            return
        lines = Text()
        missing = [f for f in files if not f.is_file()]
        for f in files[:6]:
            size = f.stat().st_size if f.is_file() else 0
            mark = "✓" if f.is_file() else "✗"
            style = muted() if f.is_file() else pal("error")
            lines.append(f"{mark} {f.name}  {human_size(size)}\n", style=style)
        if missing:
            lines.append("файл не найден", style=pal("error"))
        await holder.mount(Static(lines))
        if len(files) == 1 and files[0].is_file():
            widget = tgx_media.make_widget(files[0], max_cols=34, max_rows=10, backend="auto")
            if widget is not None:
                await holder.mount(widget)

    # --- sending ---------------------------------------------------------
    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_send()

    def action_send(self) -> None:
        files = [f for f in self.paths() if f.is_file()]
        if not files:
            self.notify("укажите существующий файл", severity="warning", timeout=4)
            return
        self.dismiss({
            "paths": [str(f) for f in files],
            "caption": self.query_one("#attach-caption", Input).value.strip(),
            "document": self.query_one("#opt-document", Checkbox).value,
            "voice": self.query_one("#opt-voice", Checkbox).value,
            "note": self.query_one("#opt-note", Checkbox).value,
            "silent": self.query_one("#opt-silent", Checkbox).value,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommentsScreen(ModalScreen[None]):
    """The discussion thread under a channel post: read it and add to it."""

    BINDINGS = [Binding("escape", "dismiss", "закрыть")]

    def __init__(self, chat: Chat, post: Msg, loader: Callable[[int], Any], sender: Callable[[int, str], Any]) -> None:
        super().__init__()
        self.chat = chat
        self.post = post
        self.loader = loader
        self.sender = sender

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Static(self._header(), id="comments-head")
            yield MessageList(id="comments-list")
            yield Input(placeholder="написать комментарий…", id="comment-input")
            yield Static(Text("enter — отправить · escape — закрыть", style=muted()), classes="dialog-hint")

    def _header(self) -> Text:
        text = Text()
        text.append("комментарии к посту ", style=f"bold {pal('primary')}")
        text.append(f"#{self.post.id}", style=muted())
        preview = one_line(self.post.text or self.post.media or "", 90)
        if preview:
            text.append(f"\n{preview}", style=muted())
        return text

    def on_mount(self) -> None:
        self.load()

    @work(exclusive=True)
    async def load(self) -> None:
        thread = self.query_one("#comments-list", MessageList)
        thread.loading = True
        try:
            rows = await self.loader(self.post.id)
        except Exception as exc:
            thread.loading = False
            self.notify(comment_error(exc), severity="error", timeout=8)
            self.query_one("#comment-input", Input).focus()
            return
        thread.loading = False
        await thread.show(self.chat, rows)
        self.query_one("#comment-input", Input).focus()

    @on(Input.Submitted, "#comment-input")
    def submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.publish(text)

    @work(group="comment-send")
    async def publish(self, text: str) -> None:
        try:
            msg = await self.sender(self.post.id, text)
        except Exception as exc:
            self.notify(comment_error(exc), severity="error", timeout=10)
            return
        if self.post.comments is not None:
            self.post.comments += 1
        await self.query_one("#comments-list", MessageList).append(msg, flash=False)


class MediaScreen(ModalScreen[None]):
    """Full-pane look at one picture — as large as the terminal allows."""

    BINDINGS = [
        Binding("escape,q,v,enter", "dismiss", "закрыть"),
        Binding("o", "open_external", "открыть в системе"),
    ]

    def __init__(self, path: Path, caption: str, backend: str = "auto", note: str = "") -> None:
        super().__init__()
        self.path = path
        self.caption = caption
        self.backend = backend
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="media-box"):
            yield Static(Text(one_line(self.caption, 110), style=f"bold {pal('primary')}"), id="media-caption")
            yield Vertical(id="media-holder")
            hint = "escape — закрыть · o — открыть в системном просмотрщике"
            if self.note:
                hint = f"{self.note} · {hint}"
            yield Static(Text(hint, style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        holder = self.query_one("#media-holder", Vertical)
        cols = max(20, self.size.width - 10)
        rows = max(8, self.size.height - 8)
        widget = tgx_media.make_widget(self.path, max_cols=cols, max_rows=rows, backend=self.backend)
        if widget is None:
            holder.mount(Static(Text("картинку не удалось прочитать", style=pal("error"))))
            return
        holder.mount(widget)
        fade_in(widget, duration=0.2)

    @work(group="open")
    async def action_open_external(self) -> None:
        error = await asyncio.to_thread(tgx_media.open_external, self.path)
        self.notify(
            f"не открылось: {error}" if error else f"открываю {self.path.name}",
            severity="error" if error else "information",
            timeout=10 if error else 4,
        )


class SearchScreen(ModalScreen[tuple[Any, Msg] | None]):
    """Search this chat or every chat, narrowed by media type, sender and dates."""

    BINDINGS = [Binding("escape", "dismiss", "закрыть")]

    def __init__(self, searcher: Callable[..., Any], chat_name: str, in_chat: bool = True) -> None:
        super().__init__()
        self.searcher = searcher
        self.chat_name = chat_name
        self.in_chat = in_chat
        self.hits: list[tuple[Any, Msg]] = []

    def compose(self) -> ComposeResult:
        scope = [("в этом чате", "chat"), ("во всех чатах", "all")] if self.in_chat else [("во всех чатах", "all")]
        with Vertical(id="dialog-wide"):
            yield Static(Text(f"поиск · {self.chat_name}", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Input(placeholder="что ищем", id="search-input")
            with Horizontal(id="search-filters"):
                yield Select(scope, value=scope[0][1], allow_blank=False, id="search-scope")
                yield Select([(label, value) for label, value in SEARCH_KINDS], value="",
                             allow_blank=False, id="search-kind")
                yield Input(placeholder="от кого (@имя)", id="search-from")
                yield Input(placeholder="с: 2026-08-01 или -7d", id="search-since")
                yield Input(placeholder="по: 2026-08-28", id="search-until")
            yield OptionList(id="search-results")
            yield Static(Text("enter — искать · ↑↓ + enter в списке — перейти к сообщению · escape — закрыть",
                              style=muted()), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    @on(Input.Submitted)
    def run_search(self) -> None:
        self.do_search()

    @on(Select.Changed, "#search-kind")
    def kind_changed(self) -> None:
        if self.query_one("#search-input", Input).value.strip() or self.kind():
            self.do_search()

    def kind(self) -> str:
        return str(self.query_one("#search-kind", Select).value or "")

    @work(exclusive=True)
    async def do_search(self) -> None:
        results = self.query_one("#search-results", OptionList)
        query = self.query_one("#search-input", Input).value.strip()
        kind = self.kind()
        if not query and not kind:
            self.notify("введите запрос или выберите тип", timeout=3)
            return
        everywhere = str(self.query_one("#search-scope", Select).value) == "all"
        since = parse_date(self.query_one("#search-since", Input).value)
        until = parse_date(self.query_one("#search-until", Input).value)
        for field, value in (("#search-since", since), ("#search-until", until)):
            if self.query_one(field, Input).value.strip() and value is None:
                self.notify("дата не разобралась: 2026-08-01, 01.08.2026 или -7d", severity="warning", timeout=6)
                return
        results.loading = True
        try:
            self.hits = list(await self.searcher(
                query, everywhere, kind or None,
                self.query_one("#search-from", Input).value.strip() or None, since, until,
            ))
        except Exception as exc:
            results.loading = False
            self.hits = []
            results.clear_options()
            self.notify(str(exc), severity="error", timeout=8)
            return
        results.loading = False
        results.clear_options()
        if not self.hits:
            results.add_option(Option(Text("  ничего не найдено", style=muted()), id="none", disabled=True))
            return
        for index, (chat, msg) in enumerate(self.hits):
            line = Text()
            line.append(f"{relative(msg.date)} ", style=muted())
            if chat is not None:
                line.append(f"{chat.name} ", style=f"bold {pal('primary')}")
            if msg.sender:
                line.append(f"{msg.sender}: ", style=muted())
            line.append(one_line(msg.text or msg.media or "—", 80))
            results.add_option(Option(line, id=str(index)))
        results.focus()

    @on(OptionList.OptionSelected, "#search-results")
    def pick(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and event.option_id.isdigit():
            self.dismiss(self.hits[int(event.option_id)])


class LoginScreen(ModalScreen[bool]):
    """Phone → code → 2FA, in place, without leaving the UI."""

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self.backend = backend
        self.phone = ""
        self.step = "phone"

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Static(Text("вход в Telegram", style=f"bold {pal('primary')}"), classes="dialog-title")
            yield Static(Text("номер телефона в международном формате, например +79161234567", style=muted()), id="login-hint")
            yield Input(placeholder="+7…", id="login-input")
            yield Static("", id="login-status")

    def on_mount(self) -> None:
        self.query_one("#login-input", Input).focus()

    @on(Input.Submitted, "#login-input")
    def submit(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.advance(value)

    @work(exclusive=True)
    async def advance(self, value: str) -> None:
        field = self.query_one("#login-input", Input)
        hint = self.query_one("#login-hint", Static)
        status = self.query_one("#login-status", Static)
        field.disabled = True
        status.update(Text("…", style=muted()))
        try:
            if self.step == "phone":
                self.phone = value
                await self.backend.send_code(value)
                self.step = "code"
                hint.update(Text("код из Telegram", style=muted()))
                field.password = False
                field.placeholder = "12345"
            elif self.step == "code":
                if await self.backend.sign_in(self.phone, value):
                    self.dismiss(True)
                    return
                self.step = "password"
                hint.update(Text("облачный пароль (2FA)", style=muted()))
                field.password = True
                field.placeholder = "пароль"
            else:
                await self.backend.sign_in_password(value)
                self.dismiss(True)
                return
            status.update(Text(""))
        except Exception as exc:
            status.update(Text(f"✗ {exc}", style=pal("error")))
        finally:
            field.disabled = False
            field.value = ""
            field.focus()


# ── application ──────────────────────────────────────────────────────────────
class TgxApp(App):
    CSS_PATH = "tgx_tui.tcss"
    TITLE = "tgx"
    SUB_TITLE = "telegram in your terminal"

    BINDINGS = [
        Binding("ctrl+q", "quit", "выход"),
        Binding("slash", "focus_filter", "чаты"),
        Binding("ctrl+f", "search_chat", "поиск"),
        Binding("ctrl+r", "reply", "ответить"),
        Binding("ctrl+t", "cycle_theme", "тема"),
        Binding("f1", "help", "справка"),
        Binding("question_mark", "help", "справка", show=False),
        Binding("ctrl+k", "focus_chats", "список чатов", show=False),
        Binding("ctrl+y", "copy_message", "копировать", show=False),
        Binding("ctrl+d", "download", "скачать", show=False),
        Binding("ctrl+s", "attach", "файл"),
        Binding("c", "comments", "комменты"),
        Binding("v", "view_media", "картинка"),
        Binding("a", "transcribe", "расшифровать", show=False),
        Binding("e", "edit_message", "правка", show=False),
        Binding("f", "forward", "переслать", show=False),
        Binding("x", "delete_message", "удалить", show=False),
        Binding("plus,plus_sign,equals_sign", "react", "реакция", show=False),
        Binding("minus,hyphen_minus", "unreact", "убрать реакцию", show=False),
        Binding("b", "press_button", "кнопки бота", show=False),
        Binding("s", "toggle_spoilers", "спойлер", show=False),
        Binding("p", "post", "пост", show=False),
        Binding("i", "manage_chat", "управление", show=False),
        Binding("t", "topics", "темы", show=False),
        Binding("l", "checklist", "чек-лист", show=False),   # k is vim-up in the message list
        Binding("P", "pin_message", "закрепить", show=False),
        Binding("n", "new_chat", "новый чат", show=False),
        Binding("o", "open_media", "открыть", show=False),
        Binding("ctrl+e", "compose_long", "многострочно", show=False),
        Binding("ctrl+b", "toggle_sidebar", "панель", show=False),
        Binding("ctrl+n", "next_unread", "непрочитанное", show=False),
        Binding("ctrl+g", "reload", "обновить", show=False),
        Binding("ctrl+u", "load_older", "старее", show=False),
        Binding("R", "mark_read", "прочитано", show=False),
        Binding("f12", "shot", "скриншот", show=False),
        Binding("escape", "escape", "", show=False),
    ]

    def __init__(
        self,
        backend: Any,
        theme_name: str = "tgx-night",
        mark_read: bool = True,
        notifications: bool = True,
        dialog_limit: int = 0,
        media: str = "auto",
        media_detected: str = "",
    ) -> None:
        super().__init__()
        self.backend = backend
        self.media_backend = media if media in tgx_media.BACKENDS else "auto"
        self.media_detected = media_detected
        self.theme_name = theme_name
        self.mark_read_on_open = mark_read
        self.notifications = notifications
        self.dialog_limit = dialog_limit
        self.current: Chat | None = None
        self.reply_to: Msg | None = None
        self.editing: Msg | None = None
        self.current_topic: Topic | None = None
        self._folders: dict[int, Folder] = {}
        self._topics: dict[int, Topic] = {}
        self._sent_ids: set[int] = set()
        self._open_timer: Any = None
        self._read_timer: Any = None
        self._loading_older = False

    # --- layout ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="🔍 фильтр чатов", id="filter")
                yield Tabs(Tab("все", id="f-all"), id="folders")
                yield ChatList(id="chats")
            with Vertical(id="chat"):
                yield Static("", id="chat-header")
                yield Tabs(id="topics")
                yield MessageList(id="messages")
                yield TypingBar()
                yield Static("", id="reply-chip")
                with Horizontal(id="composer-row"):
                    yield Composer(id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(NIGHT)
        self.register_theme(DAY)
        self.theme = self.theme_name if self.theme_name in self.available_themes else "tgx-night"
        self._sync_palette()
        try:
            self.theme_changed_signal.subscribe(self, lambda _theme: self._sync_palette(repaint=True))
        except Exception:
            pass
        self.query_one(MessageList).loading = True
        self.boot()

    # --- theming ---------------------------------------------------------
    def _sync_palette(self, repaint: bool = False) -> None:
        theme = self.get_theme(self.theme)
        if theme is not None:
            dark = bool(theme.dark)
            PAL.update(
                {
                    "primary": theme.primary,
                    "accent": theme.accent or theme.primary,
                    "foreground": theme.foreground or ("#E4EDF5" if dark else "#101E29"),
                    "background": theme.background or ("#0E1621" if dark else "#FFFFFF"),
                    "surface": theme.surface or ("#17212B" if dark else "#F2F5F8"),
                    "panel": theme.panel or ("#22303C" if dark else "#DDE5EC"),
                    "success": theme.success or "#4FCE5D",
                    "warning": theme.warning or "#E5CA77",
                    "error": theme.error or "#E9576B",
                    "text-muted": "#7E93A5" if dark else "#5B7183",
                }
            )
        if repaint:
            chats = self.query_one(ChatList)
            chats.rebuild()
            for widget in self.query(Bubble):
                widget.refresh_text()
            self.query_one(TopBar).refresh()
            self._paint_header()

    def action_cycle_theme(self) -> None:
        known = [t for t in THEMES if t in self.available_themes]
        try:
            index = known.index(self.theme)
        except ValueError:
            index = -1
        self.theme = known[(index + 1) % len(known)]
        self.notify(f"тема: {self.theme}", timeout=2)

    # --- boot ------------------------------------------------------------
    @work(exclusive=True)
    async def boot(self) -> None:
        bar = self.query_one(TopBar)
        try:
            authorized = await asyncio.wait_for(self.backend.connect(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self.query_one(MessageList).loading = False
            bar.account = "нет связи"
            self.notify(f"Telegram не ответил за {CONNECT_TIMEOUT} c — проверь сеть и запусти снова", severity="error", timeout=12)
            return
        except Exception as exc:
            self.query_one(MessageList).loading = False
            self.notify(f"нет соединения: {exc}", severity="error", timeout=8)
            return
        if not authorized:
            bar.account = "требуется вход"
            ok = await self.push_screen_wait(LoginScreen(self.backend))
            if not ok:
                self.exit(message="вход не выполнен")
                return
        bar.account = await self.backend.whoami()
        bar.live = True
        if self.media_backend != "off" and not tgx_media.available():
            self.notify(f"превью выключены: нет {tgx_media.missing()} — pip install -r requirements.txt", timeout=8)
        await self.refresh_dialogs(first=True)
        self.backend.watch(self._incoming, self._typing)

    @work(exclusive=True, group="dialogs")
    async def reload_dialogs(self) -> None:
        await self.refresh_dialogs()

    async def refresh_dialogs(self, first: bool = False) -> None:
        chats_widget = self.query_one(ChatList)
        try:
            chats = await self.backend.dialogs(FIRST_PAGE if first else self.dialog_limit)
        except Exception as exc:
            self.notify(f"не удалось получить чаты: {exc}", severity="error")
            self.query_one(MessageList).loading = False
            return
        chats_widget.set_chats(chats)
        self._paint_counts()
        if first:
            await self._load_folders()
            self.query_one(MessageList).loading = False
            if chats_widget.visible_chats:
                await self.open_chat(chats_widget.visible_chats[0])
            self.set_focus(chats_widget)
            self.load_all_dialogs()

    def _paint_counts(self) -> None:
        media = self.media_detected or tgx_media.describe(self.media_backend)
        total = len(self.query_one(ChatList).chats)
        self.query_one(TopBar).detail = f"{total} чатов · медиа: {media}"

    @work(group="dialogs-all")
    async def load_all_dialogs(self) -> None:
        """Fetch the whole dialog list behind the first page, then merge it in."""
        widget = self.query_one(ChatList)
        if len(widget.chats) < FIRST_PAGE:
            return                                  # the first page already held everything
        bar = self.query_one(TypingBar)
        bar.status("догружаю остальные чаты…", seconds=60)
        try:
            chats = await self.backend.dialogs(self.dialog_limit)
        except Exception as exc:
            bar.hide()
            self.notify(f"не удалось догрузить чаты: {exc}", severity="error", timeout=8)
            return
        merged = merge_chats(widget.chats, chats)
        widget.set_chats(merged)
        if self.current is not None:
            self.current = next((c for c in merged if c.id == self.current.id), self.current)
        bar.hide()
        self._paint_counts()

    async def _load_folders(self) -> None:
        try:
            folders = await self.backend.folders()
        except Exception:
            folders = []
        if not folders:
            self.query_one("#folders", Tabs).display = False
            return
        self._folders = {f.id: f for f in folders}
        tabs = self.query_one("#folders", Tabs)
        for folder in folders:
            tabs.add_tab(Tab(folder.title, id=f"f-{folder.id}"))

    # --- chat opening ----------------------------------------------------
    @on(OptionList.OptionHighlighted, "#chats")
    def chat_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        chat = self.query_one(ChatList).chat_at(event.option_index)
        if chat is None or (self.current and chat.id == self.current.id):
            return
        if self._open_timer is not None:
            self._open_timer.stop()
        self._open_timer = self.set_timer(0.22, lambda: self.open_chat_worker(chat))

    @on(OptionList.OptionSelected, "#chats")
    def chat_selected(self, event: OptionList.OptionSelected) -> None:
        chat = self.query_one(ChatList).chat_at(event.option_index)
        if chat is not None:
            self.open_chat_worker(chat)
            self.set_focus(self.query_one(Composer))

    @work(exclusive=True, group="history")
    async def open_chat_worker(self, chat: Chat, topic: Topic | None = None) -> None:
        await self.open_chat(chat, topic)

    async def open_chat(self, chat: Chat, topic: Topic | None = None) -> None:
        self.current = chat
        self.current_topic = topic
        self.clear_reply()
        messages = self.query_one(MessageList)
        self._paint_header()
        self._paint_composer()
        if topic is None:
            await self._paint_topics(chat)
            topic = self.current_topic
        messages.loading = True
        try:
            history = await self.backend.history(chat, limit=60,
                                                 topic_id=topic.id if topic else None)
        except Exception as exc:
            messages.loading = False
            self.notify(f"история недоступна: {exc}", severity="error")
            return
        messages.loading = False
        await messages.show(chat, history)
        self.load_previews(chat)
        if self._read_timer is not None:
            self._read_timer.stop()
            self._read_timer = None
        if self.mark_read_on_open and chat.unread:
            # Wait a moment: arrowing through the chat list shouldn't mark
            # everything read, only a chat you actually stopped on.
            self._read_timer = self.set_timer(READ_DWELL, lambda: self.mark_read_now(chat))

    @work(exclusive=True, group="previews")
    async def load_previews(self, chat: Chat) -> None:
        """Fill previews for a freshly opened chat; superseded when you switch away."""
        await self._fill_previews(chat)

    @work(group="previews-live")
    async def load_new_preview(self, chat: Chat) -> None:
        """A message that just arrived — must not cancel the batch already running."""
        await self._fill_previews(chat)

    async def _fill_previews(self, chat: Chat) -> None:
        if self.media_backend == "off" or not tgx_media.available():
            return
        if not hasattr(self.backend, "thumbnail"):
            return
        messages = self.query_one(MessageList)
        cache = tgx_media.cache_dir()
        pending = [
            row.bubble
            for row in list(reversed(messages.rows))[:24]
            if not row.bubble.has_image
            and not row.bubble.preview_tried
            and row.bubble.msg.media
            and tgx_media.wants_preview(row.bubble.msg.media)
        ]
        if not pending:
            return
        width = max(24, min(56, int(messages.size.width * 0.55)))
        limit = asyncio.Semaphore(4)

        async def fetch(bubble: Bubble) -> None:
            async with limit:
                try:
                    path = await self.backend.thumbnail(chat, bubble.msg.id, cache)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    bubble.preview_tried = True
                    return
            if path is None:
                bubble.preview_tried = True
                return
            if self.current is not chat or not bubble.is_mounted:
                return
            at_bottom = messages.is_vertical_scroll_end
            if not await bubble.attach_image(Path(path), self.media_backend, max_cols=width):
                bubble.preview_tried = True
            elif at_bottom:
                self.call_after_refresh(messages.scroll_end, animate=False)

        await asyncio.gather(*(fetch(bubble) for bubble in pending))

    async def _paint_topics(self, chat: Chat) -> None:
        """A forum group gets a bar of its threads; everything else hides it."""
        tabs = self.query_one("#topics", Tabs)
        tabs.display = bool(chat.forum)
        if not chat.forum:
            self.current_topic = None
            return
        try:
            topics = await self.backend.topics(chat)
        except Exception as exc:
            self.notify(f"темы не прочитались: {exc}", severity="error", timeout=6)
            topics = []
        self._topics = {t.id: t for t in topics}
        with tabs.prevent(Tabs.TabActivated):
            await tabs.clear()          # clearing is async: adding before it lands duplicates ids
            for topic in topics:
                await tabs.add_tab(Tab(topic.label, id=f"t-{topic.id}"))
        self.current_topic = topics[0] if topics else None

    @on(Tabs.TabActivated, "#topics")
    def topic_activated(self, event: Tabs.TabActivated) -> None:
        topic = self._topics.get(int((event.tab.id or "t-0").split("-")[1]))
        if topic is not None and self.current is not None and topic is not self.current_topic:
            self.open_chat_worker(self.current, topic)

    @work(group="todo")
    async def action_checklist(self) -> None:
        """On a checklist message — tick items; otherwise compose a new one."""
        chat = self.current
        if chat is None:
            return
        selected = self.query_one(MessageList).selected_msg()
        if selected is not None and selected.checklist:
            await self.push_screen_wait(ChecklistScreen(self.backend, chat, selected))
            for row in self.query_one(MessageList).rows:
                if row.bubble.msg is selected:
                    row.bubble.refresh_text()
                    break
            return
        draft = await self.push_screen_wait(NewChecklistScreen())
        if not draft:
            return
        try:
            msg = await self.backend.send_checklist(
                chat, draft["title"], draft["items"],
                others_can_append=draft["others_can_append"],
                others_can_complete=draft["others_can_complete"],
                reply_to=self.current_topic.id if self.current_topic else None,
            )
        except Exception as exc:
            self.notify(f"чек-лист не ушёл: {exc}", severity="error", timeout=10)
            return
        self._sent_ids.add(msg.id)
        await self.query_one(MessageList).append(msg, flash=False)

    @work(group="topics")
    async def action_topics(self) -> None:
        chat = self.current
        if chat is None:
            return
        if not chat.forum:
            self.notify("темы бывают только в группах с включёнными темами", timeout=4)
            return
        picked = await self.push_screen_wait(TopicsScreen(self.backend, chat))
        await self._paint_topics(chat)
        if picked is not None:
            self.current_topic = next((t for t in self._topics.values() if t.id == picked.id), picked)
        await self.open_chat(chat, self.current_topic)

    @work(group="pin")
    async def action_pin_message(self) -> None:
        """Pin quietly, or unpin if it is already pinned."""
        msg = self._selected()
        if msg is None or self.current is None:
            return
        unpin = msg.pinned
        try:
            await self.backend.pin(self.current, msg.id, silent=True, unpin=unpin)
        except Exception as exc:
            self.notify(f"не получилось: {exc}", severity="error", timeout=8)
            return
        msg.pinned = not unpin
        for row in self.query_one(MessageList).rows:
            if row.bubble.msg is msg:
                row.bubble.refresh_text()
                break
        self.notify(f"сообщение #{msg.id} {'откреплено' if unpin else 'закреплено (без уведомления)'}",
                    timeout=5)

    def _paint_composer(self) -> None:
        composer = self.query_one(Composer)
        chat = self.current
        if chat is not None and not chat.can_post:
            composer.placeholder = "  комментарий к посту…   (enter — отправить, ctrl+s — файл, c — тред)"
        else:
            composer.placeholder = "  написать сообщение…   (enter — отправить, ctrl+s — файл, ctrl+e — многострочно)"

    def _paint_header(self) -> None:
        header = self.query_one("#chat-header", Static)
        chat = self.current
        if chat is None:
            header.update(Text(""))
            return
        line = Text()
        line.append(f"{chat.glyph} ")
        line.append(chat.name, style=f"bold {pal('foreground')}")
        if chat.username:
            line.append(f"  @{chat.username}", style=pal("primary"))
        bits = [chat.kind]
        if not chat.can_post:
            bits.append("только комментарии")
        if chat.unread:
            bits.append(f"{chat.unread} непрочитанных")
        if chat.muted:
            bits.append("без звука")
        line.append("\n" + " · ".join(bits), style=muted())
        header.update(line)

    # --- live updates ----------------------------------------------------
    def _incoming(self, chat_id: int, msg: Msg) -> None:
        self.call_later(self._handle_incoming, chat_id, msg)

    def _typing(self, chat_id: int, who: str) -> None:
        if self.current and self.current.id == chat_id:
            self.call_later(lambda: self.query_one(TypingBar).show(who))

    async def _handle_incoming(self, chat_id: int, msg: Msg) -> None:
        chats = self.query_one(ChatList)
        chat = next((c for c in chats.chats if c.id == chat_id), None)
        if chat is None:
            self.reload_dialogs()
            return
        chat.date = msg.date or chat.date
        preview = one_line(msg.text or msg.media or "…", 70)
        chat.preview = f"you: {preview}" if msg.out else preview
        if self.current is not None and chat.id == self.current.id:
            self.query_one(TypingBar).hide()
            if msg.id not in self._sent_ids:
                await self.query_one(MessageList).append(msg)
                if msg.media:
                    self.load_new_preview(chat)
            if self.mark_read_on_open:
                await self._mark_read(chat)
        elif not msg.out:
            chat.unread += 1
            if self.notifications and not chat.muted:
                self.notify(preview, title=f"{chat.glyph} {chat.name}", timeout=5)
        chats.rebuild()
        self._paint_header()

    # --- sending ---------------------------------------------------------
    @on(Input.Submitted, "#composer")
    def composer_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.send_text(text)

    @work(group="send")
    async def send_text(self, text: str) -> None:
        chat = self.current
        if chat is None:
            self.notify("сначала выберите чат", severity="warning")
            return
        if self.editing is not None:
            target = self.editing
            self.editing = None
            try:
                updated = await self.backend.edit(chat, target.id, text)
            except Exception as exc:
                self.notify(f"правка не сохранилась: {exc}", severity="error", timeout=8)
                return
            target.text, target.edited = updated.text or text, True
            for row in self.query_one(MessageList).rows:
                if row.bubble.msg is target:
                    row.bubble.refresh_text()
                    break
            self._paint_composer()
            self.notify(f"сообщение #{target.id} изменено", timeout=4)
            return
        if not chat.can_post:
            await self._send_comment(chat, text)
            return
        reply_id = self.reply_to.id if self.reply_to else None
        try:
            msg = await self.backend.send(chat, text, reply_to=reply_id,
                                          topic_id=self.current_topic.id if self.current_topic else None)
        except Exception as exc:
            self.notify(f"не отправлено: {exc}", severity="error", timeout=8)
            return
        self._sent_ids.add(msg.id)
        self.clear_reply()
        await self.query_one(MessageList).append(msg, flash=False)
        chat.preview = f"you: {one_line(text, 70)}"
        chat.date = msg.date
        self.query_one(ChatList).rebuild()

    async def _send_comment(self, chat: Chat, text: str) -> None:
        """In a channel we may not post to, a typed message becomes a comment."""
        post = self._target_post()
        if post is None:
            self.notify("выберите пост, к которому комментировать", timeout=4)
            return
        try:
            await self.backend.send_comment(chat, post.id, text)
        except Exception as exc:
            self.notify(comment_error(exc), severity="error", timeout=10)
            return
        if post.comments is not None:
            post.comments += 1
            for row in self.query_one(MessageList).rows:
                if row.bubble.msg is post:
                    row.bubble.refresh_text()
                    break
        self.clear_reply()
        self.notify(f"комментарий к посту #{post.id} отправлен · c — открыть тред", timeout=6)

    # --- filter / folders ------------------------------------------------
    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        chats = self.query_one(ChatList)
        chats.query_text = event.value
        chats.rebuild()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.set_focus(self.query_one(ChatList))

    @on(Tabs.TabActivated, "#folders")
    def folder_activated(self, event: Tabs.TabActivated) -> None:
        chats = self.query_one(ChatList)
        tab_id = event.tab.id or "f-all"
        chats.folder = None if tab_id == "f-all" else self._folders.get(int(tab_id.split("-")[1]))
        chats.rebuild()

    # --- message actions -------------------------------------------------
    @on(MessageList.LoadOlder)
    def older_requested(self) -> None:
        self.action_load_older()

    @work(exclusive=True, group="older")
    async def action_load_older(self) -> None:
        messages = self.query_one(MessageList)
        if self._loading_older or self.current is None or messages.oldest_id is None:
            return
        self._loading_older = True
        try:
            older = await self.backend.history(self.current, limit=40, before_id=messages.oldest_id)
            await messages.prepend(older)
            if not older:
                self.notify("это начало переписки", timeout=2)
        except Exception as exc:
            self.notify(f"не догрузилось: {exc}", severity="error")
        finally:
            self._loading_older = False

    def action_reply(self) -> None:
        msg = self.query_one(MessageList).selected_msg()
        if msg is None:
            self.notify("выберите сообщение стрелками в переписке", timeout=3)
            return
        self.reply_to = msg
        chip = self.query_one("#reply-chip", Static)
        text = Text("↩ ответ на ", style=muted())
        text.append(one_line(msg.text or msg.media or f"#{msg.id}", 60), style=pal("foreground"))
        text.append("   escape — отменить", style=muted())
        chip.update(text)
        chip.add_class("visible")
        self.set_focus(self.query_one(Composer))

    def clear_reply(self) -> None:
        self.reply_to = None
        chip = self.query_one("#reply-chip", Static)
        chip.remove_class("visible")
        chip.update("")

    def action_copy_message(self) -> None:
        msg = self.query_one(MessageList).selected_msg()
        if msg is None or not (msg.text or msg.media):
            self.notify("нечего копировать", timeout=2)
            return
        self.copy_to_clipboard(msg.text or msg.media)
        self.notify("скопировано в буфер", timeout=2)

    @work(group="download")
    async def action_download(self) -> None:
        messages = self.query_one(MessageList)
        msg = messages.selected_msg()
        if msg is None or self.current is None or not msg.media:
            self.notify("в выбранном сообщении нет вложения", timeout=3)
            return
        dest = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools")) / "data" / "downloads"
        self.notify("качаю…", timeout=2)
        try:
            path = await self.backend.download(self.current, msg.id, dest)
        except Exception as exc:
            self.notify(f"не скачалось: {exc}", severity="error")
            return
        self.notify(f"сохранено: {path}" if path else "нечего скачивать", timeout=6)

    def _target_post(self) -> Msg | None:
        """Which post the comment keys act on: the reply target, the selection, or the last one."""
        messages = self.query_one(MessageList)
        if self.reply_to is not None:
            return self.reply_to
        selected = messages.selected_msg()
        if selected is not None:
            return selected
        posts = [m for m in messages.msgs if not m.service]
        return posts[-1] if posts else None

    def action_comments(self) -> None:
        chat = self.current
        if chat is None:
            return
        post = self._target_post()
        if post is None:
            self.notify("нет поста для комментариев", timeout=3)
            return
        if post.comments is None and chat.can_post:
            self.notify("у этого сообщения нет треда комментариев", timeout=3)
            return

        async def loader(post_id: int) -> list[Msg]:
            return await self.backend.comments(chat, post_id)

        async def sender(post_id: int, text: str) -> Msg:
            return await self.backend.send_comment(chat, post_id, text)

        self.push_screen(CommentsScreen(chat, post, loader, sender))

    def _selected(self) -> Msg | None:
        msg = self.query_one(MessageList).selected_msg()
        if msg is None:
            self.notify("выберите сообщение — стрелками или кликом", timeout=3)
        return msg

    def action_edit_message(self) -> None:
        msg = self._selected()
        if msg is None:
            return
        if not msg.out:
            self.notify("править можно только свои сообщения", timeout=3)
            return
        if not msg.text:
            self.notify("у сообщения нет текста для правки", timeout=3)
            return
        self.editing = msg
        composer = self.query_one(Composer)
        composer.value = msg.text
        composer.placeholder = f"  правка #{msg.id} — enter сохранить, escape отменить"
        self.set_focus(composer)

    def cancel_edit(self) -> None:
        self.editing = None
        self.query_one(Composer).value = ""
        self._paint_composer()

    @work(group="msg")
    async def action_delete_message(self) -> None:
        msg = self._selected()
        if msg is None or self.current is None:
            return
        preview = one_line(msg.text or msg.media or f"#{msg.id}", 70)
        note = "" if msg.out else "это чужое сообщение — удалить получится только с правами администратора"
        if not await self.push_screen_wait(ConfirmScreen(f"Удалить сообщение #{msg.id}?", f"{preview}\n{note}".strip())):
            return
        try:
            await self.backend.delete(self.current, [msg.id])
        except Exception as exc:
            self.notify(f"не удалилось: {exc}", severity="error", timeout=8)
            return
        self.query_one(MessageList).remove_message(msg.id)
        self.notify(f"сообщение #{msg.id} удалено", timeout=4)

    @work(group="msg")
    async def action_forward(self) -> None:
        msg = self._selected()
        if msg is None or self.current is None:
            return
        chats = [c for c in self.query_one(ChatList).chats if c.can_post]
        target = await self.push_screen_wait(ChatPickScreen(chats, f"переслать #{msg.id} в…"))
        if target is None:
            return
        try:
            await self.backend.forward(self.current, [msg.id], target)
        except Exception as exc:
            self.notify(f"не переслалось: {exc}", severity="error", timeout=8)
            return
        self.notify(f"переслано в «{target.name}»", timeout=5)

    @work(group="msg")
    async def action_react(self) -> None:
        msg = self._selected()
        if msg is None or self.current is None:
            return
        emoji = await self.push_screen_wait(ReactionScreen())
        if emoji is None:
            return
        await self._apply_reaction(msg, emoji or None)

    @work(group="msg")
    async def action_transcribe(self) -> None:
        """Голосовое → текст. Без Premium Telegram отдаёт несколько штук в неделю."""
        msg = self._selected()
        if msg is None or self.current is None:
            return
        self.notify("расшифровываю…", timeout=3)
        try:
            result = await self.backend.transcribe(self.current, msg.id)
        except Exception as exc:
            self.notify(f"не вышло: {exc}", severity="error", timeout=8)
            return
        text = (result.get("text") or "").strip()
        if not text:
            self.notify("Telegram не разобрал эту запись", severity="warning", timeout=6)
            return
        if result.get("pending"):
            text += " …"
        msg.transcript = text
        for row in self.query_one(MessageList).rows:
            if row.bubble.msg is msg:
                row.bubble.refresh_text()
                break
        left = result.get("free_left")
        self.notify(f"расшифровано{f' · бесплатных осталось {left}' if left is not None else ''}",
                    timeout=5)

    @work(group="msg")
    async def action_unreact(self) -> None:
        msg = self._selected()
        if msg is not None and self.current is not None:
            await self._apply_reaction(msg, None)

    async def _apply_reaction(self, msg: Msg, emoji: str | None) -> None:
        chat = self.current
        if chat is None:
            return
        try:
            await self.backend.react(chat, msg.id, emoji)
        except Exception as exc:
            self.notify(f"реакция не поставилась: {exc}", severity="error", timeout=8)
            return
        others = [r for r in msg.reactions if not r[2]]
        msg.reactions = tuple(others + ([(emoji, 1, True)] if emoji else []))
        for row in self.query_one(MessageList).rows:
            if row.bubble.msg is msg:
                row.bubble.refresh_text()
                break

    @work(group="msg")
    async def action_press_button(self) -> None:
        msg = self._selected()
        if msg is None or self.current is None:
            return
        if not msg.buttons:
            self.notify("в этом сообщении нет кнопок", timeout=3)
            return
        position = await self.push_screen_wait(ButtonScreen(msg.buttons))
        if position is None:
            return
        try:
            answer = await self.backend.press_button(self.current, msg.id, *position)
        except Exception as exc:
            self.notify(f"кнопка не нажалась: {exc}", severity="error", timeout=8)
            return
        self.notify(answer, timeout=6)

    def action_toggle_spoilers(self) -> None:
        messages = self.query_one(MessageList)
        if messages.selected is None or messages.selected >= len(messages.rows):
            self.notify("выберите сообщение со спойлером", timeout=3)
            return
        bubble = messages.rows[messages.selected].bubble
        bubble.spoilers_shown = not bubble.spoilers_shown
        bubble.refresh_text()

    def _selected_media(self) -> Msg | None:
        msg = self.query_one(MessageList).selected_msg()
        if msg is None:
            self.notify("выберите сообщение стрелками в переписке", timeout=3)
            return None
        if not msg.media:
            self.notify("в этом сообщении нет вложения", timeout=3)
            return None
        return msg

    @work(group="media", exclusive=True)
    async def action_view_media(self) -> None:
        """Open the picture full-pane — much more detail than the inline preview."""
        msg = self._selected_media()
        if msg is None or self.current is None:
            return
        if not tgx_media.wants_preview(msg.media):
            self.notify(f"{msg.media} — не картинка, нажмите o чтобы открыть в системе", timeout=5)
            return
        self.notify("загружаю картинку…", timeout=2)
        try:
            path = await self.backend.thumbnail(self.current, msg.id, tgx_media.cache_dir(), full=True)
        except Exception as exc:
            self.notify(f"не загрузилось: {exc}", severity="error")
            return
        if path is None:
            self.notify("картинка недоступна", timeout=3)
            return
        note = "это кадр из видео, терминал его не проигрывает" if is_video(msg.media) else ""
        self.push_screen(MediaScreen(Path(path), f"{msg.sender or ''} · {msg.text or msg.media}", self.media_backend, note))

    @work(group="open")
    async def action_open_media(self) -> None:
        """Download the original and hand it to the desktop viewer."""
        msg = self._selected_media()
        if msg is None or self.current is None:
            return
        bar = self.query_one(TypingBar)
        label = msg.media or "вложение"
        bar.status(f"качаю {label}…")
        shown = {"pct": -5}

        def progress(received: int, total: int) -> None:
            pct = int(received * 100 / total) if total else 0
            if pct >= shown["pct"] + 5:
                shown["pct"] = pct
                bar.status(f"качаю {label} — {pct}%  ({received // 1024} КБ)")

        downloads = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools")) / "data" / "downloads"
        try:
            path = await self.backend.download(self.current, msg.id, downloads, progress)
        except Exception as exc:
            bar.hide()
            self.notify(f"не скачалось: {exc}", severity="error", timeout=10)
            return
        if not path:
            bar.hide()
            self.notify("нечего открывать", timeout=3)
            return
        bar.status(f"открываю {Path(path).name}")
        error = await asyncio.to_thread(tgx_media.open_external, Path(path))
        bar.hide()
        if error:
            self.notify(f"не открылось: {error}", severity="error", timeout=10)
        else:
            self.notify(f"открыл {Path(path).name}", timeout=4)

    @work(group="read")
    async def mark_read_now(self, chat: Chat) -> None:
        if self.current is chat and chat.unread:
            await self._mark_read(chat)

    @work(group="read")
    async def action_mark_read(self) -> None:
        if self.current is not None:
            await self._mark_read(self.current)

    async def _mark_read(self, chat: Chat) -> None:
        try:
            await self.backend.mark_read(chat)
        except Exception:
            return
        chat.unread = 0
        self.query_one(ChatList).refresh_chat(chat)
        self._paint_header()

    # --- modals ----------------------------------------------------------
    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    @work
    async def action_search_chat(self) -> None:
        chat = self.current

        async def searcher(query: str, everywhere: bool, kind: str | None, from_user: str | None,
                           since: datetime | None, until: datetime | None) -> list[tuple[Any, Msg]]:
            return await self.backend.search(None if everywhere else chat, query, limit=60, kind=kind,
                                             from_user=from_user, since=since, until=until)

        picked = await self.push_screen_wait(
            SearchScreen(searcher, chat.name if chat else "все чаты", in_chat=chat is not None)
        )
        if not picked:
            return
        found_chat, msg = picked
        messages = self.query_one(MessageList)
        if found_chat is not None and (self.current is None or found_chat.id != self.current.id):
            await self.open_chat(found_chat)
        if not messages.focus_message(msg.id):
            self.notify(f"сообщение #{msg.id} выше — ctrl+u догрузит историю", timeout=5)

    @work(group="manage")
    async def action_manage_chat(self) -> None:
        """Open the management panel for the current channel or group."""
        chat = self.current
        if chat is None:
            return
        if chat.kind in {"user", "bot"}:
            self.notify("управление есть у каналов и групп", timeout=3)
            return
        await self.push_screen_wait(ManageScreen(self.backend, chat, self.query_one(ChatList).chats))
        self.query_one(ChatList).refresh_chat(chat)
        self._paint_header()

    @work(group="manage")
    async def action_new_chat(self) -> None:
        """Create a channel, a group, or a group with topics."""
        draft = await self.push_screen_wait(NewChatScreen())
        if not draft:
            return
        bar = self.query_one(TypingBar)
        bar.status("создаю…")
        try:
            created = await self.backend.create_chat(
                draft["title"], kind=draft["kind"], about=draft["about"], username=draft["username"]
            )
        except Exception as exc:
            bar.hide()
            self.notify(f"не создалось: {exc}", severity="error", timeout=10)
            return
        bar.hide()
        chats = self.query_one(ChatList)
        chats.set_chats([created] + chats.chats)
        self._paint_counts()
        await self.open_chat(created)
        self.notify(f"создан «{created.name}» — i откроет управление", timeout=6)

    @work(group="post")
    async def action_post(self) -> None:
        """Compose a formatted post with a live preview of how Telegram will show it."""
        chat = self.current
        if chat is None:
            self.notify("сначала выберите чат", severity="warning", timeout=3)
            return
        composer = self.query_one(Composer)
        bots = self.backend.list_bots() if hasattr(self.backend, "list_bots") else []
        draft = await self.push_screen_wait(PostScreen(chat.name, composer.value, bots))
        if not draft:
            return
        composer.value = ""
        bar = self.query_one(TypingBar)
        bar.status("публикую…")
        comment_id = None
        if not chat.can_post:
            post = self._target_post()
            if post is None:
                bar.hide()
                self.notify("выберите пост для комментария", timeout=4)
                return
            comment_id = post.id
        shared = {
            "parse_mode": draft["parse_mode"],
            "link_preview": draft["link_preview"],
            "silent": draft["silent"],
            "schedule": draft["schedule"],
            "files": draft["files"] or None,
        }
        try:
            if draft.get("rich"):
                # Bot API 10.1: заголовки, таблицы, чек-листы, сноски
                msg = await self.backend.publish_rich(
                    draft["as_bot"] or "", chat, draft["text"],
                    buttons=draft["buttons"], silent=draft["silent"],
                )
                bar.hide()
                self.clear_reply()
                self._sent_ids.add(msg.id)
                await self.query_one(MessageList).append(msg, flash=False)
                self.notify(f"богатое сообщение отправлено (#{msg.id})", timeout=6)
                return
            if draft["as_bot"]:
                # inline buttons only exist on bot messages, so this path goes
                # out through the bot's own token rather than the user account
                msg = await self.backend.publish_as(
                    draft["as_bot"], chat, draft["text"], buttons=draft["buttons"], **shared
                )
            else:
                msg = await self.backend.publish(
                    chat,
                    draft["text"],
                    reply_to=(self.reply_to.id if self.reply_to else None)
                    or (self.current_topic.id if self.current_topic and not comment_id else None),
                    comment_to=comment_id,
                    progress=lambda done, total: bar.status(
                        f"загружаю… {int(done * 100 / total) if total else 0}%"
                    ),
                    **shared,
                )
        except Exception as exc:
            bar.hide()
            self.notify(f"пост не ушёл: {exc}", severity="error", timeout=10)
            return
        bar.hide()
        self.clear_reply()
        if draft["schedule"] is not None:
            self.notify(f"пост запланирован на {draft['schedule']:%d.%m %H:%M}", timeout=6)
            return
        if comment_id is not None:
            self.notify(f"пост ушёл комментарием к #{comment_id}", timeout=5)
            return
        self._sent_ids.add(msg.id)
        await self.query_one(MessageList).append(msg, flash=False)

    @work(group="attach")
    async def action_attach(self) -> None:
        """Attach anything: photo, video, voice, round video, document or an album."""
        chat = self.current
        if chat is None:
            self.notify("сначала выберите чат", severity="warning", timeout=3)
            return
        composer = self.query_one(Composer)
        picked = await self.push_screen_wait(AttachScreen(composer.value))
        if not picked:
            return
        composer.value = ""
        bar = self.query_one(TypingBar)
        names = ", ".join(Path(p).name for p in picked["paths"])
        shown = {"pct": -5}

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct >= shown["pct"] + 5:
                shown["pct"] = pct
                bar.status(f"отправляю {names} — {pct}%")

        reply_id = comment_id = None
        if not chat.can_post:
            post = self._target_post()
            if post is None:
                self.notify("выберите пост для комментария", timeout=4)
                return
            comment_id = post.id
        elif self.reply_to is not None:
            reply_id = self.reply_to.id

        bar.status(f"отправляю {names}…")
        try:
            msg = await self.backend.send_file(
                chat,
                picked["paths"],
                caption=picked["caption"],
                reply_to=reply_id,
                comment_to=comment_id,
                as_document=picked["document"],
                voice=picked["voice"],
                video_note=picked["note"],
                silent=picked["silent"],
                progress=progress,
            )
        except Exception as exc:
            bar.hide()
            self.notify(f"не отправилось: {exc}", severity="error", timeout=10)
            return
        bar.hide()
        self.clear_reply()
        if comment_id is not None:
            self.notify(f"файл отправлен комментарием к #{comment_id} · c — открыть тред", timeout=6)
            return
        self._sent_ids.add(msg.id)
        await self.query_one(MessageList).append(msg, flash=False)
        chat.preview = f"you: {one_line(picked['caption'] or msg.media, 70)}"
        chat.date = msg.date
        self.query_one(ChatList).rebuild()

    @work
    async def action_compose_long(self) -> None:
        text = await self.push_screen_wait(ComposeScreen(self.query_one(Composer).value))
        if text:
            self.query_one(Composer).value = ""
            self.send_text(text)

    # --- focus / chrome --------------------------------------------------
    def action_focus_filter(self) -> None:
        self.set_focus(self.query_one("#filter", Input))

    def action_focus_chats(self) -> None:
        self.set_focus(self.query_one(ChatList))

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.toggle_class("collapsed")
        if sidebar.has_class("collapsed"):
            self.set_focus(self.query_one(Composer))

    def action_next_unread(self) -> None:
        chats = self.query_one(ChatList)
        index = chats.next_unread()
        if index is None:
            self.notify("непрочитанных нет", timeout=2)
            return
        chats.highlighted = index
        self.set_focus(chats)

    def action_reload(self) -> None:
        self.notify("обновляю список чатов…", timeout=2)
        self.reload_dialogs()

    def action_escape(self) -> None:
        messages = self.query_one(MessageList)
        if self.editing is not None:
            self.cancel_edit()
            return
        if self.reply_to is not None:
            self.clear_reply()
            return
        if messages.selected is not None:
            previous = messages.selected
            messages.selected = None
            messages._paint_selection(previous)
            return
        self.set_focus(self.query_one(Composer))

    def action_shot(self) -> None:
        path = self.save_screenshot()
        self.notify(f"скриншот: {path}", timeout=6)

    async def action_quit(self) -> None:
        try:
            await self.backend.close()
        except Exception:
            pass
        self.exit()


def build_app(
    session: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    demo: bool = False,
    theme: str = "tgx-night",
    mark_read: bool = True,
    notifications: bool = True,
    dialog_limit: int = 0,
    media: str = "auto",
    media_detected: str = "",
) -> TgxApp:
    backend: Any = DemoBackend() if demo else TelegramBackend(session, int(api_id or 0), str(api_hash or ""))
    return TgxApp(
        backend,
        theme_name=theme,
        mark_read=mark_read,
        notifications=notifications,
        dialog_limit=dialog_limit,
        media=media,
        media_detected=media_detected,
    )


async def run_async(*args: Any, **kwargs: Any) -> None:
    """Entry point used by `tgx ui`, which already owns an event loop."""
    await build_app(*args, **kwargs).run_async()


def run(*args: Any, **kwargs: Any) -> None:
    asyncio.run(run_async(*args, **kwargs))


if __name__ == "__main__":  # `python bin/tgx_tui.py --demo` for a quick look
    run(Path.home() / "telegram-cli-tools" / "data" / "tgx.session", demo="--demo" in sys.argv)
