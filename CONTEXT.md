# unli-photos

> Persistent project memory — durable knowledge only.
>
> This file is NOT a changelog, session log, TODO list, or git history.
> Git already records what changed and when. This file records what
> Git cannot: architecture, philosophy, non-goals, decisions, conventions,
> domain rules, and the current state of unfinished or partial work.
>
> Update ONLY when durable project knowledge changes (see Maintenance
> Rules at the bottom). Do not touch this file for commits, bug fixes,
> refactors, or session summaries.

---

## 1. Project Overview

**Purpose:** unli-photos auto-syncs local media folders to Google Photos,
exploiting the Pixel unlimited-storage quota via `gotohp-cli`. It is a
rewrite of the GoogleUnlimited orchestration concept with a real
engine/UI separation: one thread-based sync engine, two frontends
(Textual TUI and a headless daemon under systemd), plus a `--once` mode
for cron-like runs.

**Target Users:**
- A single Linux user backing up personal photos/videos to Google Photos
  on an "unlimited" quota path.
- Comfortable with terminal apps; wants set-and-forget daemon behavior.

**Current Milestone:** v1 shipped and in daily use (TUI + headless +
systemd service); post-v1 refinement from real-usage feedback.

**Current Development Focus:** UX polish from real usage — narrower
defaults, clearer TUI feedback (pending states, summary dashboard).

---

## 2. Technology Stack

### Frontend (TUI)
| Component | Choice |
|---|---|
| Framework | Textual (Textual CSS in `app.tcss`) |
| State Management | Engine events pushed into widgets via a thread bridge |
| Routing | TabbedContent panes (Dashboard/Uploads/Logs/Folders/Accounts/Setup) |
| Local Storage | n/a (reads engine + SQLite via repo) |

### Backend (engine + services)
| Component | Choice |
|---|---|
| Framework | Python 3.12 stdlib threading (no framework) |
| ORM | None — hand-written SQLite repository |
| Validation | Frozen dataclasses + explicit prechecks |
| Authentication | Delegated to `gotohp-cli` (Google oauth token) |

### Database
| Component | Choice |
|---|---|
| Database | SQLite in WAL mode, single connection |
| Migrations | None (schema created by repo at startup) |

### Infrastructure
| Component | Choice |
|---|---|
| Container | None |
| Reverse Proxy | n/a |
| Deployment | systemd **user** service (`unli-photos.service`), linger enabled |

### Tooling
| Component | Choice |
|---|---|
| Language/Version | Python 3.12 (`.python-version`) |
| Package manager | uv (src layout, hatchling build) |
| Tests / Lint | pytest / ruff |

---

## 3. Repository Structure

```text
src/unli_photos/
  media.py        # extension allowlist + size formatting
  config.py       # AppConfig/Paths, TOML roundtrip, default dirs
  storage/        # SQLite repository (uploads, retry_queue, logs)
  engine/         # SyncEngine: watcher, dedup, upload worker, monitor
  photos/         # gotohp wrapper: uploader, creds, binary, service
  tui/            # Textual app (app.py, app.tcss)
  cli.py          # entrypoint: TUI / --headless / --once / --version
  __main__.py
tests/            # pytest, mirrors the package layout
docs/superpowers/ # spec + implementation plan (historical)
```

---

## 4. Architecture

**Overall:** Model/view split with hard boundaries. A thread-based
`SyncEngine` owns all sync logic; the Textual TUI and the headless CLI
are thin views that only render engine events. Philosophy: orchestrate,
don't reimplement (`gotohp-cli` owns the upload protocol); simple over
clever; security parity with the original scripts.

**Folder Organization:** One layer per package: `storage` (persistence),
`engine` (orchestration), `photos` (external-process wrappers), `tui`
and `cli` (frontends). `media.py`/`config.py` are shared leaf modules.

**Data Flow:**
watchdog events → `Debouncer` (1.5s) → `wait_for_file_ready`
(size-stable 2 samples + lsof/fuser lock probe) → dedup
(`needs_upload`: exact-path + size/hash, then name|size fallback with
SHA-256 verify) → single upload worker → `GotophpUploader` subprocess →
SQLite record. Monitor thread: retry flush every 30s, safety sweep
every 15min. Events fan out to registered listeners; frontends bridge
them onto the UI thread.

---

## 5. Non-Goals

- No Windows/macOS support (Linux-first; paths and systemd assume Linux).
- No reimplementation of the gotohp upload protocol in Python.
- No multi-account simultaneous upload (single active account, gotohp model).

---

## 6. Architecture Rules

- The engine NEVER imports textual. Frontends never contain sync logic.
- Google oauth tokens travel via stdin only, never argv.
- UI widgets are touched only on the UI thread: listeners must go through
  the `_post_event` → `_post_call` bridge.
- All SQLite access goes through the `storage` repository (single
  connection, WAL).
- Events and config are frozen dataclasses; frontends never mutate them.
- `gotohp.config` is chmod 600 after every creds call; the downloaded
  binary is chmod 700.

---

## 7. Coding Conventions

**General:**
- Type hints everywhere; frozen dataclasses for value objects/events.
- Module-level aliases as test seams where subprocess calls need
  monkeypatching (`binary_download`, `service_enable`, `service_disable`,
  `CredsClient`).
- No comments beyond what's necessary; match existing ruff config.

**Backend/Engine:**
- Worker loops must check `_stop_event` between items so shutdown is fast.
- Subprocess output parsing is regex-based with explicit success criteria
  (see Domain Knowledge).

**Database:**
- `retry_add` sets an ABSOLUTE attempt count (not increment).

**Testing:**
- ALWAYS pin a config with `include_defaults=False` and tmp dirs in
  tests; otherwise the engine watches the real home folders.
- TUI tests: assertions inside `app.run_test()` context; activate the
  Logs tab before checking `RichLog.lines`; focus an `Input` before
  pressing Enter.
- End-to-end upload path is tested with stub shell scripts mimicking
  gotohp stdout, not the real binary.

---

## 8. Project Decisions

> Decisions are append-only. Never delete a decision — if no longer
> applicable, update its Status to **Superseded** or **Deprecated**.

### Wrap gotohp-cli as a subprocess
**Choice:** Orchestrate `gotohp-cli` rather than reimplement the Pixel
spoofing protocol. **Status:** Current. **Reason:** The CLI already
handles auth and device-API quirks. **Alternatives Considered:**
Reimplementing the upload protocol in Python (rejected: fragile,
duplicates upstream work).

### All-in-one app
**Choice:** `unli-photos` (canonical command, short alias `up`) runs the TUI,
`--headless` (systemd), and `--once` (cron-like), all sharing one engine and
code path. The former `unli` command name was dropped. **Status:** Current.
**Reason:** One behavior to test, three delivery modes.
**Alternatives Considered:** Separate daemon binary (rejected:
duplication).

### Events as frozen dataclasses
**Choice:** Engine emits frozen dataclass events; `format_event_line`
is the shared human formatter. **Status:** Current. **Reason:** TUI,
headless, and tests consume identical payloads (parity with the
original script's console output). **Alternatives Considered:**
Callbacks with kwargs (rejected: no stable payload contract).

### Security parity with the original scripts
**Choice:** Token via stdin only, config chmod 600, binary chmod 700,
pinned binary checksum. **Status:** Current. **Reason:** Matches the
hardening of the original PowerShell/bash scripts.
**Alternatives Considered:** argv tokens (rejected: leaks via ps).

### Retry policy: MAX_ATTEMPTS=3 then drop
**Choice:** Three attempts, then remove from the retry queue and log
"GAVE UP". **Status:** Current. **Reason:** Matches GoogleUnlimited
behavior; prevents a poisoned file from looping forever.
**Alternatives Considered:** Unlimited retries (rejected: hot loop).

### Single-instance guard via flock
**Choice:** Every engine start (TUI start/stop, `--headless`, `--once`)
acquires an flock-based `engine.lock` in the XDG state dir; a second
engine refuses to start with `EngineLocked` (holder PID + timestamp in
the file). **Status:** Current. **Reason:** TUI + uv-tool + dev-checkout
installs share XDG dirs, so the guard works across installs and
survives crashes (kernel releases flock). Headless exits 0 on
EngineLocked so systemd does not crash-loop.
**Alternatives Considered:** PID file liveness checks (rejected: stale
PID reuse races), sqlite-level guard (rejected: couples persistence to
process coordination).

---

## 9. Domain Knowledge

- Upload success requires ALL of: exit code 0, parsed
  `{"success": true, ...}` with a mediaKey, and NO error/message field
  in gotohp's stdout.
- gotohp's `creds list` marks the active account with `*`; there is
  exactly one active account at a time.
- Google token acquisition (gotohp Option 1 "Embedded Setup"): sign in
  at accounts.google.com/EmbeddedSetup, agree, then read the
  `oauth_token` cookie (value starts `oauth2_4/`) from DevTools; the
  page hanging after "I agree" is expected.
- Dedup keys on exact path first (size/hash change re-uploads), with a
  name|size fallback verified by SHA-256; a file deleted from Google
  Photos will NOT re-upload while its record exists.
- `--force` bypasses dedup. Plain rescan (`r` / `request_rescan()`) is a
  dedup-aware catch-up (uploads only unrecorded/changed files); true force
  re-upload is explicit (`Shift+R` / `request_rescan(force=True)`) and
  re-runs gotohp `--force` for every media file.
- Files that aren't ready (still downloading/recording) re-queue
  WITHOUT an attempt penalty; only real upload failures consume
  attempts.

---

## 10. Known Gotchas

### Engine threads
- Events arrive on engine threads; direct widget writes from a listener
  will break. Always route through `_post_call`.
- `engine.stop()` joins worker threads (up to 15s) and must never run
  on the UI thread; the TUI runs start/stop in a background thread with
  a `_engine_pending` status.

### Config defaults
- `AppConfig(dirs=(x,))` defaults `include_defaults=True`, which APPENDS
  the default dirs. Pass `include_defaults=False` for exact-dir control.
- In tests, unpinned configs make `watch_dirs()` fall back to REAL home
  folders and scan real media (has caused pytest hangs).

### gotohp binary
- gotohp v0.11.0 ships NO Linux arm64 CLI asset; the manifest is
  amd64-only. Never invent an arm64 checksum; the release includes
  SHA256SUMS.txt.
- The pinned SHA-256 map is keyed by release ASSET name
  (`gotohp-cli_amd64`), not the installed binary name (`gotohp-cli`).

### Tooling / environment
- pyright/LSP diagnostics about watchdog imports and some `_run_bg`
  callable types are stale tooling noise; ruff + pytest are the gates.
- Live GitHub asset downloads can be throttled/timing out in the dev
  sandbox; the download path is covered by httpx MockTransport tests.

**Assumptions to avoid:**
- Never assume a test config is already pinned to tmp dirs.
- Never assume the binary is on PATH — it lives in the app's XDG data
  dir, deliberately not on PATH.
- Never assume `retry_add` increments (it sets an absolute count).

---

## 11. Implementation Notes

- Default watch policy is NARROW BY DEFAULT: `default_watch_paths()` is
  `~/Pictures` ONLY. The original 11-folder default list backfilled too
  much (user's Screenshots were auto-uploaded); extra folders are always
  explicit. The Folders tab shows the EFFECTIVE list (explicit dirs plus
  defaults suffixed "(default)") with a Switch for `include_defaults`;
  Remove only works on explicit dirs.
- The Dashboard is a summary (engine status, stats, watching, last
  event). The full event stream lives only in the Logs tab.
- `UnliApp` builds/starts the engine on mount after `precheck()`; `s`
  start/stop runs in a background thread and reports a pending state.
- Binary install location: `~/.local/share/unli-photos/bin/gotohp-cli`
  (XDG data dir), not on PATH.
- `precheck()` = binary present AND creds non-empty; headless refuses to
  start a broken setup but TUI surfaces problems in the Setup tab.

---

## 12. Repository Map

**Important Directories:**
| Directory | Purpose |
|---|---|
| `src/unli_photos/engine/` | SyncEngine: watcher, debouncer, dedup, workers |
| `src/unli_photos/photos/` | gotohp subprocess wrappers (upload/creds/binary/service) |
| `src/unli_photos/storage/` | SQLite repository |
| `src/unli_photos/tui/` | Textual frontend |
| `tests/` | pytest suites mirroring the package |

**Important Files:**
| File | Purpose |
|---|---|
| `src/unli_photos/cli.py` | CLI entry point (TUI / --headless / --once / --version), `precheck()` |
| `src/unli_photos/__main__.py` | `python -m unli_photos` entry |
| `src/unli_photos/tui/app.py` | Textual app (all tabs, thread bridge, test seams) |
| `src/unli_photos/engine/engine.py` | The sync engine (the model) |
| `src/unli_photos/config.py` | AppConfig/Paths/TOML + default watch dirs |

**Generated — never edit manually:**
| Path | Type (file/dir) | Regenerated by |
|---|---|---|
| (none) | | |

---

## 13. Development Commands

```bash
# Development
uv sync --extra dev      # install deps + dev tools
uv run up --version       # smoke check
uv run up                 # TUI (dev)

# Build
uv build
```

**Verification commands** — run before claiming a task is done:

```bash
# Tests
uv run pytest

# Lint
uv run ruff check .
```

**Common debugging:**

```bash
# Inspect local state (config/db in XDG dirs)
cat ~/.config/unli-photos/config.toml
~/.local/share/unli-photos/bin/gotohp-cli creds list

# Service
systemctl --user status unli-photos.service
```

---

## 14. External Services

| Service | Purpose |
|---|---|
| Google Photos (via gotohp-cli) | Actual upload target; auth + device API handled upstream |
| GitHub Releases (gotohp repo) | Binary download source, pinned v0.11.0 + SHA256SUMS.txt |

**Secrets Location:** Google oauth token lives in gotohp's own config
(`gotohp.config`, chmod 600, inside the app's XDG config dir). No
secrets in this repo, no `.env`.

Configuration notes: `gotohp-cli` is invoked with `--config` pointing
at that file; the app hardens permissions after every creds call.

---

## 15. AI Collaboration Notes

**Implementation Style:**
- Prefer incremental changes over rewrites.
- Explain architectural changes before implementing them.
- Minimize unnecessary abstractions.
- Match existing code style.
- If introducing a new dependency, briefly justify why the existing
  stack was insufficient.

**Communication:**
- Ask before making architectural decisions.
- Present trade-offs when multiple approaches are reasonable.
- State assumptions explicitly.
- The project owner prefers end-to-end execution: "proceed, tap me only
  when it's done" — report after verification, not between steps.

**Learning Preference:**
Experienced developer; values correctness, security parity, and honest
verification (tests + lint) over quick demos.

---

## 16. Known Limitations

- Linux-only by design (systemd user units, XDG paths).
- gotohp v0.11.0 provides an amd64-only CLI asset; no arm64 support is
  possible until upstream ships one.
- Single active Google account at a time (gotohp model).
- Dedup means deleting a photo in Google Photos will not re-upload it
  while the local record exists; use force re-upload (Shift+R) for a
  true re-upload.

---

## Maintenance Rules

> Decisions are append-only (see §8). Never delete a decision —
> supersede or deprecate it instead.

Umbrella rule: never update this file for implementation work unless it
changes durable project knowledge. (A bug fix is usually just a bug fix
— but if fixing it revealed "the checksum map is keyed by asset name,"
that's a gotcha, and it belongs.)

Before touching this file, run through this checklist. If every answer
is "No," leave CONTEXT.md untouched.

- Did the architecture or a design decision change?
- Did project philosophy or a non-goal change?
- Did an architecture rule change (added, removed, or relaxed)?
- Did a coding convention change project-wide?
- Did domain knowledge get discovered or clarified?
- Did a new gotcha appear, or a wrong assumption get exposed?
- Did an implementation note change (something now mid-flight, or now
  resolved)?
- Did the stack, milestone, long-term direction, or external services
  change?
- Did a permanent limitation get added or lifted?

**Never update this file for:**
- commits
- completed tasks
- bug fixes
- refactors
- git history
- branch changes
- session summaries
- TODO lists
- transient blockers (issue tracker's job)

Git already records all of those.
