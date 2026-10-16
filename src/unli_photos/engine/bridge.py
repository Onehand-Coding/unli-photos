"""watchdog event bridge: filtered, debounced events feed the sync engine queue."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)

from unli_photos.engine.debounce import Debouncer

WATCHED_EVENTS = (FileCreatedEvent, FileModifiedEvent, FileMovedEvent)


class MediaEventHandler(FileSystemEventHandler):
    """Debounces created/modified/moved file events and forwards upload candidates.

    Hidden (dot) files are ignored, mirroring the original Linux watcher's inotify
    filters; destination-only handling on moves prevents double-scan uploads.
    """

    def __init__(
        self,
        forward: Callable[[Path], None],
        debouncer: Debouncer,
        max_entries: int = 100,
    ) -> None:
        super().__init__()
        self._forward = forward
        self._debouncer = debouncer
        self._max_entries = max_entries

    def on_created(self, event: FileSystemEvent) -> None:
        self._consider(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._consider(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileMovedEvent):
            self._consider(event.dest_path)

    def _consider(self, src: str | bytes) -> None:
        if isinstance(src, bytes):
            src = os.fsdecode(src)
        path = Path(src)
        if path.name.startswith("."):
            return
        self._maybe_forward(path)

    def _maybe_forward(self, path: Path) -> None:
        if not path.is_file():
            return
        if not self._debouncer.ready(path):
            return
        self._forward(path)
        self._debouncer.prune()
