"""Textual TUI: dashboard over the sync engine (engine is the model, TUI the view)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from unli_photos.cli import build_engine, format_event_line, precheck
from unli_photos.config import (
    AppConfig,
    Paths,
    default_watch_paths,
    load_config,
    save_config,
)
from unli_photos.engine.events import (
    EngineEvent,
    EngineStarted,
    EngineStopped,
    UploadFailed,
    UploadSkipped,
    UploadSucceeded,
)
from unli_photos.media import format_size
from unli_photos.photos import binary as binary_mod
from unli_photos.photos import service as service_mod
from unli_photos.photos.binary import find_binary
from unli_photos.photos.uploader import CredsClient
from unli_photos.storage.repo import Repo

# Test seams: monkeypatch these module attributes in tests.
binary_download = binary_mod.download
service_enable = service_mod.enable
service_disable = service_mod.disable


class TokenDialog(ModalScreen[str | None]):
    """Password-masked modal for pasting an OAuth token (stdin only, never argv)."""

    BINDINGS: ClassVar[list] = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    TokenDialog { align: center middle; }
    #token-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: tall $primary;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="token-box"):
            yield Label("Paste the Google Photos OAuth token:")
            yield Input(password=True, id="token-input", placeholder="paste token")
            with Horizontal():
                yield Button("Add", id="btn-add", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add":
            self.dismiss(self._value())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self._value())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _value(self) -> str | None:
        value = self.query_one("#token-input", Input).value.strip()
        return value or None


class UnliApp(App[None]):
    TITLE = "unli-photos"
    CSS_PATH = "app.tcss"

    BINDINGS: ClassVar[list] = [
        ("q", "quit", "Quit"),
        ("r", "rescan", "Rescan"),
        ("s", "toggle_engine", "Start/stop"),
        ("a", "add_token", "Add token"),
    ]

    def __init__(self, paths: Paths, engine=None) -> None:
        super().__init__()
        self._paths = paths
        self._engine = engine
        self._repo: Repo | None = None
        self._engine_pending: str | None = None
        self._uploaded = 0
        self._retries = 0

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield Static("engine: stopped", id="engine-status")
                yield Static("uploaded 0 | retries 0", id="stats")
                yield Static("watching: (loading)", id="watching")
                yield Static("last: -", id="last-event")
            with TabPane("Uploads", id="uploads-tab"):
                yield DataTable(id="uploads")
            with TabPane("Logs", id="logs-tab"):
                yield RichLog(id="logs", markup=False, highlight=False)
            with TabPane("Folders", id="folders"):
                with Horizontal(id="defaults-row"):
                    yield Label("Watch ~/Pictures by default:")
                    yield Switch(id="defaults-toggle")
                yield ListView(id="folder-list")
                with Horizontal():
                    yield Input(
                        placeholder="Add folder path", id="new-folder"
                    )
                    yield Button("Add", id="btn-folder-add")
                    yield Button("Remove selected", id="btn-folder-remove")
            with TabPane("Accounts", id="accounts"):
                yield Static("accounts: (loading)", id="accounts-status")
                yield Button("Add token (a)", id="btn-token")
            with TabPane("Setup", id="setup"):
                yield Static("binary: ...", id="binary-status")
                yield ProgressBar(id="dl-progress", show_eta=False)
                yield Button("Download gotohp-cli", id="btn-download")
                yield Static("service: ...", id="service-status")
                with Horizontal():
                    yield Button("Enable service", id="btn-enable")
                    yield Button("Disable service", id="btn-disable")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#uploads", DataTable).add_columns(
            "time", "file", "size", "status"
        )
        if self._engine is None:
            problems = precheck(self._paths)
            if problems:
                for problem in problems:
                    self.notify(problem, severity="error", timeout=10)
            else:
                self._paths.state_dir.mkdir(parents=True, exist_ok=True)
                self._repo = Repo.open(self._paths.db_path)
                self._engine = build_engine(
                    self._paths, self._repo, listener=self._post_event
                )
        if self._engine is not None:
            self._engine.add_listener(self._post_event)
            self._engine.start()
            self._set_engine_status()
        user, defaults = self._folder_model()
        effective = [*user, *defaults]
        self._set_watching(tuple(effective))
        self._refresh_folders()
        self._refresh_accounts()
        self._refresh_setup()

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        if self._repo is not None:
            self._repo.close()

    # -- engine event bridge ---------------------------------------------------

    def _post_event(self, event: EngineEvent) -> None:
        self._post_call(lambda: self._handle_event(event))

    def _post_call(self, fn) -> None:
        try:
            self.call_from_thread(fn)
        except Exception:  # same-thread (tests) or app shutting down
            with contextlib.suppress(Exception):
                fn()

    def _handle_event(self, event: EngineEvent) -> None:
        line = format_event_line(event)
        try:
            self.query_one("#logs", RichLog).write(line)
            self.query_one("#last-event", Static).update(f"last: {line}")
            self.query_one("#uploads", DataTable)
        except Exception:
            return  # widgets not mounted yet
        match event:
            case UploadSucceeded(path=path, size=size):
                self._uploaded += 1
                self._add_row(path, size, "ok")
            case UploadFailed(path=path):
                self._retries += 1
                self._add_row(path, 0, "FAIL")
            case UploadSkipped(path=path):
                self._add_row(path, 0, "skip")
            case EngineStarted(watch_dirs=dirs):
                self._set_engine_status()
                self._set_watching(dirs)
            case EngineStopped():
                self._set_engine_status()
        self._set_stats()

    def _set_watching(self, dirs: tuple[str, ...]) -> None:
        names = ", ".join(dirs) if len(dirs) <= 3 else f"{len(dirs)} dirs"
        with contextlib.suppress(Exception):
            self.query_one("#watching", Static).update(
                f"watching {len(dirs)} folder(s): {names}"
            )

    def _add_row(self, path: str, size: int, status: str) -> None:
        table = self.query_one("#uploads", DataTable)
        stamp = datetime.now().strftime("%H:%M:%S")
        name = path.rsplit("/", 1)[-1]
        table.add_row(stamp, name, format_size(size), status)

    def _set_engine_status(self) -> None:
        if self._engine_pending:
            state = f"{self._engine_pending}..."
        else:
            running = self._engine is not None and self._engine.running
            state = "running" if running else "stopped"
        with contextlib.suppress(Exception):
            self.query_one("#engine-status", Static).update(f"engine: {state}")

    def _set_stats(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#stats", Static).update(
                f"uploaded {self._uploaded} | retries {self._retries}"
            )

    # -- actions ---------------------------------------------------------------

    def action_rescan(self) -> None:
        if self._engine is not None:
            self._engine.request_rescan()
            self.notify("Rescan requested")

    def action_toggle_engine(self) -> None:
        if self._engine is None:
            return
        if self._engine_pending is not None:
            self.notify(f"Engine is {self._engine_pending}, one moment...", timeout=3)
            return
        if self._engine.running:
            self._engine_pending = "stopping"
            self._set_engine_status()
            self.notify("Stopping engine...")
            self._run_bg(self._engine.stop, self._toggle_done("stopped"))
        else:
            self._engine_pending = "starting"
            self._set_engine_status()
            self.notify("Starting engine...")
            self._run_bg(self._engine.start, self._toggle_done("running"))

    def _toggle_done(self, final_state: str):
        def done(result) -> None:
            self._engine_pending = None
            if isinstance(result, Exception):
                self.notify(f"Engine error: {result}", severity="error")
            else:
                self.notify(f"Engine {final_state}")
            self._set_engine_status()

        return done

    def action_add_token(self) -> None:
        self.push_screen(TokenDialog(), self._on_token_result)

    def _on_token_result(self, token: str | None) -> None:
        if not token:
            return
        self._run_bg(self._add_token_bg(token), self._token_done)

    def _add_token_bg(self, token: str):
        def work():
            binary = find_binary(self._paths)
            if binary is None:
                return "gotohp-cli not found"
            ok = CredsClient(
                binary=binary, config_path=self._paths.gotophp_config
            ).add(token)
            return None if ok else "gotohp rejected the token"

        return work

    def _token_done(self, error: str | None) -> None:
        if error:
            self.notify(f"Token not added: {error}", severity="error")
        else:
            self.notify("Token added")
        self._refresh_accounts()

    # -- folders ---------------------------------------------------------------

    def _folder_model(self) -> tuple[list[str], list[str]]:
        """(explicit dirs, shown defaults) - the effective watch list, deduped."""
        config = load_config(self._paths.config_file)
        user = [str(d) for d in config.dirs]
        defaults = (
            [
                str(p)
                for p in default_watch_paths()
                if str(p) not in user
            ]
            if config.include_defaults
            else []
        )
        return user, defaults

    def _refresh_folders(self) -> None:
        listing = self.query_one("#folder-list", ListView)
        listing.clear()
        user, defaults = self._folder_model()
        for directory in user:
            listing.append(ListItem(Label(directory)))
        for directory in defaults:
            listing.append(ListItem(Label(f"{directory}  (default)")))
        toggle = self.query_one("#defaults-toggle", Switch)
        with contextlib.suppress(Exception):
            toggle.value = (
                load_config(self._paths.config_file).include_defaults
            )

    def on_switch_changed(self, event: Switch.Changed) -> None:
        config = load_config(self._paths.config_file)
        if event.value == config.include_defaults:
            return
        save_config(
            self._paths.config_file,
            AppConfig(dirs=config.dirs, include_defaults=event.value),
        )
        if self._engine is not None:
            self._engine.restart_watcher()
        self._refresh_folders()

    def on_folder_add(self) -> None:
        value = self.query_one("#new-folder", Input).value.strip()
        if not value:
            return
        resolved = Path(value).expanduser()
        if not resolved.is_dir():
            self.notify(f"Not a folder: {resolved}", severity="error")
            return
        user, defaults = self._folder_model()
        if str(resolved) in user:
            self.notify("Already monitored", severity="warning")
            return
        if str(resolved) in defaults:
            self.notify("Already watched by default", severity="warning")
            return
        self._save_dirs([*user, str(resolved)])
        self.notify(f"Added {resolved}")

    def on_folder_remove(self) -> None:
        listing = self.query_one("#folder-list", ListView)
        index = listing.index
        user, _defaults = self._folder_model()
        if index is None:
            self.notify("Select a folder first", severity="warning")
            return
        if index >= len(user):
            self.notify(
                "Default folders are toggled with the switch above",
                severity="warning",
            )
            return
        removed = user[index]
        self._save_dirs([d for i, d in enumerate(user) if i != index])
        self.notify(f"Removed {removed}")

    def _save_dirs(self, dirs: list[str]) -> None:
        config = load_config(self._paths.config_file)
        save_config(
            self._paths.config_file,
            AppConfig(dirs=tuple(dirs), include_defaults=config.include_defaults),
        )
        if self._engine is not None:
            self._engine.restart_watcher()
        self._refresh_folders()

    # -- accounts ---------------------------------------------------------------

    def _refresh_accounts(self) -> None:
        def work():
            binary = find_binary(self._paths)
            if binary is None:
                return ["binary missing"]
            records = CredsClient(
                binary=binary, config_path=self._paths.gotophp_config
            ).list()
            if not records:
                return ["no accounts"]
            return [
                f"{'*' if r.active else ' '} {r.email}" for r in records
            ]

        def done(lines):
            self.query_one("#accounts-status", Static).update(
                "accounts:\n" + "\n".join(lines)
            )

        self._run_bg(work, done)

    # -- setup ---------------------------------------------------------------

    def _refresh_setup(self) -> None:
        state = binary_mod.verify(self._paths)
        self.query_one("#binary-status", Static).update(
            f"binary: gotohp-cli {binary_mod.GOTOHP_VERSION}: {state}"
        )
        self.query_one("#service-status", Static).update(
            f"service: {service_mod.status()}"
        )
        missing = state == "BIN_MISSING"
        self.query_one("#dl-progress", ProgressBar).display = missing
        self.query_one("#btn-download", Button).disabled = not missing

    def on_download_binary(self) -> None:
        bar = self.query_one("#dl-progress", ProgressBar)
        bar.display = True
        bar.update(total=None, progress=0)

        def work():
            return binary_download(
                self._paths, on_progress=self._dl_progress
            )

        self._run_bg(work, self._download_done)

    def _dl_progress(self, done: int, total: int | None) -> None:
        self._post_call(
            lambda: self.query_one("#dl-progress", ProgressBar).update(
                progress=done, total=total
            )
        )

    def _download_done(self, result) -> None:
        if isinstance(result, Exception):
            self.notify(f"Download failed: {result}", severity="error")
            return
        self.notify(f"Downloaded {Path(str(result)).name}")
        self._refresh_setup()

    def on_service_enable(self) -> None:
        self._run_bg(lambda: service_enable(Path.cwd()), self._service_done)

    def on_service_disable(self) -> None:
        self._run_bg(lambda: service_disable(), self._service_done)

    def _service_done(self, result) -> None:
        if isinstance(result, Exception):
            self.notify(f"Service error: {result}", severity="error")
        else:
            self.notify(f"Service: {result}")
        self._refresh_setup()

    # -- buttons ----------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        handlers = {
            "btn-folder-add": self.on_folder_add,
            "btn-folder-remove": self.on_folder_remove,
            "btn-token": self.action_add_token,
            "btn-download": self.on_download_binary,
            "btn-enable": self.on_service_enable,
            "btn-disable": self.on_service_disable,
        }
        handler = handlers.get(button_id)
        if handler:
            handler()

    # -- background helpers -------------------------------------------------------

    def _run_bg(
        self,
        work: Callable[[], object],
        done: Callable[[object], None] | None = None,
    ) -> None:
        def wrapper():
            try:
                result = work()
            except Exception as exc:  # surfacing into the UI
                result = exc
            if done:
                self._post_call(lambda: done(result))

        threading.Thread(target=wrapper, daemon=True, name="unli-tui-bg").start()


def run_tui(paths: Paths) -> int:
    UnliApp(paths=paths).run()
    return 0
