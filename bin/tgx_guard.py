#!/usr/bin/env python3
"""Сторож приглашений: в закрытый чат попадает ровно тот, кого звали.

Правило простое и жёсткое. Ссылка выписывается на одного конкретного человека,
живёт одно использование и записывается в журнал вместе с тем, кому она
предназначена. Дальше проверка сверяет, кто по ней на самом деле вошёл: если
это не тот человек — он удаляется из чата, а ссылка помечается нарушенной.

Ссылку легко переслать, и приглашённый может отдать её кому угодно — поэтому
сверка идёт по факту входа, а не по обещанию. Ссылку с `usage_limit=1` после
первого входа сервер закрывает сам, так что чужой по ней уже не пройдёт; но
если по ней успел войти не тот, его надо убрать — этим и занимается `check`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

DEFAULT_HOURS = 48


def journal_path() -> Path:
    base = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
    return base / "data" / "invite-guard.json"


class GuardError(RuntimeError):
    """Нарушено правило приглашений — с объяснением, что делать."""


def load(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or journal_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GuardError(f"{path} повреждён: {exc}") from exc


def save(rows: Sequence[dict[str, Any]], path: Path | None = None) -> Path:
    path = path or journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)          # журнал называет, кого куда звали
    except OSError:
        pass
    return path


def verdict(row: dict[str, Any], joined: Sequence[int]) -> tuple[str, list[int]]:
    """Кого выгнать по этой ссылке и как её теперь пометить.

    Отдельная функция без сети: именно здесь принимается решение выгнать
    человека, и его надо уметь проверить тестом, а не только на живых людях.
    """
    expected = row.get("for_user_id")
    strangers = [u for u in joined if u != expected]
    if not joined:
        return "ждёт", []
    if strangers:
        return "нарушена", strangers
    return "использована", []


class Guard:
    """Выписывает именные одноразовые ссылки и следит, кто по ним вошёл."""

    def __init__(self, client: Any, path: Path | None = None) -> None:
        self.client = client
        self.path = path or journal_path()

    async def issue(self, chat: Any, invitee: str, *, hours: int = DEFAULT_HOURS,
                    note: str = "") -> dict[str, Any]:
        """Выписать ссылку на одного человека: одно использование и срок."""
        from telethon.tl import functions

        person = await self.client.get_entity(invitee)
        peer = await self.client.get_input_entity(chat)
        label = f"@{person.username}" if getattr(person, "username", None) else str(person.id)
        expires = datetime.now(timezone.utc) + timedelta(hours=int(hours))

        result = await self.client(functions.messages.ExportChatInviteRequest(
            peer=peer, title=f"только для {label}"[:32], usage_limit=1,
            expire_date=expires, request_needed=False))

        row = {
            "link": result.link,
            "chat": str(getattr(chat, "id", chat)),
            "for_user_id": int(person.id),
            "for_label": label,
            "note": note,
            "issued": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expires": expires.isoformat(timespec="seconds"),
            "status": "ждёт",
            "joined": [],
        }
        rows = load(self.path)
        rows.append(row)
        save(rows, self.path)
        return row

    async def check(self, chat: Any, *, kick: bool = True) -> list[dict[str, Any]]:
        """Сверить, кто вошёл по каждой ссылке, и убрать чужих."""
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        rows = load(self.path)
        report = []
        for row in rows:
            if row["status"] not in {"ждёт", "нарушена"}:
                continue
            try:
                importers = await self.client(functions.messages.GetChatInviteImportersRequest(
                    peer=peer, link=row["link"], offset_date=None, offset_user=None, limit=20))
            except Exception as exc:
                row["error"] = str(exc)
                report.append({**row, "kicked": []})
                continue

            joined = [int(i.user_id) for i in (getattr(importers, "importers", None) or [])]
            row["joined"] = joined
            status, strangers = verdict(row, joined)
            row["status"] = status

            removed = []
            if strangers and kick:
                for user_id in strangers:
                    try:
                        await self._remove(peer, user_id)
                        removed.append(user_id)
                    except Exception as exc:
                        row.setdefault("errors", []).append(f"{user_id}: {exc}")
                await self.revoke(chat, row["link"])
            report.append({**row, "kicked": removed})
        save(rows, self.path)
        return report

    async def _remove(self, peer: Any, user_id: int) -> None:
        """Удалить из чата, не оставляя вечного бана: забанить и сразу разбанить."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(user_id)
        await self.client(functions.channels.EditBannedRequest(
            channel=peer, participant=target,
            banned_rights=types.ChatBannedRights(until_date=None, view_messages=True)))
        await self.client(functions.channels.EditBannedRequest(
            channel=peer, participant=target,
            banned_rights=types.ChatBannedRights(until_date=None)))

    async def revoke(self, chat: Any, link: str) -> dict[str, Any]:
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        await self.client(functions.messages.DeleteExportedChatInviteRequest(peer=peer, link=link))
        rows = load(self.path)
        for row in rows:
            if row["link"] == link and row["status"] == "ждёт":
                row["status"] = "отозвана"
        save(rows, self.path)
        return {"revoked": link}

    async def lock(self, chat: Any) -> dict[str, Any]:
        """Запретить обычным участникам звать людей — приглашать могут только админы.

        Без этого именные ссылки бессмысленны: любой участник позвал бы кого
        угодно в обход журнала.
        """
        from telethon.tl import functions, types

        peer = await self.client.get_input_entity(chat)
        await self.client(functions.messages.EditChatDefaultBannedRightsRequest(
            peer=peer, banned_rights=types.ChatBannedRights(until_date=None, invite_users=True)))
        return {"invite_users": "только администраторы"}

    def journal(self) -> list[dict[str, Any]]:
        return load(self.path)
