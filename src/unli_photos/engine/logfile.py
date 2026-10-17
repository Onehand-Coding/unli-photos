"""Shared cross-process event log: JSONL sink for owned engines, tail for observers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from unli_photos.engine.events import EngineEvent, to_json

LOG_NAME = "events.log"
_TRUNCATE_AT = 2_000_000
_KEEP_TAIL = 262_144
ATTACH_HISTORY = 65_536  # on tail start, deliver at most this much backlog


def log_path(state_dir: Path) -> Path:
    return state_dir / LOG_NAME


def truncate_if_huge(
    path: Path, at: int = _TRUNCATE_AT, keep: int = _KEEP_TAIL
) -> None:
    """Bound the log: once it passes `at`, drop everything but the last `keep`."""
    try:
        if path.stat().st_size <= at:
            return
    except OSError:
        return
    try:
        with path.open("rb") as fh:
            fh.seek(-keep, 2)
            tail = fh.read()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(tail)
        tmp.replace(path)
    except OSError:
        return


class FileEventSink:
    """Engine listener that appends JSON-serialized events (thread-safe).

    Only the flock-owning engine writes; observers read via FileTail.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        truncate_if_huge(self._path)

    def __call__(self, event: EngineEvent) -> None:
        line = to_json(event)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.write("\n")
            except OSError:
                pass


class FileTail:
    """Background poller that delivers new lines of a growing log file."""

    def __init__(
        self,
        path: Path,
        on_line: Callable[[str], None],
        poll: float = 0.5,
    ) -> None:
        self._path = path
        self._on_line = on_line
        self._poll = poll
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="unli-tail"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        position = max(0, self._size() - ATTACH_HISTORY)
        while not self._stop_event.is_set():
            position = self._drain(position)
            self._stop_event.wait(self._poll)

    def _size(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def _drain(self, position: int) -> int:
        if position > self._size():  # file replaced/truncated: start over
            position = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                fh.seek(position)
                data = fh.read()
                position = fh.tell()
        except OSError:
            return position
        for line in data.splitlines():
            stripped = line.strip()
            if stripped:
                self._on_line(stripped)
        return position
