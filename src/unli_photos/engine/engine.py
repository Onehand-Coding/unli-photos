"""The sync engine: watch, scan, dedup, readiness, upload, retry, sweep."""

from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from watchdog.observers import Observer

from unli_photos.config import AppConfig
from unli_photos.engine.bridge import MediaEventHandler
from unli_photos.engine.debounce import Debouncer
from unli_photos.engine.dedup import needs_upload, sha256_file
from unli_photos.engine.events import (
    EngineEvent,
    EngineStarted,
    EngineStopped,
    FileDetected,
    LogMessage,
    RescanDone,
    RescanStarted,
    UploadFailed,
    UploadSkipped,
    UploadStarted,
    UploadSucceeded,
)
from unli_photos.engine.lock import EngineLock, lock_path_for
from unli_photos.engine.readiness import Readiness, wait_for_file_ready
from unli_photos.engine.uploader import Uploader, UploadResult
from unli_photos.media import format_size, is_media_file
from unli_photos.storage.repo import Repo

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class EngineIntervals:
    """Tunable timings (defaults mirror the original scripts)."""

    debounce: float = 1.5          # fs event debounce window (seconds)
    readiness_sample: float = 1.0  # readiness poll spacing
    readiness_stable: int = 2      # samples of unchanged size before upload
    readiness_max: int = 30        # poll cap before NOT_READY
    retry_flush: float = 30.0      # enqueue retry queue every N seconds
    sweep: float = 900.0           # safety sweep every N seconds


_STOP = object()          # worker shutdown sentinel
_RESCAN = object()        # catch-up rescan sentinel (dedup-aware)
_FORCE_RESCAN = object()  # force re-upload sentinel (bypasses dedup)


class SyncEngine:
    """Single-process sync orchestrator. Frontends attach to the event stream.

    Threading model (Approach A): one upload worker, one monitor (retry flush +
    safety sweep), one watchdog observer thread. The engine never imports the TUI.
    """

    def __init__(
        self,
        *,
        repo: Repo,
        uploader: Uploader,
        config_fn: Callable[[], AppConfig],
        listener: Callable[[EngineEvent], None] | None = None,
        intervals: EngineIntervals | None = None,
    ) -> None:
        self._repo = repo
        self._uploader = uploader
        self._config_fn = config_fn
        self._listener = listener
        self._iv = intervals or EngineIntervals()
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._observer: Observer | None = None
        self._worker: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._engine_started = False
        self._lock_path = lock_path_for(repo.db_path or Path("state.db"))
        self._instance_lock = EngineLock(self._lock_path)

    # -- event plumbing ------------------------------------------------------

    def add_listener(self, fn: Callable[[EngineEvent], None]) -> None:
        self._listener = fn

    def _emit(self, event: EngineEvent) -> None:
        fn = self._listener
        if fn is None:
            return
        with contextlib.suppress(Exception):
            fn(event)

    def _log(self, level: str, message: str) -> None:
        self._repo.log(level, message)
        self._emit(LogMessage(level=level, message=message))

    # -- dirs ----------------------------------------------------------------

    def watch_dirs(self) -> list[Path]:
        """Resolved watch list: config dirs then defaults; fallback to ~/Pictures."""
        config = self._config_fn()
        candidates: list[Path] = []
        for raw in config.dirs:
            expanded = os.path.expandvars(os.path.expanduser(raw))
            candidates.append(Path(expanded))
        if config.include_defaults:
            from unli_photos.config import default_watch_paths

            candidates.extend(default_watch_paths())
        resolved: list[Path] = []
        seen: set[str] = set()
        for d in candidates:
            key = str(d)
            if key in seen:
                continue
            seen.add(key)
            if d.is_dir():
                resolved.append(d)
        if not resolved:
            fallback = Path.home() / "Pictures"
            fallback.mkdir(parents=True, exist_ok=True)
            resolved.append(fallback)
        return resolved

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._engine_started:
                return
        self._instance_lock.acquire()  # raises EngineLocked if another engine runs
        try:
            with self._lock:
                self._engine_started = True
            self._stop_event.clear()
            self._repo.prune_logs()
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="unli-upload"
            )
            self._worker.start()
            dirs = self.watch_dirs()
            self._start_observer(dirs)
            self._monitor = threading.Thread(
                target=self._monitor_loop, daemon=True, name="unli-monitor"
            )
            self._monitor.start()
            self._emit(EngineStarted(watch_dirs=tuple(str(d) for d in dirs)))
            # Phase 1: startup scan + pre-existing retry queue, each candidate once
            # (fresh failures are picked up by the monitor's next flush, original parity).
            for path in self._startup_candidates(dirs):
                self.enqueue(path)
        except BaseException:
            self._instance_lock.release()
            with self._lock:
                self._engine_started = False
            raise

    def stop(self) -> None:
        with self._lock:
            if not self._engine_started:
                self._instance_lock.release()
                return
            self._engine_started = False
        self._stop_event.set()
        self._queue.put(_STOP)
        if self._worker is not None:
            self._worker.join(timeout=15)
            self._worker = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        if self._monitor is not None:
            self._monitor.join(timeout=3)
            self._monitor = None
        self._instance_lock.release()
        self._emit(EngineStopped())

    @property
    def running(self) -> bool:
        return self._engine_started

    def restart_watcher(self) -> None:
        """Re-resolve watch dirs after a config change (observer restart only)."""
        if not self._engine_started:
            return
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        dirs = self.watch_dirs()
        self._start_observer(dirs)
        self._emit(EngineStarted(watch_dirs=tuple(str(d) for d in dirs)))

    def _start_observer(self, dirs: list[Path]) -> None:
        observer = Observer(timeout=0.25)
        handler = MediaEventHandler(
            forward=self.enqueue,
            debouncer=Debouncer(self._iv.debounce, max_entries=100),
        )
        for d in dirs:
            observer.schedule(handler, str(d), recursive=True)
        observer.start()
        self._observer = observer

    # -- queue ---------------------------------------------------------------

    def enqueue(self, path: Path) -> None:
        key = str(path)
        if not self._engine_started:
            return
        with self._lock:
            if key in self._active:
                return
            self._active.add(key)
        self._queue.put(path)

    def request_rescan(self, force: bool = False) -> None:
        """Queued rescan (safe from any thread while running)."""
        if self._engine_started:
            self._queue.put(_FORCE_RESCAN if force else _RESCAN)
        else:
            self.rescan_now(force=force)

    def rescan_now(self, force: bool = False) -> None:
        """Synchronous rescan (used by tests and stopped engine)."""
        if self._engine_started:
            self._queue.put(_FORCE_RESCAN if force else _RESCAN)
            return
        self._do_rescan(force=force)

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP or self._stop_event.is_set():
                break
            if item is _RESCAN:
                if self._stop_event.is_set():
                    break
                self._do_rescan()
                continue
            if item is _FORCE_RESCAN:
                if self._stop_event.is_set():
                    break
                self._do_rescan(force=True)
                continue
            path = item if isinstance(item, Path) else Path(str(item))
            try:
                self._process_candidate(path, force=False)
            finally:
                with self._lock:
                    self._active.discard(str(path))

    def _do_rescan(self, force: bool = False) -> None:
        """Rescan watch dirs inline in the worker thread.

        Default (catch-up): re-check every media file against the upload
        record; only unrecorded/changed files are processed. Force mode
        bypasses local dedup entirely (gotohp/server side skips existing).
        """
        self._emit(RescanStarted())
        candidates = self._media_candidates(self.watch_dirs())
        todo = (
            candidates
            if force
            else [p for p in candidates if needs_upload(p, self._repo)]
        )
        self._emit(RescanDone(scanned=len(candidates), queued=len(todo)))
        for path in todo:
            self._process_candidate(path, force=force)

    def _monitor_loop(self) -> None:
        next_sweep = time.monotonic() + self._iv.sweep
        while not self._stop_event.wait(self._iv.retry_flush):
            self._flush_retries()
            if time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + self._iv.sweep
                self._sweep()

    def _flush_retries(self) -> None:
        for record in self._repo.pending_retries():
            path = Path(record.path)
            if not path.is_file():
                self._repo.retry_drop(record.path)
                continue
            if self._engine_started:
                self._queue.put(path)

    def _sweep(self) -> None:
        """Safety sweep: catches files missed while paused (cheap prefilter)."""
        for path in self._media_candidates(self.watch_dirs()):
            if not self._repo.is_recorded(str(path)):
                self._log("WARN", f"Sweep caught missed file: {path.name}")
                self.enqueue(path)

    # -- scan ----------------------------------------------------------------

    def _media_candidates(self, dirs: Iterable[Path]) -> list[Path]:
        out: list[Path] = []
        for top in dirs:
            for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in sorted(filenames):
                    if fn.startswith("."):
                        continue
                    p = Path(dirpath) / fn
                    if is_media_file(p):
                        out.append(p)
        return out

    def run_once(self) -> None:
        """Synchronous scan + retry flush without watchers or timers (--once).

        Pre-existing retry entries join the scan list; files that fail during this
        pass are handled by a later pass/run, mirroring the original phase model.
        """
        self._instance_lock.acquire()
        try:
            self._stop_event.clear()
            self._repo.prune_logs()
            dirs = self.watch_dirs()
            self._emit(EngineStarted(watch_dirs=tuple(str(d) for d in dirs)))
            candidates = self._startup_candidates(dirs)
            for path in candidates:
                self._process_candidate(path, force=False)
            self._emit(EngineStopped())
        finally:
            self._instance_lock.release()

    def _startup_candidates(self, dirs: Iterable[Path]) -> list[Path]:
        """Scan candidates plus existing retry entries; only files present once."""
        out: list[Path] = self._media_candidates(dirs)
        seen = {str(p) for p in out}
        for record in self._repo.pending_retries():
            if record.path in seen:
                continue
            path = Path(record.path)
            if not path.is_file():
                self._repo.retry_drop(record.path)
                continue
            seen.add(record.path)
            out.append(path)
        return out

    # -- core pipeline -------------------------------------------------------

    def _process_candidate(self, path: Path, force: bool = False) -> None:
        if not path.is_file():
            self._repo.retry_drop(str(path))
            return
        if not is_media_file(path):
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if not force and not needs_upload(path, self._repo):
            self._emit(UploadSkipped(path=str(path), reason="already uploaded"))
            return
        self._emit(FileDetected(path=str(path), size=size))
        readiness = wait_for_file_ready(
            path,
            sample_interval=self._iv.readiness_sample,
            stable_samples=self._iv.readiness_stable,
            max_samples=self._iv.readiness_max,
            lock_probe=True,
        )
        if readiness is Readiness.NOT_FOUND:
            self._repo.retry_drop(str(path))
            return
        if readiness is Readiness.NOT_READY:
            attempts = self._repo.retry_attempts(str(path))
            self._repo.retry_add(str(path), attempts)
            self._log(
                "WARN",
                f"File still writing or locked ({path.name}) - remaining in queue.",
            )
            self._emit(UploadSkipped(path=str(path), reason="not ready"))
            return

        self._emit(UploadStarted(path=str(path), size=size))
        started = time.perf_counter()
        try:
            result = self._uploader.upload(path, force=force)
        except Exception as exc:
            result = UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        duration = time.perf_counter() - started

        if result.ok:
            content_hash = sha256_file(path)
            self._repo.record_upload(
                str(path),
                media_key=result.media_key,
                file_name=path.name,
                file_size=size,
                content_hash=content_hash,
            )
            self._repo.retry_drop(str(path))
            self._log(
                "OK",
                f"SUCCESS | {path.name} ({format_size(size)} in {duration:.1f}s)",
            )
            self._emit(
                UploadSucceeded(
                    path=str(path),
                    size=size,
                    duration=duration,
                    media_key=result.media_key,
                )
            )
            return

        attempts = self._repo.retry_attempts(str(path)) + 1
        suffix = f" ({result.error})" if result.error else ""
        if attempts >= MAX_ATTEMPTS:
            self._repo.retry_drop(str(path))
            self._log("ERROR", f"GAVE UP after {attempts} upload attempts: {path.name}{suffix}")
        else:
            self._repo.retry_add(str(path), attempts)
            self._log(
                "WARN",
                f"FAILED upload{suffix} (attempt {attempts}/{MAX_ATTEMPTS}) "
                f"- queued for retry: {path.name}",
            )
        self._emit(
            UploadFailed(
                path=str(path), duration=duration, error=result.error, attempts=attempts
            )
        )
