#!/usr/bin/env python3
"""Headless self-test for the tgx TUI: drives the demo backend and saves screenshots.

    .venv/bin/python bin/tgx_smoke.py [out_dir]

Exits non-zero on the first failed expectation, so it works as a CI gate.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.widgets import Checkbox, Input, Select, TextArea  # noqa: E402

from tgx_tui import READ_DWELL, ChatList, DemoBackend, MessageList, TgxApp, plain_task_factory  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tgx-shots")
checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    checks.append((label, bool(ok)))
    print(f"{'✓' if ok else '✗'} {label}")


def formatting_regression() -> None:
    """Entity offsets are counted in UTF-16 units — one emoji before a bold run is
    enough to shift everything if the conversion is wrong."""
    import tgx_format

    text, entities = tgx_format.parse("обычный 🎉 **после эмодзи**")
    bold = next(e for e in entities if type(e).__name__ == "MessageEntityBold")
    start, end = tgx_format.py_offsets(text, bold.offset, bold.length)
    check("bold survives an emoji before it", text[start:end] == "после эмодзи")

    text, entities = tgx_format.parse("**жир** ||секрет|| --подч-- `код`\n> цитата\n> вторая")
    kinds = {type(e).__name__[len("MessageEntity"):] for e in entities}
    check("markdown covers bold/spoiler/underline/code/quote",
          {"Bold", "Spoiler", "Underline", "Code", "Blockquote"} <= kinds)
    quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
    start, end = tgx_format.py_offsets(text, quote.offset, quote.length)
    check("consecutive quoted lines merge into one blockquote", text[start:end] == "цитата\nвторая")

    text, entities = tgx_format.parse('<b>ж</b> <tg-spoiler>секрет 🎉</tg-spoiler>', "html")
    spoiler = next((e for e in entities if type(e).__name__ == "MessageEntitySpoiler"), None)
    check("html spoilers are parsed", spoiler is not None)
    if spoiler is not None:
        start, end = tgx_format.py_offsets(text, spoiler.offset, spoiler.length)
        check("html spoiler covers the right characters", text[start:end] == "секрет 🎉")

    check("plain mode leaves the markup alone", tgx_format.parse("**нетронуто**", "none") == ("**нетронуто**", []))

    hidden = tgx_format.render(*tgx_format.parse("||тайна||"))
    check("spoilers are masked until revealed", "тайна" not in str(hidden) and "░" in str(hidden))
    shown = tgx_format.render(*tgx_format.parse("||тайна||"), reveal_spoilers=True)
    check("and readable once revealed", "тайна" in str(shown))

    roundtrip = tgx_format.unparse(*tgx_format.parse("**жир** ||сек||"))
    check("markup survives a round trip", roundtrip == "**жир** ||сек||")


def mcp_connector_regression() -> None:
    """The agent connector must stay read-only until the user opts in, must honour
    the peer allowlist, and must never hand Telethon objects to a model."""
    from mcp.server.mcpserver.exceptions import ToolError

    import tgx_mcp
    from tgx_tui import Chat

    saved_mode, saved_peers = tgx_mcp.READ_ONLY, set(tgx_mcp.ALLOWED_PEERS)
    try:
        tgx_mcp.READ_ONLY = True
        blocked = False
        try:
            tgx_mcp._check_writes()
        except tgx_mcp.TgxError:
            blocked = True
        check("connector refuses writes by default", blocked)
        check("its errors reach the model, not a generic crash", issubclass(tgx_mcp.TgxError, ToolError))

        tgx_mcp.READ_ONLY = False
        tgx_mcp._check_writes()          # opted in: must not raise
        check("writes work once the user enables them", True)

        tgx_mcp.ALLOWED_PEERS = {"allowed"}
        denied = False
        try:
            tgx_mcp._check_peer(Chat(id=1, name="Другой", username="other"))
        except tgx_mcp.TgxError:
            denied = True
        check("peer allowlist keeps agents out of other chats", denied)
        tgx_mcp._check_peer(Chat(id=2, name="ok", username="allowed"))

        exported = tgx_mcp.chat_json(Chat(id=3, name="x", entity=object(), input_entity=object()))
        check("no Telethon objects leak into tool output",
              "entity" not in exported and "input_entity" not in exported)
        check("limits are clamped", tgx_mcp._limit(10_000) == tgx_mcp.MAX_LIMIT and tgx_mcp._limit(0) == 30)
    finally:
        tgx_mcp.READ_ONLY, tgx_mcp.ALLOWED_PEERS = saved_mode, saved_peers


class _FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str, buttons: list[list[_FakeButton]] | None = None) -> None:
        self.message = text
        self.buttons = buttons or []
        self.clicked: tuple[int, int] | None = None

    async def click(self, row: int, col: int) -> None:
        self.clicked = (row, col)


class _FakeConversation:
    """Stands in for @BotFather so the fragile dialogue can be tested offline."""

    def __init__(self, script: list[_FakeMessage]) -> None:
        self.script = list(script)
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def get_response(self, timeout: float | None = None) -> _FakeMessage:
        if not self.script:
            raise asyncio.TimeoutError
        return self.script.pop(0)

    async def __aenter__(self) -> "_FakeConversation":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, script: list[_FakeMessage]) -> None:
        self.conv = _FakeConversation(script)

    def conversation(self, *args: object, **kwargs: object) -> _FakeConversation:
        return self.conv


async def bots_regression() -> None:
    """Token storage, button syntax, and every step of the BotFather conversation."""
    import tgx_bots

    # registry: tokens are credentials
    store = tgx_bots.Registry(OUT / "bots-probe.json")
    store.save({})
    store.add(tgx_bots.Bot(username="probe_bot", name="Проба", token="123456789:AAE-secret-value-0123456789abcdef"))
    mode = oct(store.path.stat().st_mode)[-3:]
    check("bot registry is written 0600", mode == "600")
    check("listing masks the token", "secret" not in store.get("probe_bot").public()["token"])
    check("revealing is explicit", store.get("probe_bot").public(reveal=True)["token"].endswith("abcdef"))
    check("forgetting a bot works", store.remove("probe_bot") and not store.load())

    # buttons, including Mini Apps
    rows = tgx_bots.parse_buttons("Сайт=https://x.io, Приложение=webapp:https://app.x.io ; Копия=copy:промокод")
    kinds = [type(button).__name__ for row in rows for button in row]
    check("link and web-app buttons are built",
          kinds == ["KeyboardButtonUrl", "KeyboardButtonWebView", "KeyboardButtonCopy"])
    for bad, why in (("ПлохоеПриложение=webapp:http://insecure", "web app over http"),
                     ("Профиль=user:абв", "non-numeric profile id"),
                     ("Просто текст", "a button with no target")):
        refused = False
        try:
            tgx_bots.parse_buttons(bad)
        except tgx_bots.BotError:
            refused = True
        check(f"refuses {why}", refused)

    # name / about / description / commands go through the plain API now, so the
    # command list has to be parsed and validated locally
    parsed = tgx_bots.BotSession.parse_commands("start - Запустить\n/help - Справка")
    check("bot commands are parsed", [(c.command, c.description) for c in parsed]
          == [("start", "Запустить"), ("help", "Справка")])
    for bad, why in (("простоТекст", "a line without a description"),
                     ("плохое имя - описание", "a command name with a space"),
                     ("", "an empty list")):
        refused = False
        try:
            tgx_bots.BotSession.parse_commands(bad)
        except tgx_bots.BotError:
            refused = True
        check(f"refuses {why}", refused)

    # BotFather: the happy path
    client = _FakeClient([
        _FakeMessage("Alright, a new bot. How are we going to call it?"),
        _FakeMessage("Good. Now let's choose a username for your bot."),
        _FakeMessage("Done! Use this token to access the HTTP API:\n"
                     "123456789:AAE-ExampleTokenValue_0123456789abcd\nKeep it secure."),
    ])
    created = await tgx_bots.BotFather(client).create("Мой бот", "my_probe_bot")
    check("token is picked out of BotFather's reply",
          created.token == "123456789:AAE-ExampleTokenValue_0123456789abcd")
    check("the dialogue sent the right things", client.conv.sent == ["/newbot", "Мой бот", "my_probe_bot"])

    # BotFather: a refusal must surface its own wording, not a generic failure
    refusing = _FakeClient([
        _FakeMessage("Alright, a new bot. How are we going to call it?"),
        _FakeMessage("Good. Now let's choose a username for your bot."),
        _FakeMessage("Sorry, this username is already taken. Try something different."),
    ])
    message = ""
    try:
        await tgx_bots.BotFather(refusing).create("Мой бот", "taken_probe_bot")
    except tgx_bots.BotError as exc:
        message = str(exc)
    check("BotFather's refusal is quoted back", "already taken" in message)

    rejected = False
    try:
        await tgx_bots.BotFather(_FakeClient([])).create("Имя", "not_ending_in_b0t")
    except tgx_bots.BotError:
        rejected = True
    check("a username that cannot work is refused before sending anything", rejected)

    # picking a bot from BotFather's keyboard
    chooser = _FakeMessage("Choose a bot", [[_FakeButton("@one_bot"), _FakeButton("@two_bot")]])
    picker = _FakeClient([_FakeMessage("Token for @two_bot: 987654321:BBB-another-token-value-012345")])
    father = tgx_bots.BotFather(picker)
    await father._pick(picker.conv, chooser, "two_bot")
    check("the right bot button is clicked", chooser.clicked == (0, 1))
    missing = ""
    try:
        await father._pick(picker.conv, chooser, "absent_bot")
    except tgx_bots.BotError as exc:
        missing = str(exc)
    check("an unknown bot lists what is available", "one_bot" in missing and "two_bot" in missing)


def rich_message_regression() -> None:
    """Rich messages (Bot API 10.1) go out over HTTP with a bot token, so the payload
    is built here — and the documented limits are checked before the request."""
    import tgx_rich

    class FakeChat:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    check("a public channel is addressed by @username",
          tgx_rich.bot_chat_id(FakeChat(username="news", id=1, kind="channel")) == "@news")
    check("a private channel gets the -100 prefix back",
          tgx_rich.bot_chat_id(FakeChat(username="", id=1847120914, kind="channel")) == "-1001847120914")
    check("a person keeps a bare id",
          tgx_rich.bot_chat_id(FakeChat(username="", id=778899, kind="user")) == "778899")

    keyboard = tgx_rich.buttons_json("Скачать=https://x.io ; Открыть=webapp:https://app.x.io, Копия=copy:код")
    check("buttons become Bot API json",
          keyboard == {"inline_keyboard": [
              [{"text": "Скачать", "url": "https://x.io"}],
              [{"text": "Открыть", "web_app": {"url": "https://app.x.io"}},
               {"text": "Копия", "copy_text": {"text": "код"}}]]})

    for markdown, why in (("x" * (tgx_rich.MAX_CHARS + 1), "text over the character limit"),
                          ("| a |" + " b |" * 25, "a table wider than 20 columns"),
                          ("   ", "an empty message")):
        refused = False
        try:
            tgx_rich.check_limits(markdown)
        except tgx_rich.RichError:
            refused = True
        check(f"refuses {why}", refused)

    check("media references are found in the text",
          tgx_rich.media_ids("![](tg://photo?id=cover) ![](tg://video?id=demo) ![](tg://photo?id=cover)")
          == ["cover", "demo"])

    sent: list[tuple[str, dict]] = []
    original = tgx_rich.call
    tgx_rich.call = lambda token, method, payload: sent.append((method, payload)) or {"message_id": 7}
    try:
        tgx_rich.send_rich("123:TOKEN", "@news", "# Заголовок\n\n- [x] сделано",
                           buttons="Сайт=https://x.io", silent=True, reply_to=42)
        method, payload = sent[-1]
        check("it calls sendRichMessage", method == "sendRichMessage")
        check("markdown travels inside rich_message",
              payload["rich_message"]["markdown"].startswith("# Заголовок"))
        check("options land where the API expects them",
              payload["chat_id"] == "@news" and payload["disable_notification"] is True
              and payload["reply_parameters"] == {"message_id": 42}
              and payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://x.io")

        sent.clear()
        tgx_rich.send_rich("123:TOKEN", "@news", "текст", draft=True)
        check("a draft uses the streaming method", sent[-1][0] == "sendRichMessageDraft")

        clashed = False
        try:
            tgx_rich.send_rich("123:TOKEN", "@news", "текст", html="<b>тоже</b>")
        except tgx_rich.RichError:
            clashed = True
        check("markdown and html cannot be given together", clashed)
    finally:
        tgx_rich.call = original


async def business_regression() -> None:
    """Connecting a bot to your private chats is a permission grant — the flags and
    the schedule have to be parsed exactly, and refused when they are not understood."""
    import tgx_business

    rights = tgx_business.parse_rights("reply,read_messages")
    check("named rights are set and the rest cleared",
          rights["reply"] and rights["read_messages"] and not rights["edit_username"])
    check("all means all", all(tgx_business.parse_rights("all").values()))
    check("none means none", not any(tgx_business.parse_rights("none").values()))
    refused = False
    try:
        tgx_business.parse_rights("reply,вседозволенность")
    except tgx_business.BusinessError:
        refused = True
    check("an unknown right is refused with the list of valid ones", refused)

    scope = tgx_business.parse_scope("contacts")
    check("scope limits the bot to contacts",
          scope["contacts"] and not scope["non_contacts"] and not scope["new_chats"])

    hours = tgx_business.parse_hours("пн-пт 9:00-18:00; сб 10:00-14:00")
    check("a weekly schedule expands to one range per day", len(hours) == 6)
    check("minutes are counted from Monday midnight", hours[0] == (9 * 60, 18 * 60))
    check("Saturday lands on the sixth day", hours[-1][0] == 5 * 24 * 60 + 10 * 60)
    check("it reads back as human text", "сб 10:00–14:00" in tgx_business.describe_hours(hours))
    for bad, why in (("пн 25:00-26:00", "hours outside a day"),
                     ("абв 9:00-10:00", "an unknown weekday"),
                     ("пн 18:00-9:00", "an end before the start")):
        refused = False
        try:
            tgx_business.parse_hours(bad)
        except tgx_business.BusinessError:
            refused = True
        check(f"refuses {why}", refused)

    # `exclude_selected` puts the excluded users in `users`, not in `exclude_users`
    # — reading the wrong field reported "no exclusions" for an account with 26.
    excluding = {"existing_chats": False, "new_chats": False, "contacts": False,
                 "non_contacts": False, "exclude_selected": True}
    check("an excluding scope is described as all-but-N",
          tgx_business.describe_scope(excluding, [], list(range(26))) == "все чаты, кроме 26")
    everything = {"existing_chats": True, "new_chats": True, "contacts": True,
                  "non_contacts": True, "exclude_selected": False}
    check("all four categories read as every chat",
          tgx_business.describe_scope(everything) == "все чаты")
    check("a partial scope names its categories",
          tgx_business.describe_scope({**everything, "contacts": False, "new_chats": False})
          == "существующие, не-контакты")

    class OneBot:
        """Telegram keeps a single connected bot — connecting a second displaces it."""
        async def get_input_entity(self, who):
            return type("U", (), {"user_id": 111})()

        async def __call__(self, request):
            raise AssertionError("connect must refuse before touching the account")

    biz = tgx_business.Business(OneBot())
    biz.connected_bots = lambda: _resolved([{"bot_id": 999, "rights": [], "recipients": {}}])
    biz._name = lambda bot_id: _resolved("@incumbent")
    refused = ""
    try:
        await biz.connect("@newcomer", tgx_business.parse_rights("reply"),
                          tgx_business.parse_scope("all"))
    except tgx_business.BusinessError as exc:
        refused = str(exc)
    check("connecting over an existing bot is refused by name", "@incumbent" in refused)
    check("the refusal points at the way through", "--replace" in refused)

    # The rollback exists because a --replace silently disconnects the incumbent;
    # it is worth nothing unless it reconstructs the connection exactly.
    with tempfile.TemporaryDirectory() as home:
        os.environ["TGX_HOME"] = home
        original = {
            "bot_id": 7982180734, "bot_username": "incumbent", "bot_access_hash": 4242,
            "rights": {name: name in {"reply", "read_messages", "manage_stories"}
                       for name, _ in tgx_business.RIGHTS},
            "flags": {"existing_chats": False, "new_chats": False, "contacts": False,
                      "non_contacts": False, "exclude_selected": True},
            "users": [[i, i * 7] for i in range(1000, 1026)], "exclude_users": [],
        }
        sent = {}

        class Recorder:
            async def __call__(self, request):
                sent["request"] = request

        biz = tgx_business.Business(Recorder())
        saved = biz._save_rollback(original)
        check("the rollback file is owner-only", oct(saved.stat().st_mode)[-3:] == "600")
        check("it survives a round trip through disk",
              json.loads(saved.read_text()) == original)

        result = await biz.restore()
        request = sent["request"]
        check("restore names the bot it brings back", result["restored"] == "@incumbent")
        check("restore reconnects rather than deletes", request.deleted is False)
        check("restore keeps the bot's access hash", request.bot.access_hash == 4242)
        granted = request.rights
        check("restore returns every right that was granted",
              granted.reply and granted.read_messages and granted.manage_stories)
        check("restore leaves ungranted rights off", not granted.edit_username)
        check("restore keeps all 26 excluded users", len(request.recipients.users) == 26)
        check("restore keeps the exclusion flag", request.recipients.exclude_selected is True)
        os.environ.pop("TGX_HOME", None)


async def _resolved(value):
    return value


def appearance_regression() -> None:
    """Аватар — четыре разных формата за одним аргументом. Перепутать их нельзя:
    видео уйдёт файлом, а эмодзи — мусорным id, и всё это заметно только в чужом
    профиле, поэтому разбор проверяется здесь."""
    import tgx_banner
    import tgx_profile

    with tempfile.TemporaryDirectory() as folder:
        picture = Path(folder) / "avatar.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n")
        clip = Path(folder) / "avatar.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")

        check("картинка распознаётся как статичный аватар",
              tgx_profile.parse_avatar(str(picture)).kind == "image")
        video = tgx_profile.parse_avatar(str(clip), start=2.5)
        check("видео распознаётся как видеоаватар", video.kind == "video")
        check("кадр обложки доезжает до запроса", video.start == 2.5)

        refused = ""
        try:
            tgx_profile.parse_avatar(str(picture), start=1)
        except tgx_profile.ProfileError as exc:
            refused = str(exc)
        check("--start у картинки отвергается", "видеоаватар" in refused)

        for bad, why in ((str(Path(folder) / "нет.png"), "несуществующий файл"),
                         (str(Path(folder) / "avatar.txt"), "неподходящее расширение"),
                         ("emoji:кот", "нечисловой id эмодзи"),
                         ("sticker:набор", "стикер без id"), ("", "пустой источник")):
            if bad.endswith(".txt"):
                Path(bad).write_text("не картинка")
            refused = False
            try:
                tgx_profile.parse_avatar(bad)
            except tgx_profile.ProfileError:
                refused = True
            check(f"отказывает: {why}", refused)

    emoji = tgx_profile.parse_avatar("emoji:5366316836101038579", colors="#e8a,ff00aa")
    check("эмодзи-аватар несёт id", emoji.emoji_id == 5366316836101038579)
    check("трёхзначный цвет разворачивается в шестизначный", emoji.colors[0] == 0xEE88AA)
    sticker = tgx_profile.parse_avatar("sticker:мой_набор:42")
    check("стикер-аватар помнит набор и номер",
          sticker.stickerset == "мой_набор" and sticker.sticker_id == 42)

    for bad, why in (("#ff00aa,#00ff00,#0000ff,#ffffff,#000000", "пять цветов"),
                     ("не-цвет", "не HEX")):
        refused = False
        try:
            tgx_profile.parse_colors(bad)
        except tgx_profile.ProfileError:
            refused = True
        check(f"градиент отвергает {why}", refused)

    check("дата без года", tgx_profile.parse_birthday("14.03") == (14, 3, None))
    check("дата с годом", tgx_profile.parse_birthday("14.03.1990") == (14, 3, 1990))
    check("дата в ISO", tgx_profile.parse_birthday("1990-03-14") == (14, 3, 1990))
    for bad, why in (("32.01", "несуществующий день"), ("14.13", "несуществующий месяц"),
                     ("14.03.1200", "год-опечатка"), ("вчера", "не дата")):
        refused = False
        try:
            tgx_profile.parse_birthday(bad)
        except tgx_profile.ProfileError:
            refused = True
        check(f"дата отвергает {why}", refused)

    # Баннер: цвета терминала и обрезка пустых полей
    check("именованный цвет ANSI разворачивается",
          tgx_banner._colour("brightcyan", "#000000") == "#B8EEFF")
    check("24-битный цвет проходит как есть",
          tgx_banner._colour("229ED9", "#000000") == "#229ED9")
    check("цвет по умолчанию берёт запасной",
          tgx_banner._colour("default", "#123456") == "#123456")

    blank = [[(" ", "#fff")] * 10 for _ in range(6)]
    framed = [[(" ", "#fff")] * 10 for _ in range(6)]
    framed[3][4] = ("█", "#229ED9")
    with tempfile.TemporaryDirectory() as folder:
        images = tgx_banner.draw([blank, framed], Path(folder), size=64, font_size=12)
        check("кадры рисуются все", len(images) == 2)
        from PIL import Image
        with Image.open(images[1]) as picture:
            check("кадр укладывается в квадрат", picture.size == (64, 64))
            colours = {picture.getpixel((x, y)) for x in range(64) for y in range(64)}
            check("залитый блок виден на кадре", len(colours) > 1)


def autotools_regression() -> None:
    """Инструменты MCP теперь не пишутся руками, а выводятся из CLI. Значит,
    сломать их можно, ничего не трогая в MCP, — поэтому проверяется сам вывод."""
    import inspect

    import tgx
    import tgx_autotools

    async def runner(path, picks, values):
        return {"path": path, "picks": picks, "values": values}

    tools = tgx_autotools.build(tgx.build_parser, runner)
    names = {t["name"]: t for t in tools}
    check("из CLI выведено много инструментов", len(tools) > 80)
    check("имена уникальны", len(names) == len(tools))
    check("вложенная команда становится инструментом", "cli_profile_photo" in names)
    check("бизнес-режим тоже выведен", "cli_business_connect" in names)
    check("заставка в аватар выведена", "cli_profile_banner" in names)

    for interactive in ("cli_ui", "cli_auth"):
        check(f"{interactive} не предлагается агенту", interactive not in names)

    signature = inspect.signature(names["cli_profile_photo"]["function"])
    check("обязательный позиционный аргумент остаётся обязательным",
          signature.parameters["source"].default is inspect.Parameter.empty)
    check("необязательный флаг получает значение по умолчанию",
          signature.parameters["suggest"].default is False)
    check("число остаётся числом",
          signature.parameters["start"].annotation == "float | None")
    check("вывод в jsonl из схемы убран", "jsonl" not in signature.parameters)

    description = names["cli_business_connect"]["description"]
    check("описание берёт человеческую подсказку команды",
          "вытеснил" in names["cli_business_restore"]["description"]
          or "подключить" in description)
    check("описание объясняет аргументы",
          "replace: отключить уже подключённого бота" in description)
    check("описание перечисляет допустимые значения",
          "(all, contacts, non-contacts, existing, new)" in description)

    # `profile photos` и плоский псевдоним `profile-photos` дают одно имя:
    # побеждает вложенная форма, иначе один из инструментов терялся молча.
    check("столкновение имён разрешается в пользу вложенной команды",
          names["cli_profile_photos"]["path"] == ("profile", "photos"))

    check("одиночный JSON разбирается", tgx_autotools.parse('{"ok": true}') == {"ok": True})
    check("строки JSONL собираются в список",
          tgx_autotools.parse('{"a": 1}\n{"a": 2}') == [{"a": 1}, {"a": 2}])
    check("обычный текст отдаётся как есть",
          tgx_autotools.parse("просто текст") == {"output": "просто текст"})
    check("пустой вывод — это успех", tgx_autotools.parse("  ") == {"ok": True})


async def forum_regression() -> None:
    """Тема — тред служебного сообщения, а не папка, и у сервера на этот счёт свои
    правила. Их легко нарушить молча, поэтому они проверяются здесь."""
    import tgx_forum

    check("«Общая» тема — это id 1", tgx_forum.GENERAL_ID == 1)
    check("цвет по названию", tgx_forum.parse_color("зелёный") == 0x8EEE98)
    check("цвет по-английски", tgx_forum.parse_color("green") == 0x8EEE98)
    check("цвет кодом", tgx_forum.parse_color("8EEE98") == 0x8EEE98)
    check("пустой цвет — это отсутствие цвета", tgx_forum.parse_color(None) is None)
    for bad, why in (("бирюзовый", "цвет не из палитры"), ("123456", "код вне шести разрешённых")):
        refused = False
        try:
            tgx_forum.parse_color(bad)
        except tgx_forum.ForumError:
            refused = True
        check(f"отвергает {why}", refused)

    class Topic:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    row = tgx_forum.topic_row(Topic(id=7, title="Релизы", closed=True, pinned=False,
                                    hidden=False, unread_count=3, icon_color=0x8EEE98,
                                    date=None, my=True))
    check("тема разбирается в плоскую запись", row["title"] == "Релизы" and row["unread"] == 3)
    check("обычная тема не помечена общей", row["general"] is False)
    check("живая тема не помечена удалённой", row["deleted"] is False)

    # forumTopicDeleted не имеет ни названия, ни флагов — только id.
    gone = tgx_forum.topic_row(Topic(id=7))
    check("удалённая тема распознаётся по отсутствию названия", gone == {"id": 7, "deleted": True})
    check("«Общая» помечается как общая", tgx_forum.topic_row(Topic(id=1, title="General"))["general"])

    calls = []

    class Server:
        """Считает запросы и отвечает так же, как сервер на нарушение правил."""
        async def get_input_entity(self, peer):
            return peer

        async def __call__(self, request):
            calls.append(type(request).__name__)
            return None

    forum = tgx_forum.Forum(Server())

    for coro, why, marker in (
            (forum.delete("g", 1), "удаление «Общей» темы", "удалить нельзя"),
            (forum.edit("g", 5, hidden=True), "скрытие обычной темы", "только «Общую»"),
            (forum.edit("g", 5), "правку без единого изменения", "нечего менять"),
            (forum.create("g", "  "), "тему без названия", "название"),
            (forum.create("g", "t", color="зелёный", icon_emoji_id=1), "две иконки сразу", "либо"),
            (forum.reorder("g", []), "порядок из пустого списка", "перечислите")):
        refused = ""
        try:
            await coro
        except tgx_forum.ForumError as exc:
            refused = str(exc)
        check(f"отвергает {why}", marker in refused)

    # Сервер отвечает TOPIC_CLOSE_SEPARATELY на попытку сделать это одним запросом.
    calls.clear()
    await forum.edit("g", 5, title="Архив", closed=True)
    check("переименование и закрытие уходят двумя запросами", len(calls) == 2)
    calls.clear()
    await forum.edit("g", 5, title="Архив")
    check("одна правка — один запрос", len(calls) == 1)

    check("«Общую» скрыть можно",
          (await forum.edit("g", tgx_forum.GENERAL_ID, hidden=True))["hidden"] is True)


async def transcribe_regression() -> None:
    """Готовый текст расшифровки приходит отдельным апдейтом. Если подписаться на
    него после запроса, быстрый ответ проскочит мимо и команда молча вернёт
    пустую строку — поэтому порядок здесь и проверяется."""
    import tgx_transcribe

    from telethon.tl import types

    class Attr:
        """Лёгкая заглушка: важны только поля, которые читает модуль."""
        def __init__(self, **kw):
            self.__dict__.update(kw)

    voice = types.DocumentAttributeAudio(duration=3, voice=True)
    music = types.DocumentAttributeAudio(duration=200, voice=False)
    round_note = types.DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)

    def message(*attrs):
        return Attr(media=Attr(document=Attr(attributes=list(attrs))))

    check("голосовое распознаётся", tgx_transcribe.is_voice(message(voice)))
    check("кружок распознаётся", tgx_transcribe.is_voice(message(round_note)))
    check("музыка — не голосовое", not tgx_transcribe.is_voice(message(music)))
    check("текст — не голосовое", not tgx_transcribe.is_voice(Attr(media=None)))
    check("музыка так и называется в отказе",
          tgx_transcribe.describe_kind(message(music)) == "музыка")
    check("текстовое сообщение названо своим именем",
          tgx_transcribe.describe_kind(Attr(media=None)) == "текстовое сообщение")

    # Сервер отвечает pending и досылает текст апдейтом.
    class Server:
        def __init__(self):
            self.handler = None
            self.subscribed_before_request = False

        async def get_input_entity(self, peer):
            return peer

        async def get_messages(self, entity, ids=None):
            return message(voice)

        def add_event_handler(self, callback, event):
            self.handler = callback
            return callback

        def remove_event_handler(self, callback, handler):
            self.handler = None

        async def __call__(self, request):
            self.subscribed_before_request = self.handler is not None
            asyncio.get_running_loop().call_soon(self._deliver)
            return types.messages.TranscribedAudio(
                transcription_id=77, text="", pending=True)

        def _deliver(self):
            update = types.UpdateTranscribedAudio(
                peer=None, msg_id=5, transcription_id=77, text="готовый текст", pending=False)
            asyncio.ensure_future(self.handler(update))

    server = Server()
    result = await tgx_transcribe.Transcriber(server).transcribe("chat", 5, wait=5)
    check("на апдейт подписались до запроса, а не после", server.subscribed_before_request)
    check("дождались готового текста", result["text"] == "готовый текст")
    check("флаг ожидания снят", result["pending"] is False)
    check("обработчик апдейта снят после работы", server.handler is None)

    # Не дождались — отдаём что есть и честно говорим об этом.
    class Silent(Server):
        async def __call__(self, request):
            return types.messages.TranscribedAudio(
                transcription_id=78, text="начало", pending=True)

    slow = await tgx_transcribe.Transcriber(Silent()).transcribe("chat", 5, wait=0.2)
    check("по таймауту отдаётся то, что успело прийти", slow["text"] == "начало")
    check("и честно помечается незаконченным", slow["pending"] is True)

    refused = ""
    try:
        await tgx_transcribe.Transcriber(Server()).transcribe("chat", 5, wait=1)
    except tgx_transcribe.TranscribeError as exc:
        refused = str(exc)
    check("голосовое не отвергается", refused == "")


async def guard_regression() -> None:
    """Сторож удаляет людей из чата — решение об этом обязано быть проверяемым
    без живых людей, поэтому оно вынесено в отдельную функцию."""
    import tgx_guard

    row = {"for_user_id": 651287, "for_label": "@makros23"}
    check("никто не вошёл — ждём", tgx_guard.verdict(row, []) == ("ждёт", []))
    check("вошёл тот, кого звали", tgx_guard.verdict(row, [651287]) == ("использована", []))
    status, strangers = tgx_guard.verdict(row, [999])
    check("вошёл чужой — ссылка нарушена", status == "нарушена")
    check("и чужой назван поимённо", strangers == [999])
    status, strangers = tgx_guard.verdict(row, [651287, 999])
    check("если вошли оба, удаляют только лишнего", status == "нарушена" and strangers == [999])

    with tempfile.TemporaryDirectory() as home:
        os.environ["TGX_HOME"] = home
        journal = Path(home) / "data" / "invite-guard.json"

        calls = []

        class Server:
            """Отвечает так же, как Telegram: по ссылке вошёл не тот человек."""
            async def get_entity(self, who):
                return type("U", (), {"id": 651287, "username": "makros23"})()

            async def get_input_entity(self, who):
                return who

            async def __call__(self, request):
                name = type(request).__name__
                calls.append(name)
                if name == "ExportChatInviteRequest":
                    check("ссылка выписана на одно использование", request.usage_limit == 1)
                    check("в заголовке ссылки — кому она", "makros23" in (request.title or ""))
                    return type("I", (), {"link": "https://t.me/+test"})()
                if name == "GetChatInviteImportersRequest":
                    return type("R", (), {"importers": [
                        type("P", (), {"user_id": 424242})()]})()
                return None

        server = Server()
        guard = tgx_guard.Guard(server, journal)
        row = await guard.issue("chat", "@makros23")
        check("журнал записан на диск", journal.exists())
        check("журнал закрыт от чужих глаз", oct(journal.stat().st_mode)[-3:] == "600")
        check("в журнале запомнили, кого звали", row["for_user_id"] == 651287)

        report = await guard.check("chat", kick=True)
        check("нарушение замечено", report[0]["status"] == "нарушена")
        check("чужого выгнали", report[0]["kicked"] == [424242])
        check("выгоняют баном и сразу разбаном, без вечного бана",
              calls.count("EditBannedRequest") == 2)
        check("нарушенная ссылка отзывается", "DeleteExportedChatInviteRequest" in calls)

        # Тот же прогон без права выгонять — только отчёт.
        calls.clear()
        guard2 = tgx_guard.Guard(Server(), journal)
        await guard2.issue("chat", "@makros23")
        calm = await guard2.check("chat", kick=False)
        check("с --no-kick никого не трогают",
              all("EditBannedRequest" != c for c in calls) and calm[-1]["kicked"] == [])
        os.environ.pop("TGX_HOME", None)


def multipart_regression() -> None:
    """Bot API берёт медиа либо по публичному URL, либо частью формы. Файл с диска
    публичного URL не имеет, поэтому он уезжает как attach:// плюс multipart —
    и если ссылку на него не поставить в текст, Telegram молча его выбросит."""
    import tgx_net
    import tgx_rich

    with tempfile.TemporaryDirectory() as folder:
        picture = Path(folder) / "banner.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\nbody")

        remote = tgx_rich.photo_media("banner", "https://example.com/b.png")
        check("публичная ссылка идёт как есть",
              remote["media"]["media"] == "https://example.com/b.png" and "_upload" not in remote)

        local = tgx_rich.photo_media("banner", str(picture))
        check("файл с диска превращается в attach://",
              local["media"]["media"] == "attach://banner")
        check("и запоминается для загрузки", local["_upload"] == picture)

        refused = False
        try:
            tgx_rich.photo_media("banner", str(Path(folder) / "нет.png"))
        except tgx_rich.RichError:
            refused = True
        check("несуществующий файл отвергается", refused)

        sent = {}

        def fake_multipart(url, fields, files, what="сервис"):
            sent.update(url=url, fields=fields, files=files)
            return {"ok": True, "result": {"message_id": 7}}

        original = tgx_net.post_multipart
        tgx_net.post_multipart = fake_multipart
        try:
            result = tgx_rich.send_rich("123:AA", "-100777", "# Заголовок\n\n![](tg://photo?id=banner)",
                                        media=[tgx_rich.photo_media("banner", str(picture))],
                                        buttons="Сайт[primary]=https://example.com", topic=4)
        finally:
            tgx_net.post_multipart = original

        check("ушло multipart-ом", bool(sent))
        check("файл лежит частью формы под своим именем", "banner" in sent["files"])
        check("и это его настоящие байты", sent["files"]["banner"][1].startswith(b"\x89PNG"))
        check("тип файла определён", sent["files"]["banner"][2] == "image/png")
        check("вложенные поля ушли строками JSON",
              isinstance(sent["fields"]["rich_message"], str))
        check("тема формы доехала", sent["fields"]["message_thread_id"] == "4")
        check("кнопки доехали", "reply_markup" in sent["fields"])
        check("метка стиля снята с подписи", "[primary]" not in sent["fields"]["reply_markup"])
        check("вернулся разобранный ответ", result == {"message_id": 7})

    # сам сборщик формы: одна граница на всё, части с именами, хвостовая граница
    seen = {}

    def capture(request, what):
        seen["body"] = request.data
        seen["type"] = request.headers.get("Content-type", "")
        return '{"ok": true, "result": 1}'

    original_open = tgx_net._open
    tgx_net._open = capture
    try:
        tgx_net.post_multipart("https://example.com/x", {"chat_id": "-100", "n": 5},
                               {"pic": ("b.png", b"\x89PNG", "image/png")})
    finally:
        tgx_net._open = original_open

    body, kind = seen["body"], seen["type"]
    boundary = kind.split("boundary=")[-1]
    check("граница объявлена в заголовке и стоит в теле", boundary.encode() in body)
    check("текстовое поле подписано именем", b'name="chat_id"' in body)
    check("число уходит как JSON", b"\r\n\r\n5\r\n" in body)
    check("файл несёт имя и тип", b'filename="b.png"' in body and b"image/png" in body)
    check("байты файла на месте", b"\x89PNG" in body)
    check("форма закрыта хвостовой границей", body.rstrip().endswith(f"--{boundary}--".encode()))


async def resolve_peer_regression() -> None:
    """«Чат не найден» — самый частый ответ CLI, и он врал: любая ошибка Telegram
    выглядела так же. Из-за этого переименованный чат и обрыв связи неотличимы."""
    import tgx

    class Dialog:
        def __init__(self, name):
            self.name, self.entity = name, f"entity:{name}"

    class Client:
        """Ищет только по точному значению — как Telethon: строка ≠ число."""
        def __init__(self, known=None, boom=None):
            self.known, self.boom, self.asked = known or {}, boom, []

        async def get_entity(self, value):
            self.asked.append(value)
            if self.boom:
                raise self.boom
            if value in self.known:
                return self.known[value]
            raise ValueError(f'Cannot find any entity corresponding to "{value}"')

        async def iter_dialogs(self, limit=None):
            for name in ("AllCrew Cockpit", "Заметки", "Playgama Bridge"):
                yield Dialog(name)

        def __aiter__(self):
            return self

    # числовой id надо отдать числом — строку Telethon ищет как имя
    client = Client(known={4330416518: "чат"})
    check("числовой id разрешается", await tgx.resolve_peer(client, "4330416518") == "чат")
    check("и передаётся именно числом", client.asked[0] == 4330416518)

    # поиск по названию среди диалогов
    found = await tgx.resolve_peer(Client(), "cockpit")
    check("находит чат по куску названия", found == "entity:AllCrew Cockpit")

    # не найдено — сообщение объясняет и подсказывает
    refused = ""
    try:
        await tgx.resolve_peer(Client(), "AllCrew Bridge")
    except tgx.PeerError as exc:
        refused = str(exc)
    check("говорит, что именно искали", "AllCrew Bridge" in refused)
    check("подсказывает похожее название", "AllCrew Cockpit" in refused)
    check("приводит ответ Telegram", "Cannot find any entity" in refused)
    check("называет, сколько диалогов просмотрено", "3 диалог" in refused)

    # чужая ошибка не маскируется под «не найдено»
    class Flood(Exception):
        pass

    leaked = None
    try:
        await tgx.resolve_peer(Client(boom=Flood("FLOOD_WAIT_420")), "что угодно")
    except Flood as exc:
        leaked = str(exc)
    except tgx.PeerError:
        leaked = "проглочено"
    check("ошибка Telegram доходит как есть, а не как «чат не найден»",
          leaked == "FLOOD_WAIT_420")

    refused = ""
    try:
        await tgx.resolve_peer(Client(), "   ")
    except tgx.PeerError as exc:
        refused = str(exc)
    check("пустой чат отвергается сразу", "не указан чат" in refused)


def rich_blocks_regression() -> None:
    """Блочная форма Bot API 10.2–10.3: единственный способ положить кнопки и файл
    ВНУТРЬ документа. Сборщики отдают готовый JSON, поэтому проверяются им же."""
    import tgx_rich as R

    check("отрисовщик и сборщик текста — разные функции",
          R.rich_text is not R.as_text)
    check("сборщик оставляет обычную строку строкой",
          isinstance(R.heading("Заголовок", 2)["text"], str))

    check("заголовок несёт размер", R.heading("x", 3) == {"type": "heading", "text": "x", "size": 3})
    check("раскрывающаяся цитата — отдельный тип",
          R.quote("x", expandable=True)["type"] == "expandable_blockquote")
    check("обычная цитата осталась прежней", R.quote("x")["type"] == "blockquote")
    check("плотная таблица помечается", R.table([["a"]], compact=True)["is_compact"] is True)
    check("у обычной таблицы флага нет", "is_compact" not in R.table([["a"]]))
    check("список раскладывается по пунктам",
          len(R.bullet_list(["a", "b"])["items"]) == 2)

    row = R.button_row("Скачать[primary]=https://a.b, Ещё=https://c.d", align="center")
    check("кнопки собираются в блок", row["type"] == "buttons" and len(row["buttons"]) == 2)
    check("стиль переезжает в кнопку", row["buttons"][0]["style"] == "primary")
    check("метка стиля снята с подписи", row["buttons"][0]["text"] == "Скачать")
    check("выравнивание сохраняется", row["align"] == "center")

    for bad, why in (("", "пустой список кнопок"),
                     (", ".join(f"К{i}=https://a.b" for i in range(9)), "девять кнопок в ряду")):
        refused = False
        try:
            R.button_row(bad)
        except R.RichError:
            refused = True
        check(f"отвергает {why}", refused)

    refused = False
    try:
        R.button_row("Плохо[фиолетовый]=https://a.b")
    except R.RichError as exc:
        refused = "фиолетовый" in str(exc)
    check("отвергает несуществующий стиль кнопки", refused)

    with tempfile.TemporaryDirectory() as folder:
        blob = Path(folder) / "build.zip"
        blob.write_bytes(b"PK\x03\x04")
        doc = R.document_block("build", caption="сборка", source=str(blob))
        check("блок-документ ссылается на attach://",
              doc["document"]["media"] == "attach://build")
        check("и помнит файл для загрузки", doc["_upload"] == blob)

    R.check_blocks([R.paragraph("a"), R.divider(), R.quote("b", expandable=True)])
    for bad, why in (([], "пустой документ"),
                     ([{"type": "паровоз"}], "неизвестный тип блока"),
                     ([{"type": "buttons", "buttons": []}], "блок кнопок без кнопок")):
        refused = False
        try:
            R.check_blocks(bad)
        except R.RichError:
            refused = True
        check(f"проверка отвергает {why}", refused)

    check("ограничение блочной формы записано в модуле",
          "новее слоя" in R.BLOCKS_ARE_WRITE_ONLY)


def poll_regression() -> None:
    """Опросы: сервер объясняет отказ скупо, поэтому всё, что можно проверить
    заранее, проверяется заранее."""
    import tgx_poll as P

    P.check("Вопрос?", ["А", "Б"])
    P.check("Вопрос?", ["Один"])          # с Bot API 10.0 хватает одного варианта
    for question, options, quiz, why in (
            ("", ["А"], None, "пустой вопрос"),
            ("Q", [], None, "ни одного варианта"),
            ("Q", ["А"] * 13, None, "тринадцать вариантов"),
            ("Q" * 301, ["А"], None, "слишком длинный вопрос"),
            ("Q", ["x" * 101], None, "слишком длинный вариант"),
            ("Q", ["А", "Б"], 5, "правильный ответ вне списка")):
        refused = False
        try:
            P.check(question, options, quiz_answer=quiz)
        except P.PollError:
            refused = True
        check(f"отвергает {why}", refused)

    check("ключ варианта — его номер в байтах", P.option_key(2) == b"\x02")
    check("страны разбираются", P.parse_countries("ru, de") == ["RU", "DE"])
    check("без стран — без ограничения", P.parse_countries("") is None)
    refused = False
    try:
        P.parse_countries("россия")
    except P.PollError:
        refused = True
    check("отвергает страну не двумя буквами", refused)

    class Thing:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    poll = Thing(question=Thing(text="Вопрос?"), quiz=False, multiple_choice=False,
                 public_voters=False, closed=False, subscribers_only=True,
                 countries_iso2=["RU"],
                 answers=[Thing(text=Thing(text="А"), option=b"\x00"),
                          Thing(text=Thing(text="Б"), option=b"\x01")])
    results = Thing(total_voters=4, results=[Thing(option=b"\x00", voters=3),
                                             Thing(option=b"\x01", voters=1)])
    view = P.describe(poll, results)
    check("вопрос разворачивается из обёртки", view["question"] == "Вопрос?")
    check("доли считаются", [a["share"] for a in view["answers"]] == [75, 25])
    check("голоса раскладываются по вариантам",
          [a["voters"] for a in view["answers"]] == [3, 1])
    check("ограничение подписчиками видно", view["members_only"] is True)
    check("страны видны", view["countries"] == ["RU"])

    empty = P.describe(poll, None)
    check("без результатов доли нулевые", all(a["share"] == 0 for a in empty["answers"]))

    check("викторина от своего имени отклоняется с объяснением",
          "Telethon" in P.QUIZ_NEEDS_BOT and "--as" in P.QUIZ_NEEDS_BOT)

    # Bot API 9.6: правильных ответов может быть несколько, срок автозакрытия — до месяца
    P.check("Q", ["А", "Б", "В"], quiz_answer=[0, 2])
    refused = False
    try:
        P.check("Q", ["А", "Б"], quiz_answer=[0, 5])
    except P.PollError:
        refused = True
    check("отвергает список, где один ответ вне диапазона", refused)
    P.check("Q", ["А"], close_in=P.MAX_CLOSE_PERIOD)
    refused = False
    try:
        P.check("Q", ["А"], close_in=P.MAX_CLOSE_PERIOD + 1)
    except P.PollError:
        refused = True
    check("отвергает срок автозакрытия больше месяца", refused)


def date_entity_regression() -> None:
    """Сущность «дата и время» (Bot API 9.5, в слое — MessageEntityFormattedDate):
    сервер хранит момент, клиент решает, как его показать."""
    from datetime import datetime, timezone

    from telethon.tl.types import MessageEntityFormattedDate as D

    import tgx_format as F

    when = datetime(2026, 8, 29, 14, 30, 5, tzinfo=timezone.utc)
    check("короткая дата", F.describe_date(D(0, 4, when, short_date=True)) == "29.08.2026")
    check("короткое время", F.describe_date(D(0, 4, when, short_time=True)) == "14:30")
    check("длинное время", F.describe_date(D(0, 4, when, long_time=True)) == "14:30:05")
    check("день недели впереди",
          F.describe_date(D(0, 4, when, day_of_week=True, short_date=True)).startswith("сб"))
    check("без флагов — дата и время",
          F.describe_date(D(0, 4, when)) == "29.08.2026 14:30")
    check("без даты — пусто", F.describe_date(D(0, 4, None)) == "")

    rendered = F.render("дата: XXXX", [D(offset=6, length=4, date=when, short_date=True)])
    check("дата подставляется в текст вместо заглушки", "29.08.2026" in rendered.plain)


def command_surface_regression() -> None:
    """Команды, дошедшие из журнала Bot API, должны быть на месте: разбор легко
    сломать соседней правкой, и тогда пропадает целая ветка."""
    import tgx
    import tgx_autotools

    leaves = {"_".join(path) for path, _, _, _ in tgx_autotools.leaves(tgx.build_parser())}
    for wanted, why in (("poll_create", "создание опроса"), ("poll_vote", "голосование"),
                        ("poll_results", "результаты"), ("poll_close", "закрытие"),
                        ("copy", "копия без подписи «переслано»"),
                        ("boosts_status", "уровень бустов"), ("boosts_mine", "свои слоты"),
                        ("forum_topics", "темы форума"), ("guard_invite", "именные приглашения"),
                        ("transcribe_get", "расшифровка"), ("profile_banner", "баннер в аватар")):
        check(f"команда на месте: {why}", wanted in leaves)

    parser = tgx.build_parser()
    flags = {}
    for path, leaf, _, _ in tgx_autotools.leaves(parser):
        flags["_".join(path)] = {o for a in leaf._actions for o in a.option_strings}
    check("у send есть обложка видео", "--cover" in flags.get("send", set()))
    check("у send есть точка старта", "--start-at" in flags.get("send", set()))
    check("у bot rich есть блоки", "--blocks" in flags.get("bot_rich", set()))
    check("у react есть снятие чужих реакций",
          "--remove-from" in flags.get("react", set()))
    check("у poll create есть несколько правильных ответов",
          "--quiz" in flags.get("poll_create", set()))


def rich_render_regression() -> None:
    """A received rich message is an Instant-View block tree — it has to become
    readable terminal text, not a bare "unsupported media" chip."""
    from telethon.tl import types

    import tgx_rich

    message = types.RichMessage(
        blocks=[
            types.PageBlockHeading1(text=types.TextPlain("Заголовок")),
            types.PageBlockParagraph(text=types.TextConcat([
                types.TextPlain("обычный "),
                types.TextBold(types.TextPlain("жирный")),
                types.TextPlain(" и "),
                types.TextUrl(types.TextPlain("ссылка"), "https://x.io", 0),
            ])),
            types.PageBlockList(items=[
                types.PageListItemText(text=types.TextPlain("раз")),
                types.PageListItemText(text=types.TextPlain("два")),
            ]),
            types.PageBlockDivider(),
            types.PageBlockBlockquote(text=types.TextPlain("цитата"), caption=types.TextEmpty()),
        ],
        photos=[], documents=[], rtl=False, part=False,
    )
    rendered = str(tgx_rich.render_message(message))
    check("headings are marked as headings", "# Заголовок" in rendered)
    check("inline styles survive into the text", "жирный" in rendered and "ссылка" in rendered)
    check("list items each get their own line", "• раз" in rendered and "• два" in rendered)
    check("a quote gets its bar", "▌ цитата" in rendered)
    check("a divider is drawn", "─────" in rendered)
    check("blocks are separated by blank lines", "\n\n" in rendered)


def article_regression() -> None:
    """Markdown must land on the narrow tag set Telegraph accepts — and the token
    that can edit every published page is a credential like any other."""
    import tgx_article

    nodes = tgx_article.markdown_to_nodes(
        "# Заголовок\n\n## Подзаголовок\n\nАбзац с **жирным** и [ссылкой](https://x.io).\n\n"
        "- раз\n- два\n\n1. первый\n2. второй\n\n> цитата\n> продолжение\n\n```\ncode\n```\n\n---\n\n"
        "![кот](https://x.io/cat.png)")
    tags = [n["tag"] for n in nodes]
    check("markdown maps onto Telegraph's tags",
          tags == ["h3", "h4", "p", "ul", "ol", "blockquote", "pre", "hr", "p"])
    check("# is h3 and ## is h4 — Telegraph has no h1/h2", tags[0] == "h3" and tags[1] == "h4")
    paragraph = nodes[2]["children"]
    kinds = [n["tag"] for n in paragraph if isinstance(n, dict)]
    check("inline bold and links survive", kinds == ["b", "a"])
    check("quoted lines are joined", nodes[5]["children"] == ["цитата продолжение"])
    check("images become img nodes",
          nodes[8]["children"][0]["attrs"]["src"] == "https://x.io/cat.png")

    probe = OUT / "telegraph-probe.json"
    original = tgx_article.token_path
    tgx_article.token_path = lambda: probe
    try:
        tgx_article.save_account({"access_token": "abcd1234567890efgh", "short_name": "tgx"})
        check("the telegraph token file is 0600", oct(probe.stat().st_mode)[-3:] == "600")
        check("the token is masked when shown", tgx_article.mask("abcd1234567890efgh") == "abcd…efgh")
        probe.unlink()
        refused = False
        try:
            tgx_article.create_page("Заголовок", "текст")     # no account: must not touch the network
        except tgx_article.ArticleError as exc:
            refused = "аккаунт" in str(exc)
        check("publishing without an account explains what to do", refused)
    finally:
        tgx_article.token_path = original


def video_regression() -> None:
    """A video must go out as a video. Without hachoir, Telethon cannot read its
    duration or dimensions and falls back to a 1×1 stub — which Telegram then
    shows as a plain file attachment."""
    import importlib.util
    import shutil
    import subprocess

    from telethon import utils

    import tgx_media

    check("hachoir is installed — that is what fills duration and size",
          importlib.util.find_spec("hachoir") is not None)
    if shutil.which("ffmpeg") is None:
        return
    clip = OUT / "clip.mp4"
    subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-t", "2",
                    "-pix_fmt", "yuv420p", "-y", str(clip)], capture_output=True, timeout=60)
    attributes, mime = utils.get_attributes(str(clip), supports_streaming=True)
    video = next((a for a in attributes if type(a).__name__ == "DocumentAttributeVideo"), None)
    check("telethon describes it as a video", video is not None and mime == "video/mp4")
    check("with real duration and dimensions, not a 1×1 stub",
          video is not None and video.duration >= 1 and video.w == 640 and video.h == 360)
    check("streaming stays on", video is not None and video.supports_streaming)
    poster = tgx_media.poster_frame(clip)
    check("a cover frame is extracted", poster is not None and poster.stat().st_size > 500)


def folder_and_paging_regression() -> None:
    """Folders are mostly built from categories ("all groups"), not from explicit
    peer lists — filtering on include_peers alone hides nearly everything."""
    from tgx_tui import Chat, Folder, merge_chats

    groups = Folder(id=1, title="группы", groups=True)
    check("category folder catches groups", groups.matches(Chat(id=1, name="", kind="group")))
    check("category folder skips channels", not groups.matches(Chat(id=2, name="", kind="channel")))

    mixed = Folder(id=2, title="mixed", include=frozenset({5}), exclude=frozenset({6}), groups=True)
    check("explicit include beats the categories", mixed.matches(Chat(id=5, name="", kind="user")))
    check("explicit exclude beats everything", not mixed.matches(Chat(id=6, name="", kind="group")))

    unread_only = Folder(id=3, title="unread", groups=True, exclude_read=True)
    check("unread-only folder hides read chats", not unread_only.matches(Chat(id=7, name="", kind="group")))
    check("unread-only folder keeps unread ones", unread_only.matches(Chat(id=8, name="", kind="group", unread=2)))

    people = Folder(id=4, title="contacts", contacts=True)
    check("contacts and strangers are told apart",
          people.matches(Chat(id=9, name="", kind="user", contact=True))
          and not people.matches(Chat(id=10, name="", kind="user")))

    old = [Chat(id=1, name="a", unread=1), Chat(id=2, name="b")]
    merged = merge_chats(old, [Chat(id=1, name="a!", unread=0), Chat(id=3, name="c")])
    check("background page keeps the open chat object", merged[0] is old[0] and merged[0].name == "a!")
    check("background page adds the missing chats", any(c.id == 3 for c in merged))


async def preview_cache_regression() -> None:
    """Previews must come from the cache, and interrupted downloads must not
    leave a zero-byte file that makes the same message refetch on every open."""
    from tgx_tui import Chat, TelegramBackend

    backend = TelegramBackend.__new__(TelegramBackend)
    backend._raw, backend._paths, backend.client = {}, {}, None
    cache = OUT / "cache-probe"
    cache.mkdir(parents=True, exist_ok=True)
    chat = Chat(id=7, name="probe")

    good = cache / "7_42.jpg"
    good.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    partial = cache / "7_42.part"
    partial.write_bytes(b"")

    found = await backend.thumbnail(chat, 42, cache)
    check("cached preview is reused, not refetched", found == good)
    check("interrupted download is cleaned up", not partial.exists())
    check("path is remembered in memory", backend._paths.get((7, 42, False)) == good)

    unusable = cache / "7_44.bin"
    unusable.write_bytes(b"not an image at all")
    check("undrawable file yields no preview", await backend.thumbnail(chat, 44, cache) is None)

    import tgx_media

    truncated = cache / "broken.png"
    truncated.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)   # header only, no pixels
    check("corrupt image never reaches the renderer", tgx_media.make_widget(truncated) is None)
    check("that verdict is remembered too", backend._paths.get((7, 44, False)) is None)

    stale = cache / "7_43.download"
    stale.write_bytes(b"")
    try:
        await backend.thumbnail(chat, 43, cache)   # no client: fails after the cleanup
    except Exception:
        pass
    check("stale partial of a missing preview is removed", not stale.exists())


async def eager_task_regression() -> None:
    """A real terminal run installs asyncio's eager task factory (textual/app.py).

    Under it, `create_task` runs the coroutine synchronously, which used to break
    Telethon's send/receive loops and hang the UI at "connecting…".  Lock in that
    `plain_task_factory()` undoes it — headless run_test() alone never sees this.
    """
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    ran: list[str] = []

    async def probe() -> None:
        ran.append("eager")

    loop.create_task(probe())
    check("eager task factory runs coroutines synchronously", ran == ["eager"])

    plain_task_factory()
    ran.clear()
    task = loop.create_task(probe())
    check("plain_task_factory() restores deferred tasks", ran == [])
    await task


def file_type_regression() -> None:
    """A cached JPEG named `.thumb` gets a dynamic UTI on macOS and `open` fails
    silently — every cached file must carry its real extension."""
    import tgx_media

    probe = OUT / "suffix-probe.thumb"
    probe.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    fixed = tgx_media.with_real_suffix(probe)
    check("cached files get their real extension", fixed.suffix == ".jpg")
    fixed.unlink(missing_ok=True)
    check("failed opens are reported, not swallowed",
          tgx_media.open_external(Path("/nonexistent/tgx-probe.jpg")) is not None)


def thumbnail_choice_regression() -> None:
    """A preview must download a thumbnail — never the full-size photo."""
    from telethon.tl import types

    from tgx_tui import TelegramBackend

    class FakeMessage:
        media = None

    photo = types.Photo(
        id=1, access_hash=1, file_reference=b"", date=None, dc_id=2, has_stickers=False, video_sizes=[],
        sizes=[
            types.PhotoStrippedSize(type="i", bytes=b"\x01\x02"),
            types.PhotoSize(type="s", w=90, h=60, size=1200),
            types.PhotoSize(type="m", w=320, h=213, size=14000),
            types.PhotoSizeProgressive(type="y", w=1280, h=853, sizes=[200000]),
        ],
    )
    message = FakeMessage()
    message.media = types.MessageMediaPhoto(photo=photo)
    chosen = TelegramBackend._pick_thumb(message)
    check("photo preview picks a thumbnail, not the original", chosen is not None and chosen.w == 320)

    # The full-size view must ask for something Telethon's _get_thumb accepts:
    # a PhotoSizeProgressive *object* is rejected there and silently yields None.
    from telethon.client.downloads import DownloadMethods

    full_choice = TelegramBackend._pick_thumb(message, full=True)
    resolved = DownloadMethods._get_thumb(photo.sizes, full_choice)
    check("full-size view resolves to a real size", resolved is not None and getattr(resolved, "w", 0) >= 320)

    progressive_only = FakeMessage()
    progressive_only.media = types.MessageMediaPhoto(photo=types.Photo(
        id=2, access_hash=1, file_reference=b"", date=None, dc_id=2, has_stickers=False, video_sizes=[],
        sizes=[types.PhotoSizeProgressive(type="y", w=1280, h=853, sizes=[200000])]))
    choice = TelegramBackend._pick_thumb(progressive_only, full=True)
    resolved = DownloadMethods._get_thumb(progressive_only.media.photo.sizes, choice)
    check("progressive-only photo still resolves", resolved is not None)

    voice = types.Document(id=2, access_hash=1, file_reference=b"", date=None, mime_type="audio/ogg",
                           size=999, dc_id=2, attributes=[], thumbs=[])
    message2 = FakeMessage()
    message2.media = types.MessageMediaDocument(document=voice)
    check("audio has no preview", TelegramBackend._pick_thumb(message2) is None)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    await eager_task_regression()
    await preview_cache_regression()
    folder_and_paging_regression()
    video_regression()
    await business_regression()
    appearance_regression()
    await forum_regression()
    await transcribe_regression()
    await guard_regression()
    multipart_regression()
    rich_blocks_regression()
    poll_regression()
    date_entity_regression()
    command_surface_regression()
    await resolve_peer_regression()
    autotools_regression()
    rich_render_regression()
    article_regression()
    rich_message_regression()
    mcp_connector_regression()
    formatting_regression()
    await bots_regression()
    thumbnail_choice_regression()
    file_type_regression()
    # drive the UI under the same task factory the real terminal driver installs
    asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
    app = TgxApp(DemoBackend())
    async with app.run_test(size=(132, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(1.2)
        await pilot.pause()
        messages = app.query_one(MessageList)
        check("chat opened", app.current is not None)
        check("history rendered", len(messages.rows) > 0)
        app.save_screenshot(str(OUT / "01-main.svg"))

        # channel posts: comments instead of plain messages
        check("channel is comment-only", app.current is not None and not app.current.can_post)
        check("composer says so", "комментарий" in app.query_one("#composer").placeholder)
        messages.focus()
        await pilot.press("end")
        await pilot.pause()
        await pilot.press("c")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("comments screen opens", any(type(s).__name__ == "CommentsScreen" for s in app.screen_stack))
        thread = app.screen.query_one("#comments-list", MessageList)
        check("thread loaded", len(thread.msgs) >= 2)
        await pilot.press(*"спасибо")
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("comment posted into the thread", any(m.out and m.text == "спасибо" for m in thread.msgs))
        app.save_screenshot(str(OUT / "01a-comments.svg"))
        await pilot.press("escape")
        await pilot.pause()

        # typing in a comment-only channel becomes a comment, not a failed send
        app.set_focus(app.query_one("#composer"))
        posted = len(app.backend._comments)
        await pilot.press(*"из инпута")
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        sent_as_comment = any(
            any(m.out and m.text == "из инпута" for m in thread_msgs)
            for thread_msgs in app.backend._comments.values()
        )
        check("composer text routes to comments in a channel", sent_as_comment)

        # inline media preview
        await asyncio.sleep(1.2)
        await pilot.pause()
        picture_bubbles = [r.bubble for r in messages.rows if r.bubble.has_image]
        check("media preview mounted in a bubble", len(picture_bubbles) >= 1)
        if picture_bubbles:
            widget = picture_bubbles[0].query(".media").first()
            check("preview has a real cell size", widget.styles.width.value >= 4 and widget.styles.height.value >= 2)
        app.save_screenshot(str(OUT / "01b-media.svg"))

        # full-pane picture viewer
        media_msg = next((r.bubble.msg for r in messages.rows if r.bubble.has_image), None)
        if media_msg is not None:
            messages.focus()                 # `v` is a key binding, not text for the composer
            messages.focus_message(media_msg.id)
            await pilot.pause()
            await pilot.press("v")
            await asyncio.sleep(0.8)
            await pilot.pause()
            check("full-screen viewer opens", any(type(s).__name__ == "MediaScreen" for s in app.screen_stack))
            app.save_screenshot(str(OUT / "01c-media-full.svg"))
            await pilot.press("escape")
            await pilot.pause()

        # clicking a bubble selects it (so v / o / ctrl+r work without arrow keys).
        # Let the viewer finish closing and drop toasts first — either would eat the click.
        app.clear_notifications()
        while len(app.screen_stack) > 1:
            await asyncio.sleep(0.2)
            await pilot.pause()
        await asyncio.sleep(0.3)
        # the viewer left the pane scrolled to the picture; bring the tail back into view
        messages.scroll_end(animate=False)
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
        index = len(messages.rows) - 1
        messages.selected = None
        await pilot.click(messages.rows[index].bubble)
        await pilot.pause()
        check("click selects a message", messages.selected == index)

        # the media chip advertises the keys
        media_bubble = next((r.bubble for r in messages.rows if r.bubble.msg.media), None)
        check("media chip shows the v / o hint", media_bubble is not None and "v — открыть" in str(media_bubble.text()))

        # chat filter
        app.set_focus(app.query_one("#filter"))
        await pilot.press(*"мария")
        await pilot.pause()
        check("filter narrows the list", len(app.query_one("#chats").visible_chats) == 1)
        for _ in range(5):
            await pilot.press("backspace")
        await pilot.pause()

        # folders
        await pilot.press("ctrl+k")
        app.query_one("#folders").active = "f-3"
        await pilot.pause()
        await asyncio.sleep(0.3)
        check("folder tab filters chats", 0 < len(app.query_one("#chats").visible_chats) < 8)
        app.query_one("#folders").active = "f-all"
        await pilot.pause()

        # sidebar collapse
        await pilot.press("ctrl+b")
        await asyncio.sleep(0.4)
        await pilot.pause()
        check("sidebar collapses", app.query_one("#sidebar").has_class("collapsed"))
        app.save_screenshot(str(OUT / "02-focus-mode.svg"))
        await pilot.press("ctrl+b")
        await asyncio.sleep(0.4)

        # selection + reply
        messages.focus()
        await pilot.press("up", "up")
        await pilot.pause()
        check("message selection works", messages.selected_msg() is not None)
        await pilot.press("ctrl+r")
        await pilot.pause()
        check("reply chip shown", app.query_one("#reply-chip").has_class("visible"))
        await pilot.press("escape")
        await pilot.pause()

        # search: scope, media type, sender and dates
        await pilot.press("ctrl+f")
        await pilot.pause()
        await asyncio.sleep(0.4)
        finder = app.screen
        check("search screen opens", type(finder).__name__ == "SearchScreen")
        check("it offers the filters", all(finder.query(sel) for sel in
              ("#search-scope", "#search-kind", "#search-from", "#search-since", "#search-until")))
        await pilot.press(*"поиск")
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("search returned hits", len(finder.hits) > 0)

        finder.query_one("#search-scope", Select).value = "all"
        finder.query_one("#search-input", Input).value = "созвон"   # present in every demo chat
        finder.query_one("#search-input", Input).focus()
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("global scope reaches several chats at once",
              len({chat.id for chat, _ in finder.hits if chat}) >= 2)

        finder.query_one("#search-input", Input).value = ""
        finder.query_one("#search-kind", Select).value = "photo"
        await asyncio.sleep(0.9)
        await pilot.pause()
        check("filtering by media type alone works",
              bool(finder.hits) and all("photo" in (m.media or "") for _, m in finder.hits))

        finder.query_one("#search-until", Input).value = "не дата"
        finder.query_one("#search-input", Input).focus()
        await pilot.press("enter")
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("a broken date is reported, not crashed", type(app.screen).__name__ == "SearchScreen")
        finder.query_one("#search-until", Input).value = ""

        finder.query_one("#search-from", Input).value = "@someone"
        finder.query_one("#search-input", Input).value = "созвон"
        finder.query_one("#search-input", Input).focus()
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("sender filter is refused for a global search, with an explanation", finder.hits == [])
        app.save_screenshot(str(OUT / "12-search.svg"))
        await pilot.press("escape")
        await pilot.pause()

        # move to a chat we may actually post in before testing plain sending
        writable = next(c for c in app.query_one(ChatList).chats if c.can_post)
        await app.open_chat(writable)
        await asyncio.sleep(0.4)
        await pilot.pause()
        messages = app.query_one(MessageList)

        # send — assert on the text, not on a count: the demo backend injects
        # incoming messages on a timer and would make a counter flaky
        app.set_focus(app.query_one("#composer"))
        await pilot.press(*"проверка связи")
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        sent = [m for m in app.query_one(MessageList).msgs if m.out and m.text == "проверка связи"]
        check("message appended after send", len(sent) == 1)

        # attaching a file: picker, preview, options, send
        probe = OUT / "attach-probe.png"
        from PIL import Image as PILImage

        PILImage.new("RGB", (64, 40), (42, 171, 238)).save(probe)
        await pilot.press("ctrl+s")
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("attach screen opens", any(type(s).__name__ == "AttachScreen" for s in app.screen_stack))
        picker = app.screen
        picker.query_one("#attach-path", Input).value = str(probe)
        picker.query_one("#attach-caption", Input).value = "смотри что нашёл"
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("attach preview lists the file", any("attach-probe" in str(w.render()) for w in picker.query("#attach-preview Static")))
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        attached = [m for m in app.query_one(MessageList).msgs if m.out and "attach-probe" in (m.media or "")]
        check("file message appended", len(attached) == 1)
        check("caption travelled with the file", bool(attached) and attached[0].text == "смотри что нашёл")
        app.save_screenshot(str(OUT / "06-attach.svg"))

        # ── post editor: markup, live preview, publishing ────────────────
        app.query_one(MessageList).focus()   # `p` is a binding, not text for the composer
        await pilot.pause()
        await pilot.press("p")
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("post editor opens", any(type(s).__name__ == "PostScreen" for s in app.screen_stack))
        editor = app.screen
        editor.query_one("#post-text", TextArea).text = "**Заголовок**\n> цитата\n||секрет|| и [ссылка](https://x.io)"
        await asyncio.sleep(0.5)
        await pilot.pause()
        rendered = str(editor.query_one("#post-render").render())
        check("preview shows the formatted text, not the markup",
              "Заголовок" in rendered and "**" not in rendered)
        app.save_screenshot(str(OUT / "08-post.svg"))
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        posted = [m for m in app.query_one(MessageList).msgs if m.out and "Заголовок" in (m.text or "")]
        check("post is published with entities", len(posted) == 1 and len(posted[0].entities) >= 3)

        # inline buttons: only a bot may hang them, and Mini Apps are allowed
        app.query_one(MessageList).focus()
        await pilot.press("p")
        await asyncio.sleep(0.6)
        await pilot.pause()
        editor = app.screen
        editor.query_one("#post-text", TextArea).text = "Пост с кнопками"
        editor.query_one("#post-buttons", Input).value = "Сайт=https://x.io, Приложение=webapp:https://app.x.io"
        await pilot.press("ctrl+s")
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("buttons from a personal account are refused",
              any(type(s).__name__ == "PostScreen" for s in app.screen_stack))
        editor.query_one("#post-as", Select).value = "tgx_demo_bot"
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        from_bot = [m for m in app.query_one(MessageList).msgs if m.text == "Пост с кнопками"]
        check("the bot posts it with its buttons",
              len(from_bot) == 1 and from_bot[0].buttons == (("Сайт", "Приложение"),)
              and from_bot[0].sender == "@tgx_demo_bot")
        app.save_screenshot(str(OUT / "10-bot-post.svg"))

        # spoilers stay hidden until asked for
        messages = app.query_one(MessageList)
        messages.focus()
        messages.focus_message(posted[0].id)
        bubble = messages.rows[messages.selected].bubble
        check("spoiler is masked in the bubble", "секрет" not in str(bubble.text()))
        await pilot.press("s")
        await pilot.pause()
        check("s reveals it", "секрет" in str(bubble.text()))

        # a rich message: the personal account sends it over MTProto (Telethon 1.44,
        # layer 227), a bot sends it over the HTTP Bot API — both paths are wired
        app.query_one(MessageList).focus()
        await pilot.press("p")
        await asyncio.sleep(0.6)
        await pilot.pause()
        editor = app.screen
        editor.query_one("#post-text", TextArea).text = "# Заголовок\n\n- [x] сделано\n- [ ] в работе"
        editor.query_one("#post-rich", Checkbox).value = True
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        from_me = [m for m in app.query_one(MessageList).msgs
                   if m.media == "📄 rich-сообщение" and m.sender == "@you"]
        check("a personal account sends a rich message itself", len(from_me) == 1)

        app.query_one(MessageList).focus()
        await pilot.press("p")
        await asyncio.sleep(0.6)
        await pilot.pause()
        editor = app.screen
        editor.query_one("#post-text", TextArea).text = "## От бота\n\nчерез Bot API"
        editor.query_one("#post-rich", Checkbox).value = True
        editor.query_one("#post-as", Select).value = "tgx_demo_bot"
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        from_bot = [m for m in app.query_one(MessageList).msgs
                    if m.media == "📄 rich-сообщение" and m.sender == "@tgx_demo_bot"]
        check("and a bot sends one too", len(from_bot) == 1)

        # ── reactions ────────────────────────────────────────────────────
        messages = app.query_one(MessageList)
        messages.focus()
        await pilot.press("end")
        await pilot.pause()
        target = messages.selected_msg()
        await pilot.press("plus")
        await asyncio.sleep(0.5)
        await pilot.pause()
        check("reaction picker opens", any(type(s).__name__ == "ReactionScreen" for s in app.screen_stack))
        await pilot.press("enter")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("reaction is set", any(mine for _, _, mine in target.reactions))
        await pilot.press("minus")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("reaction is cleared", not any(mine for _, _, mine in target.reactions))

        # ── edit ─────────────────────────────────────────────────────────
        own = next(m for m in reversed(messages.msgs) if m.out and m.text)
        messages.focus()
        messages.focus_message(own.id)
        await pilot.press("e")
        await pilot.pause()
        check("edit prefills the composer", app.query_one("#composer").value == own.text)
        app.query_one("#composer", Input).value = "исправленный текст"
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("message is edited", own.text == "исправленный текст" and own.edited)

        # ── forward ──────────────────────────────────────────────────────
        chats = app.query_one(ChatList)
        elsewhere = next(c for c in chats.chats if c.id != app.current.id and c.can_post)
        before = len(app.backend._history.get(elsewhere.id, []))
        messages.focus()
        messages.focus_message(own.id)
        await pilot.press("f")
        await asyncio.sleep(0.5)
        await pilot.pause()
        check("forward picker opens", any(type(s).__name__ == "ChatPickScreen" for s in app.screen_stack))
        app.screen.query_one("#pick-filter", Input).value = elsewhere.name[:8]
        await pilot.pause()
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("message lands in the other chat", len(app.backend._history.get(elsewhere.id, [])) == before + 1)

        # ── delete, behind a confirmation ────────────────────────────────
        messages.focus()
        messages.focus_message(own.id)
        await pilot.press("x")
        await asyncio.sleep(0.5)
        await pilot.pause()
        check("delete asks first", any(type(s).__name__ == "ConfirmScreen" for s in app.screen_stack))
        await pilot.press("y")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("message is gone", all(m.id != own.id for m in app.query_one(MessageList).msgs))

        # ── inline buttons of a bot ──────────────────────────────────────
        bot_chat = next(c for c in chats.chats if c.kind == "bot")
        pressed: list[tuple[int, int, int]] = []
        original = app.backend.press_button

        async def spy(chat, msg_id, row, col):
            pressed.append((msg_id, row, col))
            return await original(chat, msg_id, row, col)

        app.backend.press_button = spy
        await app.open_chat(bot_chat)
        await asyncio.sleep(0.8)
        await pilot.pause()
        thread = app.query_one(MessageList)
        with_buttons = next(m for m in thread.msgs if m.buttons)
        thread.focus()
        thread.focus_message(with_buttons.id)
        await pilot.press("b")
        await asyncio.sleep(0.5)
        await pilot.pause()
        check("button picker opens", any(type(s).__name__ == "ButtonScreen" for s in app.screen_stack))
        app.save_screenshot(str(OUT / "07-buttons.svg"))
        await pilot.press("enter")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("bot button is pressed", pressed == [(with_buttons.id, 0, 0)])
        app.backend.press_button = original

        # every remaining binding at least has to run without blowing up
        for key in ("ctrl+u", "ctrl+n", "ctrl+y", "R", "ctrl+g", "ctrl+j"):
            await pilot.press(key)
            await pilot.pause()
        await asyncio.sleep(0.6)
        await pilot.press("ctrl+e")
        await asyncio.sleep(0.3)
        await pilot.pause()
        check("multiline editor opens", any(type(s).__name__ == "ComposeScreen" for s in app.screen_stack))
        await pilot.press("escape")
        await pilot.pause()

        # themes
        await pilot.press("ctrl+t")
        await asyncio.sleep(0.4)
        await pilot.pause()
        check("theme switched", app.theme != "tgx-night")
        app.save_screenshot(str(OUT / "04-light.svg"))

        # ── checklists ───────────────────────────────────────────────────
        app.query_one(MessageList).focus()
        await pilot.press("l")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("checklist composer opens", any(type(s).__name__ == "NewChecklistScreen" for s in app.screen_stack))
        app.screen.query_one("#todo-title", Input).value = "План на неделю"
        app.screen.query_one("#todo-items", TextArea).text = "созвон\nмакеты\nрелиз"
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        todo = next((m for m in app.query_one(MessageList).msgs if m.checklist), None)
        check("checklist is sent with its items", todo is not None and len(todo.checklist) == 3)
        bubble = next(r.bubble for r in app.query_one(MessageList).rows if r.bubble.msg is todo)
        check("the bubble draws empty boxes", "☐ созвон" in str(bubble.text()))

        app.query_one(MessageList).focus()
        app.query_one(MessageList).focus_message(todo.id)
        await pilot.press("l")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("checklist screen opens on a checklist message",
              any(type(s).__name__ == "ChecklistScreen" for s in app.screen_stack))
        board = app.screen
        board.query_one("#todo-list").highlighted = 0
        await pilot.press("enter")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("an item gets ticked", todo.checklist[0][2] is True)
        board.query_one("#todo-new", Input).focus()
        await pilot.press(*"ретро")
        await pilot.press("enter")
        await asyncio.sleep(0.7)
        await pilot.pause()
        check("an item can be appended", len(todo.checklist) == 4 and todo.checklist[-1][1] == "ретро")
        app.save_screenshot(str(OUT / "13-checklist.svg"))
        await pilot.press("escape")
        await pilot.pause()
        check("the bubble shows the progress", "1 из 4" in str(bubble.text()))

        # ── forum topics ─────────────────────────────────────────────────
        chats_widget = app.query_one(ChatList)
        forum = next(c for c in chats_widget.chats if c.forum)
        await app.open_chat(forum)
        await asyncio.sleep(0.8)
        await pilot.pause()
        tabs = app.query_one("#topics")
        check("topic bar appears for a forum", tabs.display and app.current_topic is not None)
        check("pinned topic comes first", app.current_topic.title == "Общее")
        check("history is filtered to the topic",
              all(m.reply_to == 1 or m.id == 1 for m in app.query_one(MessageList).msgs))

        tabs.active = "t-2"
        await asyncio.sleep(1.0)
        await pilot.pause()
        check("switching the tab switches the thread",
              app.current_topic is not None and app.current_topic.id == 2
              and any(m.text == "макеты на ревью" for m in app.query_one(MessageList).msgs))

        # pinning inside the topic
        messages = app.query_one(MessageList)
        messages.focus()
        messages.focus_message(203)
        await pilot.press("P")
        await asyncio.sleep(0.8)
        await pilot.pause()
        target = next(m for m in messages.msgs if m.id == 203)
        check("message gets pinned", target.pinned)
        check("the bubble shows the pin", "📌" in str(messages.rows[messages.selected].bubble.text()))
        await pilot.press("P")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("pressing again unpins it", not target.pinned)
        check("pinned listing reflects it", all(m.id != 203 for m in await app.backend.pinned(forum)))

        # the topics screen: create, close, pin
        messages.focus()
        await pilot.press("t")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("topics screen opens", any(type(s).__name__ == "TopicsScreen" for s in app.screen_stack))
        panel = app.screen
        panel.query_one("#topic-title", Input).value = "Новая тема"
        await pilot.press("n")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("topic is created", any(t.title == "Новая тема" for t in await app.backend.topics(forum)))
        app.save_screenshot(str(OUT / "11-topics.svg"))
        panel.query_one("#topic-list").highlighted = 0
        await pilot.press("c")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("a topic can be closed from the list",
              any(t.closed for t in await app.backend.topics(forum) if t.title == "Общее"))
        await pilot.press("escape")
        await pilot.pause()

        # ── creating a channel ───────────────────────────────────────────
        app.query_one(MessageList).focus()
        await pilot.press("n")
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("new-chat dialog opens", any(type(s).__name__ == "NewChatScreen" for s in app.screen_stack))
        app.screen.query_one("#new-title", Input).value = "Тестовый канал"
        app.screen.query_one("#new-about", Input).value = "описание из теста"
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        check("channel is created and opened", app.current is not None and app.current.name == "Тестовый канал")

        # ── managing it ──────────────────────────────────────────────────
        app.query_one(MessageList).focus()
        await pilot.press("i")
        await asyncio.sleep(1.0)
        await pilot.pause()
        check("manage screen opens", any(type(s).__name__ == "ManageScreen" for s in app.screen_stack))
        panel = app.screen
        check("manage screen shows what it read",
              panel.query_one("#manage-title", Input).value == "Тестовый канал"
              and panel.query_one("#manage-about", Input).value == "описание из теста")
        app.save_screenshot(str(OUT / "09-manage.svg"))
        panel.query_one("#manage-title", Input).value = "Переименованный"
        await pilot.press("ctrl+s")
        await asyncio.sleep(1.0)
        await pilot.pause()
        check("rename reaches the chat", app.current.name == "Переименованный")

        issued: list[int] = []
        original_link = app.backend.invite_link

        async def link_spy(chat, **kwargs):
            issued.append(chat.id)
            return await original_link(chat, **kwargs)

        app.backend.invite_link = link_spy
        await pilot.press("ctrl+l")
        await asyncio.sleep(0.8)
        await pilot.pause()
        check("invite link is issued for this chat", issued == [app.current.id])
        app.backend.invite_link = original_link
        await pilot.press("escape")
        await pilot.pause()

        # read tracking: a chat you actually stop on is marked read, one you
        # only pass through on the way to another is not.  Seed the counters so
        # the check does not depend on what earlier steps already read.
        chats = app.query_one(ChatList)
        skipped, stayed = chats.chats[0], chats.chats[1]
        skipped.unread, stayed.unread = 5, 7
        chats.rebuild()
        await app.open_chat(skipped)
        await asyncio.sleep(0.2)
        await app.open_chat(stayed)
        await asyncio.sleep(READ_DWELL + 0.8)
        await pilot.pause()
        check("chat you stayed in is marked read", stayed.unread == 0)
        # the demo backend keeps injecting traffic, so assert the badge survived
        # rather than a fixed number
        check("chat you passed through keeps its badge", skipped.unread > 0)

        # help
        await pilot.press("ctrl+t")
        await pilot.press("f1")
        await asyncio.sleep(0.3)
        await pilot.pause()
        app.save_screenshot(str(OUT / "05-help.svg"))
        await pilot.press("escape")
        await pilot.pause()

    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed · screenshots in {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
