"""Thin client used by the CLI/TUI to talk to the daemon.

`send()` connects to the daemon's socket, autostarting the daemon if it isn't
running yet, then sends one command and returns the parsed response.

Build-awareness: the daemon reports the build it started with on ``ping``. If a
daemon is running but from an older build (e.g. you reinstalled), the client
shuts it down and starts a fresh one — so a reinstall doesn't leave a stale
daemon rejecting new commands. The freshness logic is wrapped so that, if
anything in it goes wrong, the client still falls back to simply ensuring some
daemon is up.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Optional

from . import config, ipc


class DaemonError(RuntimeError):
    pass


# This process's view of the installed build; constant for the process's life.
_CLIENT_BUILD = config.build_id()


def _connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(config.SOCKET_PATH))
    return s


def _ping() -> Optional[dict]:
    """Return the daemon's ping result dict, or None if unreachable."""
    if not config.SOCKET_PATH.exists():
        return None
    try:
        with _connect() as s:
            s.sendall(ipc.encode({"cmd": "ping"}))
            resp = ipc.read_message(s)
    except OSError:
        return None
    if isinstance(resp, dict) and resp.get("ok"):
        result = resp.get("result")
        return result if isinstance(result, dict) else {}
    return None


def _is_running() -> bool:
    return _ping() is not None


def _daemon_build(info: Optional[dict]) -> Optional[str]:
    return info.get("build") if isinstance(info, dict) else None


def _spawn_daemon() -> None:
    # Make the daemon import the *installed* package, never a ./tidalcli that
    # happens to be in the caller's working directory (e.g. the unpacked source
    # tree). PYTHONSAFEPATH stops Python prepending the CWD to sys.path (3.11+),
    # and running from a neutral directory is a belt-and-braces for older ones.
    # If client and daemon imported different copies, their build fingerprints
    # would differ and the client would replace the daemon forever.
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = "1"
    subprocess.Popen(
        [sys.executable, "-m", "tidalcli.daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(config.STATE_DIR),
        env=env,
    )


def _wait(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_daemon(info: Optional[dict], timeout: float = 6.0) -> bool:
    """Cleanly stop a running daemon; return True once it's gone."""
    try:
        with _connect() as s:
            s.sendall(ipc.encode({"cmd": "shutdown"}))
            ipc.read_message(s)
    except OSError:
        pass
    pid = info.get("pid") if isinstance(info, dict) else None

    def gone() -> bool:
        return _ping() is None and not _pid_alive(pid)

    if _wait(gone, timeout):
        return True
    # Last resort: signal it directly, or pkill the module by name.
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    elif shutil.which("pkill"):
        subprocess.run(["pkill", "-f", "tidalcli.daemon"], check=False)
    return _wait(gone, 3.0)


def _fresh() -> bool:
    return _daemon_build(_ping()) == _CLIENT_BUILD


# Guard against restart loops: if a freshly replaced daemon still doesn't match
# (e.g. a build-fingerprint mismatch we can't resolve), accept the running
# daemon for a cooldown window instead of replacing it again and again.
_RESTART_COOLDOWN = 30.0
_last_restart = 0.0


def _ensure_fresh_daemon(timeout: float) -> None:
    global _last_restart
    info = _ping()
    if info is not None and _daemon_build(info) == _CLIENT_BUILD:
        return  # a current-build daemon is already running
    if info is not None:
        # A daemon is up but reports a different build. Only replace it if we
        # haven't just done so — otherwise tolerate it rather than thrash.
        if time.time() - _last_restart < _RESTART_COOLDOWN:
            return
        _stop_daemon(info)
    _last_restart = time.time()
    _spawn_daemon()
    if _wait(_fresh, timeout):
        return
    # Couldn't confirm a matching build in time. Tolerate any responding daemon
    # rather than failing hard or looping; only error if nothing is up at all.
    if _ping() is None:
        raise DaemonError(
            "Daemon did not start in time. Check the log: " + str(config.LOG_FILE)
        )


def _ensure_any_daemon(timeout: float) -> None:
    """Fallback to the simple behavior: ensure *some* daemon is responding."""
    if _is_running():
        return
    _spawn_daemon()
    if not _wait(_is_running, timeout):
        raise DaemonError(
            "Daemon did not start in time. Check the log: " + str(config.LOG_FILE)
        )


def ensure_daemon(timeout: float = 10.0) -> None:
    try:
        _ensure_fresh_daemon(timeout)
    except DaemonError:
        raise
    except Exception:
        # Never let the freshness logic itself break startup.
        _ensure_any_daemon(timeout)


def send(cmd: str, **kwargs: Any) -> Any:
    """Send a command to the daemon and return its result, raising on error."""
    ensure_daemon()
    payload = {"cmd": cmd, **kwargs}
    with _connect() as s:
        s.sendall(ipc.encode(payload))
        response = ipc.read_message(s)
    if not response:
        raise DaemonError("No response from daemon.")
    if not response.get("ok"):
        raise DaemonError(response.get("error", "Unknown daemon error."))
    return response.get("result")


def try_reload() -> None:
    """Best-effort: tell a running daemon to reload its session. No-op if down."""
    if _is_running():
        try:
            send("reload")
        except DaemonError:
            pass
