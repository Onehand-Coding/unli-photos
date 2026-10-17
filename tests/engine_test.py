import time
from pathlib import Path
from typing import Any

import pytest

from unli_photos.config import AppConfig
from unli_photos.engine.engine import RESCAN_REQUEST, EngineIntervals, SyncEngine
from unli_photos.engine.events import (
    EngineStarted,
    EngineStopped,
    LogMessage,
    RescanDone,
    RescanStarted,
    UploadSkipped,
    UploadSucceeded,
)
from unli_photos.engine.lock import EngineLocked
from unli_photos.engine.uploader import UploadResult
from unli_photos.storage.repo import Repo


class FakeUploader:
    """Uploader test double: scripted results per path plus a default."""

    def __init__(self, default: UploadResult | None = None) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []
        self.results: dict[str, UploadResult] = {}
        self.default = default or UploadResult(ok=True, media_key="mk-1")

    def upload(self, path: Path, *, force: bool = False, album: str | None = None) -> UploadResult:
        self.calls.append((str(path), force, album))
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


def make_engine(
    repo, uploader, watch, intervals=FAST, config: AppConfig | None = None
) -> tuple[SyncEngine, list[Any]]:
    events: list[Any] = []
    cfg = config or AppConfig(dirs=(str(watch),), include_defaults=False)
    eng = SyncEngine(
        repo=repo,
        uploader=uploader,
        config_fn=lambda: cfg,
        listener=events.append,
        intervals=intervals,
    )
    return eng, events


class TestRunOnce:
    def test_fresh_scan_uploads_media_only(self, watch: Path, repo: Repo, tmp_path: Path):
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

    def test_album_auto_passes_folder_album_flag(self, watch: Path, repo: Repo):
        (watch / "IMG.jpg").write_bytes(b"photo")
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        eng.run_once()
        assert fake.calls and fake.calls[0][2] == "AUTO"

    def test_dir_album_key_cached_across_files(self, watch: Path, repo: Repo):
        """Second file in a dir must reuse the AF1Qip key, not create another album."""
        (watch / "IMG_1.jpg").write_bytes(b"one")
        (watch / "IMG_2.jpg").write_bytes(b"two")
        fake = FakeUploader(
            default=UploadResult(
                ok=True, media_key="mk", album_key="AF1QipDIRKEY"
            )
        )
        eng, _ = make_engine(repo, fake, watch)
        eng.run_once()
        albums = [c[2] for c in fake.calls]
        assert albums[0] == "AUTO"
        assert albums[1] == "AF1QipDIRKEY"
        assert repo.album_key_for(str(watch)) == "AF1QipDIRKEY"

    def test_album_key_not_stored_without_result_key(self, watch: Path, repo: Repo):
        (watch / "IMG.jpg").write_bytes(b"photo")
        fake = FakeUploader()  # default result has album_key=None
        eng, _ = make_engine(repo, fake, watch)
        eng.run_once()
        assert repo.album_key_for(str(watch)) is None

    def test_album_auto_disabled_passes_none(self, watch: Path, repo: Repo):
        (watch / "IMG.jpg").write_bytes(b"photo")
        fake = FakeUploader()
        cfg = AppConfig(dirs=(str(watch),), include_defaults=False, album_auto=False)
        eng, _ = make_engine(repo, fake, watch, config=cfg)
        eng.run_once()
        assert fake.calls and fake.calls[0][2] is None

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
    def test_sweep_skips_queued_files(self, watch: Path, repo: Repo):
        """Backlog files already in the queue must not re-warn each sweep."""
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng._queued = {
            str(p)  # queued but not yet processed
        }
        eng._sweep()
        warns = [e for e in evs if isinstance(e, LogMessage) and "not yet uploaded" in e.message]
        assert warns == []

    def test_sweep_reports_and_enqueues_unrecorded(self, watch: Path, repo: Repo):
        p = watch / "IMG_2.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng._sweep()
        warns = [e for e in evs if isinstance(e, LogMessage) and "not yet uploaded" in e.message]
        assert len(warns) == 1

    def test_rescan_catch_up_skips_uploaded(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()
        fake.calls.clear()
        evs.clear()
        eng.rescan_now()
        assert fake.calls == []
        done = [e for e in evs if isinstance(e, RescanDone)]
        assert done == [RescanDone(scanned=1, queued=0)]

    def test_rescan_catch_up_uploads_unrecorded_only(self, watch: Path, repo: Repo):
        old = watch / "IMG_old.jpg"
        old.write_bytes(b"v1")
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        eng.run_once()
        fake.calls.clear()
        new = watch / "IMG_new.jpg"
        new.write_bytes(b"fresh")
        eng.rescan_now()
        assert [(c[0], c[1]) for c in fake.calls] == [(str(new), False)]
        assert repo.is_recorded(str(new))
        assert repo.is_recorded(str(new))

    def test_rescan_force_reuploads_and_events(self, watch: Path, repo: Repo):
        p = watch / "IMG_1.jpg"
        p.write_bytes(b"photo")
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.run_once()
        fake.calls.clear()
        evs.clear()
        eng.rescan_now(force=True)
        assert [(c[0], c[1]) for c in fake.calls] == [(str(p), True)]
        assert any(isinstance(e, RescanStarted) for e in evs)
        done = [e for e in evs if isinstance(e, RescanDone)]
        assert done == [RescanDone(scanned=1, queued=1)]
        assert any(isinstance(e, UploadSucceeded) for e in evs)


class TestWatchdirs:
    def test_fallback_to_pictures(self, tmp_path: Path, repo: Repo, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        fake = FakeUploader()
        cfg = AppConfig(dirs=(), include_defaults=True)
        eng = SyncEngine(
            repo=repo,
            uploader=fake,
            config_fn=lambda: cfg,
            listener=lambda e: None,
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
            repo=repo,
            uploader=fake,
            config_fn=lambda: cfg,
            listener=lambda e: None,
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


class TestSingleInstance:
    def test_second_engine_refused_until_first_stops(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng1, _ = make_engine(repo, fake, watch)
        eng1.start()
        eng2, _ = make_engine(repo, fake, watch)
        with pytest.raises(EngineLocked):
            eng2.start()
        assert not eng2.running
        eng1.stop()
        eng3, _ = make_engine(repo, fake, watch)
        eng3.start()
        eng3.stop()

    def test_run_once_refused_while_engine_running(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        other, _ = make_engine(repo, fake, watch)
        eng.start()
        try:
            with pytest.raises(EngineLocked):
                other.run_once()
        finally:
            eng.stop()
        other.run_once()  # lock free again: drains without error
        other.run_once()  # released after run: sequential reuse works

    def test_lock_file_records_holder(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        eng.start()
        try:
            lock_file = repo.db_path
            assert lock_file is not None
            text = (lock_file.parent / "engine.lock").read_text()
            assert str(__import__("os").getpid()) in text
        finally:
            eng.stop()


class TestRequestFiles:
    def test_rescan_request_marker_picked_up(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, evs = make_engine(repo, fake, watch)
        eng.start()
        try:
            db_path = repo.db_path
            assert db_path is not None
            marker = db_path.parent / RESCAN_REQUEST
            marker.write_text("please")
            deadline = time.monotonic() + 5.0
            while not any(isinstance(e, RescanDone) for e in evs):
                if time.monotonic() > deadline:
                    pytest.fail("rescan request never processed")
                time.sleep(0.05)
            assert not marker.exists()
        finally:
            eng.stop()

    def test_unknown_marker_left_alone(self, watch: Path, repo: Repo):
        fake = FakeUploader()
        eng, _ = make_engine(repo, fake, watch)
        eng.start()
        stranger = None
        try:
            db_path = repo.db_path
            assert db_path is not None
            stranger = db_path.parent / "some-other.request"
            stranger.write_text("x")
            time.sleep(0.2)
            assert stranger.exists()
        finally:
            eng.stop()
            if stranger is not None:
                stranger.unlink()
