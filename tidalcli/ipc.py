"""Tiny request/response protocol over a Unix domain socket.

Each message is a single line of JSON terminated by '\n'.

    request : {"cmd": "play", "id": "12345", ...}
    response: {"ok": true, "result": ...}  |  {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import socket
from typing import Any, Optional


def encode(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def read_message(conn: socket.socket) -> Optional[dict]:
    """Read one newline-terminated JSON message from a socket."""
    buf = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    if not buf:
        return None
    line = buf.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def ok(result: Any = None) -> dict:
    return {"ok": True, "result": result}


def err(message: str) -> dict:
    return {"ok": False, "error": message}
