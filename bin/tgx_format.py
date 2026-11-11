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
def describe_date(entity: Any) -> str:
    """`MessageEntityFormattedDate` → строка по флагам, которые выбрал автор.

    Флаги независимы: можно попросить и дату, и время, и день недели сразу.
    Если не выбрано ничего, показываем дату и короткое время.
    """
    moment = getattr(entity, "date", None)
    if moment is None:
        return ""
    if getattr(entity, "relative", False):
        return moment.strftime("%d.%m.%Y %H:%M")      # относительное считает клиент
    parts = []
    if getattr(entity, "day_of_week", False):
        parts.append(("пн", "вт", "ср", "чт", "пт", "сб", "вс")[moment.weekday()])
    if getattr(entity, "long_date", False):
        parts.append(moment.strftime("%d %B %Y"))
    elif getattr(entity, "short_date", False):
        parts.append(moment.strftime("%d.%m.%Y"))
    if getattr(entity, "long_time", False):
        parts.append(moment.strftime("%H:%M:%S"))
    elif getattr(entity, "short_time", False):
        parts.append(moment.strftime("%H:%M"))
    return " ".join(parts) or moment.strftime("%d.%m.%Y %H:%M")



# ── служебные сообщения ──────────────────────────────────────────────────────
# Телеграм присылает их как сообщение без текста: вся суть лежит в поле
# `action`. Разбор идёт по имени типа, а не по самому типу, и это осознанно:
# действия прибавляются с каждым слоем MTProto, а requirements разрешают сразу
# две версии Telethon. Прямая ссылка на `types.MessageActionTodoCompletions`
# уронила бы импорт на той версии, где такого типа ещё нет, — по имени же новое
# действие просто попадает в общий случай и всё равно читается словами.


def _label(what: str, detail: Any = "") -> str:
    """[что] или [что: подробность] — без болтающихся двоеточий."""
    detail = "" if detail is None else str(detail).strip()
    return f"[{what}: {detail}]" if detail else f"[{what}]"


def _mmss(seconds: Any) -> str:
    """Длительность. С часами, иначе групповой звонок длится «60:00»."""
    total = int(seconds or 0)
    if not total:
        return ""
    if total >= 3600:
        return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def _period(seconds: Any) -> str:
    """Срок словами: сервер присылает его в секундах, а читать его человеку."""
    total = int(seconds or 0)
    if total <= 0:
        return ""
    for size, unit in ((86400, "д"), (3600, "ч"), (60, "мин")):
        if total >= size:
            return f"{total // size} {unit}"
    return f"{total} с"


def _money(currency: Any, amount: Any) -> str:
    """Сумма из минимальных единиц — в человеческую, с валютой.

    Какие валюты без дробной части, уже знает `tgx_pay`; импорт отложенный,
    чтобы модуль форматирования не тянул платежи ради одной строки.
    """
    import tgx_pay

    code = str(currency or "").upper()
    value = tgx_pay.major_units(code, amount or 0)
    if code == "XTR":
        return f"{value:g} звёзд"
    return f"{value:g} {code}".strip()


def _added(action: Any) -> str:
    count = len(getattr(action, "users", None) or [])
    return _label("в чат добавлено", count) if count > 1 else "[добавлен в чат]"


def _call(action: Any) -> str:
    kind = "видеозвонок" if getattr(action, "video", False) else "звонок"
    reason = type(getattr(action, "reason", None)).__name__
    if "Missed" in reason:
        return _label(kind, "пропущен")
    if "Busy" in reason:
        return _label(kind, "занято")
    return _label(kind, _mmss(getattr(action, "duration", 0)))


def _conference(action: Any) -> str:
    if getattr(action, "missed", False):
        return "[конференция пропущена]"
    return _label("конференция", _mmss(getattr(action, "duration", 0)))


def _scheduled(action: Any) -> str:
    when = getattr(action, "schedule_date", None)
    return _label("звонок назначен", when.strftime("%d.%m.%Y %H:%M") if when else "")


def _topic_edit(action: Any) -> str:
    closed = getattr(action, "closed", None)
    hidden = getattr(action, "hidden", None)
    title = getattr(action, "title", None)
    if closed is not None:
        return "[тема закрыта]" if closed else "[тема открыта]"
    if hidden is not None:
        return "[тема скрыта]" if hidden else "[тема показана]"
    if title:
        return _label("тема переименована", f"«{title}»")
    return "[тема изменена]"


def _ttl(action: Any) -> str:
    span = _period(getattr(action, "period", 0))
    return _label("таймер удаления", span) if span else "[таймер удаления выключен]"


def _star_gift(action: Any) -> str:
    if getattr(action, "refunded", False):
        return "[подарок возвращён]"
    stars = getattr(getattr(action, "gift", None), "stars", None)
    return _label("подарок", f"{stars} звёзд" if stars else "")


def _unique_gift(action: Any) -> str:
    gift = getattr(action, "gift", None)
    title = getattr(gift, "title", None)
    number = getattr(gift, "num", None)
    detail = f"«{title}» #{number}" if title and number else (title or "")
    return _label("уникальный подарок", detail)


def _suggested_post(action: Any) -> str:
    if getattr(action, "rejected", False):
        comment = getattr(action, "reject_comment", None)
        return _label("предложенный пост отклонён", comment or "")
    return "[предложенный пост одобрен]"


# Имя типа без префикса MessageAction → что показать. Значение либо готовая
# строка, либо функция от действия, когда в нём есть что показать подробнее.
SERVICE_ACTIONS: dict[str, Any] = {
    # чат и его жизнь
    "ChatCreate": lambda a: _label("группа создана", f"«{a.title}»" if getattr(a, "title", None) else ""),
    "ChannelCreate": lambda a: _label("канал создан", f"«{a.title}»" if getattr(a, "title", None) else ""),
    "ChatEditTitle": lambda a: _label("название", f"«{getattr(a, 'title', '')}»"),
    "ChatEditPhoto": "[фото чата обновлено]",
    "ChatDeletePhoto": "[фото чата удалено]",
    "ChatAddUser": _added,
    "ChatDeleteUser": "[удалён из чата]",
    "ChatJoinedByLink": "[вступил по ссылке]",
    "ChatJoinedByRequest": "[вступил по заявке]",
    "ChatMigrateTo": "[группа стала супергруппой]",
    "ChannelMigrateFrom": lambda a: _label("выросла из группы", f"«{getattr(a, 'title', '')}»"),
    "ChangeCreator": "[сменился владелец]",
    "NewCreatorPending": "[передача владения ждёт подтверждения]",
    "PinMessage": "[закреплено сообщение]",
    "HistoryClear": "[история очищена]",
    "ScreenshotTaken": "[сделан снимок экрана]",
    "ContactSignUp": "[зарегистрировался в Telegram]",
    "CustomAction": lambda a: _label(getattr(a, "message", "") or "служебное сообщение"),
    "NoForwardsToggle": lambda a: "[пересылка запрещена]" if getattr(a, "new_value", False) else "[пересылка разрешена]",
    "NoForwardsRequest": "[просят запретить пересылку]",
    "ManagedBotCreated": "[создан управляемый бот]",
    # звонки
    "PhoneCall": _call,
    "GroupCall": lambda a: _label("групповой звонок", _mmss(getattr(a, "duration", 0))),
    "GroupCallScheduled": _scheduled,
    "InviteToGroupCall": lambda a: _label("приглашение в звонок", len(getattr(a, "users", None) or []) or ""),
    "ConferenceCall": _conference,
    # темы форума
    "TopicCreate": lambda a: _label("тема создана", f"«{getattr(a, 'title', '')}»"),
    "TopicEdit": _topic_edit,
    # деньги, звёзды, подарки
    "PaymentSent": lambda a: _label("оплачено", _money(getattr(a, "currency", ""), getattr(a, "total_amount", 0))),
    "PaymentSentMe": lambda a: _label("оплата получена", _money(getattr(a, "currency", ""), getattr(a, "total_amount", 0))),
    "PaymentRefunded": lambda a: _label("возврат платежа", _money(getattr(a, "currency", ""), getattr(a, "total_amount", 0))),
    "PaidMessagesPrice": lambda a: _label("платные сообщения", f"{getattr(a, 'stars', 0)} звёзд"),
    "PaidMessagesRefunded": lambda a: _label("возврат за платные сообщения", f"{getattr(a, 'stars', 0)} звёзд"),
    "GiftPremium": lambda a: _label("подарен Premium", f"{getattr(a, 'days', 0)} дн." if getattr(a, "days", None) else ""),
    "GiftCode": lambda a: _label("подарочный код Premium", f"{getattr(a, 'days', 0)} дн." if getattr(a, "days", None) else ""),
    "GiftStars": lambda a: _label("подарено звёзд", getattr(a, "stars", "") or ""),
    "GiftTon": "[подарен TON]",
    "PrizeStars": lambda a: _label("приз", f"{getattr(a, 'stars', 0)} звёзд"),
    "StarGift": _star_gift,
    "StarGiftUnique": _unique_gift,
    "StarGiftPurchaseOffer": "[предложение выкупить подарок]",
    "StarGiftPurchaseOfferDeclined": "[предложение о подарке отклонено]",
    "GiveawayLaunch": "[розыгрыш начался]",
    "GiveawayResults": lambda a: _label("розыгрыш окончен", f"победителей {getattr(a, 'winners_count', 0)}"),
    "BoostApply": lambda a: _label("бустов", getattr(a, "boosts", "") or ""),
    # оформление и приватность
    "SetMessagesTTL": _ttl,
    "SetChatTheme": lambda a: _label("тема оформления", getattr(a, "theme", "")) if getattr(a, "theme", "") else "[тема оформления сброшена]",
    "SetChatWallPaper": "[обои чата]",
    # боты и мини-приложения
    "BotAllowed": lambda a: _label("бот допущен", getattr(a, "domain", "") or getattr(getattr(a, "app", None), "title", "") or ""),
    "WebViewDataSent": lambda a: _label("данные из мини-приложения", getattr(a, "text", "")),
    "WebViewDataSentMe": lambda a: _label("данные из мини-приложения", getattr(a, "text", "")),
    # Сообщества — слой 229. На 227 таких типов нет, и записи просто не находят
    # себе действия: таблица по именам этим и хороша.
    "ChangeCommunity": "[сменилось сообщество]",
    "ChatJoinedViaCommunity": "[вступил через сообщество]",
    "RequestedPeer": "[передан выбранный чат]",
    "RequestedPeerSentMe": "[передан выбранный чат]",
    "SecureValuesSent": "[переданы документы]",
    "SecureValuesSentMe": "[переданы документы]",
    # прочее
    "GameScore": lambda a: _label("счёт в игре", getattr(a, "score", "") or ""),
    "GeoProximityReached": lambda a: _label("рядом", f"{getattr(a, 'distance', 0)} м"),
    "SuggestProfilePhoto": "[предложено фото профиля]",
    "SuggestBirthday": "[предложено указать день рождения]",
    "TodoAppendTasks": lambda a: _label("в список добавлено задач", len(getattr(a, "list", None) or []) or ""),
    "TodoCompletions": lambda a: _label("отмечено выполненным", len(getattr(a, "completed", None) or []) or ""),
    "PollAppendAnswer": "[в опрос добавлен вариант]",
    "PollDeleteAnswer": "[из опроса убран вариант]",
    "SuggestedPostApproval": _suggested_post,
    "SuggestedPostRefund": "[возврат за предложенный пост]",
    "SuggestedPostSuccess": "[предложенный пост опубликован]",
    "Empty": "[служебное сообщение]",
}


def _spaced(name: str) -> str:
    """`TodoCompletions` → `todo completions`. Запасной вид для нового действия."""
    return "".join(f" {c.lower()}" if c.isupper() else c for c in name).strip()


def service_label(action: Any) -> str:
    """Служебное сообщение одной строкой.

    У такого сообщения нет ни текста, ни вложения, поэтому в ленте оно было
    пустой строкой: кто вступил, что закрепили, за что списали звёзды — всё
    одинаково никак, а в выгрузке на его месте оставалась дырка. Описываем
    словами, как и вложение, и никогда не возвращаем пустоту: действие, которого
    этот Telethon ещё не знает, читается хотя бы именем.
    """
    if action is None:
        return ""
    name = type(action).__name__.replace("MessageAction", "")
    known = SERVICE_ACTIONS.get(name)
    try:
        if callable(known):
            return known(action) or _label(_spaced(name))
        if known:
            return known
    except Exception:
        pass                                   # подробность не вышла — хватит имени
    return _label(_spaced(name) or "служебное сообщение")


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
    collapsed_marks: list[int] = []

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
            if getattr(entity, "collapsed", False):
                # Раскрывающаяся цитата (Bot API 7.4): в терминале раскрывать
                # нечего, но свернутость стоит показать — иначе длинная врезка
                # выглядит как обычный текст, которым автор её не считал.
                collapsed_marks.append(start)
        elif name == "MessageEntityFormattedDate":
            # Bot API зовёт это date_time: сервер хранит момент времени, а клиент
            # показывает его по-своему. В терминале подставляем сам момент —
            # иначе на месте даты остаётся текст, написанный автором когда-то.
            shown = describe_date(entity)
            if shown:
                chars[start:end] = list(shown.ljust(end - start)[: end - start]) \
                    if len(shown) <= end - start else list(shown)
            spans.append((start, min(end, len(chars)), Style(color=accent)))

    rendered = Text("".join(chars), style=body)
    for start, end, style in spans:
        rendered.stylize(style, start, end)
    if quotes:
        rendered = _bar_quotes(rendered, "".join(chars), quotes, muted,
                               collapsed=set(collapsed_marks))
    return rendered


def _bar_quotes(rendered: Any, text: str, quotes: Iterable[tuple[int, int]], color: str,
                collapsed: set[int] | None = None) -> Any:
    """Draw a ▌ bar in front of every quoted line; ▾ marks a collapsible one."""
    from rich.text import Text

    quoted_lines: set[int] = set()
    collapsible_lines: set[int] = set()
    for start, end in quotes:
        if start > 0 and text[start - 1] != "\n":
            continue                     # an inline quote: style it, but don't bar the line
        first = text.count("\n", 0, start)
        last = text.count("\n", 0, max(start, end - 1))
        quoted_lines.update(range(first, last + 1))
        if collapsed and start in collapsed:
            collapsible_lines.add(first)
    if not quoted_lines:
        return rendered
    lines = rendered.split("\n")
    out = []
    for index, line in enumerate(lines):
        if index in quoted_lines:
            bar = Text("▾ " if index in collapsible_lines else "▌ ", style=color)
            bar.append_text(line)
            out.append(bar)
        else:
            out.append(line)
    return Text("\n").join(out)


def preview(text: str, mode: str = "md", colors: dict[str, str] | None = None) -> Any:
    """What the message will look like once Telegram has it."""
    clean, entities = parse(text, mode)
    return render(clean, entities, colors=colors, reveal_spoilers=True)
