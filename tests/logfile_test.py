import time
from pathlib import Path

import pytest

from unli_photos.engine.events import (
    EngineStarted,
    EngineStopped,
    FileDetected,
    LogMessage,
    PrecheckIssue,
    RescanDone,
    RescanStarted,
    UploadFailed,
    UploadSkipped,
    UploadStarted,
    UploadSucceeded,
    from_json,
    to_json,
)
from unli_photos.engine.logfile import (
    FileEventSink,
    FileTail,
    log_path,
    truncate_if_huge,
)


def test_to_json_from_json_roundtrip_all_events():
    events = [
        EngineStarted(watch_dirs=("/a", "/b")),
        EngineStopped(),
        FileDetected(path="/a/x.jpg", size=5),
        UploadSkipped(path="/a/x.jpg", reason="already uploaded"),
        UploadStarted(path="/a/x.jpg", size=5),
        UploadSucceeded(path="/a/x.jpg", size=5, duration=1.25, media_key="k"),
        UploadFailed(path="/a/x.jpg", duration=0.5, error="boom", attempts=2),
        RescanStarted(),
        RescanDone(scanned=3, queued=1),
        PrecheckIssue(message="m"),
        LogMessage(level="OK", message="done"),
    ]
    for event in events:
        assert from_json(to_json(event)) == event


def test_from_json_unknown_kind_raises():
    with pytest.raises(ValueError):
        from_json('{"kind": "Nonsense"}')


def test_from_json_garbage_raises():
    with pytest.raises(ValueError):
        from_json("not json")


def test_log_path(tmp_path: Path):
    assert log_path(tmp_path) == tmp_path / "events.log"


class TestFileEventSink:
    def test_appends_jsonl(self, tmp_path: Path):
        path = tmp_path / "events.log"
        sink = FileEventSink(path)
        sink.open()
        sink(LogMessage(level="OK", message="one"))
        sink(LogMessage(level="WARN", message="two"))
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert from_json(lines[0]) == LogMessage(level="OK", message="one")
        assert from_json(lines[1]) == LogMessage(level="WARN", message="two")

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "dir" / "events.log"
        sink = FileEventSink(path)
        sink.open()
        sink(RescanDone(scanned=0, queued=0))
        assert from_json(path.read_text().splitlines()[0]) == RescanDone(
            scanned=0, queued=0
        )


class TestTruncate:
    def test_truncates_huge_file_to_keep_bytes(self, tmp_path: Path):
        path = tmp_path / "events.log"
        path.write_text("x" * 3000)
        truncate_if_huge(path, at=1000, keep=100)
        assert len(path.read_text()) == 100

    def test_small_file_untouched(self, tmp_path: Path):
        path = tmp_path / "events.log"
        path.write_text("tiny")
        truncate_if_huge(path, at=1000, keep=5)
        assert path.read_text() == "tiny"


class TestFileTail:
    def test_delivers_history_and_fresh_lines(self, tmp_path: Path):
        path = tmp_path / "events.log"
        sink = FileEventSink(path)
        sink.open()
        sink(LogMessage(level="OK", message="history"))
        lines: list[str] = []
        tail = FileTail(path, on_line=lines.append, poll=0.05)
        tail.start()
        try:
            sink(LogMessage(level="OK", message="fresh"))
            deadline = time.monotonic() + 5.0
            while not any("fresh" in line for line in lines):
                if time.monotonic() > deadline:
                    pytest.fail("fresh line never delivered")
                time.sleep(0.05)
            messages = [
                from_json(line).message  # type: ignore[attr-defined]
                for line in lines
            ]
            assert "history" in messages
            assert "fresh" in messages
        finally:
            tail.stop()

    def test_survives_truncation(self, tmp_path: Path):
        path = tmp_path / "events.log"
        path.write_text("a" * 200)
        seen: list[str] = []
        tail = FileTail(path, on_line=seen.append, poll=0.05)
        tail.start()
        try:
            truncate_if_huge(path, at=100, keep=50)
            path.write_text("b" * 10)
            deadline = time.monotonic() + 5.0
            while not any("bbbbbbbbbb" in line for line in seen):
                if time.monotonic() > deadline:
                    pytest.fail("post-truncation content never delivered")
                time.sleep(0.05)
        finally:
            tail.stop()

    def test_stop_is_quick(self, tmp_path: Path):
        path = tmp_path / "events.log"
        path.write_text("x")
        tail = FileTail(path, on_line=lambda line: None, poll=0.05)
        tail.start()
        time.sleep(0.1)
        started = time.monotonic()
        tail.stop()
        assert time.monotonic() - started < 2.0
