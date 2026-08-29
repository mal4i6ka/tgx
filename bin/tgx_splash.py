#!/usr/bin/env python3
"""Animated intro for tgx, powered by terminaltexteffects.

Purely cosmetic and always optional: no TTY, NO_COLOR, TGX_NO_SPLASH or a
missing terminaltexteffects install all degrade to a plain (or skipped) banner.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import random
import sys
from typing import Any

from tgx_render import ACCENT, PALETTE

LOGO = r"""
 ████████╗  ██████╗  ██╗  ██╗
 ╚══██╔══╝ ██╔════╝  ╚██╗██╔╝
    ██║    ██║  ███╗  ╚███╔╝
    ██║    ██║   ██║  ██╔██╗
    ██║    ╚██████╔╝ ██╔╝ ██╗
    ╚═╝     ╚═════╝  ╚═╝  ╚═╝
      telegram · in your terminal
""".strip("\n")

# Effects that look good on a six-line logo, in taste order. `random` picks one.
FEATURED = (
    "beams", "slide", "decrypt", "matrix", "wipe", "print", "waves",
    "spotlights", "colorshift", "laseretch", "pour", "expand", "middleout",
    "binarypath", "orbittingvolley", "rain", "scattered", "swarm", "sweep",
    "synthgrid", "unstable", "vhstape", "blackhole", "bubbles", "burn",
    "crumble", "errorcorrect", "fireworks", "highlight", "overflow", "rings",
    "slice", "smoke", "spray", "thunderstorm", "bouncyballs", "random_sequence",
)

DEFAULT_EFFECT = "beams"
GRADIENT = ("#8FE3FF", ACCENT, PALETTE["accent_dim"], "#2C7EA8")


def available() -> tuple[str, ...]:
    try:
        import terminaltexteffects.effects as effects_pkg
    except ModuleNotFoundError:
        return ()
    found = tuple(sorted(m.name[len("effect_"):] for m in pkgutil.iter_modules(effects_pkg.__path__) if m.name.startswith("effect_")))
    return found + ("random",)


def _load_effect(name: str) -> Any:
    """Return the effect class for `name`, or None when it cannot be found."""
    from terminaltexteffects.engine.base_effect import BaseEffect

    module = importlib.import_module(f"terminaltexteffects.effects.effect_{name}")
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, BaseEffect) and attr is not BaseEffect and attr.__module__ == module.__name__:
            return attr
    return None


def enabled(force: bool = False) -> bool:
    if force:
        return True
    if os.environ.get("TGX_NO_SPLASH") or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def play(effect: str | None = None, text: str = LOGO, force: bool = False, frame_rate: int = 120) -> bool:
    """Animate `text`. Returns True when something was actually animated."""
    if not enabled(force):
        return False
    names = available()
    if not names:
        return False
    name = (effect or DEFAULT_EFFECT).strip().lower().replace("-", "_")
    if name in {"random", "?"}:
        name = random.choice([n for n in FEATURED if n in names])
    if name not in names:
        return False
    try:
        from terminaltexteffects.utils.graphics import Color

        cls = _load_effect(name)
        if cls is None:
            return False
        anim = cls(text)
        anim.terminal_config.frame_rate = frame_rate
        config = anim.effect_config
        stops = tuple(Color(c.lstrip("#")) for c in GRADIENT)
        for attr, value in (
            ("final_gradient_stops", stops),
            ("final_gradient_steps", (12,)),
            ("beam_gradient_stops", stops[1:]),
        ):
            if hasattr(config, attr):
                setattr(config, attr, value)
        with anim.terminal_output() as terminal:
            for frame in anim:
                terminal.print(frame)
        return True
    except KeyboardInterrupt:
        print()
        return True
    except Exception:
        # An animation is never worth failing a command over.
        return False


def static(text: str = LOGO) -> None:
    """Non-animated fallback banner."""
    try:
        from rich.text import Text

        from tgx_render import console

        console().print(Text(text, style=f"bold {ACCENT}"))
    except Exception:
        print(text)
