# tidal-cli-music-player

A terminal music player for TIDAL — full-screen TUI **and** scriptable CLI —
built around an mpd-style **daemon/client** split so playback persists across
commands and terminals. Streams real lossless/hi-res audio from your own paid
account, shows album art and time-synced lyrics in the terminal, and exposes
itself to your desktop's media keys.

> **Personal use.** This uses TIDAL's unofficial app API (via `tidalapi`) as
> your own logged-in account, which is how it can play full tracks rather than
> 30-second previews. It's for personal use with an active subscription and
> lives in a grey area of TIDAL's ToS. There are no API keys to obtain.

```
┌──────────┐   Unix socket    ┌───────────────────────────────────┐
│  CLI     │ ───────────────► │              daemon               │
│  TUI     │   JSON lines     │  session ─ api ─ player (libmpv)  │  ──► D-Bus
│ (client) │ ◄─────────────── │       queue · gapless · MPRIS     │     (MPRIS)
└──────────┘                  └───────────────────────────────────┘
   autostarts + keeps the daemon up to date
```

The client autostarts the daemon on first use and owns nothing itself; the
daemon holds the session, the mpv player, and the queue. That's why `tidal next`
from any terminal — or your keyboard's Play/Pause key — works while music keeps
playing.

## Features

- **Gapless playback** of lossless FLAC / hi-res via libmpv, with a prefetched
  queue that auto-advances.
- **Two front-ends**: a full-screen **TUI** (Textual) and a scriptable **CLI**
  (Typer) — both thin clients over the same daemon.
- **Search & browse**: tracks, albums, artists, playlists, with drill-down
  (artist → albums → tracks) and back-navigation.
- **Your library**: favorites, and **My Mixes** (Daily Discovery, New Arrivals,
  My Mix 1–8, …).
- **Album art in the terminal** — true-resolution via Kitty/Sixel graphics where
  supported, Unicode-block fallback everywhere else.
- **Time-synced lyrics** that highlight and scroll with playback (plain lyrics
  where TIDAL has no synced version).
- **Quality reporting** — see what's actually being delivered vs. requested.
- **Media keys** — Play/Pause, Next, Previous, Stop work system-wide via MPRIS,
  and the track shows up in Plasma's media widget / `playerctl`.
- **Survives restarts** — the queue and play position are persisted, and the
  client transparently keeps the daemon on the current build.

## Requirements

- **Python 3.9+**
- **mpv / libmpv** — the actual audio engine. On Arch/CachyOS: `sudo pacman -S mpv`
  (this provides `libmpv`).
- An **active TIDAL subscription** (HiFi / HiFi Plus for lossless & hi-res).
- Optional, for media keys & desktop now-playing: a running **session D-Bus**
  (you have this under any normal desktop login; not present over bare SSH).
- Optional, for full-resolution album art: a terminal that supports **Sixel or
  the Kitty graphics protocol** (Konsole, Kitty, Ghostty, WezTerm, foot, …).

Python dependencies (installed automatically): `tidalapi`, `python-mpv`,
`dbus-fast`, `typer`, `rich`, `textual`, `textual-image`, `rich-pixels`,
`pillow`, `platformdirs`.

## Install

With [pipx](https://pipx.pypa.io) (recommended — keeps it isolated):

```fish
cd tidal-cli
pipx install --force .
```

Or into a virtualenv with `pip install .`. The install provides the `tidal`
command.

## First run

Log in once (OAuth — no passwords stored):

```fish
tidal login
```

This prints a `link.tidal.com/XXXX` code; open it in a browser and authorize.
Tokens are saved to your data dir (`chmod 600`) and auto-refreshed thereafter.
`tidal logout` removes them.

### Getting real lossless / hi-res

The simple device-flow login is capped at AAC 320k. For **lossless and hi-res**
you need the **PKCE** login flow. Enable it once in your config and re-login:

```fish
# ~/.config/tidal-cli/config.json
{
  "use_pkce": true,
  "quality": "high_lossless"
}
```

```fish
tidal logout
tidal login        # PKCE flow: authorize, then paste the redirected URL back
```

`quality` can be `low_96k` (LOW), `low_320k` (HIGH/AAC), `high_lossless`
(LOSSLESS/FLAC), or `hi_res_lossless` (HI-RES; delivered as DASH and assembled
for playback). Check what you're actually getting with `tidal quality`.

## Usage — TUI

```fish
tidal              # launch the full-screen player
```

Layout: search box on top, results on the left, **queue (top-right)** stacked
over **lyrics (bottom-right)**, now-playing bar with cover art along the bottom.

| Key | Action |
|-----|--------|
| `/` | focus search (type a query, Enter to run) |
| `t` | cycle search type: track / album / artist / playlist |
| `Enter` | play a track from here, or drill into an album/artist/playlist/mix |
| `a` | add the selected item to the queue |
| `m` | load **My Mixes** |
| `Esc` | back out of a drilled-in view |
| `Space` | play / pause |
| `n` / `p` | next / previous |
| `+` / `-` | volume up / down |
| `i` | show the actual streaming quality |
| `q` | quit |

## Usage — CLI

Every action is also a command, so you can script playback or bind keys to it.

```fish
# search & play
tidal search "bonobo" --type artist     # -t track|album|artist|playlist
tidal play <id> --type album            # play a track/album/artist/playlist/mix
tidal enqueue <id> -t playlist          # add to the queue
tidal show <id> -t album                # list an item's contents

# transport
tidal toggle           # play/pause     tidal pause / resume / stop
tidal next             # skip           tidal prev
tidal seek 30          # relative seconds (negative to rewind)
tidal volume 70        # 0–100

# what's going on
tidal now              # current track + position
tidal queue            # the queue
tidal quality          # actual vs requested stream quality
tidal art -s 40        # album cover in the terminal (width in columns)
tidal lyrics           # current track's lyrics

# library
tidal mixes                       # your mixes, with IDs
tidal play <mix-id> -t mix        # play a whole mix
tidal fav list / add <id> / remove <id>
```

## Album art

Art renders automatically in the TUI and on demand via `tidal art`. Quality
depends on your terminal:

- **Kitty graphics or Sixel** → true-resolution image. Konsole defaults to Sixel
  automatically (it's detected via `KONSOLE_VERSION`).
- **Otherwise** → Unicode half-blocks (works anywhere with truecolor).

Force a specific renderer with the `TIDAL_COVER_PROTOCOL` environment variable:

```fish
set -Ux TIDAL_COVER_PROTOCOL sixel    # or: tgp, halfcell, unicode, auto
```

Sixel inside the TUI can occasionally flicker (a known terminal/TUI quirk); if it
bothers you, `halfcell` is glitch-free at lower resolution.

## Media keys (MPRIS)

When a session D-Bus bus is present, the daemon registers as
`org.mpris.MediaPlayer2.tidalcli`. That makes your keyboard's **Play/Pause,
Next, Previous, Stop** keys control tidal-cli, shows the current track (with
cover) in Plasma's media widget and on the lock screen, and makes `playerctl`
work:

```fish
playerctl -p tidalcli play-pause
playerctl -p tidalcli next
playerctl -l                     # should list "tidalcli" while playing
```

## Configuration

Settings live in `~/.config/tidal-cli/config.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `use_pkce` | `false` | use the PKCE login flow (required for lossless/hi-res) |
| `quality` | `high_lossless` | `low_96k` · `low_320k` · `high_lossless` · `hi_res_lossless` |
| `initial_volume` | `80` | volume the daemon starts at |

Environment variables:

| Variable | Effect |
|----------|--------|
| `TIDAL_COVER_PROTOCOL` | force cover renderer: `auto` (default) · `sixel` · `tgp` · `halfcell` · `unicode` |
| `TIDAL_CLI_SOCKET` | override the daemon's Unix socket path |

State lives under your platform dirs (XDG on Linux): tokens in the data dir,
the socket / queue / logs in the state dir, config in the config dir.

## Updating

Reinstall the new build, then just run `tidal`:

```fish
cd tidal-cli
pipx install --force .
tidal
```

The client fingerprints the installed code and **replaces a running daemon
automatically when it's from an older build** — so you normally don't need to
kill anything by hand. (Replacing the daemon briefly stops playback while it
restarts; your queue and position are restored.)

## Troubleshooting

- **"Daemon did not start"** — check `~/.local/state/tidal-cli/daemon.log`. The
  most common cause on a first run is `libmpv` not being installed.
- **Getting AAC 320 instead of lossless** — enable `use_pkce` and re-login (see
  *Getting real lossless / hi-res*).
- **Cover art looks blocky** — your terminal doesn't expose Sixel/Kitty
  graphics, or `TIDAL_COVER_PROTOCOL` is forcing blocks. Konsole users: ensure
  Sixel is enabled in the profile.
- **Media keys do nothing** — confirm `playerctl -l` lists `tidalcli` while
  playing; if not, check the daemon log for an `MPRIS active` line. Needs a
  session D-Bus bus (i.e. a desktop login, not bare SSH).
- **Force a fresh daemon** — `pkill -f tidalcli.daemon`; it'll autostart again.

## Project layout

```
tidalcli/
├── cli.py      Typer CLI (thin client)
├── tui.py      Textual TUI (thin client)
├── client.py   socket client; autostarts + version-checks the daemon
├── daemon.py   long-lived server: session, player, queue, command dispatch
├── player.py   gapless mpv wrapper + queue/auto-advance
├── api.py      tidalapi wrappers: search, browse, favorites, mixes, lyrics, art, streams
├── auth.py     device + PKCE login, token storage/refresh
├── models.py   JSON-safe dicts for tracks/albums/artists/playlists/mixes
├── lyrics.py   LRC parsing + active-line lookup
├── art.py      cover rendering (rich-pixels) + image loading
├── mpris.py    MPRIS2 D-Bus bridge (media keys / playerctl / desktop now-playing)
├── ipc.py      newline-delimited JSON framing
└── config.py   paths, settings, build fingerprint
```

