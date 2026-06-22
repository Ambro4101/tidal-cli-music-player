"""tidal-cli: a terminal music player for TIDAL (unofficial flow).

Architecture (mpd-style):
    cli.py     -> user-facing commands (Typer)
    client.py  -> thin client; talks to the daemon over a Unix socket
    daemon.py  -> long-lived process; owns the mpv player + queue + session
    player.py  -> mpv wrapper with a queue and auto-advance
    api.py     -> catalog/search/library wrappers over tidalapi
    auth.py    -> device-auth login + non-interactive token loading
    models.py  -> normalize tidalapi objects into JSON-safe dicts
    ipc.py     -> newline-delimited JSON protocol shared by client/daemon
    config.py  -> paths and settings
"""

__version__ = "0.1.0"
