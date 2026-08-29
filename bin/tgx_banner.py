#!/usr/bin/env python3
"""Запись терминального баннера в видео — например, себе в аватар.

Кадры снимаются не с картинки, а с настоящего терминала: анимация играет
в псевдотерминале, вывод разбирает эмулятор (pyte), и каждая ячейка сетки
рисуется моноширинным шрифтом со своим цветом. Поэтому в файл попадает ровно
то, что видно в окне, вместе с блочными глифами и градиентом.
"""
from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

BACKGROUND = "#17212B"
FOREGROUND = "#E4EDF5"

# Имена цветов, которыми pyte отдаёт стандартную палитру ANSI.
ANSI = {
    "black": "#2B3743", "red": "#E9576B", "green": "#4FCE5D", "brown": "#E5CA77",
    "blue": "#229ED9", "magenta": "#C77DBB", "cyan": "#8FE3FF", "white": "#E4EDF5",
    "brightblack": "#6D7F8F", "brightred": "#FF7A8A", "brightgreen": "#7BE38A",
    "brightbrown": "#FFE3A0", "brightblue": "#5CC0F0", "brightmagenta": "#E2A0DA",
    "brightcyan": "#B8EEFF", "brightwhite": "#FFFFFF", "default": FOREGROUND,
}

FONTS = ("/System/Library/Fonts/Menlo.ttc",
         "/System/Library/Fonts/SFNSMono.ttf",
         "/Library/Fonts/DejaVuSansMono.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


class BannerError(RuntimeError):
    """Не удалось записать баннер."""


def _colour(value: Any, fallback: str) -> str:
    if not value or value == "default":
        return fallback
    text = str(value)
    if text in ANSI:
        return ANSI[text]
    if len(text) == 6:
        try:
            int(text, 16)
            return "#" + text
        except ValueError:
            pass
    return fallback


def capture(command: list[str], *, cols: int = 40, rows: int = 16, fps: int = 20,
            seconds: float = 8.0) -> list[list[list[tuple[str, str]]]]:
    """Прогнать команду в псевдотерминале и снять кадры сетки символов.

    Возвращает список кадров; кадр — строки, строка — пары (символ, цвет).
    """
    import pyte

    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    frames: list[list[list[tuple[str, str]]]] = []

    def snapshot() -> list[list[tuple[str, str]]]:
        grid = []
        for y in range(rows):
            line = screen.buffer[y]
            grid.append([(line[x].data or " ", _colour(line[x].fg, FOREGROUND))
                         for x in range(cols)])
        return grid

    environment = dict(os.environ, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows),
                       COLORTERM="truecolor")
    environment.pop("NO_COLOR", None)
    environment.pop("TGX_NO_SPLASH", None)

    pid, fd = pty.fork()
    if pid == 0:                                  # ребёнок: это уже настоящий tty
        os.execvpe(command[0], command, environment)

    try:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        interval = 1.0 / fps
        started = last = time.monotonic()
        while time.monotonic() - started < seconds:
            ready, _, _ = select.select([fd], [], [], interval)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                stream.feed(chunk)
            now = time.monotonic()
            if now - last >= interval:
                frames.append(snapshot())
                last = now
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass

    if not frames:
        raise BannerError("терминал не дал ни одного кадра")
    frames.append(snapshot())                     # последний кадр — готовый логотип
    return frames


def draw(frames: list[list[list[tuple[str, str]]]], out_dir: Path, *, size: int = 512,
         font_size: int = 34) -> list[Path]:
    """Нарисовать кадры шрифтом терминала и уложить их в квадрат."""
    from PIL import Image, ImageDraw, ImageFont

    path = next((f for f in FONTS if Path(f).exists()), None)
    if path is None:
        raise BannerError("не нашёл моноширинный шрифт с блочными символами")
    font = ImageFont.truetype(path, font_size)

    # Шаг сетки — родная высота строки шрифта: псевдографика ╔═╗ нарисована
    # именно под неё и стыкуется только при этом шаге. Сплошной блок при этом
    # заливается прямоугольником, иначе между строками остаются щели.
    ascent, descent = font.getmetrics()
    cell_w = int(round(font.getlength("█"))) or font_size // 2
    cell_h = ascent + descent
    baseline = ascent

    # Пустые поля терминала обрезаем по всем кадрам сразу, иначе логотип «дышит».
    rows, cols = len(frames[0]), len(frames[0][0])
    used_rows = [y for y in range(rows)
                 if any(f[y][x][0].strip() for f in frames for x in range(cols))]
    used_cols = [x for x in range(cols)
                 if any(f[y][x][0].strip() for f in frames for y in range(rows))]
    if used_rows and used_cols:
        y0, y1 = max(0, used_rows[0] - 1), min(rows - 1, used_rows[-1] + 1)
        x0, x1 = max(0, used_cols[0] - 1), min(cols - 1, used_cols[-1] + 1)
        frames = [[line[x0:x1 + 1] for line in f[y0:y1 + 1]] for f in frames]
        rows, cols = len(frames[0]), len(frames[0][0])

    grid_w, grid_h = cell_w * cols, cell_h * rows
    scale = min(size / grid_w, size / grid_h)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for index, frame in enumerate(frames):
        canvas = Image.new("RGB", (grid_w, grid_h), BACKGROUND)
        pen = ImageDraw.Draw(canvas)
        for y, line in enumerate(frame):
            for x, (char, colour) in enumerate(line):
                if not char or char == " ":
                    continue
                if char == "█":                   # рисуем как заливку — глифы стыкуются
                    pen.rectangle([x * cell_w, y * cell_h,
                                   (x + 1) * cell_w - 1, (y + 1) * cell_h - 1], fill=colour)
                else:
                    pen.text((x * cell_w, y * cell_h + baseline), char,
                             font=font, fill=colour, anchor="ls")
        if scale != 1.0:
            canvas = canvas.resize((max(2, int(grid_w * scale)), max(2, int(grid_h * scale))),
                                   Image.LANCZOS)
        square = Image.new("RGB", (size, size), BACKGROUND)
        square.paste(canvas, ((size - canvas.width) // 2, (size - canvas.height) // 2))
        target = out_dir / f"frame-{index:05d}.png"
        square.save(target)
        written.append(target)
    return written


def encode(frames: list[Path], out: Path, *, fps: int = 20, hold: float = 1.2) -> Path:
    """Собрать кадры в mp4, задержав последний — чтобы логотип успевал прочитаться."""
    if shutil.which("ffmpeg") is None:
        raise BannerError("нужен ffmpeg: brew install ffmpeg")
    last = frames[-1]
    for extra in range(int(fps * hold)):
        copy = last.with_name(f"frame-{len(frames) + extra:05d}.png")
        shutil.copyfile(last, copy)

    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(last.parent / "frame-%05d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not out.exists():
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["ffmpeg не справился"]
        raise BannerError(f"не удалось собрать видео: {tail[0]}")
    return out


def record(out: Path, *, effect: str = "beams", cols: int = 40, rows: int = 16,
           fps: int = 30, seconds: float = 12.0, size: int = 512, speed: int = 60,
           hold: float = 1.5, work: Path | None = None) -> dict[str, Any]:
    """Полный путь: анимация в pty → кадры → mp4.

    `speed` — частота кадров самой анимации; чем меньше, тем медленнее и
    разборчивее. Возвращает ещё и `cover` — секунду, на которой логотип уже
    собран: именно её стоит отдать в `--start`, иначе в аватаре будет виден
    пустой первый кадр, а не сам баннер.
    """
    import sys

    work = work or out.parent / ".frames"
    if work.exists():
        shutil.rmtree(work)
    script = ("import sys; sys.path.insert(0, %r); import tgx_splash; "
              "tgx_splash.play(%r, force=True, frame_rate=%d)"
              % (str(Path(__file__).resolve().parent), effect, speed))
    frames = capture([sys.executable, "-c", script], cols=cols, rows=rows, fps=fps, seconds=seconds)
    images = draw(frames, work, size=size)
    video = encode(images, out, fps=fps, hold=hold)
    shutil.rmtree(work, ignore_errors=True)
    return {"video": str(video), "frames": len(images), "effect": effect,
            "size": f"{size}×{size}", "seconds": round((len(images) + fps * hold) / fps, 1),
            "cover": round(len(images) / fps, 1)}
