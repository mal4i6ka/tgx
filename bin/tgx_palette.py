#!/usr/bin/env python3
"""Все команды tgx — внутри полноэкранного клиента.

Окно клавиатурное: у него два десятка сочетаний, и на них повешено то, что
делают каждую минуту. Но команд у tgx под четыре сотни, и вешать их на клавиши
некуда — да и не нужно: их ищут по названию, а не помнят наизусть.

Поэтому список берётся оттуда же, откуда его берёт коннектор для агентов, — из
дерева разбора командной строки. Новая команда появляется в окне сама, как и в
MCP; дописывать сюда ничего не надо. Это не экономия труда, а защита от
расхождения: два списка, которые надо помнить обновлять, однажды разойдутся.

Соединение при этом одно. Команда, открывшая своё, упёрлась бы в занятый файл
сессии — тот самый «database is locked», — поэтому на время выполнения ей
подсовывают уже открытый клиент окна, а его закрытие делают пустым действием.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any, Iterator

_lock = asyncio.Lock()


class Command:
    """Одна листовая команда: как её звать и что она просит."""

    __slots__ = ("path", "picks", "hint", "positional", "options", "flags")

    def __init__(self, path: tuple[str, ...], picks: dict[str, str], hint: str,
                 positional: list[dict[str, Any]], options: list[dict[str, Any]],
                 flags: list[dict[str, Any]]) -> None:
        self.path = path
        self.picks = picks
        self.hint = hint
        self.positional = positional
        self.options = options
        self.flags = flags

    @property
    def name(self) -> str:
        return "tgx " + " ".join(self.path)

    @property
    def needs_input(self) -> bool:
        return bool(self.positional or self.options or self.flags)

    def __repr__(self) -> str:                       # для отладки и снимков
        return f"<Command {self.name}>"


SKIP = {("ui",), ("auth",), ("banner",), ("tl-schema",)}


def _describe(action: argparse.Action) -> dict[str, Any]:
    return {"dest": action.dest, "help": action.help or "",
            "choices": list(action.choices) if action.choices else None,
            "many": action.nargs in {"+", "*"} or action.nargs == argparse.REMAINDER,
            "required": getattr(action, "required", False),
            "type": getattr(action, "type", None)}


def commands() -> list[Command]:
    """Все команды, которые имеет смысл звать из окна.

    Отсеиваем то, что окну не годится: само окно, вход по коду и выгрузку схемы —
    первые два спорят с ним за терминал, третья ничего не делает с аккаунтом.
    """
    import tgx
    import tgx_autotools

    found: list[Command] = []
    for path, parser, picks, hint in tgx_autotools.leaves(tgx.build_parser()):
        if not path or path[:1] in SKIP:
            continue
        positional, options, flags = [], [], []
        for action in parser._actions:
            if action.dest in {"help", argparse.SUPPRESS} or not action.dest:
                continue
            if not action.option_strings:
                positional.append(_describe(action))
            elif isinstance(action, (argparse._StoreTrueAction,
                                     argparse._StoreFalseAction)):
                flags.append(_describe(action))
            else:
                options.append(_describe(action))
        found.append(Command(path, picks, hint, positional, options, flags))
    found.sort(key=lambda c: c.path)
    return found


def search(all_commands: list[Command], query: str) -> Iterator[tuple[float, Command]]:
    """Простой поиск по названию и подсказке.

    Совпадение в названии весит больше, чем в описании: человек чаще помнит, как
    команда называется, чем как она объяснена.
    """
    needle = (query or "").strip().lower()
    for command in all_commands:
        name = " ".join(command.path).lower()
        if not needle:
            yield 0.5, command
            continue
        if needle in name:
            # чем ближе к началу, тем выше: «pay» должен вывести `pay …`, а не
            # `stories … --paid`
            yield 1.0 - name.index(needle) / (len(name) + 1) * 0.3, command
        elif needle in command.hint.lower():
            yield 0.4, command


async def run(command: Command, values: dict[str, Any], client: Any) -> Any:
    """Выполнить команду, одолжив ей уже открытое соединение окна."""
    import tgx
    import tgx_autotools

    class Borrowed:
        """Общий клиент под видом личного: закрытие — пустое действие."""

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def __call__(self, request: Any) -> Any:
            return self._real(request)

        async def disconnect(self) -> None:
            return None                      # окно закроет соединение само

        async def __aenter__(self) -> "Borrowed":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    borrowed = Borrowed(client)

    async def _lend() -> Any:
        return borrowed

    async def _already_in(_client: Any) -> None:
        return None

    # Подмена глобальная — значит, команды идут по одной. Две одновременные
    # вернули бы друг другу чужой клиент, и разобраться в этом было бы нечем.
    async with _lock:
        make_client, ensure_login = tgx.make_client, tgx.ensure_login
        tgx.make_client, tgx.ensure_login = _lend, _already_in
        try:
            return await tgx_autotools.execute(
                tgx.build_parser, command.path, command.picks, values)
        finally:
            tgx.make_client, tgx.ensure_login = make_client, ensure_login
