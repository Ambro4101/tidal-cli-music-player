"""Authentication against TIDAL using tidalapi's OAuth device flow.

Two entry points:

    interactive_login()  -- runs the device flow (prints a link + code, blocks
                            until you authorize in a browser), then saves tokens.
                            Called by `tidal login` in the foreground.

    load_session()       -- non-interactive. Loads saved tokens, refreshes them
                            if needed, and returns a ready Session or None.
                            Called by the daemon on startup; it must never block
                            on stdin.

We persist the tokens ourselves (rather than using tidalapi's
login_session_file) so the daemon has full, non-blocking control over loading.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import tidalapi

from . import config


def _resolve_quality(name: str):
    """Map a settings string to a tidalapi.Quality member, with fallbacks."""
    Quality = tidalapi.Quality
    mapping = {
        "low_320k": getattr(Quality, "low_320k", None) or getattr(Quality, "high", None),
        "high_lossless": getattr(Quality, "high_lossless", None)
        or getattr(Quality, "lossless", None),
        "hi_res_lossless": getattr(Quality, "hi_res_lossless", None)
        or getattr(Quality, "master", None),
    }
    return mapping.get(name) or Quality.high_lossless


def _make_session(settings: config.Settings) -> tidalapi.Session:
    session = tidalapi.Session()
    quality = _resolve_quality(settings.quality)
    if quality is not None:
        session.audio_quality = quality
    return session


def _write_private(path, text: str) -> None:
    """Atomically write a secret file that is owner-only from the moment it
    exists (no world-readable window, unlike write_text + chmod)."""
    tmp = str(path) + ".tmp"
    # O_CREAT with mode 0600 -> the file is private the instant it appears.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, str(path))  # atomic; preserves the 0600 temp's perms


def _save_tokens(session: tidalapi.Session) -> None:
    expiry = session.expiry_time
    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": expiry.isoformat() if isinstance(expiry, datetime) else None,
    }
    _write_private(config.SESSION_FILE, json.dumps(data))


def _load_tokens() -> Optional[dict]:
    if not config.SESSION_FILE.exists():
        return None
    try:
        return json.loads(config.SESSION_FILE.read_text())
    except json.JSONDecodeError:
        return None


def interactive_login(settings: config.Settings, fn_print=print) -> tidalapi.Session:
    """Run the OAuth device flow in the foreground and persist the result."""
    session = _make_session(settings)
    if settings.use_pkce:
        # PKCE flow (required for hi_res_lossless): opens/print a URL, then you
        # paste the redirect URL back. login_pkce handles the prompt.
        session.login_pkce(fn_print=fn_print)
    else:
        # Device flow: prints "visit link.tidal.com/XXXX" and polls until done.
        session.login_oauth_simple(fn_print=fn_print)
    if not session.check_login():
        raise RuntimeError("Login did not complete successfully.")
    _save_tokens(session)
    return session


def load_session(settings: config.Settings) -> Optional[tidalapi.Session]:
    """Load + refresh a saved session without any user interaction."""
    tokens = _load_tokens()
    if not tokens:
        return None

    session = _make_session(settings)
    expiry = tokens.get("expiry_time")
    expiry_dt = None
    if expiry:
        try:
            expiry_dt = datetime.fromisoformat(expiry)
        except ValueError:
            expiry_dt = None

    try:
        session.load_oauth_session(
            tokens["token_type"],
            tokens["access_token"],
            tokens["refresh_token"],
            expiry_dt,
        )
    except (KeyError, Exception):
        return None

    if session.check_login():
        return session

    # Access token expired -> try a refresh using the refresh token.
    refresh = tokens.get("refresh_token")
    if refresh:
        try:
            if session.token_refresh(refresh) and session.check_login():
                _save_tokens(session)
                return session
        except Exception:
            pass
    return None


def logout() -> None:
    if config.SESSION_FILE.exists():
        config.SESSION_FILE.unlink()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
