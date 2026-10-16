"""CLI entrypoint: TUI (default), --headless daemon, --once drain, --version."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime

from unli_photos import __version__
from unli_photos.config import Paths, default_paths, load_config
from unli_photos.engine.engine import SyncEngine
from unli_photos.engine.events import (
    EngineEvent,
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
)
from unli_photos.media import format_size
from unli_photos.photos.binary import find_binary
from unli_photos.photos.uploader import CredsClient, GotophpUploader
from unli_photos.storage.repo import Repo

HEADLESS_POLL_SECONDS = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unli",
        description="unli-photos: watch folders and upload media to Google Photos",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without the TUI (for systemd user services)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scan/upload pass and exit (implies headless)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and exit",
    )
    return parser


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def format_event_line(event: EngineEvent) -> str:
    match event:
        case EngineStarted(watch_dirs=dirs):
            names = ", ".join(dirs) if len(dirs) <= 3 else f"{len(dirs)} dirs"
            return f"watching {len(dirs)} folder(s): {names}"
        case EngineStopped():
            return "stopped"
        case FileDetected(path=path, size=size):
            return f"detected {_basename(path)} ({format_size(size)})"
        case UploadStarted(path=path, size=size):
            return f"uploading {_basename(path)} ({format_size(size)})"
        case UploadSucceeded(path=path, size=size, duration=dur):
            return f"ok {_basename(path)} ({format_size(size)} in {dur:.1f}s)"
        case UploadSkipped(path=path, reason=reason):
            return f"skip {_basename(path)}: {reason}"
        case UploadFailed(path=path, error=err, attempts=attempts):
            detail = err or "unknown error"
            return f"FAIL {_basename(path)}: {detail} (attempt {attempts})"
        case RescanStarted():
            return "rescan started"
        case RescanDone(queued=queued):
            return f"rescan queued {queued} file(s)"
        case PrecheckIssue(message=message):
            return f"precheck: {message}"
        case LogMessage(level=level, message=message):
            return f"[{level}] {message}"
    return str(event)


class HeadlessListener:
    def __call__(self, event: EngineEvent) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {format_event_line(event)}", flush=True)


def precheck(paths: Paths) -> list[str]:
    """User-actionable problems blocking startup (empty list = ready)."""
    problems: list[str] = []
    binary = find_binary(paths)
    if binary is None:
        problems.append(
            "gotohp-cli not found (download it in Setup, or put it on PATH)"
        )
        return problems
    if not CredsClient(binary=binary, config_path=paths.gotophp_config).list():
        problems.append(
            "no Google account token configured (add one in the Accounts tab)"
        )
    return problems


def build_engine(
    paths: Paths, repo: Repo, listener=None
) -> SyncEngine:
    binary = find_binary(paths)
    if binary is None:  # pragma: no cover - guarded by precheck
        raise RuntimeError("gotohp-cli not found")
    uploader = GotophpUploader(
        binary=str(binary), config_path=paths.gotophp_config
    )
    return SyncEngine(
        repo=repo,
        uploader=uploader,
        config_fn=lambda: load_config(paths.config_file),
        listener=listener,
    )


def run_headless(
    paths: Paths,
    stop: threading.Event | None = None,
) -> int:
    stop = stop or threading.Event()
    problems = precheck(paths)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.open(paths.db_path)
    listener = HeadlessListener()
    engine = build_engine(paths, repo, listener)
    engine.start()
    print("unli-photos running headless; Ctrl+C to stop", flush=True)
    try:
        while not stop.is_set() and engine.running:
            stop.wait(HEADLESS_POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        repo.close()
    return 0


def run_once(paths: Paths) -> int:
    problems = precheck(paths)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.open(paths.db_path)
    try:
        engine = build_engine(paths, repo, HeadlessListener())
        engine.run_once()
    finally:
        repo.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"unli-photos {__version__}")
        return 0
    paths = default_paths()
    if args.once:
        return run_once(paths)
    if args.headless:
        return run_headless(paths)
    from unli_photos.tui.app import run_tui

    return run_tui(paths)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
