import threading
from pathlib import Path

import pytest

from unli_photos.cli import (
    build_parser,
    format_event_line,
    main,
    precheck,
    run_headless,
    run_once,
)
from unli_photos.config import AppConfig, default_paths, save_config
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
    UploadSucceeded,
)


@pytest.fixture
def env_paths(tmp_path: Path, monkeypatch) -> tuple:
    config = tmp_path / "cfg"
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    paths = default_paths()
    watch = tmp_path / "watch"
    watch.mkdir()
    save_config(
        paths.config_file, AppConfig(dirs=(str(watch),), include_defaults=False)
    )
    return paths, tmp_path


def install_fake_cli(paths, tmp_path: Path, list_out: str = "* t@x.io") -> Path:
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    stub = paths.bin_dir / "gotohp-cli"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$3 $4\" in\n"
        "  creds\\ list*) cat <<'LISTOUT'\n"
        f"{list_out}\n"
        "LISTOUT\n"
        "  ;;\n"
        "esac\n"
        'echo \'{"success": true, "mediaKey": "m"}\'\n'
    )
    stub.chmod(0o755)
    return stub


class TestParser:
    def test_version(self, capsys):
        assert main(["--version"]) == 0
        assert "unli-photos" in capsys.readouterr().out

    def test_defaults(self):
        args = build_parser().parse_args([])
        assert args.headless is False
        assert args.once is False


class TestPrecheck:
    def test_missing_binary(self, env_paths):
        paths, _ = env_paths
        problems = precheck(paths)
        assert len(problems) == 1
        assert "gotohp-cli" in problems[0]

    def test_missing_creds(self, env_paths, tmp_path: Path):
        paths, _ = env_paths
        install_fake_cli(paths, tmp_path, list_out="No credentials found")
        problems = precheck(paths)
        assert len(problems) == 1
        assert "account" in problems[0]

    def test_ok(self, env_paths, tmp_path: Path):
        paths, _ = env_paths
        install_fake_cli(paths, tmp_path)
        assert precheck(paths) == []


class TestFormatEventLine:
    def test_upload_succeeded(self):
        line = format_event_line(
            UploadSucceeded(path="/x/photo.jpg", size=2048, duration=1.5, media_key="m")
        )
        assert "photo.jpg" in line
        assert "2.0 KB" in line
        assert "1.5s" in line

    def test_upload_failed(self):
        line = format_event_line(
            UploadFailed(path="/x/a.jpg", duration=0.1, error="boom", attempts=2)
        )
        assert "boom" in line
        assert "attempt 2" in line

    def test_skipped_and_detected(self):
        assert "nope" in format_event_line(UploadSkipped(path="/x/a.jpg", reason="nope"))
        assert "a.jpg" in format_event_line(FileDetected(path="/x/a.jpg", size=1))

    def test_engine_events(self):
        assert "2 folder(s)" in format_event_line(
            EngineStarted(watch_dirs=("/a", "/b"))
        )
        assert format_event_line(EngineStopped()) == "stopped"
        assert "scanned 10" in format_event_line(
            RescanDone(scanned=10, queued=3)
        )
        assert "queued 3" in format_event_line(RescanDone(scanned=10, queued=3))
        assert "rescan" in format_event_line(RescanStarted())

    def test_log_and_precheck(self):
        assert "[warn] careful" in format_event_line(
            LogMessage(level="warn", message="careful")
        )
        assert "precheck:" in format_event_line(PrecheckIssue(message="x"))


class TestHeadless:
    def test_precheck_failure_exits_1(self, env_paths, capsys):
        paths, _ = env_paths
        assert run_headless(paths, stop=threading.Event()) == 1
        assert "gotohp-cli" in capsys.readouterr().err

    def test_runs_and_stops_cleanly(self, env_paths, tmp_path: Path, capsys):
        paths, _ = env_paths
        install_fake_cli(paths, tmp_path)
        stop = threading.Event()
        stop.set()
        assert run_headless(paths, stop=stop) == 0
        out = capsys.readouterr().out
        assert "headless" in out
        out_err = capsys.readouterr().err
        assert out_err == ""

    def test_run_once_empty_dirs(self, env_paths, tmp_path: Path):
        paths, _ = env_paths
        install_fake_cli(paths, tmp_path)
        assert run_once(paths) == 0


def test_main_module_entrypoint():
    import unli_photos.__main__ as main_mod

    assert callable(main_mod.main)
