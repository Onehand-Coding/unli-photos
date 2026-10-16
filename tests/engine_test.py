import time
from pathlib import Path
from typing import Any

import pytest

from unli_photos.config import AppConfig
from unli_photos.engine.engine import EngineIntervals, SyncEngine
from unli_photos.engine.events import (
    EngineStarted,
    EngineStopped,
    LogMessage,
    RescanStarted,
    UploadSkipped,
    UploadSucceeded,
)
from unli_photos.engine.uploader import UploadResult
from unli_photos.storage.repo import Repo


class FakeUploader:
    """Uploader test double: scripted results per path plus a default."""

    def __init__(self, default: UploadResult | None = None) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.results: dict[str, UploadResult] = {}
        self.default = default or UploadResult(ok=True, media_key="mk-1")

    def upload(self, path: Path, *, force: bool = False) -> UploadResult:
        self.calls.append((str(path), force))
        return self.results.get(str(path), self.default)


FAST = EngineIntervals(
    debounce=0.02,
    readiness_sample=0.01,
    readiness_stable=2,
    readiness_max=10,
    retry_flush=0.05,
    sweep=60.0,
)

NEVER_READY = EngineIntervals(
    debounce=0.02,
    readiness_sample=0.01,
    readiness_stable=2,
    readiness_max=1,
    retry_flush=0.05,
    sweep=60.0,
)


@pytest.fixture
def watch(tmp_path: Path) -> Path:
    d = tmp_path / "watch"
    d.mkdir()
    return d


@pytest.fixture
def repo(tmp_path: Path):
    r = Repo.open(tmp_path / "state.db")
    yield r
    r.close()


def make_engine(repo, uploader, watch, intervals=FAST) -> tuple[SyncEngine, list[Any]]:
    events: list[Any] = []
    cfg = AppConfig(dirs=(str(watch),), include_defaults=False)
    eng = SyncEngine(
        repo=repo,
        uploader=uploader,
        config_fn=lambda: cfg,
        listener=events.append,
        intervals=intervals,
    )
    return eng, events


class TestRunOnce:
    def test_fresh_scan_uploads_media_only(
        self, watch: Path, repo: Repo, tmp_path: Path
    ):
        (watch / "IMG_1.jpg").write_bytes(b"photo")
        (watch / "notes.txt").write_bytes(b"not media")
        (watch / ".hidden.jpg").write_bytes(b"secret")
        deep = watch / "sub"
        deep.mkdir()
        (deep / "clip.mov").write_bytes(b"video")

        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()

        assert sorted(u.file_name for u in repo.recent_uploads()) == ["IMG_1.jpg", "clip.mov"]
        succ = [e for e in evs if isinstance(e, UploadSucceeded)]
        assert {e.path.split("/")[-1] for e in succ} == {"IMG_1.jpg", "clip.mov"}
        assert all(f[1] is False for f in fake.calls)

    def test_second_scan_skips_recorded(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()
        assert [e for e in evs if isinstance(e, UploadSucceeded)]
        fake.calls.clear()
        eng.run_once()
        assert fake.calls == []
        skips = [e for e in evs if isinstance(e, UploadSkipped)]
        assert any("already uploaded" in s.reason for s in skips)

    def test_started_stopped_events(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()
        assert isinstance(evs[0], EngineStarted)
        assert any(isinstance(e, EngineStopped) for e in evs)
        started = evs[0]
        assert started.watch_dirs == (str(watch),)


class TestRetry:
    def test_failure_then_giveup_after_three(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader(default=UploadResult(ok=False, error="boom"))
        eng, _ = make_engine(repo, fake, watch)

        eng.run_once()
        assert repo.retry_attempts(str(p)) == 1
        eng.run_once()
        assert repo.retry_attempts(str(p)) == 2
        eng.run_once()
        assert repo.retry_attempts(str(p)) == 0  # gave up: dropped from queue
        assert fake.calls and all(r.ok is False for r in [fake.default])
        logs = repo.recent_logs(limit=10)
        assert any("GAVE UP" in lg.message and lg.level == "ERROR" for lg in logs)

    def test_not_ready_has_no_penalty(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch, intervals=NEVER_READY)
        eng.run_once()
        assert fake.calls == []
        assert repo.retry_attempts(str(p)) == 0
        # now it becomes ready and uploads via next scan
        eng2, _ = make_engine(repo, fake, watch)
        eng2.run_once()
        assert len(fake.calls) == 1
        assert repo.is_recorded(str(p))

    def test_vanished_file_dropped_from_retry(self, watch: Path, repo: Repo):
        ghost = str(watch / "missing.jpg")
        repo.retry_add(ghost, 2)
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        eng.run_once()
        assert repo.pending_retries() == []
        assert fake.calls == []


class TestRescan:
    def test_rescan_forces_upload_and_events(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()
        fake.calls.clear()
        evs.clear()
        eng.rescan_now()
        assert [(c[0], c[1]) for c in fake.calls] == [(str(p), True)]
        assert any(isinstance(e, RescanStarted) for e in evs)
        assert any(isinstance(e, UploadSucceeded) for e in evs)


class TestWatchdirs:
    def test_fallback_to_pictures(self, tmp_path: Path, repo: Repo, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        fake = FakeUploader()
        cfg = AppConfig(dirs=(), include_defaults=True)
        eng = SyncEngine(
            repo=repo, uploader=fake, config_fn=lambda: cfg, listener=lambda e: None,
            intervals=FAST,
        )
        dirs = eng.watch_dirs()
        assert dirs == [home / "Pictures"]
        assert (home / "Pictures").is_dir()

    def test_nonexistent_dirs_filtered(self, tmp_path: Path, repo: Repo, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        existing = tmp_path / "exists"
        existing.mkdir()
        fake = FakeUploader()
        cfg = AppConfig(dirs=(str(existing), str(tmp_path / "nope")), include_defaults=False)
        eng = SyncEngine(
            repo=repo, uploader=fake, config_fn=lambda: cfg, listener=lambda e: None,
            intervals=FAST,
        )
        assert eng.watch_dirs() == [existing]


class TestWatchdog:
    def test_live_upload_for_new_file(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.start()
        try:
            (watch / "live.jpg").write_bytes(b"live")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if repo.is_recorded(str(watch / "live.jpg")):
                    break
                time.sleep(0.05)
            assert repo.is_recorded(str(watch / "live.jpg"))
            assert any(isinstance(e, UploadSucceeded) for e in evs)
        finally:
            eng.stop()
        logs = [e for e in evs if isinstance(e, LogMessage)]
        assert any("SUCCESS" in lg.message for lg in logs)

    def test_stop_emits_stopped_event(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.start()
        eng.stop()
        assert any(isinstance(e, EngineStopped) for e in evs)
        # double stop is safe
        eng.stop()
