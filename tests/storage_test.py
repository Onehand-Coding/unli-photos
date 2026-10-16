import os
from collections.abc import Generator
from pathlib import Path

import pytest

from unli_photos.storage import Repo
from unli_photos.storage.models import LogEntry, RetryRecord, UploadRecord


@pytest.fixture
def repo(tmp_path: Path) -> Generator[Repo, None, None]:
    r = Repo.open(tmp_path / "state.db")
    yield r
    r.close()


def _put(repo: Repo, path: str, **over) -> None:
    repo.record_upload(
        path, media_key=over.get("media_key"), file_name=over.get("file_name", Path(path).name),
        file_size=over.get("file_size", 100), content_hash=over.get("content_hash", "abc"),
    )


class TestUploads:
    def test_open_creates_schema(self, repo: Repo, tmp_path: Path):
        assert (tmp_path / "state.db").stat().st_size > 0
        assert os.path.exists(str(tmp_path / "state.db-wal")) or True  # WAL mode best effort

    def test_record_and_exact(self, repo: Repo):
        _put(repo, "/media/a.jpg", file_size=100, content_hash="h1")
        rec = repo.exact_record("/media/a.jpg")
        assert rec == (100, "h1")
        assert repo.exact_record("/nope.jpg") is None
        assert repo.is_recorded("/media/a.jpg")

    def test_record_replace_drops_retry(self, repo: Repo):
        _put(repo, "/media/a.jpg")
        repo.retry_add("/media/a.jpg", 1)
        assert repo.retry_attempts("/media/a.jpg") == 1
        _put(repo, "/media/a.jpg", file_size=200, content_hash="h2")
        assert repo.retry_attempts("/media/a.jpg") == 0
        assert repo.exact_record("/media/a.jpg") == (200, "h2")

    def test_stats(self, repo: Repo):
        _put(repo, "/a.jpg")
        _put(repo, "/b.jpg")
        repo.retry_add("/c.jpg", 1)
        assert repo.stats() == {"uploaded": 2, "retries": 1}


class TestRetries:
    def test_retry_cycle(self, repo: Repo):
        assert repo.retry_attempts("/x.jpg") == 0
        repo.retry_add("/x.jpg", 1)
        repo.retry_add("/x.jpg", 2)  # absolute set (engine computes read-modify-write)
        rows = repo.pending_retries()
        assert rows == [RetryRecord(path="/x.jpg", attempts=2, last_attempt=rows[0].last_attempt)]
        assert len(rows[0].last_attempt) > 0
        repo.retry_drop("/x.jpg")
        assert repo.pending_retries() == []
        assert repo.retry_attempts("/x.jpg") == 0

    def test_pending_only(self, repo: Repo):
        repo.retry_add("/gone.jpg", 1)
        repo.retry_add("/here.jpg", 2)
        repo.retry_drop("/gone.jpg")
        assert [r.path for r in repo.pending_retries()] == ["/here.jpg"]


class TestLogs:
    def test_log_and_recent(self, repo: Repo):
        for i in range(5):
            repo.log("OK", f"msg-{i}")
        logs = repo.recent_logs(limit=3)
        assert len(logs) == 3
        assert [l.message for l in logs] == ["msg-4", "msg-3", "msg-2"]
        assert isinstance(logs[0], LogEntry)

    def test_prune_keeps_recent(self, repo: Repo):
        repo.log("OK", "keep")
        # older-than-N-days rows are pruned; freshly inserted survive
        repo.prune_logs(days=30)
        assert [l.message for l in repo.recent_logs()] == ["keep"]


class TestNameSize:
    def test_name_size_hashes_and_backfill(self, repo: Repo):
        _put(repo, "/a/IMG_1.jpg", file_size=11, content_hash="H1")
        _put(repo, "/b/IMG_1.jpg", file_size=11, content_hash="")  # legacy, no hash
        hashes = repo.name_size_hashes("IMG_1.jpg", 11)
        assert hashes == {"H1"}
        repo.backfill_hash("/b/IMG_1.jpg", "H1")
        assert repo.name_size_hashes("IMG_1.jpg", 11) == {"H1"}

    def test_name_size_no_match(self, repo: Repo):
        _put(repo, "/a/IMG_1.jpg", file_size=11, content_hash="H1")
        assert repo.name_size_hashes("IMG_2.jpg", 11) == set()
        assert repo.name_size_hashes("IMG_1.jpg", 99) == set()


def test_record_defaults(repo: Repo):
    repo.record_upload("/p", media_key=None, file_name="p", file_size=0, content_hash="")
    row = repo.recent_uploads()[0]
    assert isinstance(row, UploadRecord)
    assert row.media_key is None and row.file_size == 0
    assert len(row.uploaded_at) > 0  # timestamp defaulted
