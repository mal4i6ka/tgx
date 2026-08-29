#!/usr/bin/env python3
"""Inline media previews for the tgx TUI.

Pictures are rendered by textual-image, which picks the best channel the
terminal actually offers: the kitty graphics protocol (kitty, Ghostty, WezTerm),
sixel, or — everywhere else, Terminal.app included — half-block cells, which are
plain coloured text and therefore composite and scroll like any other widget.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

BACKENDS = ("auto", "tgp", "sixel", "halfcell", "unicode", "off")

# A cell is roughly twice as tall as it is wide, and half-block rendering packs
# two pixels into one cell vertically — so rows = cols * aspect / 2 either way.
CELL_ASPECT = 2.0

_MISSING: set[str] = set()


def cache_dir() -> Path:
    base = Path(os.environ.get("TGX_HOME", Path.home() / "telegram-cli-tools"))
    return base / "data" / "media-cache"


def available() -> bool:
    try:
        import PIL  # noqa: F401
        import textual_image.widget  # noqa: F401
    except ModuleNotFoundError as exc:
        _MISSING.add(exc.name or "?")
        return False
    return True


def missing() -> str:
    return ", ".join(sorted(_MISSING)) or "pillow, textual-image"


def probe(backend: str = "auto") -> str:
    """Resolve terminal image support *before* Textual takes over stdin.

    textual-image asks the terminal whether it speaks sixel or the kitty graphics
    protocol, and it asks at import time.  That reply can only be read while stdin
    is still ours — once Textual starts its input reader the answer is swallowed
    and everything silently degrades to half cells.  So the CLI calls this before
    starting the app; afterwards the detected class is cached for the session.
    """
    if backend == "off" or not available():
        return "off"
    try:
        import textual_image.renderable  # noqa: F401  — the import performs the query
        from textual_image._terminal import get_cell_size

        get_cell_size()
    except Exception:
        pass
    return backend_name(backend)


HUMAN = {
    "tgp": "kitty graphics",
    "sixel": "sixel",
    "halfcell": "полублоки",
    "unicode": "юникод",
    "auto": "авто",
    "off": "выкл",
}


def describe(backend: str = "auto") -> str:
    return HUMAN.get(backend_name(backend), backend_name(backend))


def widget_class(backend: str = "auto") -> Any:
    """Resolve a textual-image widget class, or None when previews are off."""
    if backend == "off" or not available():
        return None
    import textual_image.widget as widgets

    table = {
        "auto": widgets.Image,
        "tgp": widgets.TGPImage,
        "sixel": widgets.SixelImage,
        "halfcell": widgets.HalfcellImage,
        "unicode": widgets.UnicodeImage,
    }
    return table.get(backend, widgets.Image)


def backend_name(backend: str = "auto") -> str:
    """Which channel is actually in use: tgp, sixel, halfcell, unicode or off."""
    if backend == "off" or not available():
        return "off"
    if backend != "auto":
        return backend
    try:
        import textual_image.renderable as renderable

        return renderable.Image.__module__.rsplit(".", 1)[-1]
    except Exception:
        return "halfcell"


def measure(path: Path, max_cols: int = 56, max_rows: int = 20) -> tuple[int, int] | None:
    """Cell size that preserves the picture's aspect ratio, or None if unreadable."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
            # Decode it here: a truncated file opens fine but explodes later inside
            # the renderer, where the exception would take the whole app down.
            img.load()
    except Exception:
        return None
    if not width or not height:
        return None
    cols = max(4, min(max_cols, width))
    rows = max(2, round(cols * (height / width) / CELL_ASPECT))
    if rows > max_rows:
        rows = max_rows
        cols = max(4, min(max_cols, round(rows * CELL_ASPECT * (width / height))))
    return cols, rows


def make_widget(path: Path, max_cols: int = 56, max_rows: int = 20, backend: str = "auto") -> Any:
    """Build a sized image widget for `path`, or None when it cannot be shown."""
    cls = widget_class(backend)
    if cls is None:
        return None
    size = measure(path, max_cols, max_rows)
    if size is None:
        return None
    cols, rows = size
    try:
        widget = cls(str(path), classes="media")
    except Exception:
        # A protocol backend can refuse at construction time; fall back to text cells.
        try:
            import textual_image.widget as widgets

            widget = widgets.HalfcellImage(str(path), classes="media")
        except Exception:
            return None
    widget.styles.width = cols
    widget.styles.height = rows
    return widget


def graphical(backend: str = "auto") -> bool:
    """True when the terminal draws real pixels (kitty protocol or sixel)."""
    name = backend_name(backend)
    return name in {"tgp", "sixel"}


SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),          # RIFF....WEBP
    (b"\x00\x00\x00", ".mp4"),  # ....ftyp
)


def suffix_for(path: Path) -> str:
    """Real file extension, sniffed from content.

    macOS (and every desktop) picks the viewer by extension: a JPEG saved as
    `.thumb` gets a dynamic UTI, no application is associated, and `open` fails
    silently.  So every cached file has to carry the extension of what it is.
    """
    try:
        head = path.open("rb").read(16)
    except Exception:
        return ".bin"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    for magic, suffix in SIGNATURES:
        if head.startswith(magic) and suffix not in {".webp", ".mp4"}:
            return suffix
    return ".bin"


def with_real_suffix(path: Path) -> Path:
    """Rename `path` to carry its real extension; returns the final path."""
    suffix = suffix_for(path)
    if path.suffix == suffix:
        return path
    target = path.with_suffix(suffix)
    try:
        path.replace(target)
    except Exception:
        return path
    return target


def open_external(path: Path) -> str | None:
    """Hand the file to the desktop's own viewer. Returns an error message, or None."""
    import subprocess

    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        done = subprocess.run([opener, str(path)], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return f"нет команды {opener}"
    except Exception as exc:
        return str(exc)
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        return message or f"{opener} вернул код {done.returncode}"
    return None


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg"}


def is_video_file(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def poster_frame(path: Path) -> Path | None:
    """A still from the middle of a video, so Telegram has something to show.

    Telethon fills duration and dimensions itself (that is what `hachoir` is for),
    but never a cover frame — without one the video arrives as a grey block.
    Needs ffmpeg; without it we simply send no thumbnail.
    """
    import shutil
    import subprocess

    if not is_video_file(path) or shutil.which("ffmpeg") is None:
        return None
    target = cache_dir() / f"poster-{Path(path).stem}-{Path(path).stat().st_size}.jpg"
    if target.exists() and target.stat().st_size:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=320:-2", str(target)],
            capture_output=True, timeout=30, check=False,
        )
    except Exception:
        return None
    return target if target.exists() and target.stat().st_size else None


PREVIEWABLE = ("photo", "video", "sticker", "animation", "image")


def wants_preview(media_label_text: str) -> bool:
    """Cheap check against the chip text the backend already produced."""
    text = (media_label_text or "").lower()
    return any(word in text for word in PREVIEWABLE)
