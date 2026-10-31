"""Upload seam between the sync engine and the actual upload backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class UploadResult:
    """Terminal state of a single upload attempt.

    `permanent` marks a backend rejection that will not change by retrying the
    same bytes (for example, an invalid or truncated image). Transient errors
    leave it False so the safety sweep can try again later.
    """

    ok: bool
    media_key: str | None = None
    album_key: str | None = None
    error: str | None = None
    permanent: bool = False


@runtime_checkable
class Uploader(Protocol):
    """Any backend able to upload one file to Google Photos."""

    def upload(
        self, path: Path, *, force: bool = False, album: str | None = None
    ) -> UploadResult: ...
