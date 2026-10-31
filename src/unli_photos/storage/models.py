"""Serializable records persisted by the storage layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UploadRecord:
    path: str
    media_key: str | None
    file_name: str
    file_size: int
    content_hash: str
    uploaded_at: str


@dataclass(frozen=True)
class RetryRecord:
    path: str
    attempts: int
    last_attempt: str


@dataclass(frozen=True)
class FailureRecord:
    """A file parked after exhausting its upload attempts.

    Size and mtime identify the exact file state that failed. A later write
    invalidates the record so the engine retries the changed file.
    """

    path: str
    file_size: int
    mtime_ns: int
    attempts: int
    last_error: str
    failed_at: str


@dataclass(frozen=True)
class LogEntry:
    id: int
    timestamp: str
    level: str
    message: str
