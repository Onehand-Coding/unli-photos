import threading
import time
from pathlib import Path

from unli_photos.engine.readiness import Readiness, wait_for_file_ready


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def test_ready_file(tmp_path: Path):
    p = tmp_path / "ok.jpg"
    _write(p, b"abc")
    assert wait_for_file_ready(
        p, sample_interval=0.01, stable_samples=2, max_samples=10, lock_probe=False
    ) is Readiness.READY


def test_missing_file(tmp_path: Path):
    assert wait_for_file_ready(
        tmp_path / "gone.jpg", sample_interval=0.01, stable_samples=2, max_samples=10,
        lock_probe=False,
    ) is Readiness.NOT_FOUND


def test_zero_byte_placeholder_waits(tmp_path: Path):
    p = tmp_path / "empty.jpg"
    p.touch()
    # writer never fills it: NOT_READY with tiny max samples
    result = wait_for_file_ready(
        p, sample_interval=0.02, stable_samples=2, max_samples=3, lock_probe=False
    )
    assert result is Readiness.NOT_READY


def test_growing_file_never_ready(tmp_path: Path):
    p = tmp_path / "growth.jpg"
    p.write_bytes(b"start")
    stop = threading.Event()

    def grow():
        i = 0
        while not stop.is_set():
            time.sleep(0.01)
            i += 1
            with p.open("ab") as f:
                f.write(b"x" * 10)

    t = threading.Thread(target=grow, daemon=True)
    t.start()
    try:
        result = wait_for_file_ready(
            p, sample_interval=0.05, stable_samples=2, max_samples=6, lock_probe=False
        )
    finally:
        stop.set()
        t.join()
    assert result is Readiness.NOT_READY


def test_locked_file_not_ready(tmp_path: Path):
    p = tmp_path / "locked.jpg"
    _write(p, b"data")
    # injected lock probe: always reports a writer holding the file
    assert wait_for_file_ready(
        p, sample_interval=0.02, stable_samples=2, max_samples=3, lock_probe=True,
        lock_probe_fn=lambda _p: True,
    ) is Readiness.NOT_READY


def test_probe_false_means_ready(tmp_path: Path):
    p = tmp_path / "free.jpg"
    _write(p, b"data")
    assert wait_for_file_ready(
        p, sample_interval=0.02, stable_samples=2, max_samples=10, lock_probe=True,
        lock_probe_fn=lambda _p: False,
    ) is Readiness.READY
