"""Normalize tidalapi objects into plain JSON-serializable dicts.

The daemon and CLI talk over a socket, so everything crossing that boundary
must be JSON-safe. These helpers also insulate the rest of the code from
tidalapi attribute quirks (some fields are optional / version-dependent).
"""
from __future__ import annotations

from typing import Any, Optional


def _artist_name(obj: Any) -> str:
    artist = getattr(obj, "artist", None)
    if artist is not None and getattr(artist, "name", None):
        return artist.name
    artists = getattr(obj, "artists", None)
    if artists:
        return ", ".join(a.name for a in artists if getattr(a, "name", None))
    return "Unknown Artist"


def _safe(obj: Any, attr: str, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _cover_url(track: Any) -> Optional[str]:
    album = getattr(track, "album", None)
    image = getattr(album, "image", None)
    if not callable(image):
        return None
    try:
        return image(640)
    except Exception:
        return None


def track_info(track: Any) -> dict:
    return {
        "kind": "track",
        "id": str(_safe(track, "id", "")),
        "title": _safe(track, "name", "Unknown"),
        "artist": _artist_name(track),
        "album": _safe(_safe(track, "album"), "name"),
        "duration": int(_safe(track, "duration", 0) or 0),
        "explicit": bool(_safe(track, "explicit", False)),
        "cover": _cover_url(track),
    }


def album_info(album: Any) -> dict:
    return {
        "kind": "album",
        "id": str(_safe(album, "id", "")),
        "title": _safe(album, "name", "Unknown"),
        "artist": _artist_name(album),
        "num_tracks": _safe(album, "num_tracks"),
        "year": _safe(album, "year"),
    }


def artist_info(artist: Any) -> dict:
    return {
        "kind": "artist",
        "id": str(_safe(artist, "id", "")),
        "name": _safe(artist, "name", "Unknown"),
    }


def playlist_info(playlist: Any) -> dict:
    return {
        "kind": "playlist",
        "id": str(_safe(playlist, "id", "")),
        "title": _safe(playlist, "name", "Unknown"),
        "creator": _safe(_safe(playlist, "creator"), "name"),
        "num_tracks": _safe(playlist, "num_tracks"),
    }


def mix_info(mix: Any) -> dict:
    return {
        "kind": "mix",
        "id": str(_safe(mix, "id", "")),
        "title": _safe(mix, "title", "Mix"),
        "subtitle": (_safe(mix, "sub_title") or _safe(mix, "short_subtitle") or ""),
    }


def fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "--:--"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_QUALITY_LABELS = {
    "LOW": "AAC ~96 kbps",
    "HIGH": "AAC ~320 kbps",
    "LOSSLESS": "FLAC lossless (CD)",
    "HI_RES_LOSSLESS": "FLAC HiRes",
}


def fmt_quality(info: dict) -> str:
    """Render a stream_info dict as e.g. 'FLAC HiRes  24-bit / 96 kHz'."""
    q = info.get("quality")
    parts = [_QUALITY_LABELS.get(q, q or "unknown")]
    bd, sr = info.get("bit_depth"), info.get("sample_rate")
    if q in ("LOSSLESS", "HI_RES_LOSSLESS") and bd and sr:
        parts.append(f"{bd}-bit / {sr / 1000:g} kHz")
    mode = info.get("audio_mode")
    if mode and mode.upper() != "STEREO":
        parts.append(mode.replace("_", " ").title())
    return "  ".join(parts)
