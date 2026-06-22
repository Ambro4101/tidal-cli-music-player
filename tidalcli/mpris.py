"""Expose the daemon as an MPRIS2 media player on D-Bus.

This is what makes the keyboard's media keys (Play/Pause, Next, Previous, Stop)
control tidal-cli system-wide: on KDE/GNOME those keys are routed to the active
MPRIS player. It also surfaces now-playing + transport controls in Plasma's
media widget, the lock screen, and notifications, and makes ``playerctl`` work.

It runs its own asyncio loop in a background thread so it never interferes with
the daemon's socket loop, and player control calls are pushed to a worker thread
so a network-bound next/prev can't stall the bus. Entirely optional: if
dbus-next isn't installed or there's no session bus (e.g. headless), the daemon
logs a line and carries on without it — playback and the TUI keys are unaffected.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Optional

BUS_NAME = "org.mpris.MediaPlayer2.tidalcli"
OBJ_PATH = "/org/mpris/MediaPlayer2"
_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
_ID_OK = re.compile(r"[^A-Za-z0-9]")

# Prefer dbus-fast (maintained, supports current Python) and fall back to
# dbus-next; both expose the same API. _backend() returns the names we need,
# or None if neither is installed.
_BACKEND = None


def _backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND or None
    for name in ("dbus_fast", "dbus_next"):
        try:
            mod = __import__(name)
            aio = __import__(name + ".aio", fromlist=["MessageBus"])
            svc = __import__(name + ".service",
                             fromlist=["ServiceInterface", "method", "dbus_property"])
            _BACKEND = {
                "name": name,
                "MessageBus": aio.MessageBus,
                "ServiceInterface": svc.ServiceInterface,
                "method": svc.method,
                "dbus_property": svc.dbus_property,
                "Variant": mod.Variant,
                "PropertyAccess": mod.PropertyAccess,
            }
            return _BACKEND
        except Exception:
            continue
    _BACKEND = {}
    return None


def _track_path(track_id) -> str:
    tid = _ID_OK.sub("_", str(track_id or ""))
    return f"/org/mpris/tidalcli/track/{tid}" if tid else _NO_TRACK


def _playback_status(status: dict) -> str:
    return {"playing": "Playing", "paused": "Paused"}.get(status.get("state"), "Stopped")


def _metadata(status: dict):
    Variant = _backend()["Variant"]
    cur = status.get("current") or {}
    if not cur:
        return {"mpris:trackid": Variant("o", _NO_TRACK)}
    md = {
        "mpris:trackid": Variant("o", _track_path(cur.get("id"))),
        "mpris:length": Variant("x", int((cur.get("duration") or 0) * 1_000_000)),
        "xesam:title": Variant("s", cur.get("title") or ""),
        "xesam:artist": Variant("as", [cur["artist"]] if cur.get("artist") else []),
        "xesam:album": Variant("s", cur.get("album") or ""),
    }
    if cur.get("cover"):
        md["mpris:artUrl"] = Variant("s", cur["cover"])
    return md


def start(player, log=None) -> Optional["MprisService"]:
    """Start the MPRIS service in a background thread. Returns the service, or
    None if D-Bus / a dbus backend isn't available."""
    if _backend() is None:  # pragma: no cover - optional dependency
        if log:
            log.info("MPRIS disabled (install dbus-fast or dbus-next)")
        return None
    svc = MprisService(player, log)
    svc.start()
    return svc


def _build_interfaces(player, loop):
    b = _backend()
    ServiceInterface = b["ServiceInterface"]
    method = b["method"]
    dbus_property = b["dbus_property"]
    PropertyAccess = b["PropertyAccess"]

    class MediaPlayer2(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self):
            pass

        @method()
        def Quit(self):
            pass

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "tidal-cli"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "tidal-cli"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return []

    class Player(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._p = player
            self._loop = loop

        async def _do(self, fn, *args):
            # Run player control off the bus loop (next/prev may hit the network).
            try:
                await self._loop.run_in_executor(None, fn, *args)
            except Exception:
                pass
            self.changed()

        @method()
        async def PlayPause(self):
            await self._do(self._p.toggle)

        @method()
        async def Play(self):
            await self._do(self._p.play)

        @method()
        async def Pause(self):
            await self._do(self._p.pause)

        @method()
        async def Stop(self):
            await self._do(self._p.stop)

        @method()
        async def Next(self):
            await self._do(self._p.next)

        @method()
        async def Previous(self):
            await self._do(self._p.prev)

        @method()
        async def Seek(self, offset: "x"):
            pos = (self._p.status().get("position") or 0) + offset / 1_000_000
            await self._do(self._p.seek, max(0.0, pos))

        @method()
        async def SetPosition(self, track_id: "o", position: "x"):
            await self._do(self._p.seek, max(0.0, position / 1_000_000))

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return _playback_status(self._p.status())

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return _metadata(self._p.status())

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return int((self._p.status().get("position") or 0) * 1_000_000)

        @dbus_property()
        def Volume(self) -> "d":
            return (self._p.status().get("volume") or 0) / 100.0

        @Volume.setter
        def Volume(self, val: "d"):
            self._p.set_volume(int(max(0.0, min(1.0, val)) * 100))

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

        def changed(self):
            try:
                st = self._p.status()
                self.emit_properties_changed({
                    "PlaybackStatus": _playback_status(st),
                    "Metadata": _metadata(st),
                })
            except Exception:
                pass

    return MediaPlayer2(), Player()


class MprisService:
    def __init__(self, player, log=None):
        self._p = player
        self._log = log
        self._loop = None
        self._player_iface = None
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mpris", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._setup())
            self._loop.run_forever()
        except Exception as exc:  # pragma: no cover - depends on a live bus
            if self._log:
                self._log.info("MPRIS unavailable: %s", exc)

    async def _setup(self) -> None:
        MessageBus = _backend()["MessageBus"]

        bus = await MessageBus().connect()
        root, self._player_iface = _build_interfaces(self._p, self._loop)
        bus.export(OBJ_PATH, root)
        bus.export(OBJ_PATH, self._player_iface)
        await bus.request_name(BUS_NAME)
        if self._log:
            self._log.info("MPRIS active as %s", BUS_NAME)

    def notify(self) -> None:
        """Thread-safe: emit PropertiesChanged so desktops update now-playing."""
        loop, iface = self._loop, self._player_iface
        if loop and iface:
            try:
                loop.call_soon_threadsafe(iface.changed)
            except Exception:
                pass
