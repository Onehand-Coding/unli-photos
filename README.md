# unli-photos

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Auto-sync local media folders to Google Photos using the Pixel
unlimited-storage quota path, powered by
[`gotohp-cli`](https://github.com/xob0t/gotohp). One thread-based sync engine,
two frontends: a professional [Textual](https://textual.textualize.io/) TUI and
a headless daemon for a systemd user service.

A Linux rewrite of the
[GoogleUnlimited](https://github.com/shishir0x/GoogleUnlimited) orchestration
concept with a real engine/UI separation. unli-photos does not implement any
upload protocol itself; it orchestrates `gotohp-cli`, which owns auth and the
Google device API.

> [!WARNING]
> Not affiliated with or endorsed by Google. The unlimited-storage behavior
> depends on the device emulation in `gotohp`; Google can change its APIs or
> policies at any time. Use at your own risk with an account you accept that
> for, and keep independent backups of your media.

## Features

- **Set-and-forget watching**: inotify watcher with debouncing, waits until
  files are fully written (size-stable + lock probe) before uploading.
- **Deduplication**: exact-path upload records catch changed files; a
  name + size fallback key with SHA-256 verification catches re-downloaded
  copies under new paths. Already-uploaded files are never re-uploaded.
- **Retries**: failed uploads are retried 3 times, then dropped with a "GAVE
  UP" log - no poisoned-file hot loops.
- **Narrow by default**: watches `~/Pictures` only; extra folders are always
  explicit (TUI Folders tab or `config.toml`).
- **Three run modes**: interactive TUI, headless daemon (`--headless`, made
  for systemd), one-shot drain (`--once`, cron-like).
- **Secure by default**: OAuth tokens travel via stdin only (never argv or
  shell history), credential files are chmod 600, the gotohp binary is
  verified against a pinned SHA-256 and installed chmod 700.

## Requirements

- Linux (x86_64), Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `systemctl`/`loginctl` for the bundled service management (optional)

> Note: gotohp v0.11.0 ships an amd64-only CLI asset; aarch64/arm64 hosts are
> not supported until upstream publishes a matching release.

## Install

```sh
# as an installed tool (adds `unli-photos` and the short alias `up` to PATH)
uv tool install git+https://github.com/Onehand-Coding/unli-photos

# or from a checkout
git clone https://github.com/Onehand-Coding/unli-photos.git
cd unli-photos
uv sync --extra dev
```

Prefer not to install anything on a machine, but upload one folder? Run
one-shot mode via uvx:

```sh
uvx --from git+https://github.com/Onehand-Coding/unli-photos up --once
```

## First run (TUI)

```sh
up          # same as: unli-photos
```

1. **Setup tab**: click *Download gotohp-cli*. The app fetches the pinned
   release (`v0.11.0`), verifies its SHA-256, and installs it to
   `~/.local/share/unli-photos/bin/gotohp-cli`.
2. **Accounts tab**: press `a` and paste an OAuth token. See the
   [gotohp README](https://github.com/xob0t/gotohp#sign-in) for how to obtain
   one (the Embedded Setup + DevTools cookie flow works from a plain browser).
   The token is piped to `gotohp-cli creds add -` over stdin and the config
   file is chmod 600 afterward.
3. **Folders tab**: watch `~/Pictures` by default or turn it off; add explicit
   folders. Changes persist to `config.toml` and the watcher restarts
   immediately.
4. **Dashboard tab**: press `s` to start/stop the engine, `r` for a catch-up
   rescan, `q` to quit.

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
up --headless               # run in the foreground
up --once                   # one scan/upload pass and exit
```

Enable the bundled service management from the Setup tab, or wire it manually:

```ini
# ~/.config/systemd/user/unli-photos.service
[Unit]
Description=unli-photos watch-and-upload to Google Photos
After=network-online.target

[Service]
WorkingDirectory=/path/to/unli-photos
ExecStart=/path/to/unli-photos/.venv/bin/unli-photos --headless
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
| `~/.local/share/unli-photos/bin/gotohp-cli` | pinned upload binary |
| `~/.config/systemd/user/unli-photos.service` | user service unit |

## How it works

```
watchdog events -> debounce (1.5s) -> file readiness (size-stable + lock probe)
  -> dedup (SQLite records; exact path, then name+size with SHA-256 verify)
  -> upload via gotohp-cli subprocess -> record/tally in SQLite
monitor thread: retry flush every 30s, safety sweep every 15 min
```

The engine runs on its own threads and publishes frozen dataclass events; both
the TUI and the headless listener are thin view layers that never touch sync
logic. A force rescan (`Shift+R`) bypasses dedup for a true re-upload - useful
after deleting media in Google Photos (dedup normally keeps deleted media from
re-uploading while a local record exists).

## Development

```sh
uv run pytest          # test suite
uv run ruff check .    # lint
```

See [docs/superpowers/specs/](docs/superpowers/) for the design document and
implementation plan.

## Contributing

Contributions are welcome! Open an issue for bugs or feature ideas; pull
requests are equally appreciated. Before submitting a PR:

- Point it at `main` with a focused scope (one feature or fix per PR).
- Run the quality gates: `uv run pytest` and `uv run ruff check .` must pass.
- New behavior needs a test; changed behavior needs an updated one.

## Credits & Attribution

- [`xob0t/gotohp`](https://github.com/xob0t/gotohp) (MIT) - **the upload
  engine**. All Google authentication, device emulation, and upload protocol
  work belongs to gotohp. unli-photos only orchestrates its CLI.
- [`shishir0x/GoogleUnlimited`](https://github.com/shishir0x/GoogleUnlimited)
  - the original auto-sync orchestration concept that inspired this project
  (dedup keys, retry policy, readiness checks, folder design). No code was
  copied; this is an independent Linux/Python implementation.
- [Textual](https://github.com/Textualize/textual) (MIT) - the TUI framework.
- [watchdog](https://github.com/gorakhargosh/watchdog) (Apache-2.0) - inotify
  file watching.
- [httpx](https://github.com/encode/httpx) (BSD-3-Clause) - pinned binary
  download.

## License

[MIT](LICENSE)
