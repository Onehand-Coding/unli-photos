import asyncio
import time
from pathlib import Path
from typing import ClassVar

import pytest

from unli_photos.config import AppConfig, default_paths, save_config
from unli_photos.engine.events import (
    EngineStarted,
    LogMessage,
    UploadSucceeded,
)
from unli_photos.photos.uploader import CredRecord
from unli_photos.tui.app import UnliApp


class FakeEngine:
    def __init__(self):
        self.listeners = []
        self.started = False
        self.rescans = 0
        self.restarts = 0

    def add_listener(self, fn):
        self.listeners.append(fn)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    @property
    def running(self):
        return self.started

    def request_rescan(self):
        self.rescans += 1

    def restart_watcher(self):
        self.restarts += 1

    def emit(self, event):
        for fn in list(self.listeners):
            fn(event)


class FakeCredsClient:
    records: ClassVar[list] = [CredRecord(email="t@x.io", active=True)]
    calls: ClassVar[list] = []

    def __init__(self, binary, config_path):
        pass

    def list(self):
        return list(type(self).records)

    def add(self, token):
        type(self).calls.append(token)
        return True

    def set_active(self, email):
        return True


@pytest.fixture
def env_paths(tmp_path: Path, monkeypatch):
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
    return paths, watch


@pytest.fixture
def fake_creds(monkeypatch):
    FakeCredsClient.calls = []
    FakeCredsClient.records = [CredRecord(email="t@x.io", active=True)]
    import unli_photos.tui.app as app_mod

    monkeypatch.setattr(app_mod, "CredsClient", FakeCredsClient)
    return FakeCredsClient


async def pilot_session(app, settle=0.3):
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(settle)
    return pilot


def run_async(coro):
    return asyncio.run(coro)


def test_app_mounts_and_starts_engine(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert engine.started

    run_async(go())


def test_rescan_binding(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        async with UnliApp(paths=paths, engine=engine).run_test(size=(100, 40)) as pilot:
            await pilot.press("r")
            await pilot.pause()

    run_async(go())
    assert engine.rescans == 1


def test_quit_binding(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("q")

    run_async(go())


def test_dashboard_shows_stats_on_event(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            engine.emit(
                UploadSucceeded(path="/w/a.jpg", size=1024, duration=1.0, media_key="m")
            )
            await pilot.pause()
            from textual.widgets import RichLog

            activity = app.query_one("#activity", RichLog)
            assert activity.lines

    run_async(go())


def test_log_line_rendered(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import TabbedContent

            app.query_one(TabbedContent).active = "logs-tab"
            await pilot.pause()
            engine.emit(LogMessage(level="warn", message="careful"))
            await pilot.pause()
            from textual.widgets import RichLog

            logs = app.query_one("#logs", RichLog)
            assert logs.lines

    run_async(go())


def test_token_dialog_adds_credential(env_paths, fake_creds, tmp_path: Path):
    paths, _ = env_paths
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    stub = paths.bin_dir / "gotohp-cli"
    stub.write_text("#!/bin/sh\ncat - >/dev/null\n")
    stub.chmod(0o755)
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.action_add_token()
            await pilot.pause()
            from textual.widgets import Input

            dialog = app.screen
            token_input = dialog.query_one("#token-input", Input)
            token_input.value = "SECRET_TOKEN"
            token_input.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            deadline = time.time() + 5
            while time.time() < deadline and not FakeCredsClient.calls:
                await asyncio.sleep(0.05)
                await pilot.pause()
            assert FakeCredsClient.calls == ["SECRET_TOKEN"]

    run_async(go())


def test_folder_add_persists_and_restarts(env_paths):
    paths, watch = env_paths
    engine = FakeEngine()
    new_dir = watch.parent / "extra"
    new_dir.mkdir()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Input

            app.query_one("#new-folder", Input).value = str(new_dir)
            app.on_folder_add()
            await pilot.pause()

    run_async(go())
    assert engine.restarts == 1
    saved = save_config  # noqa: F841
    from unli_photos.config import load_config

    assert str(new_dir) in load_config(paths.config_file).dirs


def test_setup_shows_missing_binary(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Static

            status = app.query_one("#binary-status", Static)
            assert "BIN_MISSING" in str(status.render())

    run_async(go())


def test_setup_download_button(env_paths, monkeypatch):
    paths, _ = env_paths
    engine = FakeEngine()
    import unli_photos.tui.app as app_mod

    def fake_download(paths_, *, client=None, sha256=None, on_progress=None):
        if on_progress:
            on_progress(10, 10)
        return paths_.bin_dir / "gotohp-cli"

    monkeypatch.setattr(app_mod, "binary_download", fake_download)

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.on_download_binary()
            deadline = time.time() + 5
            while time.time() < deadline:
                from textual.widgets import Static

                text = str(app.query_one("#binary-status", Static).render())
                if "BIN_MISSING" not in text and "downloading" not in text.lower():
                    break
                await asyncio.sleep(0.05)
                await pilot.pause()

    run_async(go())


def test_service_enable_button(env_paths, monkeypatch):
    paths, _ = env_paths
    engine = FakeEngine()
    import unli_photos.tui.app as app_mod

    calls = []
    monkeypatch.setattr(
        app_mod, "service_enable", lambda project_dir: calls.append(project_dir) or "enabled"
    )

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.on_service_enable()
            deadline = time.time() + 5
            while time.time() < deadline and not calls:
                await asyncio.sleep(0.05)
                await pilot.pause()
            assert len(calls) == 1

    run_async(go())


def test_engine_started_event_updates_status(env_paths):
    paths, _ = env_paths
    engine = FakeEngine()

    async def go():
        app = UnliApp(paths=paths, engine=engine)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            engine.emit(EngineStarted(watch_dirs=("/a", "/b")))
            await pilot.pause()
            from textual.widgets import Static

            text = str(app.query_one("#engine-status", Static).render())
            assert "running" in text.lower()

    run_async(go())
