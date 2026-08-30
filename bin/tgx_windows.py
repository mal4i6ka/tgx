#!/usr/bin/env python3
"""Окна, которыми может пользоваться агент.

`tgx` открывает локальные окна для того, чего терминал не умеет: мини-приложение
бота, живая картина звонка. Человек их видит и жмёт кнопки, а агент — нет: окно
живёт в браузере, MCP-сервер в другом процессе, и общего между ними ничего.

Здесь общее появляется. Устройство трёхслойное, и каждый слой нужен по своей
причине:

* **Реестр на диске.** Окно поднимается одной командой, а спрашивает про него
  другой процесс. Без записи на диске он не узнает даже адреса. Записи чистятся
  по живости процесса: окно может умереть, не убравшись за собой.
* **Состояние, которое шлёт страница.** Что происходит внутри мини-приложения,
  знает только браузер: приложение чужое, его пиксели нам не видны. Зато оно
  разговаривает с нашей страницей, и всё сказанное страница пересылает нам.
* **Поручения.** Агент просит нажать кнопку — но нажимать её должна страница.
  Поручение кладётся в очередь, страница его забирает, выполняет и приносит
  ответ. Круг замыкается за доли секунды, и агент получает результат, а не
  «отправлено».

Набор действий у окна свой: страница объявляет их сама, как в WebMCP. Встроенные
— нажать главную кнопку, нажать «назад», прочитать журнал, закрыть.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent.parent / "data"
REGISTRY = DATA / "windows.json"


class WindowError(RuntimeError):
    """С окном не вышло."""


def _alive(pid: int) -> bool:
    """Жив ли процесс. Мёртвые записи в реестре — обычное дело.

    Отказ в праве послать сигнал означает, что процесс есть, но чужой: это
    «жив», а не «мёртв». Спутать легко, и тогда реестр молча вычистит рабочее
    окно.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _load() -> list[dict[str, Any]]:
    try:
        rows = json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _save(rows: list[dict[str, Any]]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def register(name: str, kind: str, url: str) -> None:
    """Записать окно в реестр, вычистив мёртвые."""
    rows = [r for r in _load() if _alive(int(r.get("pid", 0))) and r.get("url") != url]
    rows.append({"имя": name, "вид": kind, "адрес": url, "pid": os.getpid(),
                 "открыто": time.strftime("%H:%M:%S")})
    _save(rows)


def unregister(url: str) -> None:
    _save([r for r in _load() if r.get("url") != url])


def listing() -> list[dict[str, Any]]:
    """Живые окна. Мёртвые записи заодно выбрасываются."""
    rows = [r for r in _load() if _alive(int(r.get("pid", 0)))]
    if len(rows) != len(_load()):
        _save(rows)
    return rows


def find(name: str = "") -> dict[str, Any]:
    """Окно по имени или адресу. Без имени — единственное открытое."""
    rows = listing()
    if not rows:
        raise WindowError("открытых окон нет; их поднимают `tgx inline run` и `tgx call watch`")
    def label(row: dict[str, Any]) -> str:
        """Имя, а при совпадении имён — с адресом: иначе выбирать не из чего."""
        same = sum(1 for other in rows if other.get("имя") == row.get("имя"))
        return f"{row.get('имя')} ({row.get('адрес')})" if same > 1 else str(row.get("имя"))

    if not name:
        if len(rows) > 1:
            raise WindowError("окон несколько — назовите одно из: "
                              + ", ".join(label(r) for r in rows))
        return rows[0]
    matched = [r for r in rows if name in (r.get("имя"), r.get("адрес"))]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise WindowError(f"окон с именем «{name}» несколько — назовите адрес: "
                          + ", ".join(str(r.get("адрес")) for r in matched))
    raise WindowError(f"окна «{name}» нет; открыты: " + ", ".join(label(r) for r in rows))


class Bridge:
    """Мостик между страницей и тем, кто её спрашивает.

    Живёт внутри процесса, поднявшего окно. Страница шлёт сюда состояние и
    забирает поручения; снаружи сюда приходят по HTTP.
    """

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"журнал": [], "кнопки": {}, "данные": []}
        self.tools: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}
        self.done = threading.Event()
        self._next = 0
        self._lock = threading.Lock()

    # --- со стороны страницы ---

    def push_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.state.update(state)

    def push_tools(self, tools: list[dict[str, Any]]) -> None:
        with self._lock:
            self.tools = tools

    def take_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            taken, self.pending = self.pending, []
        return taken

    def push_result(self, ticket: str, value: Any) -> None:
        with self._lock:
            self.results[ticket] = value
        self.done.set()

    # --- со стороны агента ---

    def ask(self, tool: str, args: dict[str, Any] | None = None,
            timeout: float = 10.0) -> Any:
        """Поручить странице действие и дождаться ответа.

        Ждём именно ответа, а не отправки: «нажал» без результата — это отчёт о
        намерении, по которому агент не может судить, что вышло.
        """
        with self._lock:
            self._next += 1
            ticket = str(self._next)
            self.pending.append({"ticket": ticket, "tool": tool, "args": args or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if ticket in self.results:
                    return self.results.pop(ticket)
            self.done.wait(0.05)
            self.done.clear()
        raise WindowError(f"страница не ответила на «{tool}» за {timeout:g} с — "
                          f"возможно, окно закрыли")


def _http(url: str, body: Any = None, timeout: float = 12.0) -> Any:
    """Короткий разговор с окном. Окно всегда на 127.0.0.1 — наружу не ходим."""
    import urllib.error
    import urllib.request

    if not url.startswith("http://127.0.0.1:"):
        raise WindowError("окна живут только на 127.0.0.1; чужой адрес не спрашиваем")
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return json.loads(answer.read().decode() or "null")
    except urllib.error.URLError as exc:
        raise WindowError(f"окно не отвечает: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise WindowError("окно ответило не по-нашему") from exc


def snapshot(name: str = "") -> dict[str, Any]:
    """Что сейчас в окне и что в нём можно сделать."""
    window = find(name)
    got = _http(window["адрес"].rstrip("/") + "/mcp/snapshot")
    return {"окно": window.get("имя"), "вид": window.get("вид"), **(got or {})}


def call(name: str, tool: str, args: dict[str, Any] | None = None,
         timeout: float = 10.0) -> Any:
    """Поручить окну действие и дождаться, что вышло."""
    window = find(name)
    got = _http(window["адрес"].rstrip("/") + "/mcp/ask",
                {"tool": tool, "args": args or {}, "timeout": timeout},
                timeout=timeout + 5)
    if not (got or {}).get("ok"):
        raise WindowError((got or {}).get("error") or "окно отказало без объяснений")
    return got.get("результат")


# Кусок страницы, дающий ей голос: состояние наружу, поручения внутрь.
# Списан с WebMCP по смыслу: страница объявляет свои действия сама.
BRIDGE_JS = """
window.tgx = {
  tools: [],
  registerTool(name, description, schema, handler) {
    this.tools.push({name, description, schema: schema || {}, handler});
    push('/mcp/tools', this.tools.map(t => ({name: t.name, description: t.description,
                                             schema: t.schema})));
  },
  state: {},
  setState(patch) { Object.assign(this.state, patch); push('/mcp/state', this.state); },
};

function push(path, body) {
  fetch(path, {method: 'POST', body: JSON.stringify(body)}).catch(() => {});
}

async function poll() {
  let jobs = [];
  try { jobs = await (await fetch('/mcp/pending')).json(); } catch (e) { return; }
  for (const job of jobs) {
    let value;
    try {
      const tool = window.tgx.tools.find(t => t.name === job.tool);
      value = tool ? await tool.handler(job.args || {}) : {error: 'нет такого действия'};
    } catch (e) { value = {error: String(e)}; }
    push('/mcp/result', {ticket: job.ticket, value: value === undefined ? {ok: true} : value});
  }
}
setInterval(poll, 200);
"""
