"""Filesystem paths and user settings.

Everything that needs a stable on-disk location lives here so the rest of the
code never hard-codes a path. We use platformdirs so it behaves correctly on
Linux (XDG), macOS, and Windows.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_state_dir

APP_NAME = "tidal-cli"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))
STATE_DIR = Path(user_state_dir(APP_NAME))

for _d in (CONFIG_DIR, DATA_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# DATA_DIR holds the OAuth tokens; STATE_DIR holds the control socket and the
# saved queue. Make them owner-only so nothing depends solely on per-file perms.
for _d in (DATA_DIR, STATE_DIR):
    try:
        _d.chmod(0o700)
    except OSError:
        pass

# Where we persist OAuth tokens (chmod 600). Not the catalog cache.
SESSION_FILE = DATA_DIR / "session.json"
# Unix domain socket the daemon listens on.
SOCKET_PATH = Path(os.environ.get("TIDAL_CLI_SOCKET", STATE_DIR / "daemon.sock"))
# Persisted queue/playback state so it survives a daemon restart.
QUEUE_FILE = STATE_DIR / "queue.json"
# Exclusive lock guaranteeing only one daemon runs at a time.
LOCK_FILE = STATE_DIR / "daemon.lock"
# Daemon log file.
LOG_FILE = STATE_DIR / "daemon.log"
# Daemon process id, written at startup so the client can replace a stale daemon.
PID_FILE = STATE_DIR / "daemon.pid"
# User settings.
CONFIG_FILE = CONFIG_DIR / "config.json"


def build_id() -> str:
    """A fingerprint of the installed package code.

    It's the newest mtime across the package's .py files, which changes whenever
    the package is reinstalled (the files are rewritten). The daemon captures
    this at startup and reports it on ``ping``; the client compares it against
    the current value so it can tell when a running daemon is from an older
    build and replace it — no more "Unknown command" from a stale daemon.
    """
    import glob

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    latest = 0.0
    for path in glob.glob(os.path.join(pkg_dir, "*.py")):
        try:
            latest = max(latest, os.path.getmtime(path))
        except OSError:
            pass
    return str(int(latest))


# TIDAL quality tiers, mapped to tidalapi.Quality enum *names* (resolved lazily
# in auth.py so importing config never requires tidalapi to be installed).
VALID_QUALITIES = (
    "low_320k",        # AAC 320
    "high_lossless",   # FLAC 16/44.1 (CD)
    "hi_res_lossless", # FLAC up to 24/192 (needs PKCE login)
)


@dataclass
class Settings:
    quality: str = "high_lossless"
    # PKCE login is required for hi_res_lossless. For 320k/lossless the simple
    # device flow is enough.
    use_pkce: bool = False
    initial_volume: int = 80

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                known = {k: v for k, v in data.items() if k in cls.__annotations__}
                return cls(**known)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))
