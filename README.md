# unli-photos

Auto-sync media to Google Photos with Google Pixel unlimited-storage spoofing,
powered by [`gotohp-cli`](https://github.com/xob0t/gotohp). A clean, professional
Textual TUI rewrite of the
[GoogleUnlimited](https://github.com/shishir0x/GoogleUnlimited) orchestration
concept for Linux.

## What it does

- Watches folders with inotify: `~/Pictures` by default (deliberately narrow -
  add more explicitly in the Folders tab or `config.toml`), debounces file
  events, waits until files are fully written, and uploads
  images/videos/RAW to Google Photos through `gotohp-cli`.
- Deduplicates: exact-path records catch changed files; a name+size fallback
  key with SHA-256 verification catches re-downloaded copies under new paths.
- Never re-uploads what it already uploaded, survives restarts (SQLite state),
  and retries failed uploads up to 3 times before giving up.
- Runs as an interactive TUI, a headless daemon (`--headless`, made for a
  systemd user service), or a one-shot drain (`--once`).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```sh
git clone <this-repo> unli-photos
cd unli-photos
uv sync --extra dev
```

## First run (TUI)

```sh
uv run unli
```

1. **Setup tab**: click *Download gotohp-cli*. The app fetches the pinned
   release (`v0.11.0`), verifies its SHA-256 against a built-in manifest, and
   installs it to `~/.local/share/unli-photos/bin/gotohp-cli`.
2. **Accounts tab**: press `a` and paste an OAuth token (gotohp accepts a
   Google Photos OAuth token; see the gotohp README for how to obtain one).
   The token is piped to `gotohp-cli creds add -` over stdin, never argv or
   shell history, and the config file is chmod 600 afterward.
3. **Folders tab**: add or remove watch folders (start with `~/Pictures`
   already covered by the default); changes are saved to `config.toml` and the
   watcher restarts immediately.
4. **Dashboard tab**: press `s` to start/stop the engine, `r` for a rescan
   (catch-up: uploads only new/changed files), `q` to quit.

### Keys

| Key | Action |
| --- | ------ |
| `q` | quit |
| `r` | rescan watch folders (dedup-aware catch-up) |
| `R` | force re-upload of every media file (bypasses dedup) |
| `s` | start/stop the sync engine |
| `a` | add an account token |

## Headless (systemd user service)

```sh
uv run unli --headless          # run in the foreground
uv run unli --once              # one scan/upload pass and exit
```

Enable the bundled service management from the Setup tab, or wire it manually:

```ini
# ~/.config/systemd/user/unli-photos.service
[Unit]
Description=unli-photos watch-and-upload to Google Photos
After=network-online.target

[Service]
WorkingDirectory=/path/to/unli-photos
ExecStart=/path/to/unli-photos/.venv/bin/unli --headless
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now unli-photos.service
loginctl enable-linger   # keep running after logout
```

## Files

| Path | Purpose |
| ---- | ------- |
| `~/.config/unli-photos/config.toml` | watch folders + options |
| `~/.config/unli-photos/gotohp.config` | gotohp credentials (chmod 600) |
| `~/.local/share/unli-photos/state.db` | upload history, retry queue, logs |
| `~/.local/share/unli-photos/bin/gotohp-cli` | pinned engine binary |
| `~/.config/systemd/user/unli-photos.service` | user service unit |

## Architecture

```
src/unli_photos/
  media.py      extension filter + size formatting
  config.py     AppConfig <-> config.toml, XDG paths
  storage/      SQLite repo: uploads, retry queue, logs (WAL, locked)
  engine/       SyncEngine: watchdog -> debounce -> readiness -> dedup
                -> upload worker, retry flush (30s), safety sweep (15m)
  photos/       gotohp-cli wrapper (upload/creds), binary manager
                (pinned SHA-256), systemd user service manager
  tui/          Textual app; the engine is thread-based, events are
                bridged into the UI via call_from_thread
  cli.py        entrypoint: TUI / --headless / --once / --version
```

The engine never imports Textual. It runs on its own threads and publishes
frozen dataclass events; both the TUI and the headless listener are thin
view layers. See `docs/superpowers/specs/` for the full design document.

## Development

```sh
uv run pytest          # test suite
uv run ruff check .    # lint
```

## License

MIT
