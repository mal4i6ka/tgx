#!/usr/bin/env python3
"""Text formatting for tgx: parse what you type, render what Telegram sends.

Telegram carries formatting as *entities* with offsets counted in UTF-16 code
units, so every conversion here goes through UTF-16 rather than Python indexes —
one emoji in the text is enough to shift everything otherwise.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from telethon.extensions import html as tg_html
from telethon.extensions import markdown as tg_markdown
from telethon.tl.types import MessageEntityBlockquote, MessageEntitySpoiler, MessageEntityUnderline

# Telethon's markdown lacks spoilers and underline; both are plain delimiters.
DELIMITERS = {
    **tg_markdown.DEFAULT_DELIMITERS,
    "||": MessageEntitySpoiler,
    "--": MessageEntityUnderline,
}

MODES = ("md", "html", "none")

SYNTAX = """**жирный**   __курсив__   --подчёркнутый--   ~~зачёркнутый~~
`код`   ```блок кода```   ||спойлер||   [текст](https://ссылка)
> цитата — в начале строки, несколько строк подряд склеиваются"""

HTML_SYNTAX = """<b> <i> <u> <s> <code> <pre> <blockquote> <spoiler>
<a href="https://…">текст</a>"""


# ── UTF-16 arithmetic ────────────────────────────────────────────────────────
def _u16(text: str) -> bytes:
    return text.encode("utf-16-le")


def py_offsets(text: str, offset: int, length: int) -> tuple[int, int]:
    """Entity offsets (UTF-16 units) → Python string indexes."""
    data = _u16(text)
    start = len(data[: offset * 2].decode("utf-16-le", "ignore"))
    end = len(data[: (offset + length) * 2].decode("utf-16-le", "ignore"))
    return start, end


def u16_len(text: str) -> int:
    return len(_u16(text)) // 2


# ── parsing ──────────────────────────────────────────────────────────────────
def _drop(text: str, entities: list[Any], at16: int, count16: int) -> str:
    """Remove `count16` UTF-16 units at `at16`, moving the entities with them."""
    start, end = py_offsets(text, at16, count16)
    result = text[:start] + text[end:]
    for entity in entities:
        if entity.offset >= at16 + count16:
            entity.offset -= count16
        elif entity.offset <= at16 < entity.offset + entity.length:
            entity.length = max(0, entity.length - count16)
    return result


def _quote_lines(text: str, entities: list[Any]) -> str:
    """Turn leading `> ` markers into blockquote entities."""
    while True:
        lines = text.split("\n")
        start16 = 0
        for index, line in enumerate(lines):
            if line.startswith("> "):
                text = _drop(text, entities, start16, 2)
                span = u16_len(line) - 2
                # merge with a blockquote that ends right where this line starts
                previous = next(
                    (e for e in entities
                     if isinstance(e, MessageEntityBlockquote) and e.offset + e.length + 1 == start16),
                    None,
                )
                if previous is not None:
                    previous.length += span + 1
                else:
                    entities.append(MessageEntityBlockquote(offset=start16, length=span))
                break
            start16 += u16_len(line) + 1
        else:
            return text


def parse(text: str, mode: str = "md") -> tuple[str, list[Any]]:
    """Text plus entities, ready for `send_message(formatting_entities=…)`."""
    mode = (mode or "none").lower()
    if mode in {"none", "plain", ""}:
        return text, []
    if mode == "html":
        return _parse_html(text)
    clean, entities = tg_markdown.parse(text, delimiters=DELIMITERS)
    entities = list(entities or [])
    return _quote_lines(clean, entities), entities


class _SpoilerHTMLParser(tg_html.HTMLToTelegramParser):
    """Telethon's HTML parser silently drops <spoiler>; teach it the tag."""

    SPOILER_TAGS = {"spoiler", "tg-spoiler"}

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SPOILER_TAGS:
            self._open_tags.appendleft(tag)
            self._open_tags_meta.appendleft(None)
            self._building_entities[tag] = MessageEntitySpoiler(offset=len(self.text), length=0)
            return
        super().handle_starttag(tag, attrs)


def _parse_html(text: str) -> tuple[str, list[Any]]:
    if not text:
        return text, []
    parser = _SpoilerHTMLParser()
    parser.feed(tg_html.add_surrogate(text))
    clean = tg_html.strip_text(parser.text, parser.entities)
    parser.entities.reverse()
    parser.entities.sort(key=lambda entity: entity.offset)
    return tg_html.del_surrogate(clean), list(parser.entities)


def unparse(text: str, entities: Sequence[Any] | None, mode: str = "md") -> str:
    """Entities back into markup — for editing a message that is already formatted."""
    if not entities:
        return text
    if (mode or "md").lower() == "html":
        return tg_html.unparse(text, list(entities))
    return tg_markdown.unparse(text, list(entities), delimiters=DELIMITERS)


# ── rendering ────────────────────────────────────────────────────────────────
STYLES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strike",
}

SPOILER_CHAR = "░"


def render(
    text: str,
    entities: Sequence[Any] | None = None,
    colors: dict[str, str] | None = None,
    reveal_spoilers: bool = False,
) -> Any:
    """Rich Text with Telegram formatting applied — links stay clickable."""
    from rich.style import Style
    from rich.text import Text

    palette = colors or {}
    accent = palette.get("primary", "#2AABEE")
    muted = palette.get("text-muted", "#7E93A5")
    code_color = palette.get("warning", "#E5CA77")
    body = palette.get("foreground", "#E4EDF5")

    entities = list(entities or [])
    chars = list(text)
    spans: list[tuple[int, int, Any]] = []
    quotes: list[tuple[int, int]] = []

    for entity in entities:
        name = type(entity).__name__
        try:
            start, end = py_offsets(text, entity.offset, entity.length)
        except Exception:
            continue
        if start >= end or end > len(chars):
            continue
        if name in STYLES:
            spans.append((start, end, STYLES[name]))
        elif name == "MessageEntitySpoiler":
            if reveal_spoilers:
                spans.append((start, end, Style(color=body, bgcolor=palette.get("panel", "#22303C"))))
            else:
                for i in range(start, end):
                    if chars[i] != "\n":
                        chars[i] = SPOILER_CHAR
                spans.append((start, end, Style(color=muted)))
        elif name in {"MessageEntityCode", "MessageEntityPre"}:
            spans.append((start, end, Style(color=code_color)))
        elif name == "MessageEntityTextUrl":
            spans.append((start, end, Style(color=accent, underline=True, link=getattr(entity, "url", None))))
        elif name == "MessageEntityUrl":
            link = text[start:end]
            spans.append((start, end, Style(color=accent, underline=True, link=link)))
        elif name in {"MessageEntityMention", "MessageEntityMentionName", "MessageEntityHashtag",
                      "MessageEntityCashtag", "MessageEntityBotCommand", "MessageEntityEmail",
                      "MessageEntityPhone"}:
            spans.append((start, end, Style(color=accent)))
        elif name == "MessageEntityBlockquote":
            spans.append((start, end, Style(color=muted, italic=True)))
            quotes.append((start, end))

    rendered = Text("".join(chars), style=body)
    for start, end, style in spans:
        rendered.stylize(style, start, end)
    if quotes:
        rendered = _bar_quotes(rendered, "".join(chars), quotes, muted)
    return rendered


def _bar_quotes(rendered: Any, text: str, quotes: Iterable[tuple[int, int]], color: str) -> Any:
    """Draw a ▌ bar in front of every quoted line."""
    from rich.text import Text

    quoted_lines: set[int] = set()
    for start, end in quotes:
        if start > 0 and text[start - 1] != "\n":
            continue                     # an inline quote: style it, but don't bar the line
        first = text.count("\n", 0, start)
        last = text.count("\n", 0, max(start, end - 1))
        quoted_lines.update(range(first, last + 1))
    if not quoted_lines:
        return rendered
    lines = rendered.split("\n")
    out = []
    for index, line in enumerate(lines):
        if index in quoted_lines:
            bar = Text("▌ ", style=color)
            bar.append_text(line)
            out.append(bar)
        else:
            out.append(line)
    return Text("\n").join(out)


def preview(text: str, mode: str = "md", colors: dict[str, str] | None = None) -> Any:
    """What the message will look like once Telegram has it."""
    clean, entities = parse(text, mode)
    return render(clean, entities, colors=colors, reveal_spoilers=True)
