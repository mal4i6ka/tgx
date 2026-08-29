#!/usr/bin/env python3
"""Превращение всего CLI в инструменты MCP — без ручной обвязки.

У argparse уже есть всё, что нужно инструменту: имя команды, её описание,
позиционные и необязательные аргументы, их типы, значения по умолчанию и
подсказки. Поэтому дерево разбора обходится один раз, и каждая листовая
команда становится инструментом. Новая команда в CLI появляется в MCP сама —
дописывать здесь ничего не нужно.

Команды, которым нужен живой человек за клавиатурой (вход по коду из SMS,
запуск TUI), пропускаются: в переписке с агентом они бы просто зависли.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from typing import Any, Callable, Iterator

# Диалог с пользователем или бесконечный цикл — инструментом быть не может.
SKIP = {("ui",), ("auth",), ("banner",), ("tl-schema",)}

# Аргументы, которые в MCP не имеют смысла: вывод и так разбирается машиной.
DROP = {"jsonl", "json"}


def leaves(parser: argparse.ArgumentParser) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser, dict[str, str], str]]:
    """Все листовые команды: путь, разборщик, выбор на каждом уровне и подсказка.

    Подсказка команды живёт не в её собственном разборщике, а в родительском
    списке выбора, поэтому её приходится собирать по дороге вниз.
    """

    def walk(node: argparse.ArgumentParser, path: tuple[str, ...],
             picks: dict[str, str], hint: str) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser, dict[str, str], str]]:
        groups = [a for a in node._actions if isinstance(a, argparse._SubParsersAction)]
        if not groups:
            yield path, node, picks, hint
            return
        for group in groups:
            hints = {a.dest: (a.help or "") for a in getattr(group, "_choices_actions", [])}
            for name, child in group.choices.items():
                chosen = dict(picks)
                if group.dest and group.dest != argparse.SUPPRESS:
                    chosen[group.dest] = name
                yield from walk(child, path + (name,), chosen, hints.get(name, "") or hint)

    yield from walk(parser, (), {}, "")


def tool_name(path: tuple[str, ...]) -> str:
    return "cli_" + "_".join(path).replace("-", "_")


def annotation(action: argparse.Action) -> tuple[str, Any]:
    """Тип аргумента для схемы инструмента и значение по умолчанию."""
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "bool", bool(action.default)
    inner = {int: "int", float: "float"}.get(action.type, "str")
    if action.nargs in {"+", "*"} or isinstance(action, argparse._AppendAction):
        return f"list[{inner}]", None
    return inner, action.default


def describe(path: tuple[str, ...], parser: argparse.ArgumentParser,
             fields: list[tuple[str, argparse.Action]], hint: str = "") -> str:
    """Описание для модели: что делает команда и что значит каждый аргумент."""
    head = (parser.description or hint or "").strip()
    if not head or head.startswith("usage:"):
        head = " ".join(path)
    lines = [f"tgx {' '.join(path)} — {head}"]
    for name, action in fields:
        note = (action.help or "").strip()
        if action.choices:
            note = f"{note} ({', '.join(str(c) for c in action.choices)})".strip()
        if note:
            lines.append(f"  {name}: {note}")
    return "\n".join(lines)


def build(parser_factory: Callable[[], argparse.ArgumentParser],
          runner: Callable[..., Any]) -> list[dict[str, Any]]:
    """Собрать описания инструментов для каждой листовой команды CLI.

    `runner(path, picks, values)` — сопрограмма, которая выполняет команду.
    Возвращаются готовые к регистрации функции с настоящими сигнатурами:
    схему инструмента SDK строит по ним, поэтому подделать её нельзя.
    """
    parser = parser_factory()
    # `profile photos` и старый плоский псевдоним `profile-photos` дают одно имя.
    # Побеждает вложенная форма: плоская — её же псевдоним, и без этого правила
    # один из двух инструментов молча терялся бы при регистрации.
    best: dict[str, int] = {}
    for path, _leaf, _picks, _hint in leaves(parser):
        if path not in SKIP:
            best[tool_name(path)] = max(best.get(tool_name(path), 0), len(path))

    tools = []
    for path, leaf, picks, hint in leaves(parser):
        if path in SKIP or len(path) < best.get(tool_name(path), 0):
            continue
        fields = [(a.dest, a) for a in leaf._actions
                  if a.dest not in {"help", argparse.SUPPRESS} and a.dest not in DROP
                  and not isinstance(a, argparse._SubParsersAction)]

        required, optional = [], []
        for name, action in fields:
            kind, default = annotation(action)
            if not action.option_strings and action.nargs not in {"?", "*"}:
                required.append(f"{name}: {kind}")
            else:
                optional.append(f"{name}: {kind} | None = {default!r}"
                                if kind not in {"bool"} else f"{name}: bool = {default!r}")

        name = tool_name(path)
        signature = ", ".join(required + optional)
        body = (
            f"async def {name}({signature}):\n"
            f"    return await _run({path!r}, {picks!r}, dict(locals()))\n"
        )
        scope: dict[str, Any] = {"_run": runner}
        exec(compile(body, f"<tgx:{name}>", "exec"), scope)     # настоящая сигнатура
        function = scope[name]
        function.__doc__ = describe(path, leaf, fields, hint)
        tools.append({"name": name, "function": function, "path": path,
                      "description": function.__doc__})
    return tools


async def execute(parser_factory: Callable[[], argparse.ArgumentParser],
                  path: tuple[str, ...], picks: dict[str, str],
                  values: dict[str, Any]) -> Any:
    """Выполнить команду CLI и вернуть её вывод разобранным.

    Печать перехватывается: у MCP по stdout идёт сам протокол, и одна строка
    мимо него рвёт соединение.
    """
    parser = parser_factory()
    # `func` может стоять на любом уровне: у группы `profile`, а не у листа
    # `photos`. Поэтому значения по умолчанию собираются со всей цепочки.
    node: argparse.ArgumentParser = parser
    chain = [parser]
    for step in path:
        for group in node._actions:
            if isinstance(group, argparse._SubParsersAction) and step in group.choices:
                node = group.choices[step]
                chain.append(node)
                break

    namespace = argparse.Namespace()
    for action in node._actions:
        if action.dest not in {"help", argparse.SUPPRESS}:
            setattr(namespace, action.dest, action.default)
    for holder in chain:
        for key, value in getattr(holder, "_defaults", {}).items():
            setattr(namespace, key, value)
    for key, value in picks.items():
        setattr(namespace, key, value)
    for key, value in values.items():
        if value is not None:
            setattr(namespace, key, value)
    namespace.jsonl = True

    handler = getattr(namespace, "func", None)
    if handler is None:
        raise RuntimeError(f"у команды {' '.join(path)} нет обработчика")

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        await handler(namespace)
    return parse(buffer.getvalue())


def parse(text: str) -> Any:
    """Вывод CLI — либо один JSON, либо строки JSONL, либо обычный текст."""
    body = text.strip()
    if not body:
        return {"ok": True}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    rows, plain = [], False
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            plain = True
            break
    return {"output": body} if plain or not rows else rows
