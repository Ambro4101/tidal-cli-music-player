"""The daemon: a long-lived process that owns the session, player, and queue,
and serves commands over a Unix socket.

Run directly:  python -m tidalcli.daemon
The client autostarts it on demand, so you rarely run it by hand.

It loads the saved session non-interactively. If there is no valid session,
it still starts (so `status` works) but playback/search commands return an
error telling the user to run `tidal login`.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Optional

from . import auth, config, ipc
from .api import Api
from .player import Player

logging.basicConfig(
    filename=str(config.LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tidal-daemon")

# The build this daemon process is running, captured at startup. Reported on
# ``ping`` so the client can replace the daemon after a reinstall.
BUILD_ID = config.build_id()


# Commands that change playback state — after these the daemon nudges MPRIS so
# desktop media controls (Plasma widget, lock screen) reflect the new state.
_TRANSPORT_CMDS = {
    "play", "pause", "resume", "toggle", "stop", "next", "prev", "seek", "volume",
}


class Daemon:
    def __init__(self):
        self.settings = config.Settings.load()
        self.api: Optional[Api] = None
        self.player: Optional[Player] = None
        self._running = True
        self._last_save = 0.0
        self._mpris = None
        self._reload_session()

    def _reload_session(self) -> None:
        session = auth.load_session(self.settings)
        if session is None:
            self.api = None
            log.info("No valid session; waiting for `tidal login`.")
            return
        self.api = Api(session)
        if self.player is None:
            self.player = Player(
                url_resolver=self.api.playback_url,
                initial_volume=self.settings.initial_volume,
                on_change=self._on_player_change,
                log_handler=self._mpv_log,
            )
            self._restore_queue()
            self._start_mpris()
        else:
            # New session: point the player at the new api's resolver.
            self.player._resolve = self.api.playback_url
        log.info("Session loaded.")

    def _on_player_change(self) -> None:
        # Fired when the queue/index changes (e.g. auto-advance). Persist state
        # and let any desktop media controls know the track changed.
        self._save_queue()
        self._notify_mpris()

    def _start_mpris(self) -> None:
        try:
            from . import mpris
            self._mpris = mpris.start(self.player, log)
        except Exception as exc:  # pragma: no cover - optional
            log.info("MPRIS not started: %s", exc)
            self._mpris = None

    def _notify_mpris(self) -> None:
        if self._mpris:
            try:
                self._mpris.notify()
            except Exception:
                pass

    def _mpv_log(self, level: str, component: str, message: str) -> None:
        # Surface mpv's own warnings/errors (audio-output init, demux, network)
        # into the daemon log, which would otherwise vanish silently.
        log.warning("mpv[%s/%s] %s", level, component, (message or "").strip())

    # ---- queue persistence --------------------------------------------
    def _save_queue(self) -> None:
        if not self.player:
            return
        try:
            st = self.player.status()
            data = {
                "queue": self.player.queue,
                "index": self.player.index,
                "position": st.get("position") or 0,
            }
            tmp = config.QUEUE_FILE.with_name(config.QUEUE_FILE.name + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(config.QUEUE_FILE)  # atomic
            self._last_save = time.time()
        except Exception:
            log.exception("Failed to save queue")

    def _maybe_save_position(self) -> None:
        # Piggyback on status polls to checkpoint the play position, throttled.
        if self.player and time.time() - self._last_save >= 5.0:
            self._save_queue()

    def _restore_queue(self) -> None:
        if not (self.player and config.QUEUE_FILE.exists()):
            return
        try:
            data = json.loads(config.QUEUE_FILE.read_text())
        except Exception:
            return
        queue = data.get("queue") or []
        if queue:
            self.player.restore_state(
                queue, int(data.get("index", 0)), float(data.get("position", 0))
            )
            log.info("Restored queue: %d tracks at index %s.", len(queue), data.get("index"))

    # ---- command dispatch ---------------------------------------------
    def handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        try:
            if cmd == "ping":
                return ipc.ok({"build": BUILD_ID, "pid": os.getpid()})
            if cmd == "reload":
                self._reload_session()
                return ipc.ok("reloaded")
            if cmd == "shutdown":
                self._running = False
                return ipc.ok("bye")

            if cmd == "status":
                if not self.player:
                    return ipc.ok({"state": "stopped", "logged_in": False})
                s = self.player.status()
                s["logged_in"] = self.api is not None
                self._maybe_save_position()
                return ipc.ok(s)

            # Everything below needs a session.
            if self.api is None:
                return ipc.err("Not logged in. Run: tidal login")

            resp = self._handle_session_cmd(cmd, msg)
            if cmd in _TRANSPORT_CMDS:
                self._notify_mpris()
            return resp
        except Exception as exc:  # never let one bad command kill the daemon
            log.exception("Command %s failed", cmd)
            return ipc.err(str(exc))

    def _handle_session_cmd(self, cmd: str, msg: dict) -> dict:
        api, player = self.api, self.player

        if cmd == "search":
            return ipc.ok(
                api.search(msg["query"], msg.get("type", "track"), msg.get("limit", 25))
            )

        if cmd == "browse":
            # Return an item's children as {"kind", "items"} without playing:
            # album/playlist -> tracks, artist -> albums (its discography).
            return ipc.ok(self._browse_children(msg))

        # ---- loading the queue from various sources ----
        if cmd == "play":
            tracks = self._resolve_tracks(msg)
            if not tracks:
                return ipc.err("Nothing to play.")
            player.set_queue(tracks, start=0)
            return ipc.ok(player.status())

        if cmd == "enqueue":
            tracks = self._resolve_tracks(msg)
            if not tracks:
                return ipc.err("Nothing to enqueue.")
            player.enqueue(tracks)
            return ipc.ok({"added": len(tracks)})

        # ---- transport ----
        if cmd == "pause":
            player.pause(); return ipc.ok(player.status())
        if cmd == "resume":
            player.play(); return ipc.ok(player.status())
        if cmd == "toggle":
            player.toggle(); return ipc.ok(player.status())
        if cmd == "stop":
            player.stop(); return ipc.ok(player.status())
        if cmd == "next":
            player.next(); return ipc.ok(player.status())
        if cmd == "prev":
            player.prev(); return ipc.ok(player.status())
        if cmd == "seek":
            player.seek(float(msg.get("seconds", 0))); return ipc.ok(player.status())
        if cmd == "volume":
            return ipc.ok({"volume": player.set_volume(int(msg.get("level", 80)))})

        if cmd == "queue":
            s = player.status()
            return ipc.ok({"queue": player.queue, "index": s["index"]})

        if cmd == "quality":
            st = self.player.status() if self.player else None
            cur = st.get("current") if st else None
            if not cur:
                return ipc.err("Nothing is playing.")
            info = self.api.stream_info(cur["id"])
            info["requested"] = self.settings.quality
            info["title"] = cur.get("title")
            info["artist"] = cur.get("artist")
            return ipc.ok(info)

        if cmd == "mixes":
            return ipc.ok({"kind": "mix", "items": self.api.mixes()})

        if cmd == "lyrics":
            st = self.player.status() if self.player else None
            cur = st.get("current") if st else None
            tid = msg.get("id") or (cur.get("id") if cur else None)
            if not tid:
                return ipc.err("Nothing is playing.")
            return ipc.ok({"id": tid, "lyrics": self.api.lyrics(tid)})

        if cmd == "cover":
            st = self.player.status() if self.player else None
            cur = st.get("current") if st else None
            tid = msg.get("id") or (cur.get("id") if cur else None)
            if not tid:
                return ipc.err("Nothing is playing.")
            return ipc.ok({
                "url": self.api.cover_url(tid),
                "title": (cur or {}).get("title"),
                "artist": (cur or {}).get("artist"),
            })

        # ---- favorites ----
        if cmd == "fav_list":
            return ipc.ok(api.favorite_tracks())
        if cmd == "fav_add":
            return ipc.ok({"added": api.add_favorite_track(msg["id"])})
        if cmd == "fav_remove":
            return ipc.ok({"removed": api.remove_favorite_track(msg["id"])})

        return ipc.err(f"Unknown command: {cmd}")

    def _browse_children(self, msg: dict) -> dict:
        """One level of drill-down. Returns ``{"kind", "items"}``.

        The daemon owns the parent->child mapping so the UI never has to:
            artist           -> albums (discography)
            album / playlist  -> tracks
            track             -> itself
        """
        item_type = msg.get("type", "track")
        if item_type == "artist":
            return {"kind": "album", "items": self.api.artist_albums(msg.get("id"))}
        return {"kind": "track", "items": self._resolve_tracks(msg)}

    def _resolve_tracks(self, msg: dict) -> list:
        """Turn a play/enqueue request into a list of track dicts.

        Accepts one of: id+type (track/album/artist/playlist), or an explicit
        list of track dicts under "tracks".
        """
        if msg.get("tracks"):
            return msg["tracks"]
        item_id = msg.get("id")
        item_type = msg.get("type", "track")
        if not item_id:
            return []
        if item_type == "track":
            return [self.api.track(item_id)]
        if item_type == "album":
            return self.api.album_tracks(item_id)
        if item_type == "artist":
            return self.api.artist_top_tracks(item_id)
        if item_type == "playlist":
            return self.api.playlist_tracks(item_id)
        if item_type == "mix":
            return self.api.mix_tracks(item_id)
        return []

    # ---- socket server ------------------------------------------------
    def serve(self) -> None:
        if config.SOCKET_PATH.exists():
            config.SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The socket file's permissions are the access-control boundary: anyone
        # who can connect can drive playback and the library. Create it
        # owner-only (umask removes group/other) and chmod as a belt-and-braces.
        old_umask = os.umask(0o177)
        try:
            srv.bind(str(config.SOCKET_PATH))
        finally:
            os.umask(old_umask)
        try:
            os.chmod(config.SOCKET_PATH, 0o600)
        except OSError:
            pass
        srv.listen(8)
        srv.settimeout(1.0)
        try:
            config.PID_FILE.write_text(str(os.getpid()))
        except OSError:
            pass
        log.info("Daemon listening on %s", config.SOCKET_PATH)

        def _sig(*_):
            self._running = False
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)

        try:
            while self._running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            if config.SOCKET_PATH.exists():
                config.SOCKET_PATH.unlink()
            try:
                if config.PID_FILE.exists():
                    config.PID_FILE.unlink()
            except OSError:
                pass
            if self.player:
                self.player.shutdown()
            log.info("Daemon stopped.")

    def _serve_conn(self, conn: socket.socket) -> None:
        with conn:
            msg = ipc.read_message(conn)
            if msg is None:
                return
            response = self.handle(msg)
            try:
                conn.sendall(ipc.encode(response))
            except OSError:
                pass


_LOCK_FD = None  # kept open for the daemon's lifetime to hold the lock


def _acquire_singleton_lock():
    """Return a held lock fd, or None if another daemon already owns it."""
    fd = os.open(str(config.LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main() -> None:
    # Daemonize lightly: detach from the controlling terminal.
    if os.environ.get("TIDAL_CLI_NO_FORK") != "1":
        try:
            if os.fork() > 0:
                sys.exit(0)
        except OSError:
            pass
        os.setsid()

    # Single-instance guard: if another daemon already holds the lock, exit
    # quietly instead of starting a second one and fighting over the socket.
    global _LOCK_FD
    _LOCK_FD = _acquire_singleton_lock()
    if _LOCK_FD is None:
        sys.exit(0)

    Daemon().serve()


if __name__ == "__main__":
    main()
