#!/usr/bin/env python3
"""Свои наборы стикеров: создание, правка, порядок, обложка.

Набор принадлежит человеку, но создаётся и правится **ботом** — Telegram требует
указать владельца, а вызывает метод бот. Отсюда главное неудобство, которое
здесь спрятано: нужен токен бота, а не только своя сессия.

Стикер адресуется своим документом, а не номером в наборе: номер меняется при
каждой перестановке, документ — нет. Поэтому команды принимают либо позицию в
наборе, либо файл, и позиция переводится в документ прямо перед вызовом.

Короткое имя набора уникально на весь Telegram и после создания не меняется —
`suggest` подбирает свободное, `check` проверяет занятость.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

KINDS = ("static", "animated", "video")
MIME = {"static": "image/png", "animated": "application/x-tgsticker", "video": "video/webm"}
SUFFIX = {".png": "static", ".webp": "static", ".tgs": "animated", ".webm": "video"}


class StickerError(RuntimeError):
    """Действие с набором стикеров, которое не удалось выполнить."""


def kind_of(path: Path) -> str:
    """Тип стикера по расширению — от него зависит mime, который ждёт сервер."""
    kind = SUFFIX.get(path.suffix.lower())
    if kind is None:
        raise StickerError(f"формат {path.suffix or 'без расширения'} не годится в стикеры; "
                           f"подходят {', '.join(sorted(SUFFIX))}")
    return kind


def set_ref(name: str) -> Any:
    """Набор по короткому имени, ссылке t.me/addstickers/… или паре «id:hash».

    Пара нужна потому, что часть наборов приходит без короткого имени — у них
    его попросту нет, и сослаться на такой набор можно только числами.
    """
    from telethon.tl import types

    text = name.strip()
    if ":" in text and all(p.lstrip("-").isdigit() for p in text.split(":", 1)):
        ident, access = text.split(":", 1)
        return types.InputStickerSetID(id=int(ident), access_hash=int(access))
    short = text.rstrip("/").split("/")[-1]
    return types.InputStickerSetShortName(short_name=short)


class Stickers:
    """Наборы стикеров: чтение своей сессией, правка — токеном бота."""

    def __init__(self, client: Any, bot: Any = None) -> None:
        self.client = client
        self.bot = bot            # клиент, вошедший по токену бота

    def _editor(self) -> Any:
        if self.bot is None:
            raise StickerError("наборы стикеров правит бот — добавьте --as @бот. "
                               "Владельцем набора при этом останетесь вы")
        return self.bot

    async def show(self, name: str) -> dict[str, Any]:
        """Что внутри набора."""
        from telethon.tl import functions

        try:
            result = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=set_ref(name), hash=0))
        except Exception as exc:
            raise self._explain(exc) from exc
        info = getattr(result, "set", None)
        return {
            "название": getattr(info, "title", None),
            "короткое имя": getattr(info, "short_name", None),
            "стикеров": getattr(info, "count", None),
            "маски": bool(getattr(info, "masks", False)),
            "эмодзи": bool(getattr(info, "emojis", False)),
            "видео": bool(getattr(info, "videos", False)),
            "официальный": bool(getattr(info, "official", False)),
        }

    async def check_name(self, short_name: str) -> dict[str, Any]:
        from telethon.tl import functions

        free = await self._editor()(functions.stickers.CheckShortNameRequest(
            short_name=short_name))
        return {"короткое имя": short_name, "свободно": bool(free)}

    async def suggest_name(self, title: str) -> dict[str, Any]:
        from telethon.tl import functions

        result = await self._editor()(functions.stickers.SuggestShortNameRequest(title=title))
        return {"предложено": getattr(result, "short_name", None)}

    async def create(self, owner: Any, title: str, short_name: str,
                     stickers: Sequence[tuple[str, str]], *, masks: bool = False,
                     emojis: bool = False) -> dict[str, Any]:
        """Создать набор. `stickers` — пары «путь, эмодзи»."""
        from telethon.tl import functions, types

        bot = self._editor()
        if not stickers:
            raise StickerError("в наборе должен быть хотя бы один стикер")
        items = []
        for path_text, emoji in stickers:
            path = Path(path_text).expanduser()
            if not path.is_file():
                raise StickerError(f"файла {path} нет")
            uploaded = await bot.upload_file(str(path))
            items.append(types.InputStickerSetItem(
                document=types.InputMediaUploadedDocument(
                    file=uploaded, mime_type=MIME[kind_of(path)],
                    attributes=[types.DocumentAttributeFilename(file_name=path.name)]),
                emoji=emoji or "🙂"))
        try:
            result = await bot(functions.stickers.CreateStickerSetRequest(
                user_id=await bot.get_input_entity(owner), title=title.strip(),
                short_name=short_name, stickers=items,
                masks=masks or None, emojis=emojis or None))
        except Exception as exc:
            raise self._explain(exc) from exc
        info = getattr(result, "set", None)
        return {"создан": getattr(info, "title", title),
                "короткое имя": getattr(info, "short_name", short_name),
                "стикеров": getattr(info, "count", len(items)),
                "ссылка": f"https://t.me/addstickers/{getattr(info, 'short_name', short_name)}"}

    async def _document(self, name: str, position: int) -> Any:
        """Стикер по номеру в наборе → его документ.

        Номер живёт до первой перестановки, документ — всегда, поэтому наружу
        удобнее номер, а внутрь идёт документ.
        """
        from telethon.tl import functions, types

        result = await self.client(functions.messages.GetStickerSetRequest(
            stickerset=set_ref(name), hash=0))
        documents = getattr(result, "documents", None) or []
        if not 0 <= position < len(documents):
            raise StickerError(f"в наборе {len(documents)} стикеров, "
                               f"а запрошен номер {position}")
        doc = documents[position]
        return types.InputDocument(id=doc.id, access_hash=doc.access_hash,
                                   file_reference=doc.file_reference)

    async def add(self, name: str, path_text: str, emoji: str = "🙂") -> dict[str, Any]:
        from telethon.tl import functions, types

        bot = self._editor()
        path = Path(path_text).expanduser()
        if not path.is_file():
            raise StickerError(f"файла {path} нет")
        uploaded = await bot.upload_file(str(path))
        item = types.InputStickerSetItem(
            document=types.InputMediaUploadedDocument(
                file=uploaded, mime_type=MIME[kind_of(path)],
                attributes=[types.DocumentAttributeFilename(file_name=path.name)]),
            emoji=emoji)
        try:
            await bot(functions.stickers.AddStickerToSetRequest(
                stickerset=set_ref(name), sticker=item))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"добавлен": path.name, "эмодзи": emoji}

    async def remove(self, name: str, position: int) -> dict[str, Any]:
        from telethon.tl import functions

        document = await self._document(name, position)
        try:
            await self._editor()(functions.stickers.RemoveStickerFromSetRequest(
                sticker=document))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"убран номер": position}

    async def move(self, name: str, position: int, to: int) -> dict[str, Any]:
        from telethon.tl import functions

        document = await self._document(name, position)
        await self._editor()(functions.stickers.ChangeStickerPositionRequest(
            sticker=document, position=int(to)))
        return {"перемещён": position, "на": to}

    async def set_emoji(self, name: str, position: int, emoji: str,
                        keywords: str = "") -> dict[str, Any]:
        from telethon.tl import functions

        document = await self._document(name, position)
        await self._editor()(functions.stickers.ChangeStickerRequest(
            sticker=document, emoji=emoji, mask_coords=None, keywords=keywords or None))
        return {"стикер": position, "эмодзи": emoji, "ключевые слова": keywords or "не менялись"}

    async def rename(self, name: str, title: str) -> dict[str, Any]:
        from telethon.tl import functions

        await self._editor()(functions.stickers.RenameStickerSetRequest(
            stickerset=set_ref(name), title=title.strip()))
        return {"новое название": title.strip()}

    async def set_thumb(self, name: str, position: int) -> dict[str, Any]:
        """Сделать обложкой набора один из его стикеров."""
        from telethon.tl import functions

        document = await self._document(name, position)
        await self._editor()(functions.stickers.SetStickerSetThumbRequest(
            stickerset=set_ref(name), thumb=document, thumb_document_id=None))
        return {"обложка": position}

    async def delete(self, name: str) -> dict[str, Any]:
        from telethon.tl import functions

        try:
            await self._editor()(functions.stickers.DeleteStickerSetRequest(
                stickerset=set_ref(name)))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"удалён набор": name}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        import tgx_net

        hints = {
            "SHORT_NAME_OCCUPIED": "такое короткое имя уже занято — попробуйте tgx stickers suggest",
            "SHORT_NAME_INVALID": "короткое имя может содержать только латиницу, цифры и _",
            "STICKERSET_INVALID": "набора с таким именем нет",
            "STICKERS_EMPTY": "нужен хотя бы один стикер",
            "STICKER_PNG_DIMENSIONS": "у картинки должна быть сторона 512 пикселей",
            "STICKER_FILE_INVALID": "файл не подходит под требования Telegram",
            "STICKER_EMOJI_INVALID": "к стикеру нужен хотя бы один эмодзи",
            "BOT_MISSING": "этот метод вызывает бот — добавьте --as @бот",
            "PEER_ID_INVALID": "владельца набора не найти",
        }
        return tgx_net.explain(exc, hints, StickerError)


# --- пользоваться стикерами, а не только собирать их ---

BOX_HINTS = {
    "STICKERSET_INVALID": "такого набора нет",
    "STICKERS_EMPTY": "в наборе пусто",
    "STICKER_ID_INVALID": "такого стикера нет",
    "EMOTICON_EMPTY": "нужен эмодзи, по которому искать",
    "EMOTICON_INVALID": "это не эмодзи",
    "STICKERSET_NOT_MODIFIED": "и так уже установлен",
    "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
    "SEARCH_QUERY_EMPTY": "нечего искать",
}


def sticker_row(document: Any) -> dict[str, Any]:
    """Один стикер в строку. Ссылку на него собираем сами.

    Отправить существующий стикер можно только по паре «идентификатор плюс
    ключ доступа» — без ключа сервер отвечает, что документа не существует.
    Поэтому печатаем пару целиком: это то, что понадобится команде send.
    """
    from telethon.tl import types

    emoji = ""
    packs = ""
    for attribute in getattr(document, "attributes", None) or []:
        # эмодзи несут два разных признака: обычные стикеры и эмодзи-стикеры
        if isinstance(attribute, (types.DocumentAttributeSticker,
                                  types.DocumentAttributeCustomEmoji)):
            emoji = getattr(attribute, "alt", "") or emoji
            ref = getattr(attribute, "stickerset", None)
            if getattr(ref, "short_name", None):
                packs = ref.short_name
            elif getattr(ref, "id", None) and getattr(ref, "access_hash", None):
                # без короткого имени сослаться на набор можно только числами
                packs = f"{ref.id}:{ref.access_hash}"
    # «своё», а не «найденное»: сервер подбирает стикеры по связям, и эмодзи
    # самого стикера обычно не совпадает с тем, что вы искали
    row: dict[str, Any] = {"своё эмодзи": emoji or "?",
                           "ключ": f"{document.id}:{document.access_hash}"}
    if packs:
        row["набор"] = packs
    if getattr(document, "mime_type", None):
        row["вид"] = {"image/webp": "картинка", "video/webm": "видео",
                      "application/x-tgsticker": "анимация"}.get(
                          document.mime_type, document.mime_type)
    return row


def sticker_ref(key: str) -> Any:
    """«id:hash» обратно в ссылку на документ."""
    from telethon.tl import types

    if ":" not in key:
        raise StickerError("ключ стикера выглядит как «число:число» — его печатает find")
    ident, access = key.split(":", 1)
    try:
        return types.InputDocument(id=int(ident), access_hash=int(access), file_reference=b"")
    except ValueError as exc:
        raise StickerError(f"негодный ключ стикера «{key}»") from exc


class Box:
    """Ваши стикеры: найти, поставить, отправить, убрать."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        import tgx_net

        try:
            return await self.client(request)
        except Exception as exc:
            raise tgx_net.explain(exc, BOX_HINTS, StickerError) from exc

    @staticmethod
    def _set_row(item: Any) -> dict[str, Any]:
        pack = getattr(item, "set", item)
        return {"набор": getattr(pack, "short_name", None), "название": getattr(pack, "title", None),
                "стикеров": getattr(pack, "count", None),
                "вид": "эмодзи" if getattr(pack, "emojis", False) else
                       "маски" if getattr(pack, "masks", False) else "стикеры",
                "установлен": not getattr(pack, "archived", False)}

    async def mine(self, limit: int = 50) -> list[dict[str, Any]]:
        """Наборы, которые вы сделали сами."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMyStickersRequest(
            offset_id=0, limit=limit))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def installed(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetAllStickersRequest(hash=0))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def find_sets(self, query: str, *, featured: bool = True) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.SearchStickerSetsRequest(
            q=query, hash=0, exclude_featured=None if featured else True))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def find(self, *, emoji: str = "", query: str = "",
                   limit: int = 20, custom_emoji: bool = False) -> list[dict[str, Any]]:
        """Найти отдельные стикеры: по эмодзи, по словам или по обоим."""
        from telethon.tl import functions

        if emoji and not query:
            request = (functions.messages.SearchCustomEmojiRequest(emoticon=emoji, hash=0)
                       if custom_emoji
                       else functions.messages.GetStickersRequest(emoticon=emoji, hash=0))
        else:
            request = functions.messages.SearchStickersRequest(
                q=query, emoticon=emoji or "", lang_code=["ru", "en"], offset=0,
                limit=limit, hash=0, emojis=custom_emoji or None)
        result = await self._call(request)
        found = getattr(result, "stickers", None) or getattr(result, "documents", None) or []
        return [sticker_row(d) for d in found][:limit]

    async def faved(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetFavedStickersRequest(hash=0))
        return [sticker_row(d) for d in getattr(result, "stickers", None) or []]

    async def recent(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetRecentStickersRequest(hash=0))
        return [sticker_row(d) for d in getattr(result, "stickers", None) or []]

    async def fave(self, key: str, *, remove: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.FaveStickerRequest(
            id=sticker_ref(key), unfave=remove))
        return {"стикер": key, "в избранном": not remove}

    async def install(self, name: str, *, remove: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        reference = set_ref(name)
        if remove:
            await self._call(functions.messages.UninstallStickerSetRequest(
                stickerset=reference))
        else:
            await self._call(functions.messages.InstallStickerSetRequest(
                stickerset=reference, archived=False))
        return {"набор": name, "установлен": not remove}

    async def send(self, peer: Any, key: str, *, reply_to: int = 0) -> dict[str, Any]:
        """Отправить существующий стикер, а не загрузить новый файл.

        Загруженный заново webp станет обычной картинкой: стикером его делает
        принадлежность набору, а её несёт только исходный документ.
        """
        message = await self.client.send_file(
            peer, sticker_ref(key), reply_to=reply_to or None)
        return {"отправлен": key, "id": getattr(message, "id", None)}


    async def replace(self, key: str, path: Any, emoji: str) -> dict[str, Any]:
        """Заменить стикер в наборе новым файлом, сохранив его место.

        Убрать и добавить — не то же самое: стикер уедет в конец, а у тех, кто
        уже поставил набор, порядок собьётся.
        """
        from pathlib import Path

        from telethon.tl import functions, types

        source = Path(path).expanduser()
        if not source.is_file():
            raise StickerError(f"файла {source} нет")
        uploaded = await self.client.upload_file(str(source))
        item = types.InputStickerSetItem(document=uploaded, emoji=emoji)
        await self._call(functions.stickers.ReplaceStickerRequest(
            sticker=sticker_ref(key), new_sticker=item))
        return {"заменён": key, "эмодзи": emoji}


class Library:
    """Библиотека наборов: рекомендованные, архив, маски, эмодзи, гифки.

    В графическом клиенте это вкладки панели стикеров. В терминале панели нет,
    поэтому у каждой вкладки своя команда.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        import tgx_net

        try:
            return await self.client(request)
        except Exception as exc:
            raise tgx_net.explain(exc, BOX_HINTS, StickerError) from exc

    @staticmethod
    def _set_row(item: Any) -> dict[str, Any]:
        pack = getattr(item, "set", item)
        row = {"набор": getattr(pack, "short_name", None),
               "название": getattr(pack, "title", None),
               "стикеров": getattr(pack, "count", None)}
        if getattr(pack, "id", None) and getattr(pack, "access_hash", None):
            row["ключ"] = f"{pack.id}:{pack.access_hash}"
        if getattr(item, "unread", None):
            row["непросмотренных"] = len(item.unread)
        return row

    async def featured(self, *, emoji: bool = False, old: bool = False) -> list[dict[str, Any]]:
        """Что Telegram рекомендует."""
        from telethon.tl import functions

        if old:
            request = functions.messages.GetOldFeaturedStickersRequest(offset=0, limit=50, hash=0)
        elif emoji:
            request = functions.messages.GetFeaturedEmojiStickersRequest(hash=0)
        else:
            request = functions.messages.GetFeaturedStickersRequest(hash=0)
        result = await self._call(request)
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def archived(self, *, masks: bool = False,
                       emoji: bool = False) -> list[dict[str, Any]]:
        """Наборы, убранные в архив, — они не показываются, но и не удалены."""
        from telethon.tl import functions

        result = await self._call(functions.messages.GetArchivedStickersRequest(
            offset_id=0, limit=50, masks=masks or None, emojis=emoji or None))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def masks(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetMaskStickersRequest(hash=0))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def emoji_packs(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetEmojiStickersRequest(hash=0))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def emoji_groups(self, *, kind: str = "") -> list[dict[str, Any]]:
        """Разделы панели эмодзи: «улыбки», «еда» и прочее."""
        from telethon.tl import functions

        request = {"status": functions.messages.GetEmojiStatusGroupsRequest,
                   "avatar": functions.messages.GetEmojiProfilePhotoGroupsRequest,
                   "sticker": functions.messages.GetEmojiStickerGroupsRequest,
                   }.get(kind, functions.messages.GetEmojiGroupsRequest)
        result = await self._call(request(hash=0))
        return [{"раздел": getattr(g, "title", None),
                 "эмодзи": len(getattr(g, "emoticons", None) or [])}
                for g in getattr(result, "groups", None) or []]

    async def search_emoji_sets(self, query: str) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.SearchEmojiStickerSetsRequest(
            q=query, hash=0))
        return [self._set_row(s) for s in getattr(result, "sets", None) or []]

    async def mark_seen(self, ids: list[int]) -> dict[str, Any]:
        """Убрать точку «новое» с рекомендованных наборов."""
        from telethon.tl import functions

        await self._call(functions.messages.ReadFeaturedStickersRequest(id=ids))
        return {"просмотрено наборов": len(ids)}

    async def archive(self, names: list[str], *, restore: bool = False,
                      remove: bool = False) -> dict[str, Any]:
        """Убрать наборы в архив, вернуть оттуда или снести совсем."""
        from telethon.tl import functions

        refs = [set_ref(n) for n in names]
        await self._call(functions.messages.ToggleStickerSetsRequest(
            stickersets=refs, uninstall=remove or None,
            archive=None if (restore or remove) else True,
            unarchive=restore or None))
        return {"наборов": len(refs),
                "что сделали": "удалены" if remove else "возвращены" if restore else "в архиве"}

    async def order(self, ids: list[int], *, masks: bool = False,
                    emoji: bool = False) -> dict[str, Any]:
        """Переставить наборы. Порядок задаётся числовыми id, не именами."""
        from telethon.tl import functions

        await self._call(functions.messages.ReorderStickerSetsRequest(
            order=ids, masks=masks or None, emojis=emoji or None))
        return {"порядок": len(ids)}

    async def recent(self, key: str, *, remove: bool = False,
                     attached: bool = False) -> dict[str, Any]:
        """Положить стикер в недавние или убрать оттуда."""
        from telethon.tl import functions

        await self._call(functions.messages.SaveRecentStickerRequest(
            id=sticker_ref(key), unsave=remove, attached=attached or None))
        return {"стикер": key, "в недавних": not remove}

    async def clear_recent(self, *, attached: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.ClearRecentStickersRequest(
            attached=attached or None))
        return {"недавние": "очищены"}

    async def gifs(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self._call(functions.messages.GetSavedGifsRequest(hash=0))
        return [sticker_row(d) for d in getattr(result, "gifs", None) or []]

    async def save_gif(self, key: str, *, remove: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        await self._call(functions.messages.SaveGifRequest(
            id=sticker_ref(key), unsave=remove))
        return {"гифка": key, "сохранена": not remove}

    async def whose(self, peer: Any, message_id: int) -> list[dict[str, Any]]:
        """Из какого набора стикеры, наклеенные на фотографию."""
        from telethon.tl import functions, types

        result = await self._call(functions.messages.GetAttachedStickersRequest(
            media=types.InputStickeredMediaPhoto(
                id=(await self.client.get_messages(peer, ids=message_id)).photo)))
        return [self._set_row(s) for s in result or []]
