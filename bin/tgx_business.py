#!/usr/bin/env python3
"""Telegram Business: a bot as your secretary in personal chats.

The account side lives here — connecting a bot to your own private chats, what it
is allowed to do, which chats it may touch, plus greeting and away messages, work
hours, the profile intro and deep links. The bot side (receiving those chats and
replying) is the Bot API's `business_connection_id`, which the bot's own code uses.

Connecting a bot hands it your private correspondence, so nothing here is exposed
to agents over MCP — it is a decision for a person at a keyboard.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

# BusinessBotRights, in the order a person cares about them
RIGHTS = (
    ("reply", "отвечать в чатах"),
    ("read_messages", "читать переписку"),
    ("delete_sent_messages", "удалять свои сообщения"),
    ("delete_received_messages", "удалять входящие"),
    ("edit_name", "менять имя профиля"),
    ("edit_bio", "менять описание"),
    ("edit_profile_photo", "менять аватар"),
    ("edit_username", "менять @имя"),
    ("view_gifts", "смотреть подарки"),
    ("sell_gifts", "продавать подарки"),
    ("change_gift_settings", "настройки подарков"),
    ("transfer_and_upgrade_gifts", "передавать подарки"),
    ("transfer_stars", "переводить звёзды"),
    ("manage_stories", "вести истории"),
)
RIGHT_NAMES = {name for name, _ in RIGHTS}

SCOPES = ("all", "contacts", "non-contacts", "existing", "new")

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_ORDER = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class BusinessError(RuntimeError):
    """Something the account refused, or a setting that could not be understood."""


# ── parsing ──────────────────────────────────────────────────────────────────
def parse_rights(spec: str) -> dict[str, bool]:
    """`reply,read_messages` or `all` → the BusinessBotRights flags."""
    wanted = {piece.strip().replace("-", "_") for piece in (spec or "").split(",") if piece.strip()}
    if not wanted or wanted == {"none"}:
        return {name: False for name in RIGHT_NAMES}
    if wanted == {"all"}:
        return {name: True for name in RIGHT_NAMES}
    unknown = wanted - RIGHT_NAMES
    if unknown:
        raise BusinessError(f"неизвестные права: {', '.join(sorted(unknown))}; "
                            f"доступны: {', '.join(sorted(RIGHT_NAMES))}")
    return {name: name in wanted for name in RIGHT_NAMES}


def parse_scope(scope: str) -> dict[str, bool]:
    """Which chats the bot may handle."""
    scope = (scope or "all").strip().lower()
    if scope not in SCOPES:
        raise BusinessError(f"область «{scope}» неизвестна; доступны: {', '.join(SCOPES)}")
    return {
        "existing_chats": scope in {"all", "existing"},
        "new_chats": scope in {"all", "new"},
        "contacts": scope in {"all", "contacts"},
        "non_contacts": scope in {"all", "non-contacts"},
    }


def parse_hours(spec: str) -> list[tuple[int, int]]:
    """`пн-пт 9:00-18:00; сб 10:00-14:00` → weekly minute ranges from Monday 00:00."""
    ranges: list[tuple[int, int]] = []
    for piece in (spec or "").split(";"):
        piece = piece.strip().lower()
        if not piece:
            continue
        match = re.match(r"([a-zA-Zа-яё]{2,3})(?:\s*[-–]\s*([a-zA-Zа-яё]{2,3}))?\s+"
                         r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})$", piece)
        if not match:
            raise BusinessError(f"не разобрал «{piece}»: нужно «пн-пт 9:00-18:00»")
        first, last, start_h, start_m, end_h, end_m = match.groups()
        if first not in DAYS or (last and last not in DAYS):
            raise BusinessError(f"неизвестный день недели в «{piece}»")
        start_day, end_day = DAYS[first], DAYS[last or first]
        days = range(start_day, end_day + 1) if start_day <= end_day else list(range(start_day, 7)) + list(range(0, end_day + 1))
        start = int(start_h) * 60 + int(start_m)
        end = int(end_h) * 60 + int(end_m)
        if not 0 <= start < end <= 24 * 60:
            raise BusinessError(f"часы в «{piece}» вне суток или конец раньше начала")
        for day in days:
            ranges.append((day * 24 * 60 + start, day * 24 * 60 + end))
    if not ranges:
        raise BusinessError("расписание пустое")
    return sorted(ranges)


def describe_hours(ranges: Iterable[tuple[int, int]]) -> str:
    out = []
    for start, end in ranges:
        day = DAY_ORDER[(start // (24 * 60)) % 7]
        out.append(f"{day} {start % (24 * 60) // 60:02d}:{start % 60:02d}–{end % (24 * 60) // 60:02d}:{end % 60:02d}")
    return ", ".join(out)


def describe_scope(flags: dict[str, bool], included: Sequence[int] = (),
                   excluded: Sequence[int] = ()) -> str:
    """Say in words which private chats a connected bot may touch."""
    if flags.get("exclude_selected"):
        base = "все чаты"
    else:
        parts = [word for key, word in (("existing_chats", "существующие"), ("new_chats", "новые"),
                                        ("contacts", "контакты"), ("non_contacts", "не-контакты"))
                 if flags.get(key)]
        base = "все чаты" if len(parts) == 4 else (", ".join(parts) or "ничего")
    if excluded:
        base += f", кроме {len(excluded)}"
    if included and not flags.get("exclude_selected"):
        base += f" + {len(included)} выбранных"
    return base


def rollback_path() -> Path:
    base = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
    return base / "data" / "business-rollback.json"


# ── the account side ─────────────────────────────────────────────────────────
class Business:
    """Everything the *user's* account can set up for a business bot."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def connected_bots(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self.client(functions.account.GetConnectedBotsRequest())
        found = []
        for bot in getattr(result, "connected_bots", None) or []:
            rights = getattr(bot, "rights", None)
            recipients = getattr(bot, "recipients", None)
            flags = {field: bool(getattr(recipients, field, False))
                     for field in ("existing_chats", "new_chats", "contacts", "non_contacts",
                                   "exclude_selected")}
            # Under `exclude_selected` the picked users are the *excluded* ones, and
            # Telegram still returns them in `users` — reading `exclude_users` here
            # reported "no exclusions" for an account that had 26 of them.
            listed = list(getattr(recipients, "users", None) or [])
            other = list(getattr(recipients, "exclude_users", None) or [])
            excluded = listed if flags["exclude_selected"] else other
            included = other if flags["exclude_selected"] else listed
            found.append({
                "bot_id": getattr(bot, "bot_id", None),
                "rights": sorted(name for name, _ in RIGHTS if getattr(rights, name, False)),
                "recipients": flags,
                "included_users": included,
                "excluded_users": excluded,
                "scope": describe_scope(flags, included, excluded),
                "since": getattr(bot, "date", None),
            })
        return found

    async def _snapshot(self, bot_id: int) -> dict[str, Any]:
        """Everything needed to put a connection back exactly as it was.

        Access hashes come from the same response, so the rollback never depends on
        those users still being in the session cache.
        """
        from telethon.tl import functions

        result = await self.client(functions.account.GetConnectedBotsRequest())
        hashes = {u.id: getattr(u, "access_hash", None) for u in result.users}
        for bot in result.connected_bots:
            if bot.bot_id != bot_id:
                continue
            rec, rights = bot.recipients, bot.rights
            return {
                "bot_id": bot_id,
                "bot_username": next((u.username for u in result.users if u.id == bot_id), None),
                "bot_access_hash": hashes.get(bot_id),
                "rights": {name: bool(getattr(rights, name, False)) for name, _ in RIGHTS},
                "flags": {f: bool(getattr(rec, f, False)) for f in
                          ("existing_chats", "new_chats", "contacts", "non_contacts",
                           "exclude_selected")},
                "users": [[uid, hashes.get(uid)] for uid in (getattr(rec, "users", None) or [])],
                "exclude_users": [[uid, hashes.get(uid)]
                                  for uid in (getattr(rec, "exclude_users", None) or [])],
            }
        raise BusinessError(f"бот {bot_id} не подключён — нечего сохранять")

    def _save_rollback(self, snapshot: dict[str, Any]) -> Path:
        path = rollback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
        try:
            os.chmod(path, 0o600)          # it lists who you excluded, with access hashes
        except OSError:
            pass
        return path

    async def restore(self) -> dict[str, Any]:
        """Reconnect the bot that a `--replace` displaced, with its old settings.

        Telegram stamps the connection date itself, so that one field comes back as
        today — everything else is restored byte-for-byte.
        """
        from telethon.tl import functions, types

        path = rollback_path()
        if not path.exists():
            raise BusinessError(f"нечего восстанавливать: {path} не существует")
        snapshot = json.loads(path.read_text())
        await self.client(functions.account.UpdateConnectedBotRequest(
            bot=types.InputUser(snapshot["bot_id"], snapshot["bot_access_hash"]),
            recipients=types.InputBusinessBotRecipients(
                users=[types.InputUser(i, h) for i, h in snapshot["users"]] or None,
                exclude_users=[types.InputUser(i, h) for i, h in snapshot["exclude_users"]] or None,
                **snapshot["flags"]),
            rights=types.BusinessBotRights(**snapshot["rights"]),
            deleted=False))
        name = snapshot.get("bot_username")
        return {"restored": f"@{name}" if name else snapshot["bot_id"],
                "rights": sorted(k for k, v in snapshot["rights"].items() if v),
                "scope": describe_scope(snapshot["flags"], snapshot["users"],
                                        snapshot["exclude_users"])}

    async def _name(self, bot_id: int) -> str:
        try:
            entity = await self.client.get_entity(bot_id)
            return f"@{entity.username}" if getattr(entity, "username", None) else str(bot_id)
        except Exception:
            return str(bot_id)

    async def connect(self, bot: str, rights: dict[str, bool], scope: dict[str, bool],
                      exclude: Sequence[str] = (), *, replace: bool = False) -> dict[str, Any]:
        """Connect a bot to your private chats.

        Telegram keeps exactly one connected bot per account: connecting a second
        one silently disconnects the first. Verified live — the API documents no
        such limit, and the vector in `account.getConnectedBots` suggests otherwise.
        So an existing connection has to be displaced on purpose, never by accident.
        """
        from telethon.tl import functions, types

        entity = await self.client.get_input_entity(bot)
        already = await self.connected_bots()
        displaced = [b for b in already if b["bot_id"] != getattr(entity, "user_id", None)]
        if displaced and not replace:
            names = ", ".join([await self._name(b["bot_id"]) for b in displaced])
            raise BusinessError(
                f"к аккаунту уже подключён {names}; Telegram держит только одного. "
                f"Посмотрите его настройки — tgx business bots — и повторите с --replace, "
                f"если готовы его отключить")

        saved = None
        if displaced:
            saved = self._save_rollback(await self._snapshot(displaced[0]["bot_id"]))

        excluded = [await self.client.get_input_entity(u) for u in exclude]
        try:
            await self.client(functions.account.UpdateConnectedBotRequest(
                bot=entity,
                recipients=types.InputBusinessBotRecipients(exclude_users=excluded or None, **scope),
                rights=types.BusinessBotRights(**rights),
                deleted=False,
            ))
        except Exception as exc:
            if "BOT_BUSINESS_MISSING" in str(exc):
                raise BusinessError(
                    f"у бота {bot} выключен секретарский режим. Включите его — "
                    f"tgx bot secretary {bot} on — и повторите") from exc
            raise
        return {"bot": bot, "rights": sorted(k for k, v in rights.items() if v), "scope": scope,
                "displaced": [b["bot_id"] for b in displaced],
                "rollback": str(saved) if saved else None}

    async def disconnect(self, bot: str) -> None:
        from telethon.tl import functions, types

        entity = await self.client.get_input_entity(bot)
        await self.client(functions.account.UpdateConnectedBotRequest(
            bot=entity, recipients=types.InputBusinessBotRecipients(), deleted=True))

    async def pause(self, peer: Any, paused: bool = True) -> None:
        from telethon.tl import functions

        await self.client(functions.account.ToggleConnectedBotPausedRequest(peer=peer, paused=paused))

    async def disable_in(self, peer: Any) -> None:
        from telethon.tl import functions

        await self.client(functions.account.DisablePeerConnectedBotRequest(peer=peer))

    async def quick_replies(self) -> list[dict[str, Any]]:
        """Greeting and away messages point at a quick-reply shortcut by id."""
        from telethon.tl import functions

        result = await self.client(functions.messages.GetQuickRepliesRequest(hash=0))
        return [{"shortcut_id": getattr(s, "shortcut_id", None), "shortcut": getattr(s, "shortcut", ""),
                 "messages": getattr(s, "count", None)}
                for s in (getattr(result, "quick_replies", None) or [])]

    async def set_greeting(self, shortcut_id: int | None, scope: dict[str, bool] | None = None,
                           no_activity_days: int = 7) -> None:
        from telethon.tl import functions, types

        message = None
        if shortcut_id is not None:
            message = types.InputBusinessGreetingMessage(
                shortcut_id=int(shortcut_id),
                recipients=types.InputBusinessRecipients(**(scope or parse_scope("all"))),
                no_activity_days=int(no_activity_days),
            )
        await self.client(functions.account.UpdateBusinessGreetingMessageRequest(message=message))

    async def set_away(self, shortcut_id: int | None, schedule: str = "outside",
                       scope: dict[str, bool] | None = None, offline_only: bool = True) -> None:
        from telethon.tl import functions, types

        message = None
        if shortcut_id is not None:
            plans = {"always": types.BusinessAwayMessageScheduleAlways(),
                     "outside": types.BusinessAwayMessageScheduleOutsideWorkHours()}
            if schedule not in plans:
                raise BusinessError("расписание: always или outside")
            message = types.InputBusinessAwayMessage(
                shortcut_id=int(shortcut_id), schedule=plans[schedule],
                recipients=types.InputBusinessRecipients(**(scope or parse_scope("all"))),
                offline_only=offline_only,
            )
        await self.client(functions.account.UpdateBusinessAwayMessageRequest(message=message))

    async def set_hours(self, timezone_id: str, ranges: Sequence[tuple[int, int]] | None) -> None:
        from telethon.tl import functions, types

        hours = None
        if ranges:
            hours = types.BusinessWorkHours(
                timezone_id=timezone_id,
                weekly_open=[types.BusinessWeeklyOpen(start_minute=a, end_minute=b) for a, b in ranges],
            )
        await self.client(functions.account.UpdateBusinessWorkHoursRequest(business_work_hours=hours))

    async def set_intro(self, title: str | None, description: str | None) -> None:
        from telethon.tl import functions, types

        intro = None
        if title is not None or description is not None:
            intro = types.InputBusinessIntro(title=title or "", description=description or "", sticker=None)
        await self.client(functions.account.UpdateBusinessIntroRequest(intro=intro))

    async def create_link(self, message: str, title: str | None = None) -> dict[str, Any]:
        import tgx_format
        from telethon.tl import functions, types

        body, entities = tgx_format.parse(message, "md")
        link = await self.client(functions.account.CreateBusinessChatLinkRequest(
            link=types.InputBusinessChatLink(message=body, entities=entities or None, title=title)))
        return {"link": getattr(link, "link", ""), "title": getattr(link, "title", None)}

    async def links(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        result = await self.client(functions.account.GetBusinessChatLinksRequest())
        return [{"link": getattr(l, "link", ""), "title": getattr(l, "title", None),
                 "views": getattr(l, "views", None), "message": getattr(l, "message", "")}
                for l in (getattr(result, "links", None) or [])]
