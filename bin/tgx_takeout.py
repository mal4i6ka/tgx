"""Выгрузка аккаунта — та самая «экспорт данных» из настроек, но из терминала.

Telegram даёт под это отдельный режим сессии: внутри него ограничения на
частоту мягче, поэтому выкачивать историю сотнями сообщений не запрещено.
Режим надо открыть, отработать и закрыть — незакрытый висит и мешает открыть
следующий, поэтому закрываем всегда, даже если посреди всё сломалось.

Пишем на диск по ходу дела, а не в память: у людей бывают чаты, которые в
память не влезают.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import tgx_net


class TakeoutError(RuntimeError):
    """Выгрузка не задалась."""


HINTS = {
    "TAKEOUT_INIT_DELAY": (
        "Telegram ставит выгрузку на паузу — это защита: если аккаунт угнали, у вас "
        "есть время заметить. Подтвердите запрос в другом своём Telegram (там придёт "
        "сообщение «Data export request») и повторите"),
    "TAKEOUT_REQUIRED": "этот вызов работает только внутри выгрузки",
    "TAKEOUT_INVALID": "выгрузка уже закрыта; начните заново",
    "FLOOD_WAIT": "слишком часто — подождите столько секунд, сколько назвал сервер",
    "CHANNEL_PRIVATE": "к этому чату больше нет доступа",
}


def _explain(exc: Exception) -> Exception:
    return tgx_net.explain(exc, HINTS, TakeoutError)


def _stamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


class Takeout:
    """Одна выгрузка: открыли, забрали, закрыли."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _tidy(self) -> Any:
        """Убрать из сессии пустой признак выгрузки.

        Хранилище сессии кладёт takeout_id в поле-блоб, и незаполненное поле
        читается как b'', а не как None. Telethon сравнивает с None — и решает,
        что выгрузка уже идёт; сервер же на b'' отвечает ошибкой упаковки, до
        всякой сети. Поэтому перед работой приводим пустое значение к None.
        Возвращаем то, что было, — чтобы можно было сказать, шла ли выгрузка.
        """
        session = self.client.session
        current = getattr(session, "takeout_id", None)
        if current is not None and not isinstance(current, int):
            session.takeout_id = None
            return None
        return current

    async def finish(self, *, success: bool = False) -> dict[str, Any]:
        """Закрыть висящую выгрузку.

        Незакрытая держит сессию: следующая попытка падает ещё до запроса,
        внутри Telethon. Отдельная команда нужна, потому что бросить выгрузку
        можно и не по своей воле — упал терминал, оборвалась сеть.
        """
        had = self._tidy()
        if had is None:
            return {"была открыта": False, "закрыта": True,
                    "что сделали": "сессия была помечена ошибочно, метку сняли"}
        ok = await self.client.end_takeout(success=success)
        return {"была открыта": True, "закрыта": bool(ok)}

    async def run(self, out: Path, *, chats: list[Any] | None = None,
                  limit: int = 0, files: bool = False, max_file_mb: int = 20,
                  contacts: bool = True,
                  progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        out = Path(out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        say = progress or (lambda _: None)
        summary: dict[str, Any] = {"куда": str(out), "начало":
                                   datetime.now(timezone.utc).isoformat(timespec="seconds")}

        self._tidy()
        takeout = self.client.takeout(
            finalize=True, contacts=contacts, users=True, chats=True,
            megagroups=True, channels=True, files=files or None,
            max_file_size=max_file_mb * 1024 * 1024 if files else None)

        try:
            async with takeout as session:
                if contacts:
                    say("контакты")
                    summary["контакты"] = await self._contacts(session, out)
                say("список чатов")
                summary["чаты"] = await self._dialogs(session, out)
                if chats:
                    summary["история"] = []
                    for name in chats:
                        say(f"история: {name}")
                        summary["история"].append(
                            await self._history(session, out, name, limit, files, max_file_mb))
        except ValueError as exc:
            if "still not been finished" in str(exc):
                raise TakeoutError(
                    "предыдущая выгрузка не закрыта — сессия всё ещё держит её. "
                    "Закройте её: tgx takeout-finish") from exc
            raise
        except Exception as exc:
            raise _explain(exc) from exc

        summary["конец"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary

    async def _contacts(self, session: Any, out: Path) -> dict[str, Any]:
        from telethon.tl import functions

        result = await session(functions.contacts.GetContactsRequest(hash=0))
        rows = [{"id": u.id, "имя": u.first_name, "фамилия": u.last_name,
                 "адрес": u.username, "телефон": u.phone}
                for u in getattr(result, "users", None) or []]
        path = out / "contacts.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return {"сколько": len(rows), "файл": path.name}

    async def _dialogs(self, session: Any, out: Path) -> dict[str, Any]:
        rows = []
        async for dialog in session.iter_dialogs():
            rows.append({"id": dialog.id, "название": dialog.name,
                         "вид": "канал" if dialog.is_channel else
                                "группа" if dialog.is_group else "личка",
                         "непрочитано": dialog.unread_count,
                         "последнее": _stamp(getattr(dialog, "date", None))})
        path = out / "dialogs.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return {"сколько": len(rows), "файл": path.name}

    async def _history(self, session: Any, out: Path, chat: Any, limit: int,
                       files: bool, max_file_mb: int) -> dict[str, Any]:
        entity = await session.get_entity(chat)
        name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(
            getattr(entity, "id", chat))
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in str(name))[:60].strip()
        path = out / f"{safe or 'chat'}.jsonl"
        media_dir = out / (safe or "chat")
        saved = 0
        count = 0
        with path.open("w", encoding="utf-8") as handle:
            # wait_time=0: внутри выгрузки сервер сам держит нужный темп
            async for message in session.iter_messages(entity, limit=limit or None, wait_time=0):
                row = {"id": message.id, "когда": _stamp(message.date),
                       "от": getattr(message.sender, "username", None) or
                            getattr(message, "sender_id", None),
                       "текст": message.text or ""}
                if message.media and files:
                    size = getattr(getattr(message, "file", None), "size", 0) or 0
                    if size <= max_file_mb * 1024 * 1024:
                        media_dir.mkdir(exist_ok=True)
                        where = await message.download_media(file=media_dir)
                        if where:
                            row["файл"] = Path(where).name
                            saved += 1
                    else:
                        row["файл пропущен"] = f"{size // 1024 // 1024} МБ"
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        return {"чат": name, "сообщений": count, "файлов": saved, "файл": path.name}
