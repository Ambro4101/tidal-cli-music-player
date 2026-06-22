"""Parse TIDAL lyrics.

TIDAL returns two forms: plain ``text`` and ``subtitles`` in LRC format
(``[mm:ss.xx] line``) which carries per-line timestamps. ``parse_lrc`` turns
the subtitles into a sorted ``[(seconds, text), ...]`` list so the TUI can
highlight and scroll to the current line in time with playback. Pure functions,
no I/O — easy to test and reuse.
"""
from __future__ import annotations

import re
from typing import List, Tuple

_TS = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")


def parse_lrc(subtitles: str) -> List[Tuple[float, str]]:
    """Parse LRC ``subtitles`` into sorted ``[(seconds, text), ...]``.

    Lines may carry more than one timestamp (repeated lines); each becomes its
    own entry. Lines without a timestamp are skipped. Returns ``[]`` if the
    input has no timestamps at all (i.e. it's plain, unsynced text).
    """
    out: List[Tuple[float, str]] = []
    for raw in (subtitles or "").splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        text = _TS.sub("", raw).strip()
        for m in stamps:
            minutes, seconds = int(m.group(1)), int(m.group(2))
            frac = m.group(3)
            t = minutes * 60 + seconds
            if frac:
                t += int(frac) / (10 ** len(frac))
            out.append((float(t), text))
    out.sort(key=lambda x: x[0])
    return out


def current_index(lines: List[Tuple[float, str]], pos: float) -> int:
    """Index of the active line at ``pos`` seconds, or -1 before the first."""
    idx = -1
    for i, (t, _) in enumerate(lines):
        if t <= pos:
            idx = i
        else:
            break
    return idx
