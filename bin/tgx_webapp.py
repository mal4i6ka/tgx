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

# Своя страница живёт не в корне.
#
# Приложение подаётся по его собственному пути, и у многих этот путь — просто
# «/». Оставшись в корне, окно грузило бы в рамку само себя: шапка рисовалась
# дважды, а приложение не появлялось вовсе. Уводим себя на путь, которого у
# приложений не бывает.
HOST_PATH = "/__tgx/"

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
PAGE = r"""<!doctype html>
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
 header .grow { margin-left:auto; }
 nav { display:flex; gap:2px; }
 nav button { background:var(--raised); border:1px solid var(--border); color:var(--text);
              border-radius:4px; cursor:pointer; font-size:12px; line-height:1;
              padding:5px 8px; }
 nav button:hover { border-color:var(--accent); }
 nav button:disabled { opacity:.35; cursor:default; border-color:var(--border); }
 #where { font:11px/1 ui-monospace,monospace; max-width:38ch; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }
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
  <button id="back" title="кнопка «назад» приложения">&larr;</button>
  <b>%(title)s</b>
  <nav>
    <button id="nav-back" title="назад по истории">◀</button>
    <button id="nav-fwd" title="вперёд">▶</button>
    <button id="nav-reload" title="перезагрузить">⟳</button>
    <button id="nav-home" title="в начало приложения">⌂</button>
  </nav>
  <span id="where" class="muted" title="где вы внутри приложения"></span>
  <span class="muted grow">запущено из терминала · хозяин ненастоящий</span>
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
// Все ссылки на элементы — здесь, одним списком.
//
// Разбросав их по тексту, я дважды получил одну и ту же поломку: snapshot()
// читал признак, объявленный ниже, скрипт падал в мёртвой зоне, и всё, что
// определялось дальше, просто не появлялось. Видно это только в консоли
// браузера, а со стороны выглядит как «кнопки не нарисовались».
const frame = document.getElementById('frame');
const mainBtn = document.getElementById('main');
const backBtn = document.getElementById('back');
const log = document.getElementById('log');
const navBack = document.getElementById('nav-back');
const navFwd = document.getElementById('nav-fwd');
const where = document.getElementById('where');
const HOME = %(home_json)s;
const sent = [];
// Объявлено здесь нарочно: snapshot() читает этот признак, а snapshot()
// вызывается сразу при готовности страницы. Объявление ниже по тексту роняло
// весь скрипт в мёртвой зоне — и вместе с ним всё, что определялось дальше,
// включая навигацию. Ошибка при этом видна только в консоли браузера.
let heard = false;

function note(text, bad) {
  const line = document.createElement('div');
  if (bad) line.className = 'no';
  line.textContent = text;
  log.appendChild(line); log.scrollTop = log.scrollHeight;
}

// Часть приложений шлёт сообщения не «куда придётся», а на конкретный адрес —
// обычно https://web.telegram.org. Мы не он: браузер такое сообщение молча
// выбрасывает, приложение не дожидается ответа и решает, что запущено вне
// Telegram. Со стороны это выглядит как «Environment Error» через 200 мс.
//
// Своё окно вправе принимать всё, что ему шлют: подменяем собственный
// postMessage так, чтобы чужой адрес назначения не мешал доставке. Наружу это
// ничего не открывает — сообщение и так шло к нам, просто с чужой пометкой.
(function () {
  const real = window.postMessage.bind(window);
  window.postMessage = function (data, target, transfer) {
    if (target && target !== '*' && target !== location.origin) {
      note('сообщение адресовано ' + target + ' — принимаем как своё');
      return real(data, '*', transfer);
    }
    return real(data, target === undefined ? '*' : target, transfer);
  };
})();

function reply(type, data) {
  // Тот же способ, которым отвечает настоящий клиент: сообщение уходит в рамку
  // строкой, а не объектом, — приложение ждёт именно строку.
  frame.contentWindow.postMessage(JSON.stringify({eventType: type, eventData: data}), '*');
}

// Отдельные полки: приложение вправе считать, что «надёжное» хранилище живёт
// иначе, чем обычное, и путать их — значит однажды отдать не то.
function shelf(kind) {
  const box = kind.includes('secure') ? 'tgx.secure.' : 'tgx.device.';
  return {
    getItem: (k) => localStorage.getItem(box + k),
    setItem: (k, v) => localStorage.setItem(box + k, v),
    removeItem: (k) => localStorage.removeItem(box + k),
    clear: () => Object.keys(localStorage).filter(k => k.startsWith(box))
                       .forEach(k => localStorage.removeItem(k)),
  };
}

function store(kind, data, action, answer) {
  try {
    const extra = action() || {};
    reply(answer, Object.assign({req_id: data.req_id}, extra));
  } catch (e) {
    const bad = kind.includes('secure') ? 'secure_storage_failed' : 'device_storage_failed';
    reply(bad, {req_id: data.req_id, error: String(e)});
    note(kind + ' → ' + e, true);
  }
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
    // Скрипт Telegram здоровается этим ещё до самого приложения и ждёт стиля в
    // ответ. Без ответа его собственная подготовка не заканчивается, и
    // приложение падает по своему таймауту — с виду беспричинно.
    case 'iframe_ready':
      reply('set_custom_style', '');
      reply('theme_changed', {theme_params: THEME});
      reply('viewport_changed', viewport());
      note('поздоровались со скриптом Telegram');
      break;
    case 'iframe_will_reload': break;

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
    case 'web_app_setup_settings_button': break;    // своей шестерёнки не рисуем
    case 'web_app_setup_swipe_behavior': break;    // смахивать в окне нечем
    case 'web_app_setup_closing_behavior': break;  // закрытие спрашиваем сами
    case 'web_app_toggle_orientation_lock': break; // экран у окна один

    // Полноэкранный режим. Приложение ждёт ответа и без него висит на заставке:
    // «Тюряга» так и делала. Настоящего полного экрана у рамки нет, но сказать
    // об этом надо ответом, а не молчанием.
    case 'web_app_request_fullscreen':
      reply('fullscreen_changed', {is_fullscreen: true});
      break;
    case 'web_app_exit_fullscreen':
      reply('fullscreen_changed', {is_fullscreen: false});
      break;

    // «Добавить на домашний экран» — этого у окна нет; отвечаем «не умеем»,
    // и приложение перестаёт ждать.
    case 'web_app_check_home_screen':
      reply('home_screen_checked', {status: 'unsupported'});
      break;
    case 'web_app_add_to_home_screen':
      reply('home_screen_added', {});
      break;

    // Своих методов у нас нет, но запрос требует ответа с тем же номером —
    // иначе приложение ждёт его до бесконечности.
    case 'web_app_invoke_custom_method':
      reply('custom_method_invoked', {req_id: data.req_id, error: 'UNSUPPORTED_METHOD'});
      break;

    // Хранилища. Приложение считает их само собой разумеющимися: Wallet без
    // них уходит в «Some technical issue» и дальше не идёт. Держим в
    // localStorage — он у нашего окна свой и наружу не выходит.
    case 'web_app_device_storage_save_key':
    case 'web_app_secure_storage_save_key':
      store(kind, data, () => {
        if (data.value === null || data.value === undefined) shelf(kind).removeItem(data.key);
        else shelf(kind).setItem(data.key, data.value);
      }, kind.includes('secure') ? 'secure_storage_key_saved' : 'device_storage_key_saved');
      break;
    case 'web_app_device_storage_get_key':
    case 'web_app_secure_storage_get_key':
      store(kind, data, () => ({value: shelf(kind).getItem(data.key)}),
            kind.includes('secure') ? 'secure_storage_key_received'
                                    : 'device_storage_key_received');
      break;
    case 'web_app_device_storage_clear':
    case 'web_app_secure_storage_clear':
      store(kind, data, () => shelf(kind).clear(),
            kind.includes('secure') ? 'secure_storage_cleared' : 'device_storage_cleared');
      break;
    case 'web_app_secure_storage_restore_key':
      // Восстановить ключ, сохранённый на другом устройстве, нам неоткуда:
      // отвечаем отсутствием, а не ошибкой — так приложение заведёт новый.
      reply('secure_storage_key_restored', {req_id: data.req_id, value: null});
      break;
    default:
      note(kind);   // неизвестное показываем, но не выдумываем ответ
  }
});

// Приложение, запретившее встраивание, не отдаёт ошибку наружу: рамка просто
// остаётся пустой. Надёжный признак один — тишина: настоящее приложение
// здоровается первым делом. Молчит дольше нескольких секунд — показываем
// причину, а не белый прямоугольник.
window.addEventListener('message', () => { heard = true; }, true);
setTimeout(() => {
  if (heard) return;
  // Две разные беды выглядят одинаково — пустой рамкой. Различаем по тому,
  // доступно ли нам её содержимое: своё, но молчащее, значит приложение
  // загрузилось и решило не работать; недоступное — что его вообще не пустили.
  let ours = false;
  try { ours = !!(frame.contentDocument && frame.contentDocument.body); } catch (e) {}
  const box = document.getElementById('blocked');
  if (ours) {
    box.querySelector('h2').textContent = 'Приложение не отозвалось';
    box.querySelector('p').textContent =
      'Страница загрузилась и доступна — значит, дело не во встраивании. ' +
      'Часть приложений проверяет, что запущена в настоящем Telegram, и вне его ' +
      'работать отказывается. Это их защита, и обойти её мы не пытаемся.';
    note('приложение загрузилось, но работать отказалось', true);
  } else {
    note('приложение не отозвалось — вероятно, запрещает встраивание', true);
  }
  box.style.display = 'block';
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
    'где сейчас': where.textContent,
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
// --- глаза и руки внутри самого приложения ---
// Работают, только когда приложение подано через проводник: иначе рамка чужая
// и правило одного происхождения не пустит нас ни к одному её элементу.
function inside() {
  try { return frame.contentDocument || null; } catch (e) { return null; }
}

const REFS = new Map();
let refCount = 0;

function label(el) {
  // textContent, а не innerText: innerText отражает отрисованный текст, и в
  // фоновой вкладке он пуст. textContent берётся из дерева и есть всегда —
  // ровно то, что нужно агенту, который окно не показывает.
  const own = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
               el.getAttribute('title') || el.value || el.textContent || '').trim();
  return own.replace(/\s+/g, ' ').slice(0, 80);
}

function visible(el) {
  // Идём вверх по предкам и смотрим только на display/visibility/opacity —
  // это вычисляемый стиль, он не зависит от отрисовки. offsetParent и
  // getBoundingClientRect в фоновой вкладке врут (браузер её не красит), а
  // здесь важно именно «есть в разметке», а не «сейчас на экране».
  const win = el.ownerDocument.defaultView;
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    const style = win.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity || '1') <= 0.05) return false;
  }
  return true;
}

const CLICKABLE = 'a,button,input,select,textarea,[role=button],[role=link],[role=tab],' +
                  '[role=menuitem],[role=checkbox],[role=switch],[onclick],[tabindex]';

// То, чего человек не видит никогда: тела скриптов, стили, запасной текст для
// выключенного JavaScript. В разметку оно попадать не должно — иначе агент
// читает исходник вместо страницы.
const UNSEEN = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'HEAD']);

function scan() {
  const doc = inside();
  if (!doc) return null;
  REFS.clear(); refCount = 0;
  const rows = [];
  for (const el of doc.querySelectorAll(CLICKABLE)) {
    if (!visible(el)) continue;
    const ref = 'e' + (++refCount);
    REFS.set(ref, el);
    const row = {ref, тег: el.tagName.toLowerCase(), надпись: label(el)};
    const role = el.getAttribute('role'); if (role) row.роль = role;
    if (el.disabled) row.недоступен = true;
    if (el.type) row.вид = el.type;
    if (el.href) row.адрес = el.getAttribute('href');
    if (el.checked !== undefined && el.type && /checkbox|radio/.test(el.type))
      row.отмечен = !!el.checked;
    rows.push(row);
  }
  return rows;
}

function pageText() {
  const doc = inside();
  if (!doc) return null;
  // textContent живёт в дереве, а не в отрисовке: фоновая вкладка не красится,
  // и innerText там пуст, а textContent — нет. Но textContent берёт и то, что
  // человек никогда не увидит: тела скриптов, стили, запасной текст для
  // выключенного JavaScript. Пройдёмся по узлам и возьмём только видимые.
  if (!doc.body) return '';
  const skip = UNSEEN;
  const parts = [];
  const walk = (node) => {
    if (node.nodeType === 3) { parts.push(node.textContent); return; }
    if (node.nodeType !== 1) return;
    if (skip.has(node.tagName)) return;
    if (!visible(node)) return;
    for (const child of node.childNodes) walk(child);
  };
  walk(doc.body);
  return parts.join(' ').replace(/[ \t]+/g, ' ')
              .replace(/\n\s*\n\s*\n+/g, '\n\n').trim().slice(0, 8000);
}

const NO_FRAME = {error: 'внутрь приложения не заглянуть: оно подано не через ' +
                         'проводник, а чужую страницу правило одного происхождения ' +
                         'закрывает. Запустите без --direct'};

window.tgx.registerTool('page', 'видимый текст приложения целиком', {}, () => {
  const text = pageText();
  return text === null ? NO_FRAME : {адрес: inside().location.href, текст: text};
});
window.tgx.registerTool('elements',
  'что на странице можно нажать и заполнить, со ссылками ref', {}, () => {
  const rows = scan();
  return rows === null ? NO_FRAME : {адрес: inside().location.href, элементы: rows};
});
window.tgx.registerTool('click', 'нажать элемент по ref из elements',
  {ref: {type: 'string'}}, (a) => {
  if (!inside()) return NO_FRAME;
  const el = REFS.get(a.ref);
  if (!el) return {error: 'нет такого ref — спросите elements заново'};
  if (el.disabled) return {error: 'элемент недоступен'};
  el.scrollIntoView({block: 'center'});
  el.click();
  note('агент нажал: ' + (label(el) || el.tagName));
  return {нажато: label(el) || el.tagName.toLowerCase()};
});
window.tgx.registerTool('fill', 'вписать текст в поле по ref',
  {ref: {type: 'string'}, text: {type: 'string'}}, (a) => {
  if (!inside()) return NO_FRAME;
  const el = REFS.get(a.ref);
  if (!el) return {error: 'нет такого ref — спросите elements заново'};
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(
    el.constructor.prototype, 'value');
  // Через нативный сеттер, иначе React и подобные не заметят изменения
  if (setter && setter.set) setter.set.call(el, a.text || ''); else el.value = a.text || '';
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  note('агент вписал в ' + (label(el) || el.tagName));
  return {вписано: a.text || ''};
});
window.tgx.registerTool('layout',
  'разметка страницы: видимые блоки с текстом и координатами — снимок без картинки',
  {}, () => {
  const doc = inside();
  if (!doc) return NO_FRAME;
  const out = [];
  const walk = (el, depth) => {
    if (depth > 12 || out.length > 200) return;
    for (const child of el.children) {
      if (UNSEEN.has(child.tagName)) continue;
      if (!visible(child)) continue;
      const box = child.getBoundingClientRect();
      // Не фильтруем по размеру: в невидимой вкладке он обнуляется, а дерево —
      // нет. Координаты кладём как есть, подсказкой; агент опирается на текст.
      const own = [...child.childNodes]
        .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
        .filter(Boolean).join(' ').slice(0, 60);
      const row = {тег: child.tagName.toLowerCase(),
                   x: Math.round(box.x), y: Math.round(box.y),
                   ш: Math.round(box.width), в: Math.round(box.height)};
      const cls = (child.className && child.className.toString) ?
                   child.className.toString().slice(0, 40) : '';
      if (cls) row.класс = cls;
      if (own) row.текст = own;
      const ref = [...REFS.entries()].find(([, e]) => e === child);
      if (ref) row.ref = ref[0];
      out.push(row);
      walk(child, depth + 1);
    }
  };
  scan();                    // обновить REFS, чтобы ссылки совпадали с elements
  walk(doc.body, 0);
  return {адрес: doc.location.href, блоки: out};
});
window.tgx.registerTool('scroll', 'прокрутить приложение',
  {y: {type: 'number'}}, (a) => {
  const doc = inside();
  if (!doc) return NO_FRAME;
  doc.defaultView.scrollBy(0, a.y === undefined ? 400 : a.y);
  return {прокручено: a.y === undefined ? 400 : a.y};
});
window.tgx.registerTool('go', 'перейти внутри приложения по адресу',
  {url: {type: 'string'}}, (a) => {
  if (!inside()) return NO_FRAME;
  if (!a.url) return {error: 'нужен адрес'};
  inside().location.href = a.url;
  note('агент перешёл: ' + a.url);
  return {переход: a.url};
});
window.tgx.registerTool('forward', 'вперёд по истории приложения', {}, () => {
  const doc = inside();
  if (!doc) return NO_FRAME;
  doc.defaultView.history.forward();
  return {вперёд: true};
});
window.tgx.registerTool('reload', 'перезагрузить приложение', {}, () => {
  const doc = inside();
  if (!doc) return NO_FRAME;
  doc.location.reload();
  return {перезагружено: true};
});
window.tgx.registerTool('home', 'вернуться в начало приложения', {}, () => {
  frame.src = HOME;
  note('агент вернулся в начало');
  return {домой: true};
});
window.tgx.registerTool('back', 'назад по истории приложения', {}, () => {
  const doc = inside();
  if (!doc) return NO_FRAME;
  doc.defaultView.history.back();
  return {назад: true};
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

// Навигация окна — не то же самое, что кнопка «назад» приложения.
//
// Ту рисует само приложение, и она уводит на его собственный предыдущий экран,
// но появляется далеко не всегда. А внутри приложения есть обычная история
// браузера, и без этих кнопок из него некуда деться: зашёл вглубь — и сиди.
function place() {
  const doc = inside();
  if (!doc) { where.textContent = 'приложение недоступно'; return; }
  where.textContent = doc.location.pathname + doc.location.search;
  // Историю рамки со стороны не сосчитать — доступна лишь её длина, и та
  // общая. Кнопки поэтому не отключаем: лучше нажатие впустую, чем
  // недоступная кнопка там, где идти есть куда.
  try { navBack.disabled = doc.defaultView.history.length <= 1; } catch (e) {}
}

navBack.onclick = () => { try { inside().defaultView.history.back(); } catch (e) {} };
navFwd.onclick = () => { try { inside().defaultView.history.forward(); } catch (e) {} };
document.getElementById('nav-reload').onclick = () => {
  try { inside().location.reload(); } catch (e) { frame.src = frame.src; }
};
document.getElementById('nav-home').onclick = () => { frame.src = HOME; };
frame.addEventListener('load', () => { place(); announce(); });
setInterval(place, 700);   // адрес меняется и без перезагрузки: маршрутизатор внутри
place();
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
                 on_data: Callable[[str], None] | None = None,
                 through: bool = True) -> None:
        import tgx_windows

        self.title, self.app_url = title, url
        self.on_data = on_data or (lambda _: None)
        self.received: list[str] = []
        self.closed = threading.Event()
        self.bridge = tgx_windows.Bridge()
        self.through = through
        self.fetcher = None
        # Происхождение известно заранее — иначе первый же запрос страницы
        # некуда направить: запасной путь узнаёт его лишь из ответа, а ответа
        # ещё нет.
        if through:
            import urllib.parse

            import tgx_proxy

            parts = urllib.parse.urlsplit(url)
            self.fetcher = tgx_proxy.Fetcher()
            self.fetcher.origin = f"{parts.scheme}://{parts.netloc}"
            self.fetcher.pinned = True
        # Сервер обязан быть многопоточным: поручение агента ждёт ответа
        # страницы, а страница за ответом ходит сюда же. Один поток —
        # и они заперли бы друг друга насмерть.
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{HOST_PATH}"

    def framed(self) -> str:
        """Адрес для рамки: через проводник, если он включён.

        Приложение подаётся **по его же пути**, а не под префиксом. Это важнее,
        чем кажется: почти всякое приложение — одностраничное, и его
        маршрутизатор разбирает `location.pathname` сам. Под префиксом
        `/x/https/хост/bot/…` он видит путь, не начинающийся с ожидаемого
        `/bot`, и не рисует ничего — белый экран без единой ошибки. Отдавая
        приложение по родному пути, мы оставляем маршрутизатору привычный вид,
        а префикс `/x/` остаётся только для чужих доменов.
        """
        if not self.through:
            return self.app_url

        import urllib.parse

        parts = urllib.parse.urlsplit(self.app_url)
        tail = parts.path or "/"
        if parts.query:
            tail += "?" + parts.query
        if parts.fragment:
            tail += "#" + parts.fragment
        return tail

    def page(self) -> str:
        import tgx_windows

        return PAGE % {"title": _escape(self.title), "url": _escape(self.framed()),
                       "title_json": json.dumps(self.title, ensure_ascii=False),
                       "theme": json.dumps(THEME), "bridge": tgx_windows.BRIDGE_JS,
                       "home_json": json.dumps(self.framed()),
                       "refused": json.dumps(REFUSED, ensure_ascii=False)}

    def _handler(self) -> type:
        host = self

        class Handler(BaseHTTPRequestHandler):
            # Без этого сервер отвечает по HTTP/1.0, и браузер отказывается
            # подгружать куски приложения: «Failed to fetch dynamically imported
            # module» при том, что тот же адрес прекрасно отдаётся curl.
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass                      # это окно, а не веб-сервер

            def _ok(self, body: bytes, kind: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            OURS = ("/mcp/", "/sent", "/closed", "/x/", HOST_PATH)

            def _ours_page(self) -> bool:
                """Наша собственная страница-окно."""
                return self.path.rstrip("/") == HOST_PATH.rstrip("/") or \
                    self.path.startswith(HOST_PATH + "?")

            def _stray(self) -> bool:
                """Запрос приложения, который не попал под переписывание.

                Такие остаются: адрес мог собраться в коде из кусков, которых в
                тексте не видно. Отдавать на них нашу разметку — верный способ
                сломать приложение молча, поэтому уводим их туда же.
                """
                if self._ours_page() or self.path.startswith(self.OURS):
                    return False
                return bool(getattr(host.fetcher, "origin", ""))

            def _proxy(self, method: str, stray: bool = False) -> None:
                """Чужая страница, поданная с нашего адреса."""
                import tgx_proxy

                if host.fetcher is None:
                    host.fetcher = tgx_proxy.Fetcher()
                if stray:
                    self.path = tgx_proxy.to_path(host.fetcher.origin + self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                try:
                    code, headers, payload = host.fetcher.get(
                        self.path, method, dict(self.headers), body)
                except ValueError as exc:
                    code, headers, payload = 400, {}, str(exc).encode()
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)

            def do_GET(self) -> None:     # noqa: N802 — имя задаёт библиотека
                if self.path.startswith("/x/"):
                    return self._proxy("GET")
                if self._stray():
                    return self._proxy("GET", stray=True)
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
                if self.path.startswith("/x/"):
                    return self._proxy("POST")
                if self._stray():
                    return self._proxy("POST", stray=True)
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
