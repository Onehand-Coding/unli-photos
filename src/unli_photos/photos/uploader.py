"""gotohp-cli wrapper: upload/creds as subprocesses with strict flag surfacing."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from unli_photos.engine.uploader import UploadResult

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
MEDIA_KEY_RE = re.compile(r'"mediaKey"\s*:\s*"([^"]+)"')
ERROR_RE = re.compile(r'"error"\s*:\s*"([^"]+)"')
MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]+)"')
SUCCESS_RE = re.compile(r'"success"\s*:\s*true')
DEFAULT_TIMEOUT_SECONDS = 600.0
TIMEOUT_BYTES_PER_SECOND = 50_000


def parse_upload_output(text: str) -> tuple[str | None, str | None]:
    """(media_key, error) parsed from gotohp JSON output (parity to the original)."""
    media_key = None
    match = MEDIA_KEY_RE.search(text)
    if match:
        media_key = match.group(1)
    error = None
    err_match = ERROR_RE.search(text)
    if err_match:
        error = err_match.group(1)
    else:
        msg_match = MESSAGE_RE.search(text)
        if msg_match:
            error = msg_match.group(1)
    return media_key, error


class GotophpUploader:
    """Runs `gotohp-cli upload --no-tui` once per file, never via shell."""

    def __init__(
        self,
        binary: Path | str,
        config_path: Path | None = None,
        account: str | None = None,
    ) -> None:
        self._binary = str(binary)
        self._config_path = config_path
        self._account = account

    def _config_args(self) -> list[str]:
        if self._config_path is None:
            return []
        return ["--config", str(self._config_path)]

    def upload(
        self,
        path: Path,
        *,
        force: bool = False,
        album: str | None = None,
        timeout: float | None = None,
    ) -> UploadResult:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        effective = (
            timeout
            if timeout is not None
            else max(DEFAULT_TIMEOUT_SECONDS, size // TIMEOUT_BYTES_PER_SECOND)
        )
        argv: list[str] = [
            self._binary,
            *self._config_args(),
            "upload",
            "--no-tui",
        ]
        if self._account:
            argv += ["--account", self._account]
        if force:
            argv += ["--force"]
        if album:
            argv += ["--album", album]
        argv.append(str(path))
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=effective,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return UploadResult(ok=False, error="timeout")
        except OSError as exc:
            return UploadResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        media_key, error = parse_upload_output(f"{proc.stdout or ''}\n{proc.stderr or ''}")
        if not error and proc.stderr and proc.stderr.strip():
            error = proc.stderr.strip()
        explicit_success = bool(SUCCESS_RE.search(proc.stdout or "")) or media_key is not None
        ok = proc.returncode == 0 and error is None and explicit_success
        self._harden_config()
        return UploadResult(ok=ok, media_key=media_key, error=error)

    def _harden_config(self) -> None:
        if self._config_path is not None and Path(self._config_path).exists():
            with contextlib.suppress(OSError):
                os.chmod(self._config_path, 0o600)


@dataclass(frozen=True)
class CredRecord:
    email: str
    active: bool


def parse_creds_list(text: str) -> list[CredRecord]:
    """Parse `gotohp creds list` output; `*` marks the active account."""
    records: list[CredRecord] = []
    for line in text.splitlines():
        email_match = EMAIL_RE.search(line)
        if email_match is None:
            continue
        active = "*" in line
        records.append(CredRecord(email=email_match.group(0), active=active))
    return records


class CredsClient:
    """Account management through gotohp-cli (tokens via stdin only)."""

    def __init__(self, binary: Path | str, config_path: Path | None) -> None:
        self._binary = str(binary)
        self._config_path = config_path

    def _config_args(self) -> list[str]:
        if self._config_path is None:
            return []
        return ["--config", str(self._config_path)]

    def _run(
        self, args: list[str], token_stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = [self._binary, *self._config_args(), *args]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            input=token_stdin,
            check=False,
        )
        self._harden_config()
        return proc

    def list(self) -> list[CredRecord]:
        proc = self._run(["creds", "list"])
        return parse_creds_list(proc.stdout or "")

    def add(self, token: str) -> bool:
        """`creds add -` with the token on stdin (never argv, never shell history)."""
        proc = self._run(["creds", "add", "-"], token_stdin=token)
        return proc.returncode == 0

    def set_active(self, email: str) -> bool:
        proc = self._run(["creds", "set", email])
        return proc.returncode == 0

    def _harden_config(self) -> None:
        if self._config_path is not None and Path(self._config_path).exists():
            with contextlib.suppress(OSError):
                os.chmod(self._config_path, 0o600)
