from pathlib import Path

import pytest

from unli_photos.engine.dedup import needs_upload, sha256_file
from unli_photos.storage.repo import Repo


@pytest.fixture
def repo(tmp_path: Path):
    r = Repo.open(tmp_path / "state.db")
    yield r
    r.close()


def _mkfile(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_sha256_file(tmp_path: Path):
    p = _mkfile(tmp_path, "a.bin", b"hello world")
    import hashlib

    assert sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_fresh_file_needs_upload(tmp_path: Path, repo: Repo):
    p = _mkfile(tmp_path, "IMG_1.jpg", b"data")
    assert needs_upload(p, repo) is True


def test_recorded_unchanged_needs_no_upload(tmp_path: Path, repo: Repo):
    p = _mkfile(tmp_path, "IMG_1.jpg", b"data")
    h = sha256_file(p)
    repo.record_upload(str(p), media_key="k", file_name=p.name, file_size=4, content_hash=h)
    assert needs_upload(p, repo) is False


def test_size_change_means_upload(tmp_path: Path, repo: Repo):
    p = _mkfile(tmp_path, "IMG_1.jpg", b"data")
    h = sha256_file(p)
    repo.record_upload(str(p), media_key="k", file_name=p.name, file_size=4, content_hash=h)
    p.write_bytes(b"data-more")  # modified in place
    assert needs_upload(p, repo) is True


def test_hash_change_same_size_means_upload(tmp_path: Path, repo: Repo):
    p = _mkfile(tmp_path, "IMG_1.jpg", b"aaaa")
    h = sha256_file(p)
    repo.record_upload(str(p), file_name=p.name, file_size=4, content_hash=h)
    p.write_bytes(b"bbbb")  # same size, different content
    assert needs_upload(p, repo) is True


def test_relocated_same_content_rerecorded(tmp_path: Path, repo: Repo):
    orig = _mkfile(tmp_path, "IMG_1.jpg", b"data")
    h = sha256_file(orig)
    repo.record_upload(str(orig), media_key="k", file_name="IMG_1.jpg", file_size=4, content_hash=h)
    clone = tmp_path / "moved" / "IMG_1.jpg"
    clone.parent.mkdir()
    clone.write_bytes(b"data")
    assert needs_upload(clone, repo) is False
    assert repo.exact_record(str(clone)) == (4, h)  # relocation recorded under new path


def test_relocated_different_content_uploads(tmp_path: Path, repo: Repo):
    orig = _mkfile(tmp_path, "IMG_1.jpg", b"data")
    h = sha256_file(orig)
    repo.record_upload(str(orig), file_name="IMG_1.jpg", file_size=4, content_hash=h)
    twin = _mkfile(tmp_path, "IMG_1.jpg", b"other!e")  # same name+size(4), different bytes
    assert needs_upload(twin, repo) is True


def test_zero_size_still_uploadable(tmp_path: Path, repo: Repo):
    p = _mkfile(tmp_path, "empty.jpg", b"")
    assert needs_upload(p, repo) is True
