"""Thread-safe filesystem event debouncer (parity with the original 1.5s window)."""

from __future__ import annotations

import threading
import time
from pathlib import Path


class Debouncer:
    """Accepts the first event for a path; rejects repeats inside the window."""

    def __init__(self, window_s: float, max_entries: int = 100) -> None:
        self._window = max(window_s, 0.0)
        self._max_entries = max_entries
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def ready(self, path: Path | str, now: float | None = None) -> bool:
        """True when the path may be processed now (last accept older than window).

        Timestamps are only refreshed on accepted events, so repeat events inside
        the window never extend the hold (original PowerShell semantics).
        """
        key = str(path)
        time_now = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last.get(key)
            if previous is not None and (time_now - previous) < self._window:
                return False
            self._last[key] = time_now
        return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._last)

    def prune(self, max_entries: int | None = None, now: float | None = None) -> None:
        """Drop stale entries once the map grows past max_entries (original pacing)."""
        threshold = self._max_entries if max_entries is None else max_entries
        time_now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._last) <= threshold:
                return
            horizon = max(self._window, 60.0)
            self._last = {
                key: stamp
                for key, stamp in self._last.items()
                if (time_now - stamp) < horizon
            }
