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
class LogEntry:
    id: int
    timestamp: str
    level: str
    message: str
