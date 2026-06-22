"""Full-screen terminal player (Textual).

This is *just another client of the daemon* — it issues the same
``client.send(...)`` commands the CLI does and never touches mpv or tidalapi
directly. Launched by bare ``tidal`` (or ``tidal tui``).

Async boundary
--------------
``client.send`` is blocking socket I/O; Textual's UI runs on an async event
loop. So every daemon call runs in a Textual *thread worker* and pushes results
back to the UI with ``call_from_thread``. The interface never freezes while a
search or play command is in flight.

Layout
------
    Header
    Search input
    ┌ Results ────────────┬ Queue ──────┐
    │ navigable table     │ live queue  │
    └─────────────────────┴─────────────┘
    Now playing: title — artist  [progress]  pos/dur  state  vol
    Footer (keybindings)
"""
from __future__ import annotations

import os
from functools import partial
from typing import List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
)

# Cover art renders as a true image (Sixel / Kitty graphics) when textual-image
# is present and the terminal supports it; otherwise the TUI still runs without
# art. Auto-detection queries the terminal and must happen before Textual starts
# its IO threads, so importing textual_image.renderable here (at import time, in
# the real terminal) primes it. Set TIDAL_COVER_PROTOCOL to force a renderer:
#   sixel  -> Konsole, xterm, foot, WezTerm, recent Windows Terminal
#   tgp    -> Kitty, Ghostty (Kitty graphics protocol)
#   halfcell / unicode -> block fallbacks
#   auto   -> pick the best automatically (default)
_COVER_PROTOCOL = os.environ.get("TIDAL_COVER_PROTOCOL", "").strip().lower()
if not _COVER_PROTOCOL:
    # Konsole advertises partial Kitty-protocol support that isn't actually
    # usable, so auto-detect can land on a broken path; its Sixel works well.
    # Default Konsole to sixel (it sets KONSOLE_VERSION); everyone else: auto.
    _COVER_PROTOCOL = "sixel" if os.environ.get("KONSOLE_VERSION") else "auto"
try:
    import textual_image.renderable  # noqa: F401  (primes terminal detection)
    from textual_image.widget import (
        AutoImage, SixelImage, TGPImage, HalfcellImage, UnicodeImage,
    )
    _COVER_WIDGETS = {
        "auto": AutoImage, "sixel": SixelImage, "tgp": TGPImage,
        "halfcell": HalfcellImage, "unicode": UnicodeImage,
    }
    CoverWidget = _COVER_WIDGETS.get(_COVER_PROTOCOL, AutoImage)
    _COVER_IS_IMAGE = True
except Exception:  # pragma: no cover - optional dependency
    from textual.widgets import Static as CoverWidget
    _COVER_IS_IMAGE = False

from . import client
from .models import fmt_duration
from .lyrics import parse_lrc, current_index
from rich.text import Text

SEARCH_TYPES = ["track", "album", "artist", "playlist"]


class TidalTUI(App):
    CSS = """
    #search { height: 3; margin: 0 1; }
    #panes { height: 1fr; }
    #results { width: 2fr; }
    #rightcol { width: 1fr; }
    #queue { height: 1fr; }
    #lyrics { height: 1fr; border-top: solid $accent; padding: 0 1; }
    #lyricsbody { width: 1fr; }
    #nowbar { height: 9; border-top: solid $accent; margin-bottom: 1; }
    #cover { width: 18; height: 9; padding: 0 1; }
    #nowinfo { width: 1fr; padding: 1 1; }
    #nowtext { height: 1; }
    #progress { height: 1; }
    """

    BINDINGS = [
        Binding("space", "toggle", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("p", "prev", "Prev"),
        Binding("a", "enqueue", "Add"),
        Binding("t", "cycle_type", "Type"),
        Binding("m", "mixes", "Mixes"),
        Binding("i", "quality", "Quality"),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "back", "Back"),
        Binding("plus", "vol_up", "Vol+"),
        Binding("minus", "vol_down", "Vol-"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.search_type = "track"      # what the NEXT search will query
        self.results_kind = "track"     # kind of items CURRENTLY shown
        self.results_label = ""         # breadcrumb for the current listing
        self.view_stack: List[dict] = []  # for drill-in / back navigation
        self.results: List[dict] = []
        self.queue_tracks: List[dict] = []
        self.queue_index = -1
        self.volume = 80
        self._cover_url = None
        self._last_now = None   # last now-text string actually rendered
        self._last_prog = None  # last (total, progress) actually rendered
        self._lyrics_track = None     # track id lyrics are currently loaded for
        self._lyrics_lines = []       # [(seconds, text)] when synced, else []
        self._lyrics_plain = ""       # plain text (synced or not)
        self._lyric_idx = -1          # active synced line index

    # ------------------------------------------------------------------ #
    # Composition / mount                                                 #
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search…  (press / to focus, Enter to run)", id="search")
        with Horizontal(id="panes"):
            yield DataTable(id="results")
            with Vertical(id="rightcol"):
                yield DataTable(id="queue")
                with VerticalScroll(id="lyrics"):
                    yield Static("", id="lyricsbody")
        with Horizontal(id="nowbar"):
            yield CoverWidget(id="cover")
            with Vertical(id="nowinfo"):
                yield Label("Nothing playing", id="nowtext")
                yield ProgressBar(id="progress", total=100, show_eta=False, show_percentage=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "tidal-cli"
        self.sub_title = f"search: {self.search_type}"

        results = self.query_one("#results", DataTable)
        results.cursor_type = "row"
        results.add_columns("Title", "Artist", "Album / Info", "Time")

        queue = self.query_one("#queue", DataTable)
        queue.cursor_type = "row"
        queue.add_columns("#", "Title", "Artist", "Time")

        # Live now-playing refresh, plus an initial state + queue pull.
        self.set_interval(0.5, self.poll)
        self.poll()
        self._refresh_queue()

    # ------------------------------------------------------------------ #
    # Daemon plumbing (all blocking calls run in worker threads)          #
    # ------------------------------------------------------------------ #
    def _send(self, cmd: str, **kw):
        """Blocking call, meant to run inside a thread worker."""
        try:
            return client.send(cmd, **kw)
        except client.DaemonError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", title="TIDAL")
        except Exception as exc:  # pragma: no cover - defensive
            self.call_from_thread(self.notify, str(exc), severity="error")
        return None

    def _run(self, cmd: str, **kw) -> None:
        """Fire a command, then refresh status + queue."""
        self.run_worker(partial(self._command_worker, cmd, kw), thread=True)

    def _command_worker(self, cmd: str, kw: dict) -> None:
        self._send(cmd, **kw)
        status = self._send("status")
        queue = self._send("queue")
        if status is not None:
            self.call_from_thread(self._apply_status, status)
        if queue is not None:
            self.call_from_thread(self._apply_queue, queue)

    def poll(self) -> None:
        self.run_worker(self._poll_worker, thread=True, exclusive=True, group="poll")

    def _poll_worker(self) -> None:
        status = self._send("status")
        if status is not None:
            self.call_from_thread(self._apply_status, status)

    def _refresh_queue(self) -> None:
        self.run_worker(self._queue_worker, thread=True, exclusive=True, group="queue")

    def _queue_worker(self) -> None:
        data = self._send("queue")
        if data is not None:
            self.call_from_thread(self._apply_queue, data)

    # ------------------------------------------------------------------ #
    # UI updates (always on the UI thread via call_from_thread)           #
    # ------------------------------------------------------------------ #
    def _apply_status(self, status: dict) -> None:
        nowtext = self.query_one("#nowtext", Label)
        progress = self.query_one("#progress", ProgressBar)

        # Compute the desired now-text and progress, then only repaint when they
        # actually change. Repainting next to a Sixel cover re-injects the image
        # (which can smear adjacent lines), so avoiding no-op redraws — and all
        # redraws while paused — keeps the picture stable.
        if status.get("logged_in") is False:
            now = "Not logged in — run `tidal login` in another terminal."
            prog = (100, 0)
            cur = None
        else:
            cur = status.get("current")
            if not cur:
                now = "Nothing playing"
                prog = (100, 0)
            else:
                pos = int(status.get("position") or 0)
                dur = int(cur.get("duration") or status.get("duration") or 0)
                state = status.get("state", "")
                self.volume = int(status.get("volume") or self.volume)
                now = (
                    f"♪ {cur['title']} — {cur['artist']}    "
                    f"{fmt_duration(pos)} / {fmt_duration(dur)}    "
                    f"[{state}]   vol {self.volume}"
                )
                prog = (max(dur, 1), pos)

        if now != self._last_now:
            nowtext.update(now)
            self._last_now = now
        if prog != self._last_prog:
            progress.update(total=prog[0], progress=prog[1])
            self._last_prog = prog

        if status.get("logged_in") is False:
            return

        # Keep the queue highlight in sync when the track changes.
        idx = status.get("index", -1)
        if idx != self.queue_index:
            self.queue_index = idx
            self._refresh_queue()

        # Update the cover when the track (cover URL) changes.
        new_cover = cur.get("cover") if cur else None
        if new_cover != self._cover_url:
            self._cover_url = new_cover
            self._refresh_cover(new_cover)

        # Lyrics: (re)load on track change; follow the active line when synced.
        tid = cur.get("id") if cur else None
        if tid != self._lyrics_track:
            self._lyrics_track = tid
            self._refresh_lyrics(tid)
        elif self._lyrics_lines and cur:
            new_idx = current_index(self._lyrics_lines, int(status.get("position") or 0))
            if new_idx != self._lyric_idx:
                self._lyric_idx = new_idx
                self._render_lyrics()

    def _refresh_cover(self, url) -> None:
        if not url:
            self._set_cover(None)
            return
        self.run_worker(partial(self._cover_worker, url), thread=True,
                        exclusive=True, group="cover")

    def _cover_worker(self, url: str) -> None:
        from .art import load_image, CoverError
        try:
            img = load_image(url)
        except CoverError:
            img = None  # stay quiet in the TUI; the `art` command reports why
        self.call_from_thread(self._set_cover, img)

    def _set_cover(self, img) -> None:
        # AutoImage renders true pixels via Kitty/Sixel when the terminal
        # supports it, and falls back to Unicode blocks otherwise.
        try:
            widget = self.query_one("#cover")
            if _COVER_IS_IMAGE:
                widget.image = img
        except Exception:
            pass

    # ---- lyrics ------------------------------------------------------- #
    def _refresh_lyrics(self, tid) -> None:
        self._lyrics_lines = []
        self._lyrics_plain = ""
        self._lyric_idx = -1
        try:
            self.query_one("#lyricsbody", Static).update(
                Text("Loading lyrics…" if tid else "Nothing playing.",
                     style="dim italic"))
        except Exception:
            pass
        if not tid:
            return
        self.run_worker(partial(self._lyrics_worker, tid), thread=True,
                        exclusive=True, group="lyrics")

    def _lyrics_worker(self, tid) -> None:
        note = None
        try:
            data = client.send("lyrics", id=tid)
            lyr = data.get("lyrics") if data else None
        except Exception as exc:
            lyr = None
            if "Unknown command" in str(exc):
                note = "Daemon out of date — quit, then: pkill -f tidalcli.daemon"
        self.call_from_thread(self._set_lyrics, tid, lyr, note)

    def _set_lyrics(self, tid, lyr, note=None) -> None:
        if tid != self._lyrics_track:
            return  # the track changed again while we were fetching
        if note:
            self._lyrics_lines = []
            self._lyrics_plain = ""
            self._lyric_idx = -1
            try:
                self.query_one("#lyricsbody", Static).update(Text(note, style="italic yellow"))
            except Exception:
                pass
            return
        if not lyr:
            self._lyrics_lines = []
            self._lyrics_plain = ""
        else:
            lines = parse_lrc(lyr.get("subtitles") or "")
            self._lyrics_lines = lines
            self._lyrics_plain = ("\n".join(t for _, t in lines) if lines
                                  else (lyr.get("text") or ""))
        self._lyric_idx = -1
        self._render_lyrics()

    def _render_lyrics(self) -> None:
        try:
            body = self.query_one("#lyricsbody", Static)
            scroller = self.query_one("#lyrics", VerticalScroll)
        except Exception:
            return
        if self._lyrics_lines:  # time-synced: highlight + follow the active line
            active = self._lyric_idx
            txt = Text()
            for i, (_, line) in enumerate(self._lyrics_lines):
                if i:
                    txt.append("\n")
                disp = line or "♪"
                if i == active:
                    txt.append(disp, style="bold reverse")
                elif i < active:
                    txt.append(disp, style="dim")
                else:
                    txt.append(disp)
            body.update(txt)
            if active >= 0:
                scroller.scroll_to(y=max(0, active - 3), animate=False)
        elif self._lyrics_plain:  # unsynced plain text
            body.update(Text(self._lyrics_plain))
        else:
            body.update(Text("No lyrics available.", style="dim italic"))

    def _apply_results(self, results: List[dict], query: str) -> None:
        # A fresh search is a new root: clear the drill-in breadcrumb.
        self.view_stack = []
        label = f"search: {self.search_type} · {query}"
        self._show_listing(results or [], self.search_type, label)

    def action_mixes(self) -> None:
        """Load the user's TIDAL mixes as a new root listing."""
        self.run_worker(self._mixes_worker, thread=True)

    def _mixes_worker(self) -> None:
        res = self._send("mixes")
        if res is not None:
            self.call_from_thread(self._apply_mixes, res.get("items", []))

    def _apply_mixes(self, items: List[dict]) -> None:
        self.view_stack = []
        self._show_listing(items, "mix", "My Mixes")
        if not items:
            self.sub_title = "My Mixes — none found"

    def _show_listing(self, items: List[dict], kind: str, label: str, cursor: int = 0) -> None:
        """Render any listing (search results or a drilled-in tracklist)."""
        self.results = items or []
        self.results_kind = kind
        self.results_label = label
        self.sub_title = label

        table = self.query_one("#results", DataTable)
        table.clear()
        for r in self.results:
            if kind == "track":
                table.add_row(r["title"], r["artist"], r.get("album") or "",
                              fmt_duration(r["duration"]))
            elif kind == "album":
                info = " · ".join(
                    x for x in (r.get("category") or "", str(r.get("year") or "")) if x
                )
                table.add_row(r["title"], r["artist"], info, "")
            elif kind == "artist":
                table.add_row(r["name"], "", "", "")
            elif kind == "playlist":
                table.add_row(r["title"], r.get("creator") or "",
                              f"{r.get('num_tracks') or ''} tracks", "")
            elif kind == "mix":
                table.add_row(r["title"], r.get("subtitle") or "", "", "")
        if self.results:
            table.focus()
            try:
                table.move_cursor(row=min(cursor, len(self.results) - 1))
            except Exception:
                pass

    def _push_view(self) -> None:
        """Save the current listing so `back` can restore it (with cursor)."""
        table = self.query_one("#results", DataTable)
        self.view_stack.append({
            "items": self.results,
            "kind": self.results_kind,
            "label": self.results_label,
            "cursor": table.cursor_row or 0,
        })

    def _enter_listing(self, items: List[dict], kind: str, label: str) -> None:
        """Push the current view, then descend into a new one."""
        self._push_view()
        self._show_listing(items, kind, label)

    def _apply_queue(self, data: dict) -> None:
        self.queue_tracks = data.get("queue", [])
        self.queue_index = data.get("index", self.queue_index)
        table = self.query_one("#queue", DataTable)
        table.clear()
        for i, t in enumerate(self.queue_tracks):
            marker = "▶" if i == self.queue_index else str(i + 1)
            table.add_row(marker, t["title"], t["artist"], fmt_duration(t["duration"]))

    # ------------------------------------------------------------------ #
    # Events                                                              #
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            query = event.value.strip()
            if query:
                self.run_worker(partial(self._search_worker, query), thread=True)

    def _search_worker(self, query: str) -> None:
        results = self._send("search", query=query, type=self.search_type, limit=50)
        if results is not None:
            self.call_from_thread(self._apply_results, results, query)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if event.data_table.id == "results":
            self._activate_result(row)
        elif event.data_table.id == "queue":
            if 0 <= row < len(self.queue_tracks):
                self._run("play", tracks=self.queue_tracks[row:])

    # ------------------------------------------------------------------ #
    # Actions (bindings)                                                  #
    # ------------------------------------------------------------------ #
    def _activate_result(self, row: Optional[int]) -> None:
        """Enter on a result: tracks play from here; containers drill in."""
        if row is None or not (0 <= row < len(self.results)):
            return
        if self.results_kind == "track":
            self._run("play", tracks=self.results[row:])
        else:
            self._drill_in(row)

    def _drill_in(self, row: int) -> None:
        item = self.results[row]
        kind = self.results_kind
        label = self._container_label(item, kind)
        self.run_worker(partial(self._browse_worker, item, kind, label), thread=True)

    def _browse_worker(self, item: dict, kind: str, label: str) -> None:
        res = self._send("browse", id=item["id"], type=kind)
        if res is not None:
            # The daemon tells us the child kind (artist -> album, else track),
            # so the next listing renders and behaves correctly.
            self.call_from_thread(
                self._enter_listing, res.get("items", []), res.get("kind", "track"), label
            )

    @staticmethod
    def _container_label(item: dict, kind: str) -> str:
        if kind == "album":
            return f"album: {item.get('title','')} — {item.get('artist','')}"
        if kind == "artist":
            return f"artist: {item.get('name','')}"
        if kind == "playlist":
            return f"playlist: {item.get('title','')}"
        if kind == "mix":
            return f"mix: {item.get('title','')}"
        return kind

    def action_back(self) -> None:
        """Pop a drilled-in view, or fall back to focusing the results pane."""
        if self.view_stack:
            v = self.view_stack.pop()
            self._show_listing(v["items"], v["kind"], v["label"], cursor=v["cursor"])
        else:
            self.action_focus_results()

    def action_enqueue(self) -> None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self.results)):
            return
        if self.results_kind == "track":
            self._run("enqueue", tracks=[self.results[row]])
        else:
            # Queue the whole container (album/artist/playlist) at once.
            self._run("enqueue", id=self.results[row]["id"], type=self.results_kind)

    def action_toggle(self) -> None:
        self._run("toggle")

    def action_next(self) -> None:
        self._run("next")

    def action_prev(self) -> None:
        self._run("prev")

    def action_vol_up(self) -> None:
        self.volume = min(100, self.volume + 5)
        self._run("volume", level=self.volume)

    def action_vol_down(self) -> None:
        self.volume = max(0, self.volume - 5)
        self._run("volume", level=self.volume)

    def action_quality(self) -> None:
        self.run_worker(self._quality_worker, thread=True)

    def _quality_worker(self) -> None:
        info = self._send("quality")
        if info:
            from .models import fmt_quality
            msg = f"{fmt_quality(info)}   (requested: {info.get('requested')})"
            self.call_from_thread(self.notify, msg, title="Now streaming")

    def action_cycle_type(self) -> None:
        i = SEARCH_TYPES.index(self.search_type)
        self.search_type = SEARCH_TYPES[(i + 1) % len(SEARCH_TYPES)]
        self.sub_title = f"search: {self.search_type}"

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_results(self) -> None:
        self.query_one("#results", DataTable).focus()


def run_tui() -> None:
    TidalTUI().run()
