#!/usr/bin/env python3
"""Мини-приложение бота, запущенное из терминала.

Подписанный адрес, открытый в обычной вкладке, даёт сломанное приложение.
Мини-приложение ждёт вокруг себя **хозяина**: скрипт `telegram-web-app.js`
внутри страницы шлёт наружу сообщения — «я готов», «дай тему», «покажи главную
кнопку», «закрой меня», — и ждёт ответов. В браузере отвечать некому, поэтому
приложение либо висит на заставке, либо считает, что запущено вне Telegram.

Здесь хозяина изображаем мы: локальная страница держит приложение в рамке,
разговаривает с ним по тому же протоколу и рисует его кнопки своими руками.
Всё это живёт на 127.0.0.1, пока идёт команда, и наружу не смотрит.

Чего не изображаем: оплату, доступ к контактам, запись видео. Такие запросы
приложение получит с отказом, а вы увидите их в терминале — лучше честный
отказ, чем тишина, в которой непонятно, что пошло не так.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# то, на что мы отвечаем по-настоящему
ANSWERED = ("web_app_ready", "web_app_request_theme", "web_app_request_viewport",
            "web_app_expand", "web_app_close", "web_app_setup_main_button",
            "web_app_setup_secondary_button", "web_app_setup_back_button",
            "web_app_setup_settings_button", "web_app_open_link", "web_app_data_send",
            "web_app_trigger_haptic_feedback", "web_app_set_header_color",
            "web_app_set_background_color", "web_app_request_safe_area",
            "web_app_request_content_safe_area", "web_app_set_bottom_bar_color")

# то, чего у нас нет: отвечаем отказом, а не молчанием
REFUSED = {
    "web_app_open_invoice": "оплата возможна только в настоящем Telegram",
    "web_app_request_phone": "номер телефона отсюда не отдаётся",
    "web_app_request_write_access": "разрешение на переписку выдаётся в Telegram",
    "web_app_open_scan_qr_popup": "камеры у терминала нет",
    "web_app_read_text_from_clipboard": "к буферу обмена страницу не пускаем",
    "web_app_biometry_request_access": "отпечатка пальца здесь не будет",
    "web_app_request_emoji_status_access": "эмодзи-статус меняется в Telegram",
}

# Цвета из системы Altery — те же, что на странице звонка.
PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>
 :root { --canvas:#121212; --surface:#181818; --raised:#1f1f1f; --border:#323232;
         --text:#f2efe8; --muted:#aba59b; --accent:#ca6534; }
 * { box-sizing:border-box; }
 html,body { height:100%%; margin:0; }
 body { background:var(--canvas); color:var(--text); display:flex; flex-direction:column;
        font:14px/1.45 "SF Pro Text","IBM Plex Sans",-apple-system,sans-serif; }
 header { display:flex; align-items:center; gap:12px; padding:10px 16px;
          border-bottom:1px solid var(--border); background:var(--surface); }
 header b { font-weight:600; }
 header .muted { color:var(--muted); font-size:12px; }
 #back { display:none; background:none; border:0; color:var(--text); cursor:pointer;
         font-size:18px; padding:0 4px; }
 #wrap { flex:1; position:relative; }
 #frame { position:absolute; inset:0; width:100%%; height:100%%; border:0; background:#fff; }
 #blocked { display:none; position:absolute; inset:0; padding:32px; overflow:auto;
            background:var(--canvas); }
 #blocked h2 { margin:0 0 12px; font-size:16px; font-weight:600; }
 #blocked p { max-width:52ch; color:var(--muted); }
 #blocked a { color:var(--accent); }
 footer { padding:10px 16px; border-top:1px solid var(--border); background:var(--surface);
          display:flex; flex-direction:column; gap:8px; }
 button.main { display:none; width:100%%; padding:12px; border:0; border-radius:8px;
               background:var(--accent); color:#fff; font-size:15px; cursor:pointer; }
 button.main[disabled] { opacity:.5; cursor:default; }
 #log { max-height:120px; overflow:auto; font:12px/1.5 ui-monospace,monospace;
        color:var(--muted); }
 #log div { padding:1px 0; }
 #log .no { color:var(--accent); }
</style></head><body>
<header>
  <button id="back" title="назад">&larr;</button>
  <b>%(title)s</b>
  <span class="muted">запущено из терминала · хозяин ненастоящий</span>
</header>
<div id="wrap"><iframe id="frame" src="%(url)s" allow="clipboard-write; fullscreen"></iframe>
  <div id="blocked">
    <h2>Приложение отказалось открываться в рамке</h2>
    <p>Так делают не все, но многие: приложение объявляет, кто вправе держать его
       в рамке, и посторонних — включая это окно — не пускает. Запрет исполняет
       браузер, обойти его нельзя.</p>
    <p>Отдельной вкладкой оно откроется, но хозяина у него там не будет: часть
       возможностей не заработает.</p>
    <p><a href="%(url)s" target="_blank" rel="noopener">Открыть вкладкой</a></p>
  </div>
</div>
<footer>
  <button class="main" id="main"></button>
  <div id="log"></div>
</footer>
<script>
%(bridge)s
const THEME = %(theme)s;
const frame = document.getElementById('frame');
const mainBtn = document.getElementById('main');
const backBtn = document.getElementById('back');
const log = document.getElementById('log');
const sent = [];

function note(text, bad) {
  const line = document.createElement('div');
  if (bad) line.className = 'no';
  line.textContent = text;
  log.appendChild(line); log.scrollTop = log.scrollHeight;
}

function reply(type, data) {
  // Тот же способ, которым отвечает настоящий клиент: сообщение уходит в рамку
  // строкой, а не объектом, — приложение ждёт именно строку.
  frame.contentWindow.postMessage(JSON.stringify({eventType: type, eventData: data}), '*');
}

function viewport() {
  return {height: frame.clientHeight, width: frame.clientWidth,
          is_expanded: true, is_state_stable: true};
}

const REFUSED = %(refused)s;

window.addEventListener('message', (event) => {
  let message = event.data;
  if (typeof message === 'string') { try { message = JSON.parse(message); } catch (e) { return; } }
  if (!message || !message.eventType) return;
  const kind = message.eventType, data = message.eventData || {};

  if (REFUSED[kind]) { note(kind + ' → ' + REFUSED[kind], true); return; }

  switch (kind) {
    case 'web_app_ready':
      note('приложение готово');
      reply('theme_changed', {theme_params: THEME});
      reply('viewport_changed', viewport());
      reply('safe_area_changed', {top: 0, bottom: 0, left: 0, right: 0});
      reply('content_safe_area_changed', {top: 0, bottom: 0, left: 0, right: 0});
      break;
    case 'web_app_request_theme': reply('theme_changed', {theme_params: THEME}); break;
    case 'web_app_request_viewport':
    case 'web_app_expand': reply('viewport_changed', viewport()); break;
    case 'web_app_request_safe_area':
      reply('safe_area_changed', {top: 0, bottom: 0, left: 0, right: 0}); break;
    case 'web_app_request_content_safe_area':
      reply('content_safe_area_changed', {top: 0, bottom: 0, left: 0, right: 0}); break;
    case 'web_app_setup_main_button':
      mainBtn.style.display = data.is_visible ? 'block' : 'none';
      mainBtn.textContent = data.text || '';
      mainBtn.disabled = data.is_active === false;
      if (data.color) mainBtn.style.background = data.color;
      if (data.text_color) mainBtn.style.color = data.text_color;
      break;
    case 'web_app_setup_back_button':
      backBtn.style.display = data.is_visible ? 'block' : 'none'; break;
    case 'web_app_open_link':
      note('открывает ' + data.url); window.open(data.url, '_blank', 'noopener'); break;
    case 'web_app_data_send':
      sent.push(data.data); note('прислало данные: ' + data.data);
      fetch('/sent', {method: 'POST', body: data.data}); break;
    case 'web_app_close':
      note('приложение просит закрыть окно'); fetch('/closed', {method: 'POST'}); break;
    case 'web_app_trigger_haptic_feedback': break;   // вибрации у окна нет
    default:
      note(kind);   // неизвестное показываем, но не выдумываем ответ
  }
});

// Приложение, запретившее встраивание, не отдаёт ошибку наружу: рамка просто
// остаётся пустой. Надёжный признак один — тишина: настоящее приложение
// здоровается первым делом. Молчит дольше нескольких секунд — показываем
// причину, а не белый прямоугольник.
let heard = false;
window.addEventListener('message', () => { heard = true; }, true);
setTimeout(() => {
  if (heard) return;
  document.getElementById('blocked').style.display = 'block';
  note('приложение не отозвалось — вероятно, запрещает встраивание', true);
}, 5000);

// Окно должно быть видно и управляемо снаружи: агент не смотрит на пиксели,
// он спрашивает состояние и просит действия. Объявляем их так же, как это
// делает страница в WebMCP, — сама, а не через список где-то в другом файле.
// Начальное «спрятано» задано таблицей стилей, а не инлайном, поэтому
// смотреть на element.style бесполезно: там пусто и у скрытой кнопки.
const shown = (el) => getComputedStyle(el).display !== 'none';

function snapshot() {
  return {
    приложение: %(title_json)s,
    'главная кнопка': shown(mainBtn)
      ? {надпись: mainBtn.textContent, доступна: !mainBtn.disabled} : null,
    'кнопка назад': shown(backBtn),
    'встраивание запрещено': shown(document.getElementById('blocked')),
    'приложение отозвалось': heard,
    журнал: [...log.children].map(x => x.textContent),
    'прислано данных': sent.slice(),
  };
}

function announce() { window.tgx.setState(snapshot()); }

window.tgx.registerTool('snapshot', 'что сейчас в окне: кнопки, журнал, присланные данные',
  {}, () => snapshot());
window.tgx.registerTool('press_main', 'нажать главную кнопку приложения', {}, () => {
  if (!shown(mainBtn)) return {error: 'главной кнопки сейчас нет'};
  if (mainBtn.disabled) return {error: 'главная кнопка неактивна'};
  reply('main_button_pressed', {}); note('главную кнопку нажал агент');
  return {нажато: mainBtn.textContent};
});
window.tgx.registerTool('press_back', 'нажать кнопку «назад»', {}, () => {
  if (!shown(backBtn)) return {error: 'кнопки «назад» сейчас нет'};
  reply('back_button_pressed', {}); note('«назад» нажал агент');
  return {нажато: 'назад'};
});
window.tgx.registerTool('send_event',
  'послать приложению произвольное событие протокола',
  {type: {type: 'string'}, data: {type: 'object'}}, (a) => {
    if (!a.type) return {error: 'нужно имя события'};
    reply(a.type, a.data || {}); note('агент послал ' + a.type);
    return {послано: a.type};
  });
window.tgx.registerTool('close', 'закрыть окно', {}, () => {
  fetch('/closed', {method: 'POST'}); note('окно закрыл агент');
  return {закрыто: true};
});

// состояние обновляем при каждом изменении, а не по опросу: агент должен
// видеть окно таким, какое оно сейчас, а не каким было секунду назад
new MutationObserver(announce).observe(document.body, {subtree: true, childList: true,
                                                       attributes: true});
announce();

mainBtn.onclick = () => reply('main_button_pressed', {});
backBtn.onclick = () => reply('back_button_pressed', {});
new ResizeObserver(() => reply('viewport_changed', viewport())).observe(frame);
</script></body></html>"""

# тема, которую отдаём приложению: тёмная, чтобы совпадала с окном вокруг
THEME = {"bg_color": "#121212", "secondary_bg_color": "#181818", "text_color": "#f2efe8",
         "hint_color": "#aba59b", "link_color": "#ca6534", "button_color": "#ca6534",
         "button_text_color": "#ffffff", "header_bg_color": "#181818",
         "accent_text_color": "#ca6534", "section_bg_color": "#1f1f1f",
         "section_header_text_color": "#aba59b", "subtitle_text_color": "#aba59b",
         "destructive_text_color": "#c25a4b", "bottom_bar_bg_color": "#181818"}


class Host:
    """Окно вокруг мини-приложения: рамка, кнопки и разговор по протоколу."""

    def __init__(self, title: str, url: str, port: int = 0,
                 on_data: Callable[[str], None] | None = None) -> None:
        import tgx_windows

        self.title, self.app_url = title, url
        self.on_data = on_data or (lambda _: None)
        self.received: list[str] = []
        self.closed = threading.Event()
        self.bridge = tgx_windows.Bridge()
        # Сервер обязан быть многопоточным: поручение агента ждёт ответа
        # страницы, а страница за ответом ходит сюда же. Один поток —
        # и они заперли бы друг друга насмерть.
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/"

    def page(self) -> str:
        import tgx_windows

        return PAGE % {"title": _escape(self.title), "url": _escape(self.app_url),
                       "title_json": json.dumps(self.title, ensure_ascii=False),
                       "theme": json.dumps(THEME), "bridge": tgx_windows.BRIDGE_JS,
                       "refused": json.dumps(REFUSED, ensure_ascii=False)}

    def _handler(self) -> type:
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass                      # это окно, а не веб-сервер

            def _ok(self, body: bytes, kind: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:     # noqa: N802 — имя задаёт библиотека
                if self.path.startswith("/mcp/pending"):
                    return self._json(host.bridge.take_pending())
                if self.path.startswith("/mcp/snapshot"):
                    return self._json({"состояние": host.bridge.state,
                                       "действия": host.bridge.tools,
                                       "прислано": host.received,
                                       "просит закрыть": host.closed.is_set()})
                self._ok(host.page().encode(), "text/html; charset=utf-8")

            def _json(self, value: Any) -> None:
                self._ok(json.dumps(value, ensure_ascii=False).encode(),
                         "application/json; charset=utf-8")

            def do_POST(self) -> None:    # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode(errors="replace") if length else ""
                if self.path.startswith("/sent") and body:
                    host.received.append(body)
                    host.on_data(body)
                elif self.path.startswith("/closed"):
                    host.closed.set()
                elif self.path.startswith("/mcp/state"):
                    host.bridge.push_state(json.loads(body or "{}"))
                elif self.path.startswith("/mcp/tools"):
                    host.bridge.push_tools(json.loads(body or "[]"))
                elif self.path.startswith("/mcp/result"):
                    got = json.loads(body or "{}")
                    host.bridge.push_result(str(got.get("ticket")), got.get("value"))
                elif self.path.startswith("/mcp/ask"):
                    # Просьба снаружи: ждём, пока страница выполнит и ответит.
                    got = json.loads(body or "{}")
                    try:
                        value = host.bridge.ask(str(got.get("tool")), got.get("args") or {},
                                                float(got.get("timeout") or 10.0))
                        return self._json({"ok": True, "результат": value})
                    except Exception as exc:
                        return self._json({"ok": False, "error": str(exc)})
                self._ok(b"{}", "application/json")

        return Handler

    def start(self) -> str:
        import tgx_windows

        self.thread.start()
        tgx_windows.register(self.title, "мини-приложение", self.url)
        return self.url

    def wait(self, seconds: float) -> bool:
        """Дождаться, пока приложение попросит закрыться. True — попросило."""
        return self.closed.wait(seconds)

    def stop(self) -> None:
        import tgx_windows

        tgx_windows.unregister(self.url)
        self.server.shutdown()
        self.server.server_close()


def _escape(text: str) -> str:
    """Чужой адрес и чужое имя попадают в разметку — экранируем."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
