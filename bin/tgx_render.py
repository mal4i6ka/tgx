#!/usr/bin/env python3
"""Presentation layer shared by the tgx CLI and the tgx TUI.

Rule of the house: colour is for humans only.  When stdout is not a terminal —
or NO_COLOR / TGX_PLAIN / --plain are in play — every printer here falls back to
exactly the byte stream the pre-TUI CLI produced, so pipes and scripts keep
working untouched.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Iterable, Sequence

try:
    from rich.box import ROUNDED
    from rich.console import Console
    from rich.json import JSON
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    RICH = True
except ModuleNotFoundError:  # degrade gracefully instead of exploding
    RICH = False


# --- palette -----------------------------------------------------------------
# Telegram-flavoured, shared with the Textual stylesheet so CLI and TUI match.
ACCENT = "#2AABEE"
PALETTE = {
    "accent": ACCENT,
    "accent_dim": "#229ED9",
    "ok": "#4FCE5D",
    "warn": "#E5CA77",
    "err": "#E9576B",
    "muted": "#6D7F8F",
    "text": "#E4EDF5",
    "own": "#8AD5A0",
}

# Telegram's own seven sender colours, picked by a stable hash of the sender id.
NAME_COLORS = ("#E17076", "#7BC862", "#E5CA77", "#65AADD", "#A695E7", "#EE7AAE", "#6EC9CB")

KIND_ICONS = {
    "channel": "📣",
    "group": "👥",
    "user": "👤",
    "bot": "🤖",
    "chat": "💬",
    "folder": "🗂",
}

_FORCE_PLAIN = False
_CONSOLE: Any = None
_ERR_CONSOLE: Any = None


def set_plain(value: bool) -> None:
    """Force machine-readable output for the rest of the process."""
    global _FORCE_PLAIN, _CONSOLE, _ERR_CONSOLE
    _FORCE_PLAIN = bool(value)
    _CONSOLE = _ERR_CONSOLE = None


def pretty() -> bool:
    if _FORCE_PLAIN or not RICH:
        return False
    if os.environ.get("TGX_PLAIN") or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def console(stderr: bool = False) -> Any:
    global _CONSOLE, _ERR_CONSOLE
    if stderr:
        if _ERR_CONSOLE is None:
            _ERR_CONSOLE = Console(stderr=True, highlight=False)
        return _ERR_CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Console(highlight=False, soft_wrap=False)
    return _CONSOLE


def name_color(seed: Any) -> str:
    try:
        n = int(seed)
    except (TypeError, ValueError):
        n = sum(ord(c) for c in str(seed or ""))
    return NAME_COLORS[abs(n) % len(NAME_COLORS)]


def kind_icon(kind: str) -> str:
    return KIND_ICONS.get(str(kind or "").lower(), "•")


# --- primitives --------------------------------------------------------------
def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def print_jsonl(items: Iterable[dict[str, Any]]) -> None:
    """Always raw — jsonl exists to be parsed."""
    for item in items:
        print(json.dumps(item, ensure_ascii=False, default=str))


def emit(obj: Any, title: str | None = None) -> None:
    """Print a command result: syntax-highlighted for humans, plain JSON for pipes."""
    if not pretty():
        print(dumps(obj))
        return
    c = console()
    if isinstance(obj, dict) and _is_flat_result(obj):
        c.print(_result_panel(obj, title))
        return
    body = JSON(dumps(obj), indent=2)
    body.text.no_wrap = False          # long values wrap instead of being cropped
    body.text.overflow = "fold"
    c.print(Panel(body, border_style=PALETTE["muted"], box=ROUNDED, title=_dim_title(title), title_align="left", padding=(0, 1)))


def fail(message: str) -> None:
    """A refusal the user can act on: one sentence, on stderr, no stack trace."""
    if not pretty():
        print(dumps({"ok": False, "error": message}), file=sys.stderr)
        return
    console(stderr=True).print(
        Panel(Text(message, style=PALETTE["text"]), border_style=PALETTE["err"],
              box=ROUNDED, title=_dim_title("не вышло"), title_align="left", padding=(0, 1)))


def note(message: str) -> None:
    """Предупреждение рядом с успехом: команда сработала, но кое-что стоит знать."""
    if not pretty():
        print(dumps({"note": message}), file=sys.stderr)
        return
    console(stderr=True).print(Text(f"  ⚠ {message}", style=PALETTE["warn"]))


def _is_flat_result(obj: dict[str, Any]) -> bool:
    """Small dicts of scalars read better as a key/value card than as JSON."""
    if len(obj) > 8:
        return False
    return all(v is None or isinstance(v, (str, int, float, bool)) for v in obj.values())


def _dim_title(title: str | None) -> Any:
    if not title:
        return None
    return Text(f" {title} ", style=f"bold {ACCENT}")


def _result_panel(obj: dict[str, Any], title: str | None) -> Any:
    ok = obj.get("ok")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=PALETTE["muted"], justify="right")
    grid.add_column(style=PALETTE["text"])
    for key, value in obj.items():
        if key == "ok":
            continue
        grid.add_row(key, _value_text(value))
    if not grid.row_count:
        grid.add_row("ok", _value_text(ok))
    head = title or ("done" if ok else "result")
    style = PALETTE["ok"] if ok else ACCENT
    mark = "✓ " if ok else ""
    return Panel(grid, border_style=style, box=ROUNDED, title=Text(f" {mark}{head} ", style=f"bold {style}"), title_align="left", padding=(0, 1))


def _value_text(value: Any) -> Any:
    if value is True:
        return Text("yes", style=PALETTE["ok"])
    if value is False:
        return Text("no", style=PALETTE["err"])
    if value is None:
        return Text("—", style=PALETTE["muted"])
    if isinstance(value, (int, float)):
        return Text(str(value), style=f"bold {ACCENT}")
    return Text(str(value))


def print_table(rows: list[dict[str, Any]], fields: Sequence[str], title: str | None = None) -> None:
    if not rows:
        if pretty():
            console().print(Text("  nothing here", style=f"italic {PALETTE['muted']}"))
        return
    if not pretty():
        _plain_table(rows, list(fields))
        return

    table = Table(
        box=ROUNDED,
        border_style="#2C3E50",
        header_style=f"bold {ACCENT}",
        title=_dim_title(title),
        title_justify="left",
        expand=False,
        pad_edge=False,
    )
    numeric = {f for f in fields if all(isinstance(r.get(f), (int, float)) or r.get(f) in (None, "") for r in rows)}
    for f in fields:
        table.add_column(f, justify="right" if f in numeric else "left", overflow="fold", max_width=60, no_wrap=f in {"id", "kind"})
    for r in rows:
        cells = []
        for f in fields:
            value = r.get(f)
            if f == "kind":
                cells.append(Text(f"{kind_icon(value)} {value}", style=PALETTE["muted"]))
            elif f in {"name", "title"}:
                cells.append(Text(str(value or ""), style="bold"))
            elif f == "unread":
                n = int(value or 0)
                cells.append(Text(str(n) if n else "·", style=f"bold {ACCENT}" if n else PALETTE["muted"]))
            elif f == "username":
                text = str(value or "")
                cells.append(Text(("@" + text if text and not text.startswith("@") else text) or "—", style=PALETTE["accent_dim"] if text else PALETTE["muted"]))
            elif f == "id":
                cells.append(Text(str(value if value is not None else ""), style=PALETTE["muted"]))
            else:
                cells.append(_value_text(value))
        table.add_row(*cells)
    c = console()
    c.print(table)
    c.print(Text(f"  {len(rows)} row{'s' if len(rows) != 1 else ''}", style=PALETTE["muted"]))


def _plain_table(rows: list[dict[str, Any]], fields: list[str]) -> None:
    widths = {f: min(max(len(str(r.get(f, ""))) for r in rows + [{f: f}]), 40) for f in fields}
    print("  ".join(f.ljust(widths[f]) for f in fields))
    print("  ".join("-" * widths[f] for f in fields))
    for r in rows:
        vals = []
        for f in fields:
            s = str(r.get(f, ""))
            if len(s) > widths[f]:
                s = s[: widths[f] - 1] + "…"
            vals.append(s.ljust(widths[f]))
        print("  ".join(vals))


def print_messages(rows: list[dict[str, Any]], title: str | None = None, show_chat: bool = False) -> None:
    """Chat-shaped transcript for `history` / `search`."""
    if not pretty():
        for m in rows:
            if show_chat:
                print(f"[{m['date']}] {m.get('chat')} #{m['id']}: {m['text']}")
            else:
                who = m.get("sender") or m.get("sender_id") or "?"
                print(f"[{m['date']}] {m['id']} {who}: {m['text']}")
        return

    c = console()
    if title:
        c.print(Rule(Text(f" {title} ", style=f"bold {ACCENT}"), style="#2C3E50"))
    day = None
    for m in rows:
        stamp = str(m.get("date") or "")
        this_day, _, clock = stamp.partition("T")
        if this_day != day:
            day = this_day
            c.print(Rule(Text(f" {day or '—'} ", style=PALETTE["muted"]), style="#22303C"))
        who = str(m.get("chat") if show_chat else (m.get("sender") or m.get("sender_id") or "?"))
        head = Text()
        head.append(f"{clock[:5] or '--:--'} ", style=PALETTE["muted"])
        head.append(who, style=f"bold {name_color(m.get('sender_id') or m.get('chat_id') or who)}")
        meta = []
        if m.get("views"):
            meta.append(f"👁 {m['views']}")
        if m.get("reply_to"):
            meta.append(f"↩ {m['reply_to']}")
        meta.append(f"#{m.get('id')}")
        head.append("  " + " · ".join(meta), style=PALETTE["muted"])
        c.print(head)
        text = str(m.get("text") or "")
        if text:
            body = Text(text, style=PALETTE["text"])
            body.pad_left(2)
            c.print(body)
        else:
            c.print(Text("  (no text)", style=f"italic {PALETTE['muted']}"))
    c.print(Text(f"\n  {len(rows)} message{'s' if len(rows) != 1 else ''}", style=PALETTE["muted"]))


def hint(message: str) -> None:
    """A one-line aside on stderr — never pollutes piped stdout."""
    if pretty() or sys.stderr.isatty():
        if RICH:
            console(stderr=True).print(Text(f"  {message}", style=PALETTE["muted"]))
            return
    print(message, file=sys.stderr)
