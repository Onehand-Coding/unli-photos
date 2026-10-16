"""File readiness gate: size stability plus optional writer-lock probe."""

from __future__ import annotations

import enum
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


class Readiness(enum.Enum):
    READY = enum.auto()
    NOT_FOUND = enum.auto()
    NOT_READY = enum.auto()


LOCK_TOOLS = ("lsof", "fuser")


def _probe_locked(path: Path) -> bool:
    """Best-effort writer-lock check via lsof/fuser; absent tools mean unlocked."""
    for tool in LOCK_TOOLS:
        binary = _which(tool)
        if binary is None:
            continue
        try:
            proc = subprocess.run(
                [binary, str(path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode == 0:
            return True
        return False
    return False


def _which(tool: str) -> str | None:
    from shutil import which

    return which(tool)


def wait_for_file_ready(
    path: Path,
    *,
    sample_interval: float = 1.0,
    stable_samples: int = 2,
    max_samples: int = 30,
    lock_probe: bool = True,
    lock_probe_fn: Callable[[Path], bool] | None = None,
) -> Readiness:
    """Wait until the file exists non-empty and stops changing (then upload).

    Mirrors the original watcher: 0-byte placeholders wait, growing files reset the
    stability counter, and an optional writer-lock probe delays release-locked files.
    Returning NOT_READY means "still downloading/locked": caller re-queues without
    penalty. NOT_FOUND means the path vanished.
    """
    probe = _probe_locked if lock_probe_fn is None else lock_probe_fn
    last_size = -1
    stable = 0
    for _ in range(max(1, int(max_samples))):
        try:
            size = path.stat().st_size
        except OSError:
            return Readiness.NOT_FOUND
        if size <= 0:
            stable = 0
            last_size = -1
            time.sleep(sample_interval)
            continue
        if size != last_size:
            last_size = size
            stable = 1
        else:
            stable += 1
        if stable >= max(1, int(stable_samples)):
            if lock_probe and probe(path):
                time.sleep(sample_interval)
                continue
            return Readiness.READY
        time.sleep(sample_interval)
    return Readiness.NOT_READY
