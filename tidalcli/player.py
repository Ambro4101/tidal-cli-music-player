"""Playback engine: a queue-aware wrapper around libmpv (python-mpv) that does
*true gapless* playback via mpv's internal playlist.

How gapless works here
----------------------
We never decode audio ourselves. Instead of loading one track at a time and
reacting to "track ended", we keep a small rolling *window* of pre-resolved
stream URLs inside mpv's own playlist. mpv (with gapless-audio + prefetch)
opens the next entry before the current one finishes, so natural track-to-track
transitions have no silence.

Two indexing spaces
-------------------
    * logical queue  -- ``self.queue`` (track dicts) + ``self.index``
    * mpv playlist   -- a growing list that mirrors a window of the queue

``self._loaded[mpv_pos] == logical_index`` maps between them. Because we only
ever ``replace`` at the head and ``append`` at the tail (in increasing logical
order), an mpv playlist position is exactly an index into ``self._loaded``.

Transitions
-----------
    * Natural end-of-track : mpv advances itself -> the playlist-pos observer
      fires -> we sync ``self.index`` and refill the window. Gapless.
    * Manual next          : reuse the prefetched entry (``playlist-next``) when
      present; otherwise rebuild from the target.
    * Manual prev / jump   : rebuild from the target (a small gap is expected
      on a deliberate skip).

Threading
---------
mpv callbacks run on mpv's event thread; daemon commands run on connection
threads. Shared state is guarded by ``self._lock``. URL resolution (a network
call) is done *off the lock* by a single dedicated refill thread, so transport
never blocks on the network. A generation counter (``self._gen``) lets a
rebuild invalidate any prefetch that was in flight.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

import mpv

# How many tracks past the current one to keep resolved + loaded in mpv.
# 1 is enough for gapless (the next track is already open); bump for safety.
WINDOW_AHEAD = 1


class Player:
    def __init__(
        self,
        url_resolver: Callable[[str], str],
        initial_volume: int = 80,
        on_change: Optional[Callable[[], None]] = None,
        log_handler: Optional[Callable[[str, str, str], None]] = None,
    ):
        self._resolve = url_resolver
        self._on_change = on_change          # called when queue/index changes
        self._lock = threading.RLock()

        # Logical queue.
        self.queue: List[dict] = []
        self.index: int = -1
        self.state: str = "stopped"  # stopped | playing | paused

        # mpv-playlist mirror.
        self._loaded: List[int] = []        # mpv pos -> logical index
        self._appended_through: int = -1    # highest logical index appended
        self._gen: int = 0                  # bumped on every rebuild
        self._pending_seek: Optional[float] = None  # set by restore_state

        mpv_kwargs = dict(
            video=False,
            ytdl=False,
            audio_display=False,
            idle="yes",               # stay alive when the playlist drains
            gapless_audio="yes",      # force gapless, not the default "weak"
            prefetch_playlist=True,   # open the next entry early (network-friendly)
            # When ffmpeg's DASH demuxer reads our local .mpd, it defaults to a
            # tiny protocol whitelist (file,crypto,data) and refuses to fetch the
            # https:// segments. Widen it to the network protocols we need.
            demuxer_lavf_o="protocol_whitelist=[file,crypto,data,tcp,tls,http,https]",
        )
        if log_handler is not None:
            # Route mpv's own warnings/errors somewhere visible.
            mpv_kwargs["log_handler"] = log_handler
            mpv_kwargs["loglevel"] = "warn"
        self._mpv = mpv.MPV(**mpv_kwargs)
        self._mpv.volume = max(0, min(100, initial_volume))
        self._mpv.observe_property("playlist-pos", self._on_playlist_pos)

        # Single refill worker: serializes prefetch so appends never race.
        self._shutdown = False
        self._refill_event = threading.Event()
        self._refill_thread = threading.Thread(target=self._refill_loop, daemon=True)
        self._refill_thread.start()

    # ------------------------------------------------------------------ #
    # Public queue API (unchanged signatures: the daemon needs no edits)  #
    # ------------------------------------------------------------------ #
    def set_queue(self, tracks: List[dict], start: int = 0) -> None:
        with self._lock:
            self.queue = list(tracks)
            self.index = -1
        if self.queue:
            self._start_at(start)
        self._notify_change()

    def enqueue(self, tracks: List[dict]) -> None:
        with self._lock:
            was_idle = not self.queue or self.state == "stopped"
            self.queue.extend(tracks)
        if was_idle and self.queue:
            self._start_at(self.index if self.index >= 0 else 0)
        else:
            self._signal_refill()  # new tracks may now fall inside the window
        self._notify_change()

    def clear(self) -> None:
        with self._lock:
            self.queue = []
            self.index = -1
            self._loaded = []
            self._appended_through = -1
            self._gen += 1
        self._mpv.command("stop")
        self.state = "stopped"
        self._notify_change()

    def restore_state(self, queue: List[dict], index: int = 0, position: float = 0.0) -> None:
        """Load a saved queue without playing (used on daemon startup).

        Leaves playback stopped but cued at ``index``; the next ``play`` resumes
        there and seeks to ``position``. Does not fire on_change (it's a load,
        not a user edit).
        """
        with self._lock:
            self.queue = list(queue)
            self.index = index if 0 <= index < len(self.queue) else 0
            self._loaded = []
            self._appended_through = self.index - 1
            self._gen += 1
            self.state = "stopped"
            self._pending_seek = float(position or 0)

    # ------------------------------------------------------------------ #
    # Transport                                                           #
    # ------------------------------------------------------------------ #
    def play(self) -> None:
        """Resume if paused, otherwise (re)start at the current index."""
        with self._lock:
            paused = self.state == "paused"
            idx = self.index if self.index >= 0 else 0
            have = bool(self.queue)
        if paused:
            self._mpv.pause = False
            self.state = "playing"
        elif have:
            self._start_at(idx)

    def pause(self) -> None:
        if self.state == "playing":
            self._mpv.pause = True
            self.state = "paused"

    def toggle(self) -> None:
        if self.state == "playing":
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        with self._lock:
            self._loaded = []
            self._appended_through = self.index - 1
            self._gen += 1
        self._mpv.command("stop")
        self.state = "stopped"
        self._notify_change()

    def next(self) -> None:
        with self._lock:
            nxt = self.index + 1
            has = nxt < len(self.queue)
            # Is the next logical track already the next mpv entry? If so we can
            # jump to it instantly and keep gaplessness even on a manual skip.
            cur_pos = _prop(self._mpv, "playlist_pos")
            reuse = (
                cur_pos is not None
                and 0 <= cur_pos + 1 < len(self._loaded)
                and self._loaded[cur_pos + 1] == nxt
            )
        if not has:
            self.stop()
            return
        if reuse:
            self._mpv.command("playlist-next", "force")  # observer syncs index
        else:
            self._start_at(nxt)

    def prev(self) -> None:
        with self._lock:
            prv = self.index - 1
        if prv >= 0:
            self._start_at(prv)
        else:
            self._start_at(0)

    def seek(self, seconds: float) -> None:
        try:
            self._mpv.seek(seconds, reference="relative")
        except Exception:
            pass

    def set_volume(self, vol: int) -> int:
        vol = max(0, min(100, vol))
        self._mpv.volume = vol
        return vol

    # ------------------------------------------------------------------ #
    # Window machinery                                                    #
    # ------------------------------------------------------------------ #
    def _start_at(self, idx: int) -> None:
        """Rebuild the mpv playlist starting at logical ``idx`` and play it.

        Resolves the head URL *before* taking the lock so a network call never
        blocks transport, and so a failure leaves state untouched (it raises and
        the daemon reports it).
        """
        with self._lock:
            if not (0 <= idx < len(self.queue)):
                return
            track = self.queue[idx]
        url = self._resolve(track["id"])  # network; may raise -> propagate
        with self._lock:
            self._gen += 1
            self.index = idx
            self._loaded = [idx]
            self._appended_through = idx
            self.state = "playing"
            self._mpv.command("loadfile", url, "replace")
            self._mpv.pause = False
            seek_to = self._pending_seek
            self._pending_seek = None
        self._signal_refill()
        if seek_to and seek_to > 1:
            self._seek_when_ready(seek_to)
        self._notify_change()

    def _seek_when_ready(self, pos: float) -> None:
        """Seek once playback actually starts (used to resume a restored track)."""
        def worker() -> None:
            try:
                self._mpv.wait_until_playing(timeout=5)
                self._mpv.seek(pos, reference="absolute")
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _notify_change(self) -> None:
        cb = self._on_change
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _signal_refill(self) -> None:
        self._refill_event.set()

    def _refill_loop(self) -> None:
        while not self._shutdown:
            self._refill_event.wait()
            self._refill_event.clear()
            if self._shutdown:
                return
            try:
                self._do_refill()
            except Exception:
                pass  # prefetch is best-effort; never crash the worker

    def _do_refill(self) -> None:
        """Append tracks until the window (index + WINDOW_AHEAD) is loaded.

        Only this thread appends, so ``_appended_through`` advances here alone.
        ``_gen`` guards against a rebuild that happened mid-resolve.
        """
        while True:
            with self._lock:
                gen = self._gen
                if not self.queue:
                    return
                target = min(self.index + WINDOW_AHEAD, len(self.queue) - 1)
                nxt = self._appended_through + 1
                if nxt > target:
                    return
                track = self.queue[nxt]
            try:
                url = self._resolve(track["id"])  # off the lock
            except Exception:
                with self._lock:
                    if self._gen == gen and self._appended_through + 1 == nxt:
                        self._appended_through = nxt  # skip a broken track
                continue
            with self._lock:
                if self._gen != gen:
                    return  # rebuilt under us; a fresh refill is already queued
                if self._appended_through + 1 != nxt:
                    continue
                self._mpv.command("loadfile", url, "append")
                self._loaded.append(nxt)
                self._appended_through = nxt

    def _on_playlist_pos(self, _name: str, value: Optional[int]) -> None:
        """mpv advanced (or drained). Sync the logical index and refill ahead."""
        if value is None or value < 0:
            with self._lock:
                at_end = self.index >= len(self.queue) - 1
            if at_end:
                self.state = "stopped"
            return
        changed = False
        with self._lock:
            if 0 <= value < len(self._loaded):
                new_index = self._loaded[value]
                changed = new_index != self.index
                self.index = new_index
                self.state = "paused" if _prop(self._mpv, "pause") else "playing"
        self._signal_refill()
        if changed:
            self._notify_change()

    # ------------------------------------------------------------------ #
    # Status / lifecycle                                                  #
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        with self._lock:
            current = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
            qlen = len(self.queue)
            idx = self.index
        return {
            "state": self.state,
            "current": current,
            "position": _prop(self._mpv, "time_pos"),
            "duration": _prop(self._mpv, "duration"),
            "volume": _prop(self._mpv, "volume"),
            "index": idx,
            "queue_length": qlen,
        }

    def shutdown(self) -> None:
        self._shutdown = True
        self._refill_event.set()
        try:
            self._mpv.terminate()
        except Exception:
            pass


def _prop(player: "mpv.MPV", name: str):
    try:
        return getattr(player, name)
    except Exception:
        return None
