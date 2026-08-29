#!/usr/bin/env python3
"""Rich Messages — Bot API 10.1/10.2 (June–July 2026).

A rich message is a *document-grade* message: headings, tables, task lists,
footnotes, collapsible blocks, formulas, media galleries. Only bots can send
them, and only through the HTTP Bot API — MTProto (and therefore Telethon) has
no method for it — so this module talks to api.telegram.org directly with the
bot token tgx already stores.

Content can be given three ways: `markdown` (the dialect below), `html`, or an
explicit `blocks` tree. Markdown is what tgx uses: it is the same thing the user
already writes, and Telegram builds the block tree on its side.

Reference: https://core.telegram.org/bots/api#rich-message-formatting-options
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

API = "https://api.telegram.org"
TIMEOUT = 60

# Documented limits — checked here so a mistake fails locally with a clear reason
# instead of coming back as a bare 400 from the server.
MAX_CHARS = 32768
MAX_BLOCKS = 500
MAX_MEDIA = 50
MAX_TABLE_COLUMNS = 20

RICH_SYNTAX = """**жирный**   *курсив*   ~~зачёркнутый~~   ==выделенный==   ||спойлер||
`код`   ```python … ```   $x^2 + y^2$   ---

# Заголовок 1 … ###### Заголовок 6

- пункт   1. нумерованный   - [ ] задача   - [x] сделано

> цитата
> продолжение цитаты

| Заголовок | Ещё |
|:----------|:---:|
| слева     | по центру |

![](https://…/photo.jpg "подпись")      картинка, видео, аудио, документ
![](tg://photo?id=cover)                 медиа, приложенное к сообщению
[ссылка](https://t.me)   [почта](mailto:a@b.io)   [телефон](tel:+123)
[упоминание](tg://user?id=123)   ![](tg://emoji?id=5368324170671202286)

Сноска[^1]

[^1]: текст сноски"""


class RichError(RuntimeError):
    """Something the Bot API refused, or a limit broken before we even asked."""


# ── addressing ───────────────────────────────────────────────────────────────
def bot_chat_id(chat: Any) -> str:
    """The Bot API wants @username, or the -100… form for channels and supergroups.

    tgx keeps bare entity ids internally, so the prefix is put back here.
    """
    username = (getattr(chat, "username", "") or "").lstrip("@")
    if username:
        return f"@{username}"
    chat_id = int(getattr(chat, "id", 0) or 0)
    if not chat_id:
        raise RichError("не понял, кому отправлять")
    kind = getattr(chat, "kind", "")
    if kind in {"channel", "group"}:
        return f"-100{chat_id}"
    return str(chat_id)


# ── buttons ──────────────────────────────────────────────────────────────────
def split_style(label: str) -> tuple[str, str]:
    """`Скачать[primary]` → («Скачать», «primary»). Без скобок — как было."""
    if not (label.endswith("]") and "[" in label):
        return label, ""
    head, _, tail = label.rpartition("[")
    return head.strip(), tail[:-1].strip().lower()


def buttons_json(spec: str) -> dict[str, Any] | None:
    """The same button syntax as the rest of tgx, in the Bot API's own JSON shape."""
    if not (spec or "").strip():
        return None
    rows: list[list[dict[str, Any]]] = []
    for line in spec.split(";"):
        row: list[dict[str, Any]] = []
        for chunk in line.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            label, sep, target = chunk.partition("=")
            if not sep:
                raise RichError(f"кнопка «{chunk}» без адреса — нужно «Текст=https://…»")
            label, target = label.strip(), target.strip()
            # Тот же синтаксис стиля, что и в MTProto-ветке: Текст[primary]=…
            # Bot API поля стиля пока не документирует, поэтому метку снимаем
            # всегда — иначе она уезжает в подпись кнопки, как и случилось.
            label, style = split_style(label)
            kind, _, rest = target.partition(":")
            if target.startswith(("http://", "https://", "tg://")):
                row.append({"text": label, "url": target})
            elif kind == "webapp":
                if not rest.startswith("https://"):
                    raise RichError(f"веб-приложению «{label}» нужен https-адрес")
                row.append({"text": label, "web_app": {"url": rest}})
            elif kind == "cb":
                row.append({"text": label, "callback_data": rest})
            elif kind == "switch":
                row.append({"text": label, "switch_inline_query": rest})
            elif kind == "copy":
                row.append({"text": label, "copy_text": {"text": rest}})
            elif kind == "user":
                raise RichError("кнопка-профиль в Bot API недоступна — используйте ссылку t.me")
            else:
                row.append({"text": label, "callback_data": target})
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


# ── local checks ─────────────────────────────────────────────────────────────
def check_limits(markdown: str, media: Sequence[Any] = ()) -> None:
    if not markdown.strip():
        raise RichError("пустое сообщение")
    if len(markdown) > MAX_CHARS:
        raise RichError(f"слишком длинно: {len(markdown)} символов при лимите {MAX_CHARS}")
    if len(media) > MAX_MEDIA:
        raise RichError(f"слишком много вложений: {len(media)} при лимите {MAX_MEDIA}")
    blocks = sum(1 for line in markdown.splitlines() if line.strip())
    if blocks > MAX_BLOCKS:
        raise RichError(f"похоже на {blocks} блоков при лимите {MAX_BLOCKS}")
    for line in markdown.splitlines():
        if line.strip().startswith("|") and line.count("|") - 1 > MAX_TABLE_COLUMNS:
            raise RichError(f"в таблице больше {MAX_TABLE_COLUMNS} колонок")


def media_ids(markdown: str) -> list[str]:
    """The tg://…?id=NAME references used in the text, in order of appearance."""
    return list(dict.fromkeys(re.findall(r"tg://(?:photo|video|document|audio)\?id=([A-Za-z0-9_-]{1,64})", markdown)))


# ── the HTTP side ────────────────────────────────────────────────────────────
def call(token: str, method: str, payload: dict[str, Any]) -> Any:
    import tgx_net

    try:
        answer = tgx_net.post_json(f"{API}/bot{token}/{method}", payload, "Bot API")
    except tgx_net.NetError as exc:
        raise RichError(str(exc)) from exc
    if not answer.get("ok"):
        raise RichError(_explain_send(answer.get("description", "без объяснений")))
    return answer["result"]


def call_with_files(token: str, method: str, payload: dict[str, Any],
                    uploads: dict[str, Any]) -> Any:
    """Тот же вызов Bot API, но с файлами формы — вложенные поля едут строками JSON."""
    import mimetypes

    import tgx_net

    fields = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
              for k, v in payload.items()}
    files = {}
    for name, path in uploads.items():
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        files[name] = (path.name, path.read_bytes(), mime)
    try:
        answer = tgx_net.post_multipart(f"{API}/bot{token}/{method}", fields, files, "Bot API")
    except tgx_net.NetError as exc:
        raise RichError(str(exc)) from exc
    if not answer.get("ok"):
        raise RichError(_explain_send(answer.get("description", "без объяснений")))
    return answer["result"]


def send_rich(
    token: str,
    chat_id: str,
    markdown: str = "",
    *,
    html: str = "",
    blocks: Sequence[dict[str, Any]] | None = None,
    media: Sequence[dict[str, Any]] = (),
    buttons: str = "",
    silent: bool = False,
    protect: bool = False,
    reply_to: int | None = None,
    topic: int | None = None,
    is_rtl: bool = False,
    skip_entity_detection: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    """Send one rich message. `draft=True` streams it as a partial message."""
    given = [bool(markdown.strip()), bool(html.strip()), bool(blocks)]
    if sum(given) != 1:
        raise RichError("нужно ровно одно из: markdown, html или blocks")
    if markdown:
        check_limits(markdown, media)

    rich: dict[str, Any] = {}
    if markdown:
        rich["markdown"] = markdown
    elif html:
        rich["html"] = html
    else:
        check_blocks(blocks or [])
        rich["blocks"] = list(blocks or [])
    if media:
        rich["media"] = list(media)
    if is_rtl:
        rich["is_rtl"] = True
    if skip_entity_detection:
        rich["skip_entity_detection"] = True

    payload: dict[str, Any] = {"chat_id": chat_id, "rich_message": rich}
    if topic:
        payload["message_thread_id"] = int(topic)   # тема форума на стороне Bot API
    keyboard = buttons_json(buttons)
    if keyboard:
        payload["reply_markup"] = keyboard
    if silent:
        payload["disable_notification"] = True
    if protect:
        payload["protect_content"] = True
    if reply_to:
        payload["reply_parameters"] = {"message_id": int(reply_to)}
    method = "sendRichMessageDraft" if draft else "sendRichMessage"
    uploads = {m["id"]: m.pop("_upload") for m in rich.get("media", []) if m.get("_upload")}
    # Блок ссылается на attach://имя — файл кладётся в ту же форму. Ссылка лежит
    # в поле, названном по типу блока: у документа в document, у видео в video.
    # Пока искали только в document, у видеоблока имя выходило пустым, и в форму
    # уезжало поле без имени: сервер отвечал 400 с пустым телом.
    for block in rich.get("blocks", []) or []:
        source = block.pop("_upload", None)
        if source is None:
            continue
        holder = block.get(block.get("type") or "")
        reference = str(holder.get("media", "")) if isinstance(holder, dict) else ""
        name = reference.removeprefix("attach://")
        if not name:
            raise RichError(f"блок «{block.get('type')}» ждёт файл, но ссылки "
                            f"attach:// в нём нет")
        uploads[name] = source
    if uploads:
        return call_with_files(token, method, payload, uploads)
    return call(token, method, payload)


# ── rendering what arrives ───────────────────────────────────────────────────
# Rich messages reuse Telegram's Instant View model: blocks are PageBlock*, and
# their text is the familiar TextPlain / TextBold / TextConcat tree.
HEADING_LEVELS = {"PageBlockTitle": 1, "PageBlockSubtitle": 2, "PageBlockHeader": 2,
                  "PageBlockSubheader": 3, "PageBlockKicker": 3,
                  "PageBlockHeading1": 1, "PageBlockHeading2": 2, "PageBlockHeading3": 3,
                  "PageBlockHeading4": 4, "PageBlockHeading5": 5, "PageBlockHeading6": 6}

TEXT_STYLES = {"TextBold": "bold", "TextItalic": "italic", "TextUnderline": "underline",
               "TextStrike": "strike", "TextMarked": "reverse"}


def rich_text(node: Any, colors: dict[str, str] | None = None) -> Any:
    """A Telegram RichText tree → a Rich Text object."""
    from rich.style import Style
    from rich.text import Text

    palette = colors or {}
    accent = palette.get("primary", "#2AABEE")
    code_color = palette.get("warning", "#E5CA77")
    muted = palette.get("text-muted", "#7E93A5")

    if node is None:
        return Text("")
    kind = type(node).__name__
    if kind == "TextEmpty":
        return Text("")
    if kind == "TextPlain":
        return Text(getattr(node, "text", "") or "")
    if kind == "TextConcat":
        joined = Text()
        for part in getattr(node, "texts", None) or []:
            joined.append_text(rich_text(part, colors))
        return joined
    inner = rich_text(getattr(node, "text", None), colors)
    if kind in TEXT_STYLES:
        inner.stylize(TEXT_STYLES[kind])
    elif kind in {"TextCode", "TextFixed"}:
        inner.stylize(Style(color=code_color))
    elif kind == "TextUrl":
        inner.stylize(Style(color=accent, underline=True, link=getattr(node, "url", None)))
    elif kind in {"TextEmail", "TextPhone", "TextAnchor"}:
        inner.stylize(Style(color=accent))
    elif kind == "TextSpoiler":
        inner = Text("░" * max(1, len(inner.plain)), style=muted)
    elif kind in {"TextSubscript", "TextSuperscript"}:
        inner.stylize(Style(color=muted))
    return inner


# Ответы Bot API на богатые сообщения приходят кодами; человеку нужно знать,
# что делать, а не как это называется внутри.
SEND_HINTS = {
    "RICH_MESSAGE_PHOTO_INVALID": (
        "ссылка tg://photo?id= принимает только фотографию, и ссылки "
        "tg://video?id= не существует. Видео кладётся блоком: --blocks с "
        '{"type": "video", "video": {"type": "video", "media": "attach://имя"}} '
        "и --attach имя=путь"),
    "RICH_MESSAGE_TOO_LONG": "текст длиннее, чем принимает богатое сообщение",
    "RICH_MESSAGE_BLOCKS_TOO_MUCH": "блоков больше, чем разрешено",
    "MESSAGE_EMPTY": "нечего отправлять",
    "BUTTON_URL_INVALID": "у кнопки негодный адрес",
    "MEDIA_EMPTY": "медиа не приложено — проверьте --media имя=путь",
    "WEBPAGE_MEDIA_EMPTY": "по ссылке ничего не нашлось",
}


def _explain_send(description: str) -> str:
    """Код в ответе Bot API → что с этим делать.

    Разбор общий, через `tgx_net.explain`: свою копию этой логики я однажды уже
    написал в восьми модулях, и половина подсказок молча не срабатывала. Bot API
    отдаёт код строкой, а не исключением, поэтому оборачиваем — правило важнее
    удобства.
    """
    import tgx_net

    told = tgx_net.explain(RichError(description), SEND_HINTS, RichError)
    text = str(told)
    return text if text != description else f"Bot API отказал: {description}"


def render_message(rich: Any, colors: dict[str, str] | None = None, width: int = 72) -> Any:
    """A received RichMessage → a readable block of terminal text."""
    from rich.text import Text

    palette = colors or {}
    accent = palette.get("primary", "#2AABEE")
    muted = palette.get("text-muted", "#7E93A5")
    out = Text()

    def add(line: Any, spacer: bool = True) -> None:
        """Every entry starts a new line; `spacer` also leaves a blank line before it."""
        if out.plain:
            out.append("\n\n" if spacer else "\n")
        out.append_text(line if isinstance(line, Text) else Text(str(line)))

    def walk(blocks: Sequence[Any], indent: str = "") -> None:
        for block in blocks or []:
            kind = type(block).__name__
            text = rich_text(getattr(block, "text", None), colors)
            if kind in HEADING_LEVELS:
                level = HEADING_LEVELS[kind]
                head = Text(f"{indent}{'#' * level} ", style=muted)
                text.stylize(f"bold {accent}")
                head.append_text(text)
                add(head)
            elif kind == "PageBlockParagraph":
                add(Text(indent).append_text(text))
            elif kind in {"PageBlockList", "PageBlockOrderedList"}:
                ordered = kind == "PageBlockOrderedList"
                for number, item in enumerate(getattr(block, "items", None) or [], 1):
                    marker = f"{number}. " if ordered else "• "
                    item_text = getattr(item, "text", None)
                    line = Text(f"{indent}  {marker}", style=muted)
                    if item_text is not None:
                        line.append_text(rich_text(item_text, colors))
                    add(line, spacer=number == 1)
                    walk(getattr(item, "blocks", None) or [], indent + "    ")
            elif kind in {"PageBlockBlockquote", "PageBlockPullquote"}:
                quote = Text(f"{indent}▌ ", style=muted)
                text.stylize("italic")
                quote.append_text(text)
                add(quote)
            elif kind == "PageBlockPreformatted":
                body = rich_text(getattr(block, "text", None), colors)
                body.stylize(palette.get("warning", "#E5CA77"))
                add(Text(f"{indent}  ").append_text(body))
            elif kind == "PageBlockDivider":
                add(Text(f"{indent}{'─' * min(width, 40)}", style=muted))
            elif kind == "PageBlockTable":
                add(Text(f"{indent}┌ таблица", style=muted))
                for row in (getattr(block, "rows", None) or [])[:12]:
                    cells = []
                    for cell in getattr(row, "cells", None) or []:
                        cells.append(rich_text(getattr(cell, "text", None), colors).plain.strip())
                    add(Text(f"{indent}│ " + " · ".join(c for c in cells if c), style=muted), spacer=False)
            elif kind == "PageBlockDetails":
                summary = rich_text(getattr(block, "title", None), colors)
                add(Text(f"{indent}▸ ", style=accent).append_text(summary))
                walk(getattr(block, "blocks", None) or [], indent + "  ")
            elif kind in {"PageBlockPhoto", "PageBlockVideo", "PageBlockAudio", "PageBlockCollage",
                          "PageBlockSlideshow", "PageBlockEmbed", "PageBlockMap"}:
                add(Text(f"{indent}[{kind[len('PageBlock'):].lower()}]", style=accent))
            elif kind == "PageBlockFooter":
                add(Text(f"{indent}— ", style=muted).append_text(text))
            elif getattr(block, "blocks", None):
                walk(block.blocks, indent + "  ")
            elif text.plain:
                add(Text(indent).append_text(text))
    walk(getattr(rich, "blocks", None) or [])
    return out


# ── блочная форма (Bot API 10.2–10.3) ───────────────────────────────────────
# `InputRichMessage` принимает ровно одно из markdown, html или blocks. Блоки —
# единственный способ положить кнопки и файлы ВНУТРЬ документа: markdown этого
# не умеет, а тег <tg-button-row> живёт только в html-форме.
BLOCK_TYPES = {
    "paragraph", "heading", "list", "pre", "divider", "blockquote",
    "expandable_blockquote", "table", "buttons", "document", "photo", "video",
    "footer", "caption", "math", "details",
}
BUTTON_STYLES = ("danger", "success", "primary")
MAX_BUTTONS_IN_ROW = 8
MAX_HEADING = 6


def as_text(value: Any) -> Any:
    """RichText принимает и простую строку — оставляем её как есть.

    Имя нарочно не `rich_text`: так называется отрисовщик полученных сообщений
    ниже, и одноимённая функция молча его затирала.
    """
    return value


def paragraph(text: Any) -> dict[str, Any]:
    return {"type": "paragraph", "text": as_text(text)}


def heading(text: Any, size: int = 2) -> dict[str, Any]:
    if not 1 <= int(size) <= MAX_HEADING:
        raise RichError(f"размер заголовка {size} вне 1–{MAX_HEADING}; 1 — самый крупный")
    return {"type": "heading", "text": as_text(text), "size": int(size)}


def bullet_list(items: Sequence[Any], ordered: bool = False) -> dict[str, Any]:
    return {"type": "list", "is_ordered": bool(ordered) or None,
            "items": [{"blocks": [paragraph(i)]} for i in items]}


def preformatted(code: str, language: str = "") -> dict[str, Any]:
    block = {"type": "pre", "text": code}
    if language:
        block["language"] = language
    return block


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def quote(text: Any, *, credit: Any = None, expandable: bool = False) -> dict[str, Any]:
    """Раскрывающаяся цитата появилась в 10.3 — для длинных врезок."""
    block = {"type": "expandable_blockquote" if expandable else "blockquote",
             "text": as_text(text)}
    if credit:
        block["credit"] = as_text(credit)
    return block


def table(rows: Sequence[Sequence[Any]], *, bordered: bool = False, striped: bool = False,
          compact: bool = False, caption: Any = None) -> dict[str, Any]:
    """`is_compact` — из 10.3: у ячеек меньше отступы."""
    block: dict[str, Any] = {"type": "table",
                             "cells": [[{"text": as_text(c)} for c in row] for row in rows]}
    for flag, name in ((bordered, "is_bordered"), (striped, "is_striped"), (compact, "is_compact")):
        if flag:
            block[name] = True
    if caption:
        block["caption"] = as_text(caption)
    return block


def button_row(spec: str, align: str = "") -> dict[str, Any]:
    """Кнопки ВНУТРИ документа — то, чего нет ни в markdown, ни у обычного аккаунта.

    Синтаксис тот же, что у кнопок под сообщением, включая стиль в скобках;
    в ряд помещается от одной до восьми.
    """
    keyboard = buttons_json(spec)
    if not keyboard:
        raise RichError("не указано ни одной кнопки")
    flat = [b for row in keyboard["inline_keyboard"] for b in row]
    if not 1 <= len(flat) <= MAX_BUTTONS_IN_ROW:
        raise RichError(f"в ряду должно быть от 1 до {MAX_BUTTONS_IN_ROW} кнопок, а не {len(flat)}")
    for source, target in zip(_specs(spec), flat):
        _, style = split_style(source)
        if style and style not in BUTTON_STYLES:
            raise RichError(f"стиль «{style}» для кнопки в документе не подходит; "
                            f"есть: {', '.join(BUTTON_STYLES)}")
        if style:
            target["style"] = style
    block: dict[str, Any] = {"type": "buttons", "buttons": flat}
    if align:
        block["align"] = align
    return block


def _specs(spec: str) -> list[str]:
    """Подписи кнопок в том же порядке, в каком их собрал buttons_json."""
    out = []
    for line in spec.split(";"):
        for chunk in line.split(","):
            chunk = chunk.strip()
            if chunk:
                out.append(chunk.partition("=")[0].strip())
    return out


def media_block(kind: str, identifier: str, caption: Any = None,
                source: str | None = None) -> dict[str, Any]:
    """Видео или фотография блоком.

    Проверено на живом сервере: блочная форма действительно принимает видео —
    приходит `PageBlockVideo` с настоящим mp4 внутри, а не стоп-кадр, как через
    ссылку tg://photo?id=. Ссылка attach:// кладётся в поле, названное по типу
    блока, — иначе имя поля формы выходит пустым и сервер отвечает 400 без тела.
    """
    if kind not in {"video", "photo"}:
        raise RichError(f"блок «{kind}»: сюда годятся video и photo")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise RichError(f"идентификатор «{identifier}»: только A-Z a-z 0-9 _ - и до 64 символов")
    block: dict[str, Any] = {"type": kind,
                             kind: {"type": kind, "media": f"attach://{identifier}"}}
    if caption:
        block["caption"] = {"text": as_text(caption)}
    if source is not None:
        from pathlib import Path

        path = Path(source).expanduser()
        if not path.is_file():
            raise RichError(f"файла {path} нет")
        block["_upload"] = path
    return block


def document_block(identifier: str, caption: Any = None, source: str | None = None) -> dict[str, Any]:
    """Файл внутри документа (10.3): ссылка на него — tg://document?id=…"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise RichError(f"идентификатор «{identifier}»: только A-Z a-z 0-9 _ - и до 64 символов")
    block: dict[str, Any] = {"type": "document",
                             "document": {"type": "document", "media": f"attach://{identifier}"}}
    if caption:
        block["caption"] = {"text": as_text(caption)}
    if source is not None:
        from pathlib import Path

        path = Path(source).expanduser()
        if not path.is_file():
            raise RichError(f"файла {path} нет")
        block["_upload"] = path
    return block


# Отправка блоков работает, а чтение обратно — нет: конструкторы блочной формы
# новее слоя 227, и Telethon отдаёт такое сообщение как MessageMediaUnsupported.
# Получатель с обновлённым клиентом видит документ полностью; tgx — пока нет.
BLOCKS_ARE_WRITE_ONLY = ("блочная форма новее слоя MTProto, который знает Telethon: "
                         "отправить её можно, а показать в tgx — пока нет")


def check_blocks(blocks: Sequence[dict[str, Any]]) -> None:
    """Проверить дерево блоков до отправки — сервер объясняет отказ скупо."""
    if not blocks:
        raise RichError("список блоков пуст")
    if len(blocks) > MAX_BLOCKS:
        raise RichError(f"слишком много блоков: {len(blocks)} при лимите {MAX_BLOCKS}")
    for index, block in enumerate(blocks, 1):
        kind = block.get("type")
        if kind not in BLOCK_TYPES:
            raise RichError(f"блок {index}: тип «{kind}» неизвестен; "
                            f"есть: {', '.join(sorted(BLOCK_TYPES))}")
        if kind == "buttons":
            count = len(block.get("buttons") or [])
            if not 1 <= count <= MAX_BUTTONS_IN_ROW:
                raise RichError(f"блок {index}: кнопок {count}, а можно от 1 до {MAX_BUTTONS_IN_ROW}")
        if kind == "heading" and not 1 <= int(block.get("size", 1)) <= MAX_HEADING:
            raise RichError(f"блок {index}: размер заголовка вне 1–{MAX_HEADING}")


# что за медиа — видно по расширению; посылать видео как фотографию значит
# молча получить стоп-кадр вместо ролика
MEDIA_KINDS = {".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video",
               ".gif": "animation",
               ".mp3": "audio", ".m4a": "audio", ".ogg": "audio", ".oga": "audio",
               ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo"}


def media_kind(url_or_path: str) -> str:
    """Тип медиа по расширению. Незнакомое отдаём документом, а не картинкой."""
    from pathlib import Path

    suffix = Path(url_or_path.split("?", 1)[0]).suffix.lower()
    if not suffix:
        return "photo"
    return MEDIA_KINDS.get(suffix, "document")


def photo_media(identifier: str, url_or_path: str) -> dict[str, Any]:
    """A media entry for a tg://photo?id=<identifier> reference in the text.

    Локальный файл превращается в ссылку `attach://…`: Bot API берёт медиа либо
    по публичному URL, либо частью той же формы, а публичного URL у файла с
    диска нет. Сам файл кладётся в `_upload` и уезжает multipart-ом.

    Тип берётся из расширения. Раньше здесь всегда стояло «фото», и отправленный
    ролик молча превращался в стоп-кадр: сервер не спорит, а просто берёт кадр.
    """
    from pathlib import Path

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise RichError(f"идентификатор «{identifier}»: только A-Z a-z 0-9 _ - и до 64 символов")
    entry: dict[str, Any] = {"id": identifier,
                             "media": {"type": media_kind(url_or_path), "media": url_or_path}}
    if not url_or_path.startswith(("http://", "https://")):
        source = Path(url_or_path).expanduser()
        if not source.is_file():
            raise RichError(f"файла {source} нет — нужен путь на диске или https-ссылка")
        entry["media"]["media"] = f"attach://{identifier}"
        entry["_upload"] = source
    return entry
