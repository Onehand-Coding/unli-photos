"""SQLite-backed upload history, retry queue, and activity log (WAL, thread-safe)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import TracebackType

from unli_photos.storage.models import LogEntry, RetryRecord, UploadRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    path         TEXT PRIMARY KEY,
    media_key    TEXT,
    file_name    TEXT,
    file_size    INTEGER,
    content_hash TEXT,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS retry_queue (
    path         TEXT PRIMARY KEY,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    level     TEXT NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_file ON uploads(file_name, file_size);
CREATE INDEX IF NOT EXISTS idx_uploads_hash ON uploads(content_hash);
CREATE INDEX IF NOT EXISTS idx_logs_ts      ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_level   ON logs(level);
"""


class Repo:
    """Single-connection data access object. All queries are parameterized."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    @classmethod
    def open(cls, db_path: Path) -> "Repo":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.executescript(_SCHEMA)
        return cls(conn)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- uploads ------------------------------------------------------------

    def is_recorded(self, path: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM uploads WHERE path = ?", (path,)
            ).fetchone()
        return row is not None

    def exact_record(self, path: str) -> tuple[int, str | None] | None:
        """(file_size, content_hash) recorded for path, or None when unseen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT file_size, content_hash FROM uploads WHERE path = ?", (path,)
            ).fetchone()
        if row is None:
            return None
        return int(row["file_size"]), row["content_hash"]

    def record_upload(
        self,
        path: str,
        media_key: str | None = None,
        file_name: str = "",
        file_size: int = 0,
        content_hash: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO uploads
                    (path, media_key, file_name, file_size, content_hash, uploaded_at)
                VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """,
                (path, media_key, file_name, file_size, content_hash),
            )
            self._conn.execute("DELETE FROM retry_queue WHERE path = ?", (path,))

    def name_size_hashes(self, file_name: str, file_size: int) -> set[str]:
        """Hashes recorded for other paths with identical name+size (dual-key dedup)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_hash FROM uploads "
                "WHERE file_name = ? AND file_size = ? AND file_size > 0 "
                "AND content_hash IS NOT NULL AND content_hash != ''",
                (file_name, file_size),
            ).fetchall()
        return {str(r["content_hash"]) for r in rows}

    def backfill_hash(self, path: str, content_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE uploads SET content_hash = ? WHERE path = ?",
                (content_hash, path),
            )

    def recent_uploads(self, limit: int = 200) -> list[UploadRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, media_key, file_name, file_size, content_hash, uploaded_at "
                "FROM uploads ORDER BY uploaded_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            UploadRecord(
                path=r["path"],
                media_key=r["media_key"],
                file_name=r["file_name"],
                file_size=int(r["file_size"] or 0),
                content_hash=r["content_hash"] or "",
                uploaded_at=r["uploaded_at"],
            )
            for r in rows
        ]

    def stats(self) -> dict[str, int]:
        with self._lock:
            uploads = self._conn.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"]
            retries = self._conn.execute("SELECT COUNT(*) AS c FROM retry_queue").fetchone()["c"]
        return {"uploaded": int(uploads), "retries": int(retries)}

    # -- retries ------------------------------------------------------------

    def retry_attempts(self, path: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM retry_queue WHERE path = ?", (path,)
            ).fetchone()
        return 0 if row is None else int(row["attempts"])

    def retry_add(self, path: str, attempts: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO retry_queue (path, attempts, last_attempt)
                VALUES (?, ?, datetime('now', 'localtime'))
                """,
                (path, attempts),
            )

    def retry_drop(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM retry_queue WHERE path = ?", (path,))

    def pending_retries(self) -> list[RetryRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, attempts, last_attempt FROM retry_queue ORDER BY path"
            ).fetchall()
        return [
            RetryRecord(path=r["path"], attempts=int(r["attempts"]), last_attempt=r["last_attempt"] or "")
            for r in rows
        ]

    # -- logs ---------------------------------------------------------------

    def log(self, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (level, message) VALUES (?, ?)", (level, message)
            )

    def recent_logs(self, limit: int = 200) -> list[LogEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, level, message FROM logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            LogEntry(
                id=int(r["id"]),
                timestamp=r["timestamp"],
                level=r["level"],
                message=r["message"],
            )
            for r in rows
        ]

    def prune_logs(self, days: int = 30) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM logs WHERE timestamp < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
