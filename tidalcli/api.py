"""Thin wrappers over tidalapi for search, browsing, and library access.

Everything returns JSON-safe dicts (via models.py) so results can travel over
the socket unchanged. The one non-dict function is `playback_url`, used by the
daemon to feed mpv.
"""
from __future__ import annotations

import base64
import os
import tempfile
from typing import Any, List

import tidalapi

from . import models

# Where we drop DASH manifests for mpv to read (tiny XML files).
_MPD_DIR = os.path.join(tempfile.gettempdir(), "tidalcli-streams")


def _is_network_url(u) -> bool:
    """Only http(s) URLs are safe to hand to mpv."""
    return isinstance(u, str) and u.startswith(("https://", "http://"))


def _write_mpd(track_id: str, xml: str) -> str:
    """Write a DASH manifest to a temp .mpd file and return its path. mpv's
    demuxer reads this and fetches/assembles the segments itself."""
    os.makedirs(_MPD_DIR, exist_ok=True)
    path = os.path.join(_MPD_DIR, f"{track_id}.mpd")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    return path


class Api:
    def __init__(self, session: tidalapi.Session):
        self.session = session

    # ---- search -------------------------------------------------------
    def search(self, query: str, kind: str = "track", limit: int = 25) -> List[dict]:
        model_map = {
            "track": tidalapi.Track,
            "album": tidalapi.Album,
            "artist": tidalapi.Artist,
            "playlist": tidalapi.Playlist,
        }
        models_arg = [model_map.get(kind, tidalapi.Track)]
        result = self.session.search(query, models=models_arg, limit=limit)

        if kind == "track":
            return [models.track_info(t) for t in result.get("tracks", [])]
        if kind == "album":
            return [models.album_info(a) for a in result.get("albums", [])]
        if kind == "artist":
            return [models.artist_info(a) for a in result.get("artists", [])]
        if kind == "playlist":
            return [models.playlist_info(p) for p in result.get("playlists", [])]
        return []

    # ---- single items / tracklists ------------------------------------
    def track(self, track_id: str) -> dict:
        return models.track_info(self.session.track(track_id))

    def album_tracks(self, album_id: str) -> List[dict]:
        album = self.session.album(album_id)
        return [models.track_info(t) for t in album.tracks()]

    def artist_top_tracks(self, artist_id: str) -> List[dict]:
        artist = self.session.artist(artist_id)
        return [models.track_info(t) for t in artist.get_top_tracks()]

    def artist_albums(self, artist_id: str) -> List[dict]:
        """Full albums followed by EPs/singles, each tagged with a category."""
        artist = self.session.artist(artist_id)
        out: List[dict] = []
        for getter, category in (
            ("get_albums", "Album"),
            ("get_ep_singles", "EP/Single"),
        ):
            fn = getattr(artist, getter, None)
            if not callable(fn):
                continue
            try:
                items = fn() or []
            except Exception:
                items = []
            for a in items:
                info = models.album_info(a)
                info["category"] = category
                out.append(info)
        return out

    def playlist_tracks(self, playlist_id: str) -> List[dict]:
        playlist = self.session.playlist(playlist_id)
        return [models.track_info(t) for t in playlist.tracks()]

    # ---- library / favorites ------------------------------------------
    def mixes(self) -> List[dict]:
        """The user's mixes (Daily Discovery, New Arrivals, My Mix 1-8, …)."""
        try:
            from tidalapi.mix import Mix
            items = list(self.session.mixes())
        except Exception:
            return []
        return [models.mix_info(m) for m in items if isinstance(m, Mix)]

    def mix_tracks(self, mix_id: str) -> List[dict]:
        """Tracks of a mix (videos are skipped)."""
        items = self.session.mix(mix_id).items()
        return [models.track_info(t) for t in items
                if isinstance(t, tidalapi.Track)]

    def favorite_tracks(self) -> List[dict]:
        return [models.track_info(t) for t in self.session.user.favorites.tracks()]

    def add_favorite_track(self, track_id: str) -> bool:
        return bool(self.session.user.favorites.add_track(track_id))

    def remove_favorite_track(self, track_id: str) -> bool:
        return bool(self.session.user.favorites.remove_track(track_id))

    # ---- playback url -------------------------------------------------
    def lyrics(self, track_id: str):
        """Lyrics for a track as {'text', 'subtitles'} (subtitles = LRC with
        timestamps), or None when the track has no lyrics available."""
        try:
            lyr = self.session.track(track_id).lyrics()
        except Exception:
            return None
        text = getattr(lyr, "text", "") or ""
        subs = getattr(lyr, "subtitles", "") or ""
        if not text and not subs:
            return None
        return {"text": text, "subtitles": subs}

    def cover_url(self, track_id: str, size: int = 640):
        """Fresh cover URL for a track id (works even when the queued track dict
        predates the cover field, e.g. restored from an old saved queue)."""
        track = self.session.track(track_id)
        album = getattr(track, "album", None)
        image = getattr(album, "image", None) if album is not None else None
        if not callable(image):
            return None
        try:
            return image(size)
        except Exception:
            return None

    def stream_info(self, track_id: str) -> dict:
        """The quality a track is *actually* delivered in (may differ from the
        requested tier if it's unavailable at that quality)."""
        track = self.session.track(track_id)
        stream = track.get_stream()
        q = getattr(stream, "audio_quality", None)
        mode = getattr(stream, "audio_mode", None)
        return {
            "quality": getattr(q, "value", None) or (str(q) if q is not None else None),
            "bit_depth": getattr(stream, "bit_depth", None),
            "sample_rate": getattr(stream, "sample_rate", None),
            "audio_mode": getattr(mode, "value", None) or (str(mode) if mode else None),
            "mpd": bool(getattr(stream, "is_mpd", False)),
        }

    def playback_url(self, track_id: str) -> str:
        """Resolve a playable source for mpv.

          * BTS manifest (single full-file URL)  -> return that URL directly.
            Covers AAC and single-file FLAC.
          * MPD/DASH manifest (multi-segment, e.g. lossless/HiRes over PKCE)
            -> write the raw DASH manifest to a temp .mpd file and return its
            path; mpv's demuxer fetches and assembles the segments. This is the
            correct way to play TIDAL's segmented streams (an earlier attempt to
            hand-concatenate segment URLs via edl:// did not work).
          * Encrypted stream (HiRes/MQA with a key) -> raise; mpv can't decrypt.
          * Legacy get_url() fallback.
        """
        track = self.session.track(track_id)
        stream = track.get_stream()

        try:
            manifest = stream.get_stream_manifest()
        except Exception:
            manifest = None

        if manifest is not None and (
            getattr(manifest, "is_encrypted", False)
            or getattr(manifest, "encryption_key", None)
        ):
            raise RuntimeError(
                "This track's stream is encrypted (HiRes/MQA); mpv can't play "
                "it directly. Set quality to 'high_lossless' in config.json."
            )

        # DASH / MPD: let mpv's demuxer handle the segmented stream.
        if getattr(stream, "is_mpd", False):
            try:
                return _write_mpd(track_id, stream.get_manifest_data())
            except Exception as exc:
                raise RuntimeError(f"Could not read the DASH manifest: {exc}")

        # BTS: a single direct file URL.
        if manifest is not None:
            urls = [u for u in (manifest.get_urls() or []) if _is_network_url(u)]
            if urls:
                return urls[0]

        get_url = getattr(track, "get_url", None)
        if callable(get_url):
            return get_url()

        raise RuntimeError(f"Could not resolve a stream URL for track {track_id}")
