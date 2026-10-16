import hashlib
import stat
from pathlib import Path

import httpx
import pytest

from unli_photos.config import Paths
from unli_photos.photos.binary import (
    BinaryError,
    download,
    find_binary,
    target_path,
    verify,
)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    return Paths(
        config_dir=config_dir,
        config_file=config_dir / "config.toml",
        state_dir=state_dir,
        db_path=state_dir / "state.db",
        bin_dir=state_dir / "bin",
        gotophp_config=config_dir / "gotohp.config",
    )


def test_verify_missing(paths: Paths):
    assert verify(paths) == "BIN_MISSING"


def test_verify_ok(paths: Paths):
    target = target_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"good binary bytes"
    target.write_bytes(payload)
    assert verify(paths, sha256=hashlib.sha256(payload).hexdigest()) == "BIN_OK"


def test_verify_ok_against_pinned_manifest(paths: Paths, monkeypatch):
    """Regression: verify must key the manifest by release asset, not binary name."""
    import unli_photos.photos.binary as bm

    target = target_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"pinned release payload"
    target.write_bytes(payload)
    asset = bm.asset_for_machine()
    assert asset is not None
    monkeypatch.setitem(
        bm.EXPECTED_SHA256, asset, hashlib.sha256(payload).hexdigest()
    )
    assert verify(paths) == "BIN_OK"


def test_verify_corrupt_payload(paths: Paths):
    target = target_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"tampered bytes")
    corrupted = verify(
        paths, sha256=hashlib.sha256(b"expected bytes").hexdigest()
    )
    assert corrupted == "BIN_CORRUPT"


def test_find_binary_bin_dir_first(paths: Paths):
    target = target_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    target.chmod(0o700)
    assert find_binary(paths) == target


def test_find_binary_path_fallback(paths: Paths, monkeypatch, tmp_path: Path):
    fake = tmp_path / "gotohp-cli"
    fake.touch()
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_binary(paths) == fake


def test_find_binary_none(paths: Paths, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert find_binary(paths) is None


def _download_transport(payload: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Length": str(len(payload))},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_streams_and_hashes(paths: Paths):
    payload = b"gotohp-cli-bytes" * 1000
    sha = hashlib.sha256(payload).hexdigest()
    client = _download_transport(payload)
    progress: list[tuple[int, int | None]] = []
    out = download(
        paths,
        client=client,
        sha256=sha,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert out == target_path(paths)
    assert out.read_bytes() == payload
    assert stat.S_IMODE(out.stat().st_mode) & 0o700 == 0o700
    assert progress[-1] == (len(payload), len(payload))
    assert not list(paths.bin_dir.glob("*.download"))  # tmp cleaned


def test_download_rejects_bad_hash(paths: Paths):
    client = _download_transport(b"tampered")
    sha = hashlib.sha256(b"different").hexdigest()
    with pytest.raises(BinaryError):
        download(paths, client=client, sha256=sha)
    assert not target_path(paths).exists()


def test_download_unsupported_arch(paths: Paths, monkeypatch):
    monkeypatch.setattr(
        "unli_photos.photos.binary.asset_for_machine", lambda: None
    )
    client = _download_transport(b"x")
    with pytest.raises(BinaryError, match="architecture"):
        download(paths, client=client, sha256=hashlib.sha256(b"x").hexdigest())
