#!/usr/bin/env python3
"""Telegraph articles: markdown in, a page with Instant View out.

Telegraph is a plain HTTP service, not part of MTProto, so this module talks to
api.telegra.ph directly (stdlib urllib — no extra dependency). The access token
it hands out lets anyone edit your pages, so it is stored 0600 and shown masked.

Telegraph's own content format is a node tree, not HTML text, and it only knows a
narrow set of tags — h3/h4, p, blockquote, pre, ul/ol/li, hr, img, a, b, i, code,
s — so the converter below maps markdown onto exactly that.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

API = "https://api.telegra.ph"
TIMEOUT = 30


class ArticleError(RuntimeError):
    """Something Telegraph refused, with its own wording kept."""


def token_path() -> Path:
    base = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
    return base / "data" / "telegraph.json"


def load_account() -> dict[str, Any]:
    path = token_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ArticleError(f"{path} повреждён: {exc}") from exc


def save_account(data: dict[str, Any]) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)          # the token can edit every page you published
    except OSError:
        pass


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if token and len(token) > 10 else ""


# ── markdown → Telegraph nodes ───────────────────────────────────────────────
INLINE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)"          # image
    r"|\[(?P<text>[^\]]+)\]\((?P<href>[^)]+)\)"        # link
    r"|\*\*(?P<bold>[^*]+)\*\*"
    r"|(?<!\*)\*(?P<italic>[^*]+)\*(?!\*)"
    r"|__(?P<italic2>[^_]+)__"
    r"|~~(?P<strike>[^~]+)~~"
    r"|`(?P<code>[^`]+)`"
)


def inline_nodes(text: str) -> list[Any]:
    """Inline markdown → a list of strings and tag nodes."""
    nodes: list[Any] = []
    position = 0
    for match in INLINE.finditer(text):
        if match.start() > position:
            nodes.append(text[position:match.start()])
        groups = match.groupdict()
        if groups["src"] is not None:
            nodes.append({"tag": "img", "attrs": {"src": groups["src"], "alt": groups["alt"] or ""}})
        elif groups["href"] is not None:
            nodes.append({"tag": "a", "attrs": {"href": groups["href"]},
                          "children": inline_nodes(groups["text"])})
        elif groups["bold"] is not None:
            nodes.append({"tag": "b", "children": inline_nodes(groups["bold"])})
        elif groups["italic"] is not None:
            nodes.append({"tag": "i", "children": inline_nodes(groups["italic"])})
        elif groups["italic2"] is not None:
            nodes.append({"tag": "i", "children": inline_nodes(groups["italic2"])})
        elif groups["strike"] is not None:
            nodes.append({"tag": "s", "children": inline_nodes(groups["strike"])})
        elif groups["code"] is not None:
            nodes.append({"tag": "code", "children": [groups["code"]]})
        position = match.end()
    if position < len(text):
        nodes.append(text[position:])
    return nodes or [""]


def markdown_to_nodes(markdown: str) -> list[Any]:
    """Markdown → the node tree Telegraph expects."""
    nodes: list[Any] = []
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):                                  # fenced code
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            nodes.append({"tag": "pre", "children": ["\n".join(body)]})
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):             # horizontal rule
            nodes.append({"tag": "hr"})
            index += 1
            continue

        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:                                                     # Telegraph has h3 and h4 only
            tag = "h3" if len(heading.group(1)) == 1 else "h4"
            nodes.append({"tag": tag, "children": inline_nodes(heading.group(2).strip())})
            index += 1
            continue

        if stripped.startswith(">"):                                    # blockquote, joined lines
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            nodes.append({"tag": "blockquote", "children": inline_nodes(" ".join(quote))})
            continue

        bullet = re.match(r"([-*+]|\d+[.)])\s+(.*)", stripped)
        if bullet:                                                      # list, ordered or not
            ordered = not bullet.group(1) in {"-", "*", "+"}
            items = []
            while index < len(lines):
                match = re.match(r"([-*+]|\d+[.)])\s+(.*)", lines[index].strip())
                if not match or (not match.group(1) in {"-", "*", "+"}) != ordered:
                    break
                items.append({"tag": "li", "children": inline_nodes(match.group(2).strip())})
                index += 1
            nodes.append({"tag": "ol" if ordered else "ul", "children": items})
            continue

        paragraph = [stripped]                                          # plain paragraph
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(
                r"(#{1,6}\s|>|```|[-*+]\s|\d+[.)]\s|(-{3,}|\*{3,}|_{3,})$)", lines[index].strip()):
            paragraph.append(lines[index].strip())
            index += 1
        nodes.append({"tag": "p", "children": inline_nodes(" ".join(paragraph))})
    return nodes


# ── the HTTP side ────────────────────────────────────────────────────────────
def _call(method: str, **fields: Any) -> dict[str, Any]:
    import tgx_net

    try:
        body = tgx_net.post_form(f"{API}/{method}", fields, "telegra.ph")
    except tgx_net.NetError as exc:
        raise ArticleError(str(exc)) from exc
    if not body.get("ok"):
        raise ArticleError(f"telegra.ph отказал: {body.get('error', 'без объяснений')}")
    return body["result"]


def create_account(short_name: str, author_name: str | None = None,
                   author_url: str | None = None) -> dict[str, Any]:
    account = _call("createAccount", short_name=short_name, author_name=author_name,
                    author_url=author_url)
    save_account(account)
    return account


def account_token() -> str:
    token = load_account().get("access_token", "")
    if not token:
        raise ArticleError("нет аккаунта telegra.ph — создайте его: `tgx article account --name tgx`")
    return str(token)


def create_page(title: str, markdown: str, author_name: str | None = None,
                author_url: str | None = None) -> dict[str, Any]:
    if not title.strip():
        raise ArticleError("нужен заголовок")
    nodes = markdown_to_nodes(markdown)
    if not nodes:
        raise ArticleError("пустая статья")
    return _call("createPage", access_token=account_token(), title=title.strip(),
                 author_name=author_name, author_url=author_url,
                 content=json.dumps(nodes, ensure_ascii=False), return_content="false")


def edit_page(path: str, title: str, markdown: str, author_name: str | None = None) -> dict[str, Any]:
    return _call("editPage/" + path.strip("/"), access_token=account_token(), title=title.strip(),
                 author_name=author_name,
                 content=json.dumps(markdown_to_nodes(markdown), ensure_ascii=False),
                 return_content="false")


def page_list(limit: int = 50) -> list[dict[str, Any]]:
    result = _call("getPageList", access_token=account_token(), limit=str(min(200, max(1, limit))))
    return list(result.get("pages", []))
