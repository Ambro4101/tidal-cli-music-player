"""Render album covers as terminal graphics.

Uses rich-pixels (unicode half-blocks + truecolor), which works in any truecolor
terminal and embeds cleanly inside Textual — no kitty/sixel protocol needed.

render_cover raises CoverError with a specific reason (missing deps, no URL, or
a download/decode failure) so callers can tell the user what actually went wrong
instead of a vague catch-all.
"""
from __future__ import annotations

import io
import urllib.request


class CoverError(Exception):
    """Why a cover couldn't be rendered (carries a human-readable reason)."""


def render_cover(url, px: int = 32):
    """Fetch a cover image and return a rich-pixels renderable.

    Raises CoverError with a specific reason on failure. `px` is the square
    pixel size to downscale to; with half-blocks that yields roughly `px` cells
    wide by `px/2` cells tall.
    """
    if not url:
        raise CoverError("no cover art available for this track")

    try:
        from PIL import Image
        from rich_pixels import Pixels
    except Exception as exc:
        raise CoverError(
            "image libraries missing — run `pipx runpip tidal-cli install "
            "pillow rich-pixels` (or reinstall)"
        ) from exc

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tidal-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((px, px))
        return Pixels.from_image(img)
    except Exception as exc:
        raise CoverError(f"couldn't fetch or decode the image ({exc})") from exc


def load_image(url, max_px: int = 640):
    """Download a cover and return a PIL image (for the textual-image widget,
    which renders true pixels via Kitty/Sixel when the terminal supports it).

    Returns a higher-resolution image than the half-block path needs, so a
    graphics-capable terminal has real detail to show. Raises CoverError.
    """
    if not url:
        raise CoverError("no cover art available for this track")
    try:
        from PIL import Image
    except Exception as exc:
        raise CoverError(
            "Pillow missing — run `pipx runpip tidal-cli install pillow` (or reinstall)"
        ) from exc
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tidal-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if max_px and (img.width > max_px or img.height > max_px):
            img.thumbnail((max_px, max_px))
        return img
    except Exception as exc:
        raise CoverError(f"couldn't fetch or decode the image ({exc})") from exc
