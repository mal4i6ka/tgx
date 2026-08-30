#!/usr/bin/env python3
"""Живая страница звонка — то, чего терминал показать не может.

Терминал распоряжается звонком, но участники меняются каждую секунду: кто-то
говорит, кто-то поднял руку, кого-то заглушили. Перерисовывать это таблицей —
значит мигать всем экраном; поэтому картина выносится на страницу, которая
обновляется сама.

Страница поднимается только на 127.0.0.1 и живёт, пока идёт команда: наружу она
не смотрит и ничего не сохраняет. Данные она берёт у того же процесса, который
их и опрашивает, — второго соединения с Telegram не появляется.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# Цвета взяты из системы Altery: тёмный холст, приглушённая терракота.
PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
 :root { --canvas:#121212; --surface:#181818; --raised:#1f1f1f; --border:#323232;
         --text:#f2efe8; --muted:#aba59b; --accent:#ca6534; --ok:#6fa06a; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--canvas); color:var(--text);
        font:14px/1.45 "SF Pro Text","IBM Plex Sans",-apple-system,sans-serif; }
 header { padding:16px 24px; border-bottom:1px solid var(--border);
          display:flex; align-items:baseline; gap:12px; }
 h1 { margin:0; font-size:18px; font-weight:600; }
 .meta { color:var(--muted); font-size:12px; }
 main { padding:16px 24px; display:grid; gap:8px;
        grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
 .card { background:var(--surface); border:1px solid var(--border); border-radius:6px;
         padding:10px 12px; display:flex; align-items:center; gap:10px; }
 .card.speaking { border-color:var(--ok); background:var(--raised); }
 .card.hand { border-color:var(--accent); }
 .who { flex:1; font-weight:500; }
 .tag { font-size:11px; color:var(--muted); }
 .dot { width:8px; height:8px; border-radius:50%%; background:var(--border); }
 .dot.live { background:var(--ok); }
 .dot.muted { background:var(--border); }
 footer { padding:12px 24px; color:var(--muted); font-size:12px;
          border-top:1px solid var(--border); }
 .empty { color:var(--muted); padding:24px; }
</style></head><body>
<header><h1>%(title)s</h1><span class="meta" id="meta">подключение…</span></header>
<main id="grid"><div class="empty">ждём участников…</div></main>
<footer>Страница обновляется сама. Звук — в приложении Telegram: %(link)s</footer>
<script>
 async function tick() {
   try {
     const r = await fetch('/data', {cache:'no-store'});
     const d = await r.json();
     document.getElementById('meta').textContent =
       d.count + ' участников · обновлено ' + d.at;
     const grid = document.getElementById('grid');
     if (!d.people.length) { grid.innerHTML = '<div class="empty">пока никого</div>'; return; }
     grid.innerHTML = d.people.map(p => {
       const cls = p.hand ? 'card hand' : (p.muted ? 'card' : 'card speaking');
       const tags = [p.muted ? 'заглушён' : 'говорит',
                     p.hand ? 'рука поднята' : '', p.video ? 'видео' : '']
                    .filter(Boolean).join(' · ');
       return `<div class="${cls}"><span class="dot ${p.muted?'muted':'live'}"></span>
               <span class="who">${p.who}</span><span class="tag">${tags}</span></div>`;
     }).join('');
   } catch (e) {
     document.getElementById('meta').textContent = 'команда завершена';
   }
 }
 tick(); setInterval(tick, 2000);

 // Окно должно быть видно агенту: он не смотрит на пиксели, а спрашивает
 // состояние. Список участников уже приходит с сервера — объявляем его как
 // действие, чтобы спрашивающий получал ровно то, что видит человек.
 %(bridge)s
 window.tgx.registerTool('snapshot', 'кто сейчас в звонке', {}, async () => {
   const data = await (await fetch('data')).json();
   return {звонок: %(title_json)s, участников: data.count, обновлено: data.at,
           люди: data.people};
 });
 window.tgx.registerTool('refresh', 'обновить картину сейчас же', {}, async () => {
   await tick(); return {обновлено: true};
 });
</script></body></html>
"""


class Dashboard:
    """Крошечный сервер на 127.0.0.1, показывающий, кто сейчас в звонке."""

    def __init__(self, title: str, link: str, source: Callable[[], list[dict[str, Any]]],
                 port: int = 0) -> None:
        import tgx_windows

        self.title, self.link, self.source = title, link, source
        self.bridge = tgx_windows.Bridge()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/"

    def page(self) -> str:
        import tgx_windows

        return PAGE % {"title": self.title, "link": self.link,
                       "title_json": json.dumps(self.title, ensure_ascii=False),
                       "bridge": tgx_windows.BRIDGE_JS}

    def _handler(self) -> type:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass                     # тишина: это не веб-сервер, а окно

            def _json(self, value: Any) -> None:
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:    # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode(errors="replace") if length else ""
                if self.path.startswith("/mcp/state"):
                    dashboard.bridge.push_state(json.loads(body or "{}"))
                elif self.path.startswith("/mcp/tools"):
                    dashboard.bridge.push_tools(json.loads(body or "[]"))
                elif self.path.startswith("/mcp/result"):
                    got = json.loads(body or "{}")
                    dashboard.bridge.push_result(str(got.get("ticket")), got.get("value"))
                elif self.path.startswith("/mcp/ask"):
                    got = json.loads(body or "{}")
                    try:
                        value = dashboard.bridge.ask(
                            str(got.get("tool")), got.get("args") or {},
                            float(got.get("timeout") or 10.0))
                        return self._json({"ok": True, "результат": value})
                    except Exception as exc:
                        return self._json({"ok": False, "error": str(exc)})
                self._json({})

            def do_GET(self) -> None:     # noqa: N802 — имя задаёт библиотека
                if self.path.startswith("/mcp/pending"):
                    return self._json(dashboard.bridge.take_pending())
                if self.path.startswith("/mcp/snapshot"):
                    return self._json({"состояние": dashboard.bridge.state,
                                       "действия": dashboard.bridge.tools})
                if self.path.startswith("/data"):
                    people = dashboard.source()
                    body = json.dumps({
                        "count": len(people),
                        "at": __import__("time").strftime("%H:%M:%S"),
                        "people": [{"who": str(p.get("кто")), "muted": bool(p.get("заглушён")),
                                    "hand": bool(p.get("рука поднята")),
                                    "video": bool(p.get("видео"))} for p in people],
                    }, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                else:
                    body = dashboard.page().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def start(self) -> str:
        import tgx_windows

        self.thread.start()
        tgx_windows.register(self.title, "звонок", self.url)
        return self.url

    def stop(self) -> None:
        import tgx_windows

        tgx_windows.unregister(self.url)
        self.server.shutdown()
        self.server.server_close()
