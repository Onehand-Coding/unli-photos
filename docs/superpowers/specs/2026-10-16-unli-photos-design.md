# unli-photos Design

A clean, professional Python TUI (Textual) implementation of the GoogleUnlimited concept:
auto-sync local media to Google Photos with Pixel original-quality unlimited backup,
powered by the `gotohp-cli` engine as a subprocess, with SHA-256 deduplication, retry
queue, and a headless systemd user service mode.

## Sources

- Original orchestration layer: https://github.com/shishir0x/GoogleUnlimited (bash/powershell)
- Upload engine being wrapped: https://github.com/xob0t/gotohp (Go, `gotohp-cli` binary)

## Goals

- Real-time watcher over standard user folders + custom dirs (startup scan, 15-min
  safety sweep, 30s retry flush).
- Upload history in a local SQLite DB with SHA-256 content hashing; never re-upload
  unchanged files; cross-location dedup via `file_name|file_size` + hash verification.
- Retry queue: failed uploads retried up to 3 attempts, then dropped with an ERROR log.
- TUI dashboard: stats, live upload activity, upload history, logs, folders, accounts,
  binary/service setup.
- Headless mode (`unli --headless`) for a systemd user service installed from the TUI.
- Secure handling: OAuth token entry via hidden paste modal, `chmod 600` on
  `gotohp.config`, parameterized SQL, checksum-pinned binary downloads, no telemetry.

## Non-goals (v1)

- Reimplementing the Pixel spoofing protocol in Python (we wrap gotohp-cli).
- macOS launchd / Windows autostart installers (watcher code stays cross-platform,
  only the systemd unit installer ships in v1).
- Byte-level upload progress (gotohp `--no-tui` emits terminal-state JSON only).
- Parallel uploads, threads setting, debounce tuning, per-file inline re-upload,
  TOML settings beyond the watch list.

## Architecture (Approach A: thread engine, TUI as view layer)

```
                     ┌────────────────────────────────────────────┐
   Event objects     │                Textual TUI                 │
 (dataclasses)  ────►│  DASHBOARD / UPLOADS / LOGS / FOLDERS /    │
                     │  ACCOUNTS / SETUP screens                  │
                     └───────────▲────────────────────────────────┘
                                 │ callbacks (thread-safe UI post)
                     ┌───────────┴────────────────────────────────┐
                     │              SyncEngine                    │
                     │  watchdog Observer thread (debounced)      │
                     │  upload worker thread (1 file at a time)   │
                     │  retry flush timer (30s)                   │
                     │  safety sweep timer (15min)                │
                     └───────────▲────────────────────────────────┘
                                 │
     ┌───────────────────────────┼─────────────────────────────┐
Storage (sqlite3 repo)   Uploader (gotohp-cli proc)      Config (TOML + defaults)
```

- Same `SyncEngine` in TUI and headless mode: engine never imports textual.
- Engine notifies registered listeners; the TUI bridges events onto the UI event loop.

### Project layout (src + hatchling + uv)

```
unli-photos/
├── pyproject.toml
├── uv.lock, .python-version (3.12+)
├── README.md, LICENSE (MIT), .gitignore, ruff config inside pyproject
├── src/unli_photos/
│   ├── __init__.py (version)     __main__.py
│   ├── cli.py                    # `unli` TUI | `unli --headless [--once]` | `--version`
│   ├── config.py                 # TOML + XDG paths + default dirs
│   ├── media.py                  # media extension filter, size formatting
│   ├── storage/{db,models,repo}.py
│   ├── engine/{events,watcher,readiness,dedup,engine}.py
│   ├── photos/{uploader,binary,service}.py
│   └── tui/ (app.py, screens, widgets, css)
└── tests/
```

### Concurrency details

- watchdog Observer runs its own thread; our handler filters + debounces (1.5s per
  path) and enqueues candidate files.
- Single upload worker: for each candidate: extension filter → readiness gate →
  local dedup → gotohp upload → record. Readiness gate: file exists, size > 0, size
  stable across 2 consecutive 1s samples, writer-lock check via `lsof|fuser` when
  available; not-ready files re-queue without penalty.
- Adaptive per-file timeout: `max(600s, size_bytes / 50KB)` seconds, matching the
  original's guard against hung uploads.
- Strictly one upload in flight (parity with the original scripts); queue holds the rest.

## Data model (SQLite, WAL)

- `uploads(path PK, media_key, file_name, file_size, content_hash, uploaded_at)`
- `retry_queue(path PK, attempts, last_attempt)`
- `logs(id, timestamp, level, message)`, 30-day retention
- Partial-dup logic: path+size+hash first, then `file_name|file_size` lookup verified
  against SHA-256 to handle relocated files across machines/booted OSes.

## Engine behavior

1. Startup scan (Phase 1): recursive scan of watched dirs for media extensions; skip
   already-uploaded; include retry queue with <3 attempts; upload each (with dedup).
2. Real-time watch (Phase 2): fs events (created/changed/moved-in), debounced, same
   pipeline. Entries whose files no longer exist are purged from the retry queue.
3. Safety sweep every 15 min catches anything missed while offline/paused.
4. Manual "Rescan all" bypasses local dedup and passes `--force` to gotohp (server
   cache is the safety net against remote duplicates).
5. All engine activity logs to the `logs` table (and headless stdout).
6. `--once` mode: run the startup scan + retry flush once and exit (used by the
   systemd smoke path and manual testing).

## gotohp integration

- Uploader protocol `Uploader` (test seam). Real implementation `GotophpUploader`:
  `gotohp-cli upload --no-tui [--account EMAIL] [--force] [--config PATH] FILE`,
  stdout JSON parsed fields: `success`, `mediaKey`, `error`/`message`.
- Creds: `creds list` (parse active `*` + emails), `creds add` via stdin pipe only
  (never argv, never shell history), `creds set EMAIL`.
- Binary manager: pinned manifest `{name, version, sha256}` for Linux x86_64 + arm64
  from gotohp release; downloads to `~/.local/share/unli-photos/bin/gotohp-cli`,
  verifies hash, chmod 700. Download via httpx streamed with progress events.
- Config file `~/.config/unli-photos/gotohp.config` passed with `--config` on every
  call; chmod 600 enforced after every invocation.

## Service (Linux)

- systemd **user** unit rendered at `~/.config/systemd/user/unli-photos.service`:
  `ExecStart=<abs project path>/.venv/bin/unli --headless`, WorkingDirectory=abs project
  dir, Restart=on-failure, After=network-online.target.
- Enable/disable/status from the SETUP screen; `loginctl enable-linger` attempted when
  available (degrades gracefully). Credentials must exist before enable (like the original).

## File locations

```
~/.config/unli-photos/config.toml       # [watch] dirs, include_defaults
~/.config/unli-photos/gotohp.config     # gotohp's own credentials store (chmod 600)
~/.local/share/unli-photos/state.db     # SQLite (uploads, retry_queue, logs)
~/.local/share/unli-photos/bin/         # gotohp-cli binary (chmod 700)
```

## TUI

Tabs: `DASHBOARD` (stat cards: uploaded count, retries, in-flight + last uploads,
watched dirs), `UPLOADS` (DataTable history, newest first), `LOGS` (30-day window),
`FOLDERS` (list from config + defaults, add/remove dirs with persistence), `ACCOUNTS`
(active account, list/add/switch; hidden-token modal with instructions), `SETUP`
(binary verify/download with progress, systemd service enable/disable/status);
Global keys: `r` rescan, `q` quit, tab navigation `tab/shift+tab`.

## Tooling

- uv + hatchling + src layout; Python >= 3.12; deps: `textual`, `watchdog`, `httpx`.
- Dev deps: `pytest`, `ruff`.
- Quality gates: `uv run pytest`, `uv run ruff check`, smoke-run the TUI boot and
  `unli --headless --once` locally (`--once` runs the startup scan pipeline once and
  exits; useful for tests and manual verification).

## Testing strategy

- Injected `Uploader` fakes: engine pipeline tests entirely offline.
- Unit: media filter, config round-trip, DB repo (dedup/retry/log semantics),
  readiness checker using slowed writes, event debounce unit, service unit renderer,
  binary hash verifier, upload JSON parser.
- TUI: Textual Pilot smoke tests (boot, tab switching, folder add flow) with fake
  engine; no network.
- Integration smoke: `unli --headless --once` against a tmp watch dir with a fake
  `gotohp-cli` shim on PATH (manual, not run in tests).

## Error handling / security

- Any upload failure: retry (3 attempts, error logged), then dropped from queue.
- Readiness gate failure: stays in queue with WARN, no attempt penalty.
- Security posture: parameterized SQL, token entry via stdin never argv or shell
  history, chmod 600 on credentials and DB, chmod 700 on binaries, checksum-verified
  downloads, tokens never written to logs (engine messages sanitized before storage).
- Crash safety: WAL mode; uploads are recorded only after gotohp exits 0 with
  `success`/`mediaKey`; dedup makes restarts safe (a repeated already-uploaded file is
  simply skipped, mid-crash re-uploads are harmless).
