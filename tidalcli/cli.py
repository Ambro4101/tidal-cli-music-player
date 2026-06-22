"""Command-line surface (Typer). Maps subcommands to client/daemon calls.

    tidal login / logout
    tidal search <query> [--type track|album|artist|playlist]
    tidal play <id> [--type ...]      tidal enqueue <id> [--type ...]
    tidal pause | resume | toggle | stop | next | prev
    tidal seek <seconds>              tidal volume <0-100>
    tidal queue                       tidal now
    tidal fav [list]                  tidal fav add|remove <id>
"""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import auth, client, config
from .models import fmt_duration, fmt_quality

app = typer.Typer(add_completion=False, help="A terminal music player for TIDAL.")
fav_app = typer.Typer(help="Manage favorite tracks.")
app.add_typer(fav_app, name="fav")
console = Console()


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    """Run the full-screen TUI when invoked with no subcommand."""
    if ctx.invoked_subcommand is None:
        from .tui import run_tui
        run_tui()


@app.command()
def tui():
    """Launch the full-screen terminal player."""
    from .tui import run_tui
    run_tui()


# ---- auth -------------------------------------------------------------
@app.command()
def login():
    """Authorize this device with your TIDAL account."""
    settings = config.Settings.load()
    console.print("[bold]Starting TIDAL login...[/bold]")
    try:
        auth.interactive_login(settings, fn_print=lambda m: console.print(m))
    except Exception as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        raise typer.Exit(1)
    console.print("[green]Logged in.[/green]")
    client.try_reload()


@app.command()
def logout():
    """Remove saved credentials."""
    auth.logout()
    console.print("Logged out.")


# ---- search -----------------------------------------------------------
@app.command()
def search(
    query: str,
    type: str = typer.Option("track", "--type", "-t", help="track|album|artist|playlist"),
    limit: int = typer.Option(25, "--limit", "-n"),
):
    """Search the TIDAL catalog."""
    results = client.send("search", query=query, type=type, limit=limit)
    if not results:
        console.print("No results.")
        return
    _print_results(results, type)


# ---- playback ---------------------------------------------------------
@app.command()
def play(
    id: str = typer.Argument(..., help="track/album/artist/playlist id"),
    type: str = typer.Option("track", "--type", "-t"),
):
    """Replace the queue and start playing."""
    status = client.send("play", id=id, type=type)
    _print_now(status)


@app.command()
def enqueue(
    id: str = typer.Argument(...),
    type: str = typer.Option("track", "--type", "-t"),
):
    """Append to the current queue."""
    res = client.send("enqueue", id=id, type=type)
    console.print(f"Added {res.get('added', 0)} track(s).")


@app.command()
def pause():
    """Pause playback."""
    _print_now(client.send("pause"))


@app.command()
def resume():
    """Resume playback."""
    _print_now(client.send("resume"))


@app.command()
def toggle():
    """Toggle play/pause."""
    _print_now(client.send("toggle"))


@app.command()
def stop():
    """Stop playback."""
    client.send("stop")
    console.print("Stopped.")


@app.command()
def next():
    """Skip to the next track."""
    _print_now(client.send("next"))


@app.command()
def prev():
    """Go to the previous track."""
    _print_now(client.send("prev"))


@app.command()
def seek(seconds: float = typer.Argument(..., help="relative seconds, may be negative")):
    """Seek relative to the current position."""
    _print_now(client.send("seek", seconds=seconds))


@app.command()
def volume(level: int = typer.Argument(..., min=0, max=100)):
    """Set volume (0-100)."""
    res = client.send("volume", level=level)
    console.print(f"Volume: {res.get('volume')}")


@app.command()
def queue():
    """Show the current queue."""
    data = client.send("queue")
    tracks = data.get("queue", [])
    if not tracks:
        console.print("Queue is empty.")
        return
    idx = data.get("index", -1)
    table = Table(title="Queue")
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Artist")
    table.add_column("Time", justify="right")
    for i, t in enumerate(tracks):
        marker = "[green]>[/green]" if i == idx else str(i + 1)
        table.add_row(marker, t["title"], t["artist"], fmt_duration(t["duration"]))
    console.print(table)


@app.command()
def show(
    id: str = typer.Argument(..., help="album/artist/playlist/track id"),
    type: str = typer.Option("album", "--type", "-t", help="album|artist|playlist|track"),
):
    """List an item's contents (album/playlist -> tracks, artist -> albums)."""
    res = client.send("browse", id=id, type=type)
    items = res.get("items", []) if res else []
    if not items:
        console.print("Nothing to show.")
        return
    _print_results(items, res.get("kind", "track"))


@app.command()
def now():
    """Show what's playing."""
    _print_now(client.send("status"))


@app.command()
def art(size: int = typer.Option(32, "--size", "-s", help="cover width in terminal columns")):
    """Show the current track's album cover in the terminal."""
    info = client.send("cover")
    if not info or not info.get("url"):
        console.print("No cover art available for the current track.")
        return
    from .art import render_cover, CoverError
    try:
        pixels = render_cover(info["url"], px=size)
    except CoverError as exc:
        console.print(f"Couldn't show the cover: {exc}")
        return
    console.print(pixels)
    console.print(f"[bold]{info.get('title','')}[/bold] — {info.get('artist','')}")


@app.command()
def lyrics():
    """Show the current track's lyrics."""
    info = client.send("lyrics")
    lyr = info.get("lyrics") if info else None
    if not lyr:
        console.print("No lyrics available for the current track.")
        return
    from .lyrics import parse_lrc
    lines = parse_lrc(lyr.get("subtitles") or "")
    text = "\n".join(t for _, t in lines) if lines else (lyr.get("text") or "")
    console.print(text or "No lyrics available for the current track.")


@app.command()
def mixes():
    """List your TIDAL mixes (Daily Discovery, New Arrivals, My Mix 1-8, …)."""
    res = client.send("mixes")
    items = res.get("items", []) if isinstance(res, dict) else (res or [])
    if not items:
        console.print("No mixes found.")
        return
    from rich.table import Table
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Mix")
    table.add_column("About")
    for m in items:
        table.add_row(m["id"], m["title"], m.get("subtitle") or "")
    console.print(table)
    console.print("\nPlay one with:  [bold]tidal play <ID> -t mix[/bold]   "
                  "(or [bold]tidal enqueue <ID> -t mix[/bold], "
                  "[bold]tidal show <ID> -t mix[/bold])")


@app.command()
def quality():
    """Show the quality the current track is actually streaming in."""
    info = client.send("quality")
    console.print(f"[bold]{info.get('title','')}[/bold] — {info.get('artist','')}")
    console.print(f"Streaming:  {fmt_quality(info)}")
    req = info.get("requested")
    if req:
        console.print(f"Requested:  {req}")
    if info.get("quality") == "HI_RES_LOSSLESS" and info.get("mpd"):
        console.print("[dim](delivered as DASH; assembled for playback)[/dim]")


# ---- favorites --------------------------------------------------------
@fav_app.command("list")
def fav_list():
    """List favorite tracks."""
    results = client.send("fav_list")
    if not results:
        console.print("No favorites.")
        return
    _print_results(results, "track")


@fav_app.command("add")
def fav_add(id: str):
    """Add a track to favorites."""
    client.send("fav_add", id=id)
    console.print("Added to favorites.")


@fav_app.command("remove")
def fav_remove(id: str):
    """Remove a track from favorites."""
    client.send("fav_remove", id=id)
    console.print("Removed from favorites.")


# ---- rendering helpers ------------------------------------------------
def _print_results(results, kind):
    table = Table()
    table.add_column("ID", style="dim")
    if kind == "track":
        table.add_column("Title"); table.add_column("Artist")
        table.add_column("Album"); table.add_column("Time", justify="right")
        for r in results:
            table.add_row(r["id"], r["title"], r["artist"],
                          r.get("album") or "", fmt_duration(r["duration"]))
    elif kind == "album":
        table.add_column("Title"); table.add_column("Artist"); table.add_column("Type / Year")
        for r in results:
            yr = str(r.get("year") or "")
            cat = r.get("category") or ""
            info = " · ".join(x for x in (cat, yr) if x)
            table.add_row(r["id"], r["title"], r["artist"], info)
    elif kind == "artist":
        table.add_column("Name")
        for r in results:
            table.add_row(r["id"], r["name"])
    elif kind == "playlist":
        table.add_column("Title"); table.add_column("Creator"); table.add_column("Tracks")
        for r in results:
            table.add_row(r["id"], r["title"], r.get("creator") or "",
                          str(r.get("num_tracks") or ""))
    console.print(table)


def _print_now(status):
    cur = status.get("current") if status else None
    if not cur:
        console.print("Nothing playing.")
        return
    pos = fmt_duration(int(status.get("position") or 0))
    dur = fmt_duration(cur.get("duration"))
    state = status.get("state", "")
    console.print(
        f"[bold]{cur['title']}[/bold] — {cur['artist']}  "
        f"[dim]({pos}/{dur})[/dim]  [{state}]"
    )


if __name__ == "__main__":
    app()
