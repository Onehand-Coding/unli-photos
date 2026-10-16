# unli-photos Implementation Plan

> **For agentic workers:** Executing inline in the current session per user instruction ("proceed, report when done"). No subagent handoff. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `unli-photos`, a professional Textual TUI that auto-uploads local media to Google Photos (Pixel unlimited spoofing) by orchestrating `gotohp-cli`, with SQLite dedup history, retry queue, watch/scan/sweep engine, credentials manager, binary manager, and a systemd user service headless mode.

**Architecture:** Thread-based `SyncEngine` (watchdog observer + single upload worker + monitor timers) emitting typed dataclass events; SQLite `Repo` behind one connection (WAL, parameterized SQL); `photos` layer wrapping `gotohp-cli` upload/creds and managing the pinned binary + systemd unit installer; Textual app is a pure view layer over engine events.

**Tech Stack:** Python >= 3.12 (uv-managed, dev via `uv run`), hatchling build backend, src layout. Runtime deps: `textual`, `watchdog`, `httpx`. Dev deps: `pytest`, `ruff`. Stdlib used: `sqlite3`, `tomllib`, `argparse`, `subprocess`, `hashlib`, `shutil`.

**Reference behavior sources:**
- https://github.com/shishir0x/GoogleUnlimited `google-unlimited.sh` (watch/scan/retry/sweep/service/creds flows, checksums, unit template)
- https://github.com/shishir0x/GoogleUnlimited `WatchAndUpload.ps1` (dedup/refresh model, adaptive timeout)
- https://github.com/xob0t/gotohp CLI (`upload --no-tui`, `creds add -/list/set`, release v0.11.0)

---

### Task 0: git init
- [ ] `git init` in project root; `git status` clean/untracked ready.

### Task 1: Repo scaffold
**Files:** Create `pyproject.toml`, `.gitignore`, `.python-version` (3.12), `LICENSE` (MIT), `README.md` (stub), `src/unli_photos/__init__.py`, `tests/__init__.py`
- `__init__.py`: `__version__ = "0.1.0"`
- `pyproject.toml`: hatchling backend; `[project] name="unli-photos"`, `requires-python=">=3.12"`, deps as above, dev extra; `[project.scripts] unli="unli_photos.cli:main"`; ruff config section; pytest section.
- `.gitignore`: `.venv/`, `dist/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.ruff_cache/`, `state.db*`, `gotohp.config`, `*.token`
- [ ] `uv sync` creates `.venv` + `uv.lock` with zero errors; commit `chore: scaffold project (hatchling, src layout, uv)`.

### Task 2: `media.py`
**Files:** `src/unli_photos/media.py`, `tests/media_test.py`
- `MEDIA_EXTENSIONS: frozenset[str]` covering the original's full list (images incl. RAW, videos; 37+ entries).
- `is_media_file(path: Path) -> bool` (case-insensitive).
- `format_size(n_bytes: int) -> str` ("512 B", "1.0 KB", "1.5 MB", "1.0 GB").
- [ ] TDD: tests first, red, implement, green. Commit `feat: media extension filter and size formatting`.

### Task 3: `config.py`
**Files:** `src/unli_photos/config.py`, `tests/config_test.py`
- `@dataclass(frozen) AppConfig`: `dirs: tuple[str, ...]`, `include_defaults: bool = True`.
- `load_config(path: Path) -> AppConfig` (missing/unparsable -> defaults; read `[watch]` table).
- `save_config(path: Path, cfg: AppConfig) -> None` (create parent dirs; basic-string escape each dir via `json.dumps`).
- `default_watch_paths() -> tuple[Path, ...]`: Downloads, Pictures, Desktop, Documents, Music, Movies, Videos, `~/Google Drive`, `~/Google Photos`, `~/OneDrive/Pictures`, `~/Pictures/iCloud Photos/Photos` relative to `Path.home()`.
- `@dataclass(frozen) Paths`: `config_dir`, `config_file`, `state_dir`, `db_path`, `bin_dir`, `gotophp_config`; `default_paths()` honors `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME` (fallback `~/.config`, `~/.local/share`).
- [ ] TDD roundtrip + defaults + XDG override via monkeypatched env. Commit.

### Task 4: `storage/`
**Files:** `src/unli_photos/storage/{__init__,models,db,repo}.py`, `tests/storage_test.py`
- Models: `UploadRecord(path, media_key, file_name, file_size, content_hash, uploaded_at)`, `RetryRecord(path, attempts, last_attempt)`, `LogEntry(id, timestamp, level, message)` (dataclasses, str/int fields).
- Repo schema (parity with original scripts): `uploads(path TEXT PRIMARY KEY, media_key TEXT, file_name TEXT, file_size INTEGER, content_hash TEXT, uploaded_at TEXT DEFAULT ...)`, `retry_queue(path TEXT PRIMARY KEY, attempts INTEGER, last_attempt TEXT)`, `logs(id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT, level TEXT, message TEXT)`; indexes on uploads(file_name, file_size), uploads(content_hash), logs(timestamp, level).
- `Repo.open(db_path: Path) -> Repo`: WAL, `synchronous=NORMAL`, `busy_timeout=30000`, `check_same_thread=False`, guarded by `threading.RLock`.
- Methods: `close()`; `is_recorded(path) -> bool`; `exact_record(path) -> tuple[int, str|None] | None`; `record_upload(path, media_key=None, file_name="", file_size=0, content_hash="")` (INSERT OR REPLACE + `retry_drop`); `retry_attempts(path) -> int`; `retry_add(path, attempts)`; `retry_drop(path)`; `pending_retries() -> list[RetryRecord]`; `name_size_hashes(file_name, file_size) -> set[str]` (non-empty hashes only); `backfill_hash(path, content_hash)`; `log(level, message)`; `recent_logs(limit=200) -> list[LogEntry]`; `recent_uploads(limit=200) -> list[UploadRecord]`; `stats() -> dict[str, int]` (`uploaded`, `retries`); `prune_logs(days=30)`.
- [ ] TDD dedup/retry/log semantics incl. size-changed vs hash-changed cases. Commit.

### Task 5: engine package
**Files:** `src/unli_photos/engine/{__init__,events,debounce,readiness,dedup,engine,bridge}.py`, `tests/{debounce,readiness,dedup,engine}_test.py`

**events.py** - frozen dataclasses (fields as str/int/float only, paths stringified):
`EngineStarted(watch_dirs: tuple[str,...])`, `EngineStopped()`, `FileDetected(path: str, size: int)`, `UploadStarted(path, size)`, `UploadSucceeded(path, size, duration: float, media_key: str|None)`, `UploadFailed(path, duration, error: str|None, attempts: int)`, `UploadSkipped(path, reason: str)`, `RescanStarted()`, `RescanTick(done, total)`, `RescanDone(queued: int)`, `LogMessage(level, message)`, `PrecheckIssue(message)`.

**debounce.py** - `Debouncer(window_s: float, max_entries: int = 100, seconds_param: float = 1.5)`: `ready(path: Path, now = time.monotonic) -> bool` (first event accepted; then only when >= window since last accepted); `prune(max_entries=100)` drops entries older than 60s once dict exceeds max.

**readiness.py** - `Readiness(enum): READY / NOT_FOUND / NOT_READY`; `wait_for_file_ready(path, *, sample_interval=1.0, stable_samples=2, max_samples=30, lock_probe=True) -> Readiness`. Contract: NOT_FOUND once missing; size<=0 waits; size unstable resets stability counter; after `stable_samples` equal samples, (optional) writer-lock probe via `lsof`/`fuser` (exit 0 -> locked -> keep waiting); out of samples -> NOT_READY. (Windows-only exclusive-open probe is extra: skip.)

**dedup.py** - `sha256_file(path, chunk=1MiB) -> str`; `needs_upload(path, repo) -> bool` implementing the original's dual-key logic:
1. file missing -> False (caller handles existence).
2. `repo.exact_record(path)`: size changed -> True (modified); stored hash set and differs -> True; else compare fresh hash if stored hash exists; equal -> False; stored hash empty -> backfill fresh and False.
3. no exact record: `repo.name_size_hashes(name, size)` non-empty: if fresh hash in set -> `record_upload(path, media_key=None, content_hash=fresh)` backfilling the relocated entry then False; if pool has only empty hashes -> backfill first and True.
4. unseen -> True.

**bridge.py** (watchdog event source) - `MediaEventHandler(engine, debounce_window=1.5)`: watchdog `FileSystemEventHandler` for created/modified/moved; resolves moved path, passes through `Debouncer`, forwards existing files to `engine.enqueue`.

**engine.py** - `EngineIntervals` dataclass (`debounce=1.5`, `readiness_sample=1.0`, `retry_wait=30`, `sweep_wait=900`, `worker_poll=0.2`); `class SyncEngine`:
- ctor: (`*, repo, uploader, listener: Callable[[EngineEvent], None] | None, config_paths for dirs, intervals`).
- `watch_dirs() -> list[Path]` resolved (config.dirs expanded + defaults if include_defaults, existing only; ensure `~/Pictures` fallback), dedup preserving order.
- `start()/stop()`: worker thread (`queue.Queue`), watchdog Observer + MediaEventHandler per dir, monitor thread (retry flush each `retry_wait`; safety sweep each `sweep_wait`); emits `EngineStarted`/`EngineStopped`; `stop()` clears sentinel, joins.
- `enqueue(path)`, `request_rescan() -> None` (posts rescan request onto worker queue; worker does dedup-bypass scan + `--force` uploads; emits `RescanStarted/RescanDone`).
- `_process_candidate(path, force=False)` core pipeline (extension filter -> exists check -> dedup -> readiness -> upload -> record/retry), emitting events and repo logs (parity with original).
- `run_once()`: synchronous scan + retry flush drain (no threads) for `--once`/tests.
- `stop()` harmlessly no-ops when not started.

- [ ] TDD: all four test files (fake `Uploader` recording calls/returning scripted `UploadResult`s; tmp watch dir; small intervals). Commit.

### Task 6: `photos/` integration layer
**Files:** `src/unli_photos/photos/{__init__,uploader,binary,service}.py`, `tests/{uploader,binary,service}_test.py`

**uploader.py**:
- `@dataclass UploadResult(ok: bool, media_key: str|None = None, error: str|None = None)`; `parse_upload_output(stdout: str) -> tuple[str|None, str|None]` (mediaKey regex, `success`, `error`/`message` capture; parity to ps1 parser).
- `Protocol Uploader`: `upload(path: Path, *, force: bool = False) -> UploadResult.
- `GotophpUploader(binary: Path|str, config_path: Path|None, account: str|None = None)`: builds `[str(binary), *("--config", cfg), "upload", "--no-tui", *(["--account", acct] if account else []), *(["--force"] if force else []), str(path)]`; `subprocess.run` with `capture_output=True, text=True, timeout=max(600, size // 50_000)`; success iff exit 0, no parsed error, and (`"success":true` or mediaKey pick). Config chmod 0o600 post-call when exists. Decides `UploadResult.ok`; never raises for normal failure (timeout -> ok=False, error="timeout").
- `@dataclass CredRecord(email: str, active: bool)`; `parse_creds_list(text) -> list[CredRecord]` (line-based, `*` prefix marks active; email regex; "No credentials found" -> []).
- `CredsClient(binary, config_path)`: `list()`, `add(token)` (runs `creds add -` with `input=token`), `set_active(email)` (`creds set email`), chmod 0o600 after each.

**binary.py**:
- `GOTOHP_VERSION = "v0.11.0"`; `ASSETS: dict[str, str]` = arch -> asset name (`x86_64: gotohp-cli_amd64`, `aarch64: gotohp-cli_arm64` when released); `SHA256: dict[str, str]` pinned (amd64 value from original = `945021d5dbc36a96f53121eb4f907ace39d9457cea1b43107ed1986441b29030`; arm64 to be computed from the actual artifact during execution before wiring).
- `disk_archiver(paths) -> Path` = `paths.bin_dir / "gotohp-cli"`; `sha256_file()`; `find_binary(paths) -> Path|None` (bin_dir then `shutil.which("gotohp-cli")`).
- `verify(paths) -> "BIN_OK"|"BIN_MISSING"|"BIN_CORRUPT"`.
- `download(paths, *, on_progress: Callable[[int, int | None], None] | None = None, client: httpx.Client | None = None) -> Path`: streams `releases/download/GOTOHP_VERSION/<asset>` with `follow_redirects=True` to `*.download` tmp, verifies sha during streaming, asserts asset exists in SHA256 table, chmod 0o700, rename final. `BinaryError` on hash mismatch / HTTP failure.

**service.py**:
- `SERVICE_NAME = "unli-photos"`; `unit_path() -> Path` at `~/.config/systemd/user/unli-photos.service`; `unit_text(project_dir: Path, exec_start: str) -> str`: `After=network-online.target`, `Restart=on-failure`, `RestartSec=10`, `WorkingDirectory=<project_dir>`.
- `derive_exec_start() -> str`: `<sys.executable>/../unli --headless` when `unli` script exists next to interpreter (venv layout), else `<sys.executable> -m unli_photos --headless`.
- `systemctl_available() -> bool` (`shutil.which`).
- `enable(paths, project_dir) -> str` (verifies ExecStart file exists; writes unit; `systemctl --user daemon-reload`, `enable --now`; attempt `loginctl enable-linger` best-effort); `disable() -> str` (`disable --now`, remove unit, `daemon-reload`); `status() -> str` (`is-active` + `is-enabled`, "not installed" when unit absent, "unavailable" when systemctl missing).
- [ ] TDD fake systemctl on PATH + fake unli script + fake binary. Commit.

### Task 7: `cli.py` headless + `__main__.py`
**Files:** `src/unli_photos/cli.py`, `src/unli_photos/__main__.py`, `tests/cli_test.py`
- `main(argv=None) -> int`: `argparse` with `--headless`, `--version`, `--once`; `--version` exits 0 after printing `unli-photos <ver>`; default path -> `run_tui()`.
- `run_headless(args)`: init `Paths`, `Repo`, resolve binary via `find_binary`/`verify` (fail -> stderr + 1); creds precheck (fail -> stderr + 1 with sign-in instructions); build engine with mapped listener + stdout formatter `[HH:MM:SS] [LEVEL] msg` (format_size on valid events); `--once` -> `run_once` + exit 0; else `start()` wait on `signal` loop until Ctrl+C -> `stop()`.
- `__main__.py`: `raise SystemExit(main())`.
- [ ] TDD with fake uploader + tmp XDG + fake gotohp shim on PATH (`--once` run with fake binary shim). Commit.

### Task 8: TUI
**Files:** `src/unli_photos/tui/{__init__,app,panes,dialogs}.py`, `src/unli_photos/tui/app.tcss`, `tests/tui_test.py`
- `UnliApp(App)` (CSS_PATH=app.tcss) - `TabbedContent` panes: `DashboardPane`, `UploadsPane`, `LogsPane`, `FoldersPane`, `AccountsPane`, `SetupPane`, four screens; bindings `q quit`, `r rescan` handled at App level.
- Ctor takes `engine` + `paths`; mounts listeners to bridge engine events via `self.call_from_thread(self._dispatch_event, ev)`; `_dispatch_event` fans out to acceptable panes (presence-guarded).
- Panes:
  - DashboardPane: stat cards (uploaded / retries / engine state / watch dirs count), last 10 uploads list; subscribed to messages by re-querying.
  - UploadsPane: `DataTable` with columns Time / Name / Size / Status; auto-refresh from engine events + full reload from `repo.recent_uploads()` when shown.
  - LogsPane: `RichLog` fed by `LogMessage` events + `repo.recent_logs(limit=200)` on activation.
  - FoldersPane: `ListView` of active watch dirs; `Input` + Add with dir validation; Remove button; `config.save_config(paths.config_file, cfg)` + engine re-observation on change; toast feedback.
  - AccountsPane: active label + `ListView` accounts (listing `CredsClient.list()`); Set Active button; Add Account -> dialog with `Input(password=True)` and instructions, submits via blocking `CredsClient.add(token)` in a worker; toast on success.
  - SetupPane: binary status + Download button (progress callback into ProgressBar via `call_from_thread`), service status text + Enable/Disable buttons; refreshable.
- `run_tui(paths)`: build Repo, engine, GotophpUploader(existing binary), CredsClient; app runs; engine started on mount; stopped on unmount.
- [ ] TDD: pilot tests for tab switching, FOLDER add/result, event propagation on dashboard. Commit.

### Task 9: README + polish
- [ ] README: features, uv quickstart, integration notes (config file sample, service parity, checksum pins), upstream credits (gotohp + GoogleUnlimited inspiration).
- [ ] `uv run ruff check` zero pending issues; `uv run pytest -q` all green; smoke `uv run unli --version` works.
- [ ] `docs/superpowers/{specs,plans}` untouched; commit `docs: readme and polish`.

### Task 10: CONTEXT.md + final verification
- [ ] CONTEXT.md: project purpose, engine layering, states, dev commands, gotchas (inotify, self-credits, checksums).
- [ ] Full gates: pytest, ruff, TUI boot sanity run (`app --headless --once` with fake shim only in tests; NOT against real creds).
- [ ] Commit `chore: wrap up implementation`, close all checkboxes.

---

## Design Decisions
- The upload engine is a subprocess: `gotohp-cli upload --no-tui` is the surface, its JSON result drives state transitions exactly like original scripts.
- Checksums pinned at execution time: `v0.11.0` (matches the original's pinned manifest); verify from downloaded artifacts; amd64 starts with the value cited in `google-unlimited.sh` (`945021d5dbc36a96f53121eb4f907ace39d9457cea1b43107ed1986441b29030`), arm64 computed fresh.
- Engine never imports textual; TUI obtains its own engine instance; storage Repo shared only between the fronts explicitly passed in.
- `uv-managed dev flow only: `uv sync`, `uv run pytest`, `uv run ruff check`; no Makefile.
- README targets uv-tool installation; no `pip` story beyond README mention.
