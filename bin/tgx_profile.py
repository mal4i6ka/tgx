#!/usr/bin/env python3
"""Оформление профиля: аватары во всех форматах, цвета, статус, день рождения.

Telegram давно перерос «аватар — это jpeg». Одним и тем же полем
`photos.uploadProfilePhoto` ставятся четыре разные вещи: картинка, короткое
видео с выбранным кадром обложки, эмодзи на градиенте и стикер на градиенте.
Тот же набор принимают канал, группа, бот и даже фотография, которую вы
ставите другому человеку в своей адресной книге, — поэтому разбор источника
здесь один на всех, а `avatar()` возвращает готовые поля запроса.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

AVATAR_SYNTAX = """фото.jpg | фото.png       статичный аватар
видео.mp4 | видео.mov     видеоаватар; --start СЕК выбирает кадр обложки
emoji:5384541907051357217 эмодзи на градиенте; --colors подбирает фон
sticker:набор:123         стикер из набора на градиенте
--square                  обрезать по центру в квадрат перед отправкой
--trim СЕК                укоротить видео (Telegram берёт короткие ролики)"""

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".gif", ".mkv", ".avi"}

# Telegram's own hint for profile videos; longer clips come back rejected.
VIDEO_SECONDS = 10.0

STATUS_KINDS = {"profile": "GetDefaultProfilePhotoEmojisRequest",
                "group": "GetDefaultGroupPhotoEmojisRequest",
                "status": "GetDefaultEmojiStatusesRequest",
                "background": "GetDefaultBackgroundEmojisRequest"}


class ProfileError(RuntimeError):
    """Источник аватара или оформление, которое не удалось понять или поставить."""


@dataclass
class Avatar:
    """Разобранный источник: что именно и откуда брать."""
    kind: str                      # image | video | emoji | sticker
    path: Path | None = None
    emoji_id: int | None = None
    stickerset: str | None = None
    sticker_id: int | None = None
    colors: list[int] | None = None
    start: float | None = None


# ── разбор того, что ввёл человек ────────────────────────────────────────────
def parse_colors(spec: str | None) -> list[int] | None:
    """`#e8a4ff,ff00aa` → цвета градиента. Telegram берёт от одного до четырёх."""
    if not spec:
        return None
    parts = [p.strip().lstrip("#") for p in spec.split(",") if p.strip()]
    if not 1 <= len(parts) <= 4:
        raise ProfileError(f"цветов должно быть от 1 до 4, а не {len(parts)}")
    out = []
    for part in parts:
        if len(part) == 3:                      # #e8a → #ee88aa
            part = "".join(c * 2 for c in part)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", part):
            raise ProfileError(f"«{part}» не похоже на цвет; нужен HEX вида #e8a4ff")
        out.append(int(part, 16))
    return out


def parse_birthday(spec: str) -> tuple[int, int, int | None]:
    """`14.03`, `14.03.1990` или `1990-03-14` → день, месяц, год."""
    text = (spec or "").strip()
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    else:
        dotted = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?", text)
        if not dotted:
            raise ProfileError(f"дату «{spec}» не разобрать; нужно 14.03, 14.03.1990 или 1990-03-14")
        day, month = int(dotted.group(1)), int(dotted.group(2))
        year = int(dotted.group(3)) if dotted.group(3) else None
    if not 1 <= month <= 12:
        raise ProfileError(f"месяца {month} не существует")
    if not 1 <= day <= 31:
        raise ProfileError(f"дня {day} не существует")
    if year is not None and not 1900 <= year <= 2100:
        raise ProfileError(f"год {year} выглядит опечаткой")
    return day, month, year


def parse_avatar(source: str, *, colors: str | None = None, start: float | None = None) -> Avatar:
    """Один аргумент на все четыре формата аватара."""
    text = (source or "").strip()
    if not text:
        raise ProfileError("не указан источник аватара; см. tgx profile formats")

    if text.startswith("emoji:"):
        raw = text[6:].strip()
        if not raw.isdigit():
            raise ProfileError(f"нужен числовой id эмодзи, а не «{raw}»; "
                               f"список — tgx profile emojis")
        return Avatar("emoji", emoji_id=int(raw), colors=parse_colors(colors))

    if text.startswith("sticker:"):
        raw = text[8:].strip()
        if ":" not in raw:
            raise ProfileError("формат — sticker:короткое_имя_набора:id_стикера")
        short, _, sticker = raw.rpartition(":")
        if not sticker.isdigit():
            raise ProfileError(f"id стикера должен быть числом, а не «{sticker}»")
        return Avatar("sticker", stickerset=short, sticker_id=int(sticker),
                      colors=parse_colors(colors))

    path = Path(text).expanduser()
    if not path.exists():
        raise ProfileError(f"файла {path} нет")
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return Avatar("video", path=path, start=start)
    if suffix in IMAGE_SUFFIXES:
        if start is not None:
            raise ProfileError("--start имеет смысл только для видеоаватара")
        return Avatar("image", path=path)
    raise ProfileError(f"формат {suffix or 'без расширения'} не годится в аватары; "
                       f"картинки — {', '.join(sorted(IMAGE_SUFFIXES))}; "
                       f"видео — {', '.join(sorted(VIDEO_SUFFIXES))}")


# ── подготовка файла ─────────────────────────────────────────────────────────
def dimensions(path: Path) -> tuple[int, int, float] | None:
    """Ширина, высота и длительность — чтобы предупредить до отправки, а не после."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "csv=p=0:s=,", str(path)],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    numbers = [p for p in re.split(r"[,\n]", out.stdout) if p.strip()]
    try:
        width, height = int(numbers[0]), int(numbers[1])
        duration = float(numbers[2]) if len(numbers) > 2 else 0.0
    except (IndexError, ValueError):
        return None
    return width, height, duration


def normalise(path: Path, out_dir: Path, *, square: bool = False,
              trim: float | None = None) -> tuple[Path, list[str]]:
    """Обрезать по центру и/или укоротить. Возвращает файл и что было сделано."""
    size = dimensions(path)
    notes: list[str] = []
    video = path.suffix.lower() in VIDEO_SUFFIXES
    if size and video and size[2] > VIDEO_SECONDS and trim is None:
        notes.append(f"ролик длиной {size[2]:.0f}с — Telegram может отказать; "
                     f"попробуйте --trim {int(VIDEO_SECONDS)}")
    if size and not square and size[0] != size[1]:
        notes.append(f"кадр {size[0]}×{size[1]} не квадратный — Telegram обрежет "
                     f"по центру сам; --square сделает это предсказуемо")
    if not square and trim is None:
        return path, notes

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"avatar-{path.stem}{path.suffix or '.mp4'}"
    command = ["ffmpeg", "-y", "-i", str(path)]
    if trim is not None:
        command += ["-t", str(trim)]
        notes.append(f"укорочено до {trim:g}с")
    if square:
        command += ["-vf", "crop='min(iw,ih)':'min(iw,ih)'"]
        notes.append("обрезано по центру в квадрат")
    command.append(str(target))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise ProfileError("для --square и --trim нужен ffmpeg; поставьте его "
                           "(brew install ffmpeg) или уберите эти ключи") from exc
    if result.returncode != 0 or not target.exists():
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["ffmpeg молча не справился"]
        raise ProfileError(f"не удалось подготовить файл: {tail[0]}")
    return target, notes


# ── постановка ───────────────────────────────────────────────────────────────
class Appearance:
    """Оформление: аватары, цвета, статус, день рождения, канал в профиле."""

    def __init__(self, client: Any, cache: Path | None = None) -> None:
        self.client = client
        self.cache = cache or Path.home() / ".cache" / "tgx" / "avatars"

    @staticmethod
    def _title(peer: Any) -> str:
        """Имя для отчёта: репр Telethon-сущности читать невозможно."""
        for field in ("title", "username", "first_name"):
            value = getattr(peer, field, None)
            if value:
                return f"@{value}" if field == "username" else str(value)
        return str(getattr(peer, "id", peer))

    async def _markup(self, avatar: Avatar) -> Any:
        from telethon.tl import types

        colors = avatar.colors or [0x6E7F8F]
        if avatar.kind == "emoji":
            return types.VideoSizeEmojiMarkup(emoji_id=avatar.emoji_id, background_colors=colors)
        stickerset = types.InputStickerSetShortName(short_name=avatar.stickerset)
        return types.VideoSizeStickerMarkup(stickerset=stickerset, sticker_id=avatar.sticker_id,
                                            background_colors=colors)

    async def upload(self, avatar: Avatar, *, square: bool = False,
                     trim: float | None = None) -> tuple[dict[str, Any], list[str]]:
        """Разобранный источник → поля запроса, одинаковые для всех получателей."""
        if avatar.kind in {"emoji", "sticker"}:
            return {"video_emoji_markup": await self._markup(avatar)}, []

        path, notes = normalise(avatar.path, self.cache, square=square, trim=trim)
        uploaded = await self.client.upload_file(str(path))
        if avatar.kind == "video":
            fields: dict[str, Any] = {"video": uploaded}
            if avatar.start is not None:
                fields["video_start_ts"] = float(avatar.start)
            return fields, notes
        return {"file": uploaded}, notes

    async def set_photo(self, avatar: Avatar, *, fallback: bool = False, bot: str | None = None,
                        square: bool = False, trim: float | None = None) -> dict[str, Any]:
        """Свой аватар, публичный запасной или аватар своего бота."""
        from telethon.tl import functions

        fields, notes = await self.upload(avatar, square=square, trim=trim)
        target = await self.client.get_input_entity(bot) if bot else None
        await self.client(functions.photos.UploadProfilePhotoRequest(
            fallback=fallback or None, bot=target, **fields))
        return {"kind": avatar.kind, "where": bot or ("запасной аватар" if fallback else "профиль"),
                "notes": notes}

    async def photos(self, limit: int = 20) -> list[dict[str, Any]]:
        from telethon.tl import functions, types

        me = await self.client.get_me()
        result = await self.client(functions.photos.GetUserPhotosRequest(
            user_id=me, offset=0, max_id=0, limit=limit))
        rows = []
        for photo in result.photos:
            video = [s for s in (getattr(photo, "video_sizes", None) or [])]
            rows.append({
                "id": photo.id,
                "date": getattr(photo, "date", None),
                "kind": "эмодзи/стикер" if any(
                    isinstance(s, (types.VideoSizeEmojiMarkup, types.VideoSizeStickerMarkup))
                    for s in video) else ("видео" if video else "фото"),
            })
        return rows

    async def delete_photos(self, ids: Sequence[int]) -> int:
        """Убрать аватары по id из `tgx profile photos`."""
        from telethon.tl import functions

        me = await self.client.get_me()
        result = await self.client(functions.photos.GetUserPhotosRequest(
            user_id=me, offset=0, max_id=0, limit=100))
        wanted = {int(i) for i in ids}
        chosen = [p for p in result.photos if p.id in wanted]
        missing = wanted - {p.id for p in chosen}
        if missing:
            raise ProfileError(f"среди ваших аватаров нет {', '.join(str(m) for m in sorted(missing))}")
        await self.client(functions.photos.DeletePhotosRequest(
            id=[self._input_photo(p) for p in chosen]))
        return len(chosen)

    @staticmethod
    def _input_photo(photo: Any) -> Any:
        from telethon.tl import types
        return types.InputPhoto(id=photo.id, access_hash=photo.access_hash,
                                file_reference=photo.file_reference)

    async def set_chat_photo(self, chat: Any, avatar: Avatar, *, square: bool = False,
                             trim: float | None = None) -> dict[str, Any]:
        """Аватар канала или группы — те же четыре формата."""
        from telethon.tl import functions, types

        fields, notes = await self.upload(avatar, square=square, trim=trim)
        entity = await self.client.get_input_entity(chat)
        photo = types.InputChatUploadedPhoto(**fields)
        try:
            await self.client(functions.channels.EditPhotoRequest(channel=entity, photo=photo))
        except (TypeError, ValueError, AttributeError):
            await self.client(functions.messages.EditChatPhotoRequest(
                chat_id=getattr(entity, "chat_id", entity), photo=photo))
        return {"kind": avatar.kind, "where": self._title(chat), "notes": notes}

    async def set_contact_photo(self, user: Any, avatar: Avatar | None, *, suggest: bool = False,
                                square: bool = False, trim: float | None = None) -> dict[str, Any]:
        """Фотография, которую вы ставите человеку у себя — или предлагаете ему."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        if avatar is None:
            await self.client(functions.photos.UploadContactProfilePhotoRequest(
                user_id=entity, save=True))
            return {"cleared": self._title(user)}
        fields, notes = await self.upload(avatar, square=square, trim=trim)
        await self.client(functions.photos.UploadContactProfilePhotoRequest(
            user_id=entity, suggest=suggest or None, save=None if suggest else True, **fields))
        return {"kind": avatar.kind, "where": self._title(user),
                "mode": "предложено" if suggest else "поставлено у себя", "notes": notes}

    async def set_color(self, color: int | None = None, emoji_id: int | None = None, *,
                        for_profile: bool = False, chat: Any = None) -> dict[str, Any]:
        """Цвет имени и узор фона — у себя или у канала."""
        from telethon.tl import functions, types

        if chat is not None:
            entity = await self.client.get_input_entity(chat)
            await self.client(functions.channels.UpdateColorRequest(
                channel=entity, for_profile=for_profile or None, color=color,
                background_emoji_id=emoji_id))
        else:
            peer_color = types.PeerColor(color=color, background_emoji_id=emoji_id)
            await self.client(functions.account.UpdateColorRequest(
                for_profile=for_profile or None, color=peer_color))
        return {"where": self._title(chat) if chat is not None else "профиль",
                "part": "профиль" if for_profile else "имя",
                "color": color, "background_emoji_id": emoji_id}

    async def set_status(self, emoji_id: int | None, until: int | None = None, *,
                         chat: Any = None) -> dict[str, Any]:
        """Эмодзи-статус рядом с именем; `None` снимает его."""
        from telethon.tl import functions, types

        status = types.EmojiStatusEmpty() if emoji_id is None \
            else types.EmojiStatus(document_id=emoji_id, until=until)
        if chat is not None:
            entity = await self.client.get_input_entity(chat)
            await self.client(functions.channels.UpdateEmojiStatusRequest(
                channel=entity, emoji_status=status))
        else:
            await self.client(functions.account.UpdateEmojiStatusRequest(emoji_status=status))
        return {"where": self._title(chat) if chat is not None else "профиль",
                "emoji_id": emoji_id, "until": until}

    async def set_birthday(self, spec: str | None) -> dict[str, Any]:
        from telethon.tl import functions, types

        if spec is None:
            await self.client(functions.account.UpdateBirthdayRequest(birthday=None))
            return {"birthday": None}
        day, month, year = parse_birthday(spec)
        await self.client(functions.account.UpdateBirthdayRequest(
            birthday=types.Birthday(day=day, month=month, year=year)))
        return {"birthday": f"{day:02d}.{month:02d}" + (f".{year}" if year else "")}

    async def set_personal_channel(self, chat: Any | None) -> dict[str, Any]:
        """Канал, который показывается прямо в профиле."""
        from telethon.tl import functions, types

        entity = types.InputChannelEmpty() if chat is None \
            else await self.client.get_input_entity(chat)
        await self.client(functions.account.UpdatePersonalChannelRequest(channel=entity))
        return {"personal_channel": self._title(chat) if chat is not None else None}

    async def suggested(self, kind: str = "profile", limit: int = 40) -> list[dict[str, Any]]:
        """Готовые эмодзи, которые Telegram предлагает для аватара или статуса."""
        from telethon.tl import functions

        if kind not in STATUS_KINDS:
            raise ProfileError(f"вид «{kind}» неизвестен; есть: {', '.join(STATUS_KINDS)}")
        request = getattr(functions.account, STATUS_KINDS[kind])
        result = await self.client(request(hash=0))
        # EmojiList carries bare ids; EmojiStatuses wraps them in status objects.
        ids = list(getattr(result, "document_id", None) or [])
        if not ids:
            ids = [s.document_id for s in (getattr(result, "statuses", None) or [])
                   if getattr(s, "document_id", None)]
        ids = ids[:limit]
        return [{"emoji_id": i, "emoji": alt} for i, alt in zip(ids, await self.emoji_chars(ids))]

    async def emoji_chars(self, ids: Sequence[int]) -> list[str]:
        """id премиум-эмодзи → сам символ, чтобы список читался глазами."""
        from telethon.tl import functions, types

        if not ids:
            return []
        try:
            documents = await self.client(
                functions.messages.GetCustomEmojiDocumentsRequest(document_id=list(ids)))
        except Exception:
            return ["" for _ in ids]
        alt = {}
        for doc in documents:
            for attribute in getattr(doc, "attributes", None) or []:
                if isinstance(attribute, types.DocumentAttributeCustomEmoji):
                    alt[doc.id] = attribute.alt
        return [alt.get(i, "") for i in ids]
