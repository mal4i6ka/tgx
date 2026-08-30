#!/usr/bin/env python3
"""Приложение, проведённое через свой адрес.

Мини-приложение живёт на чужом домене, и это ставит крест на двух вещах сразу:
чужая страница может запретить себя встраивать, а её содержимое в любом случае
закрыто от нас правилом одного происхождения — ни прочитать, ни нажать.

Обход один: подавать приложение со своего адреса. Тогда рамка становится своей,
запрет на встраивание снимается вместе с заголовками, а страница внутри
оказывается доступна целиком — со своей навигацией, списками, полями и всем
прочим, чем она и является: обычным веб-приложением.

Устройство: любой адрес превращается в путь `/x/<схема>/<хост>/<путь>`. Такой
вид позволяет вести через себя и сторонние домены — шрифты, картинки, запросы
к чужому API, — и всё это остаётся одним происхождением для браузера.

Честно о границах. Так работает не всё: приложение может ходить по вебсокетам,
ставить служебного работника, проверять контрольные суммы файлов или сверять
собственный адрес. Мы этого не чиним и не притворяемся, что починили, — что не
поехало, то видно в журнале окна.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

# Заголовки, которые нельзя пересылать: часть описывает соединение, а не ответ,
# часть — ровно то, что мы обходим.
DROP_RESPONSE = {
    "content-security-policy", "content-security-policy-report-only",
    "x-frame-options", "content-encoding", "content-length", "transfer-encoding",
    "connection", "keep-alive", "public-key-pins", "cross-origin-opener-policy",
    "cross-origin-embedder-policy", "cross-origin-resource-policy",
    "strict-transport-security", "report-to", "nel",
    # свои Server и Date ставит наш сервер; чужие приезжают вторыми, и заголовок
    # оказывается задвоен — часть браузеров на таком ответе спотыкается
    "server", "date", "alt-svc", "cf-ray", "age",
    "access-control-allow-origin", "access-control-allow-credentials",
    "timing-allow-origin",
}
DROP_REQUEST = {"host", "connection", "keep-alive", "accept-encoding",
                "content-length", "origin", "referer", "sec-fetch-site",
                "sec-fetch-mode", "sec-fetch-dest", "upgrade-insecure-requests"}

TEXTUAL = ("text/", "javascript", "json", "xml", "css", "ecmascript")

# Абсолютные адреса в разметке, стилях и коде — их надо увести на себя.
# Внутри JSON и строк JavaScript слэши экранированы, поэтому «\/» — такая же
# часть адреса, как «/». Без этого разбор обрывается на первом же обратном
# слэше, хвост адреса остаётся снаружи, и в тексте появляется мусор.
ABSOLUTE = re.compile(
    rb"""(?P<q>["'(=\s])(?P<scheme>https?):\\?/\\?/(?P<rest>(?:\\/|[^\s"'()<>\\])+)""")


def to_path(url: str) -> str:
    """https://host/a/b?q#f → /x/https/host/a/b?q#f

    Якорь обязателен. Telegram кладёт в него подписанные данные запуска, а на
    сервер якорь не уходит вовсе — его разбирает браузер уже на странице.
    Отбросив его, мы отдаём приложению адрес без пропуска, и оно останавливается
    на «Telegram initData is missing».
    """
    parts = urllib.parse.urlsplit(url)
    tail = parts.path or "/"
    if parts.query:
        tail += "?" + parts.query
    if parts.fragment:
        tail += "#" + parts.fragment
    return f"/x/{parts.scheme}/{parts.netloc}{tail}"


def from_path(path: str) -> str:
    """/x/https/host/a/b?q → https://host/a/b?q. Чужой формат отвергаем.

    Якоря здесь не бывает: до сервера он не доходит. Если всё же пришёл —
    отрезаем, чужому серверу он не нужен.
    """
    path = path.split("#", 1)[0]
    if not path.startswith("/x/"):
        raise ValueError(f"путь «{path}» не для проводника")
    rest = path[3:]
    scheme, _, rest = rest.partition("/")
    if scheme not in {"http", "https"}:
        raise ValueError(f"схема «{scheme}» не поддерживается")
    host, _, tail = rest.partition("/")
    if not host:
        raise ValueError("в пути нет хоста")
    return f"{scheme}://{host}/{tail}"


ROOTED = re.compile(rb"""(?P<q>["'(=])(?P<slash>\\?/)(?P<rest>[A-Za-z0-9_\-./][^\s"'()<>]*)""")


def rewrite_rooted(body: bytes, origin: str) -> bytes:
    """Пути от корня — на текущее происхождение приложения.

    Внутри проведённой страницы «/assets/x.js» — это корень **нашего** окна, а
    не приложения: браузер разрешает такой путь от адреса страницы, и до
    приложения он не доходит. Пока этого не было, вместо скриптов приложению
    отдавалась наша же разметка, и оно молча не запускалось.
    """
    prefix = to_path(origin).rstrip("/").encode()

    def swap(match: "re.Match[bytes]") -> bytes:
        rest = match.group("rest")
        if rest.startswith(b"/") or rest.startswith(b"x/"):
            return match.group(0)          # чужая схема или уже наш путь
        slash = match.group("slash")
        head = prefix.replace(b"/", b"\\/") if slash.startswith(b"\\") else prefix
        return match.group("q") + head + slash + rest

    return ROOTED.sub(swap, body)


def rewrite(body: bytes, base: str) -> bytes:
    """Увести абсолютные адреса на себя, относительные оставить как есть.

    Относительные разрешаются браузером от адреса страницы, а он у нас уже свой,
    — значит, и они попадут к нам сами. Трогаем только абсолютные.
    """
    def swap(match: "re.Match[bytes]") -> bytes:
        quote = match.group("q")
        scheme = match.group("scheme").decode()
        rest = match.group("rest").decode(errors="replace")
        # Внутри JSON и строк JavaScript слэши экранированы. Разбирать адрес
        # надо по чистому виду, а возвращать — по тому, в каком он был найден:
        # иначе в готовой строке появляются лишние слэши.
        slashed = "\\/" in rest or b"\\/" in match.group(0)
        path = to_path(f"{scheme}://{rest.replace(chr(92) + '/', '/')}")
        if slashed:
            path = path.replace("/", "\\/")
        return quote + path.encode()

    return ABSOLUTE.sub(swap, body)


# Скрипт, который правит то, что не видно в тексте: адреса, собранные в
# переменных во время работы. Ставится первым, до кода приложения.
HOOK = """<script>(function () {
  const own = location.origin;
  const APP = "__APP_ORIGIN__";          // куда ведёт эта страница на самом деле
  const PREFIX = '/x/' + APP.replace('://', '/');
  function ours(url) {
    try {
      const full = new URL(url, location.href);
      if (!/^https?:$/.test(full.protocol)) return url;  // data:, blob:, mailto:
      // Свой адрес зеркалит адрес приложения: путь от корня уже верный.
      if (full.origin === own) return full.href;
      return own + '/x/' + full.protocol.slice(0, -1) + '/' + full.host +
             full.pathname + full.search + full.hash;
    } catch (e) { return url; }
  }
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    if (typeof input === 'string') return realFetch(ours(input), init);
    if (input instanceof Request) return realFetch(new Request(ours(input.url), input), init);
    return realFetch(input, init);
  };
  const realOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return realOpen.call(this, method, ours(url), ...rest);
  };
  // Динамический импорт и работники — то, чего перехват fetch не видит.
  const realWorker = window.Worker;
  window.Worker = function (url, options) { return new realWorker(ours(url), options); };
  window.Worker.prototype = realWorker.prototype;
  if (window.EventSource) {
    const realES = window.EventSource;
    window.EventSource = function (url, cfg) { return new realES(ours(url), cfg); };
    window.EventSource.prototype = realES.prototype;
  }
  // Ссылки, собранные в коде и вставленные в разметку, ловим по факту вставки.
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes || []) {
        if (!node.querySelectorAll) continue;
        for (const el of [node, ...node.querySelectorAll('[src],[href]')]) {
          for (const attr of ['src', 'href']) {
            const value = el.getAttribute && el.getAttribute(attr);
            if (!value || value.startsWith('data:') || value.startsWith('blob:')) continue;
            const fixed = ours(value);
            if (fixed !== value) el.setAttribute(attr, fixed);
          }
        }
      }
    }
  }).observe(document.documentElement, {childList: true, subtree: true});

  // Вебсокеты через себя не ведём — их надо вернуть на настоящий сервер.
  //
  // Иначе выходит хуже, чем «не поддерживаем»: адрес вида /cable уже переписан
  // на нас, приложение стучится в ws://наш-адрес/cable, там пусто, и оно просто
  // не запускается. Приложение на ActionCable так и вставало белым экраном.
  const realWS = window.WebSocket;
  function back(url) {
    try {
      const full = new URL(url, location.href);
      if (full.host !== location.host) return url;      // уже чужой — не трогаем
      const app = new URL(APP);
      const path = full.pathname.startsWith(PREFIX)
        ? full.pathname.slice(PREFIX.length) : full.pathname;
      const scheme = app.protocol === 'https:' ? 'wss:' : 'ws:';
      return scheme + '//' + app.host + path + full.search;
    } catch (e) { return url; }
  }
  window.WebSocket = function (url, protocols) {
    const real = back(url);
    if (real !== url) console.info('tgx: вебсокет идёт напрямую: ' + real);
    return new realWS(real, protocols);
  };
  window.WebSocket.prototype = realWS.prototype;
  Object.assign(window.WebSocket, {CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3});
  // Служебный работник перехватил бы запросы у нас за спиной.
  if (navigator.serviceWorker) {
    try { Object.defineProperty(navigator, 'serviceWorker', {value: undefined}); }
    catch (e) { /* некоторым браузерам этого не переопределить */ }
  }
})();</script>"""


class Fetcher:
    """Один поход за чужой страницей и возврат её своими словами."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.jar: dict[str, str] = {}
        # откуда пришла страница: по нему достраиваются пути, которые
        # приложение собирает уже во время работы
        self.origin = ""
        # закреплено ли происхождение снаружи — тогда ответы его не меняют
        self.pinned = False

    def get(self, path: str, method: str = "GET", headers: dict[str, str] | None = None,
            body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        url = from_path(path)
        passed = {k: v for k, v in (headers or {}).items()
                  if k.lower() not in DROP_REQUEST}
        passed["Accept-Encoding"] = "gzip, deflate"
        host = urllib.parse.urlsplit(url).netloc
        if host in self.jar:
            passed["Cookie"] = self.jar[host]
        request = urllib.request.Request(url, data=body, method=method, headers=passed)
        # Сертификаты проверяем тем же способом, что и остальной tgx: свой
        # контекст здесь означал бы вторую настройку, которая однажды разойдётся
        # с первой.
        import tgx_net

        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=tgx_net.context()) as answer:
                return self._read(answer, host)
        except urllib.error.HTTPError as exc:
            return self._read(exc, host)
        except Exception as exc:
            return 502, {"Content-Type": "text/plain; charset=utf-8"}, (
                f"проводник не дошёл до {url}: {exc}".encode())

    def _read(self, answer: Any, host: str) -> tuple[int, dict[str, str], bytes]:
        raw = answer.read()
        encoding = (answer.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)

        out: dict[str, str] = {}
        for name, value in answer.headers.items():
            low = name.lower()
            if low in DROP_RESPONSE:
                continue
            if low == "set-cookie":
                # печенья держим у себя: браузеру они ни к чему, а приложению нужны
                self.jar[host] = "; ".join(
                    filter(None, [self.jar.get(host), value.split(";", 1)[0]]))
                continue
            if low == "location":
                value = to_path(urllib.parse.urljoin(answer.geturl(), value))
            out[name] = value

        kind = (out.get("Content-Type") or answer.headers.get("Content-Type") or "").lower()

        # Чужой сервер разрешает доступ своему домену, а мы теперь под другим:
        # куски приложения, которые сборщик грузит с пометкой crossorigin,
        # браузер на таком заголовке отвергает. Всё это происходит на своей
        # машине, поэтому разрешаем прямо.
        out["Access-Control-Allow-Origin"] = "*"
        out["Access-Control-Allow-Headers"] = "*"
        out["Timing-Allow-Origin"] = "*"

        parts = urllib.parse.urlsplit(answer.geturl())
        origin = f"{parts.scheme}://{parts.netloc}"
        if "html" in kind and not self.pinned:
            # Происхождение запоминаем один раз и больше не меняем.
            #
            # Сначала я обновлял его каждым ответом — и запасной путь уводило к
            # первому попавшемуся домену со шрифтом. Потом сузил до HTML — но
            # виджет поддержки внутри приложения тоже отдаёт HTML, и после его
            # загрузки запросы приложения уходили к нему: сервер отвечал
            # «wrong verify token», а приложение вставало белым экраном. Окно
            # открывают для одного приложения, его адрес известен заранее —
            # значит, и меняться ему незачем.
            self.origin = origin
        # Разметку и стили переписываем, код — нет.
        #
        # В коде адрес текстом не отличить от чего угодно другого: строка
        # `/https?:\/\//gi` — это регулярное выражение, и подмена внутри него
        # ломает разбор всего файла. Так и вышло: приложение падало на «invalid
        # regular expression flags», пока я правил код наравне с разметкой.
        # Поэтому за код отвечает перехват во время работы — он видит настоящие
        # адреса, а не похожий на них текст, — и запасной путь на сервере.
        code = any(mark in kind for mark in ("javascript", "ecmascript"))
        if any(mark in kind for mark in TEXTUAL) and not code:
            # Пути от корня не трогаем: наш адрес теперь зеркалит адрес
            # приложения, и «/assets/x.js» приходит к нам сам, а оттуда его
            # подхватывает запасной путь. Переписывая их, мы бы ломали
            # маршрутизатор приложения — он разбирает свой путь сам.
            raw = rewrite(raw, answer.geturl())
            if "html" in kind:
                raw = _inject(raw, origin)
        return getattr(answer, "status", None) or answer.getcode() or 200, out, raw


def _inject(html: bytes, origin: str = "") -> bytes:
    """Поставить наш скрипт первым — он должен успеть до кода приложения."""
    hook = HOOK.replace("__APP_ORIGIN__", origin).encode()
    lowered = html.lower()
    for anchor in (b"<head>", b"<head "):
        at = lowered.find(anchor)
        if at != -1:
            end = html.find(b">", at) + 1
            return html[:end] + hook + html[end:]
    return hook + html
