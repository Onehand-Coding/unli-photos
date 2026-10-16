"""SHA-256 based upload decisions (parity with the original dual-key dedup)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from unli_photos.storage.repo import Repo


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 hex digest of file contents ("" for unreadable/missing files)."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def needs_upload(path: Path, repo: Repo) -> bool:
    """False when the file is already recorded unchanged; True when it should upload.

    Order mirrors the original watcher:
    1. exact path match: size change or hash change means modified-in-place (upload);
       a legacy row without hash is backfilled and treated as unchanged.
    2. dual-key fallback (file_name+file_size): identical content hash records the
       relocated copy under the new path skips it; a hash mismatch uploads (different
       camera file sharing name+size).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return True
    name = path.name
    exact = repo.exact_record(str(path))
    if exact is not None:
        recorded_size, recorded_hash = exact
        if recorded_size > 0 and size > 0 and recorded_size != size:
            return True
        fresh = sha256_file(path)
        if recorded_hash:
            return fresh != recorded_hash
        if fresh:
            repo.backfill_hash(str(path), fresh)
        return False

    if size > 0:
        pool = repo.name_size_hashes(name, size)
        if pool:
            fresh = sha256_file(path)
            if fresh in pool:
                repo.record_upload(
                    str(path),
                    media_key=None,
                    file_name=name,
                    file_size=size,
                    content_hash=fresh,
                )
                return False
    return True
