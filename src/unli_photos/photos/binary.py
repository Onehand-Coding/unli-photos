"""gotohp-cli binary manager: pinned checksum, streamed download, verification."""

from __future__ import annotations

import hashlib
import platform
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import httpx

from unli_photos.config import Paths

GOTOHP_VERSION = "v0.11.0"
GOTOHP_DOWNLOAD_URL = (
    f"https://github.com/xob0t/gotohp/releases/download/{GOTOHP_VERSION}"
)

_ASSET_BY_MACHINE: dict[str, str] = {
    "x86_64": "gotohp-cli_amd64",
    "amd64": "gotohp-cli_amd64",
}

EXPECTED_SHA256: dict[str, str] = {
    "gotohp-cli_amd64": "945021d5dbc36a96f53121eb4f907ace39d9457cea1b43107ed1986441b29030",
}

BINARY_NAME = "gotohp-cli"


class BinaryError(RuntimeError):
    pass


def asset_for_machine() -> str | None:
    return _ASSET_BY_MACHINE.get(platform.machine().lower())


def target_path(paths: Paths) -> Path:
    return paths.bin_dir / BINARY_NAME


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def find_binary(paths: Paths) -> Path | None:
    """bin_dir copy first; PATH fallback; None when nowhere usable."""
    target = target_path(paths)
    if target.is_file() and os_access(target):
        return target
    which = shutil.which(BINARY_NAME)
    if which:
        return Path(which)
    return None


def os_access(path: Path) -> bool:
    try:
        return path.stat().st_mode & stat.S_IXUSR != 0
    except OSError:
        return False


def verify(paths: Paths, *, sha256: str | None = None) -> str:
    """BIN_OK | BIN_MISSING | BIN_CORRUPT against the pinned manifest."""
    target = target_path(paths)
    if not target.is_file():
        return "BIN_MISSING"
    expected = sha256 if sha256 is not None else EXPECTED_SHA256.get(BINARY_NAME)
    if not expected:
        return "BIN_CORRUPT"
    return "BIN_OK" if sha256_file(target) == expected else "BIN_CORRUPT"


ProgressFn = Callable[[int, int | None], None]


def download(
    paths: Paths,
    *,
    client: httpx.Client | None = None,
    sha256: str | None = None,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Stream the pinned release asset into bin_dir and verify its hash.

    Raises BinaryError on unsupported architecture, HTTP failure, or hash mismatch.
    """
    asset = asset_for_machine()
    if asset is None:
        raise BinaryError(f"no gotohp-cli release asset for architecture {platform.machine()}")
    expected = sha256 if sha256 is not None else EXPECTED_SHA256[asset]
    url = f"{GOTOHP_DOWNLOAD_URL}/{asset}"
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.bin_dir / f"{BINARY_NAME}.download"

    own_client: httpx.Client
    close_own = client is None
    own_client = client if client is not None else httpx.Client(
        follow_redirects=True, timeout=60.0
    )
    try:
        digest = hashlib.sha256()
        done = 0
        total: int | None = None
        with own_client.stream("GET", url) as resp:
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            total = int(length) if length else None
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        if digest.hexdigest() != expected:
            tmp.unlink(missing_ok=True)
            raise BinaryError(f"checksum mismatch for {asset} (expected {expected})")
        tmp.chmod(0o700)
        final = target_path(paths)
        tmp.replace(final)
        return final
    finally:
        if close_own:
            own_client.close()
