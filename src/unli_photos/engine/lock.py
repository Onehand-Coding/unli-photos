"""Single-instance guard: flock-based lock on the XDG state directory.

Any engine start (TUI, headless, --once) acquires the lock; a second engine
anywhere on the machine - including across uv-tool and dev-checkout installs,
which share XDG dirs - refuses to start until the first one stops. The kernel
releases the lock if the process dies, so crashes leave no stale state.
"""

from __future__ import annotations

import fcntl
import os
from datetime import datetime
from pathlib import Path


class EngineLocked(RuntimeError):
    """Raised when another process already holds the engine lock."""

    def __init__(self, content: str) -> None:
        self.holder = content
        super().__init__(f"another engine is running: {content}")


def read_holder(path: Path) -> str:
    try:
        text = path.read_text().strip()
    except OSError:
        return "?"
    return text.split("\n", 1)[0] or "?"


def lock_path_for(db_path: Path) -> Path:
    return db_path.parent / "engine.lock"


class EngineLock:
    """Exclusive advisory lock guarding engine lifetime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        holder = read_holder(self._path)
        handle = open(self._path, "w", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise EngineLocked(holder) from None
        self._handle = handle
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"{os.getpid()} (started {stamp})\n")
        handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
