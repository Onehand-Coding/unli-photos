# unli-photos

Auto-sync media to Google Photos using Pixel unlimited-storage spoofing via
`gotohp-cli`. Textual TUI + headless daemon, Linux-first. Rewrite of the
GoogleUnlimited orchestration concept with a real engine/UI separation.

## Stack

- Python 3.12 (`.python-version`), managed with uv, src layout, hatchling build.
- Runtime deps: `textual` (TUI), `watchdog` (inotify), `httpx` (binary download).
- Dev: `pytest`, `ruff` (both via `uv sync --extra dev`).
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run unli --version`.

## Architecture

Thread-based `SyncEngine` (`src/unli_photos/engine/engine.py`) is the model;
frontends are thin views. The engine never imports textual.

- `media.py`: extension allowlist (42 entries) + size formatting.
- `config.py`: frozen `AppConfig` (dirs + include_defaults), TOML roundtrip,
  `Paths` + `default_paths()` (reads XDG env at call time, good for tests).
- `storage/`: single-connection SQLite WAL repo. Tables: `uploads` (path PK,
  media_key, name, size, sha), `retry_queue` (path PK, attempts), `logs`.
  `retry_add` sets an ABSOLUTE attempt count (not increment).
- `engine/`: watchdog observer -> `Debouncer` (1.5s, refreshed only on accepted
  events) -> `wait_for_file_ready` (size-stable 2 samples + lsof/fuser lock
  probe; NOT_READY re-queues without attempt penalty) -> dedup (`needs_upload`:
  exact-path record with size/hash change check, then dual-key name|size fallback
  with SHA-256 verify) -> single upload worker. Monitor thread: retry flush
  every 30s (max 3 attempts then give up + drop), safety sweep every 15min
  (cheap `is_recorded` prefilter). `request_rescan` queues a force-rescan that
  bypasses dedup. `run_once` drains scan+retries inline for `--once`.
- `photos/`: `GotophpUploader` runs
  `gotohp-cli --config <cfg> upload --no-tui [--account E] [--force] <file>`
  with adaptive timeout `max(600, size // 50_000)`s; success requires rc 0 AND
  parsed `"success": true`/mediaKey AND no error/message field. `CredsClient`
  pipes tokens to `creds add -` via stdin (never argv), chmods gotohp.config
  to 600 after every call. `binary.py` pins gotohp v0.11.0 with the amd64
  SHA-256 from the official SHA256SUMS.txt; streams to `*.download`, verifies,
  chmods 700, renames. `service.py` writes/starts/stops the user unit.
- `tui/app.py`: `UnliApp` builds/starts the engine on mount (after `precheck`),
  bridges engine events with `_post_event` -> `call_from_thread` (falls back to
  direct call when on the app thread, which is what the tests rely on).
  Test seams: module-level aliases `binary_download`, `service_enable`,
  `service_disable` (monkeypatch targets) and `CredsClient`.
- `cli.py`: `precheck()` (binary present + creds non-empty), `run_headless`
  (accepts a `threading.Event` stop for tests), `run_once`, `--version`.

## Gotchas

- Engine events are emitted from engine threads; anything touching widgets must
  go through `_post_call`. Direct widget writes from a listener will break.
- In tests ALWAYS pin a config with `include_defaults=False` and tmp dirs,
  otherwise `watch_dirs()` falls back to the REAL home folders
  (~/Downloads, ~/Pictures, ...) and starts scanning real media.
- `AppConfig(dirs=(x,))` defaults `include_defaults=True`, which APPENDS the
  default dirs; pass `include_defaults=False` for exact-dir control.
- gotohp v0.11.0 has NO Linux arm64 CLI asset; the manifest is amd64-only.
  Don't invent an arm64 checksum; the release also ships SHA256SUMS.txt.
- Watchdog LSP "could not be resolved" diagnostics are stale tooling noise;
  runtime imports and tests work.
- Live GitHub asset downloads are extremely slow in this sandbox (curl times
  out on the 18MB binary); the download path is covered by httpx MockTransport
  tests instead of a live round-trip.
- pytest process hangs if a test leaves the sqlite repo closed while a daemon
  engine thread still processes a big queue; the worker loop checks
  `_stop_event` between items to keep shutdown fast.

## Decisions

- Wrap `gotohp-cli` as a subprocess instead of reimplementing the Pixel
  spoofing: the CLI already handles auth/device API quirks; we orchestrate.
- All-in-one app: `unli` (TUI) and `unli --headless` (systemd) share the
  engine; `--once` reuses the same code path for cron-like use.
- Events as frozen dataclasses (not callbacks with kwargs) so TUI/headless/
  tests consume identical payloads; `format_event_line` is the shared
  human formatter (parity with the original script's console output).
- Token via stdin only, config chmod 600, binary chmod 700, pinned checksum:
  security parity with the original PowerShell/bash scripts.
- MAX_ATTEMPTS=3 then drop from retry queue (logged as GAVE UP), matching
  GoogleUnlimited behavior.

## Non-goals

- Windows/macOS support (Linux-first; paths and systemd assume Linux).
- Reimplementing gotohp upload protocol in Python.
- Multi-account simultaneous upload; single active account (gotohp model).

## Verification

- `uv run pytest` (110 tests) and `uv run ruff check .` are the gates.
- The end-to-end uploader path runs against stub shell scripts that mimic
  gotohp's stdout (`{"success": true, "mediaKey": ...}` / `creds list` output
  with `*` marking the active account).
