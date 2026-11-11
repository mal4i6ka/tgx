#!/usr/bin/env python3
"""Вход по QR-коду: код рисуется в терминал, телефон его сканирует.

Telegram отдаёт одноразовую ссылку `tg://login?token=…`, живущую меньше минуты.
Ждать её импорта надо **непрерывно**: сервер сообщает о входе ровно тому
вызову, который в этот момент ждёт, — отпустить и вернуться позже нельзя, вход
не завершится. Поэтому ожидание и перерисовка истёкшего кода идут одним циклом.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

# Кодировщик QR — отдельная зависимость: своими руками это таблицы Галуа и
# маскирование, а не десять строк. segno чистый питон и без зависимостей.
QR_MISSING = ("для входа по QR нужен segno — поставьте его: "
              "pip install -r requirements.txt")


def available() -> bool:
    """Есть ли кодировщик. Через find_spec, а не пробным импортом: тот тянет
    модуль в память ради одного «да» и выглядит как забытая строка."""
    import importlib.util

    return importlib.util.find_spec("segno") is not None


class QrError(RuntimeError):
    """Не вышло войти по коду."""


def render(url: str, *, invert: bool = False, border: int = 2) -> str:
    """Матрица QR → полблока на знак, чтобы код влез в терминал по высоте.

    Рисуем **светлые** модули, а тёмные оставляем фоном: на тёмном терминале
    это и есть правильная полярность, и зона тишины по краю выходит светлой —
    без неё сканер код не находит. На светлом терминале всё наоборот, поэтому
    есть `invert`: иначе код просто не читается, молча и без объяснений.
    """
    try:
        import segno
    except ModuleNotFoundError as exc:
        raise QrError(QR_MISSING) from exc

    # Уровень L: ссылка короткая и живёт полминуты, запас на грязь не нужен,
    # зато символ выходит меньше и влезает в невысокое окно.
    code = segno.make(url, error="l")
    rows = [[bool(cell) for cell in row] for row in code.matrix]
    width = len(rows[0]) + border * 2
    quiet = [[False] * width for _ in range(border)]
    body = [[False] * border + row + [False] * border for row in rows]
    grid = quiet + body + quiet
    if len(grid) % 2:
        grid.append([False] * width)

    # True в матрице — тёмный модуль. «Чернила» терминала кладём на светлые.
    def ink(dark: bool) -> bool:
        return dark if invert else not dark

    lines = []
    for top, bottom in zip(grid[::2], grid[1::2]):
        line = []
        for upper, lower in zip(top, bottom):
            up, low = ink(upper), ink(lower)
            line.append("█" if up and low else "▀" if up else "▄" if low else " ")
        lines.append("".join(line))
    return "\n".join(lines)


async def login(
    client: Any,
    *,
    invert: bool = False,
    show: Callable[[str], None],
    ask_password: Callable[[], str],
) -> Any:
    """Войти по QR. Возвращает того, кто вошёл.

    Код живёт меньше минуты. Истёк — Telethon сообщает об этом исключением по
    таймауту, и тогда нужен новый токен и новый рисунок; это не ошибка, а
    обычный ход дела, поэтому цикл, а не отказ. Если на аккаунте двухфакторная,
    сканирование доводит дело до пароля и просит его отдельно.
    """
    from telethon.errors import SessionPasswordNeededError

    if not available():
        raise QrError(QR_MISSING)

    qr = await client.qr_login()
    while True:
        show(render(qr.url, invert=invert))
        try:
            return await qr.wait()
        except asyncio.TimeoutError:
            await qr.recreate()          # код истёк — рисуем следующий
        except SessionPasswordNeededError:
            return await client.sign_in(password=ask_password())
