"""Media extension filter and byte-size formatting helpers shared across the app."""

from __future__ import annotations

from pathlib import Path
from typing import Final

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        # standard images
        ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp",
        ".tiff", ".tif", ".svg", ".ico", ".jfif", ".avif",
        # camera RAW (DSLR and mirrorless)
        ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
        ".pef", ".srw", ".raf",
        # videos
        ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".3g2", ".wmv",
        ".webm", ".mpg", ".mpeg", ".mts", ".m2ts", ".ts", ".flv", ".vob",
    }
)


def is_media_file(path: Path | str) -> bool:
    """True when path has a media extension (case-insensitive)."""
    if isinstance(path, str):
        path = Path(path)
    return path.suffix.lower() in MEDIA_EXTENSIONS


def format_size(num_bytes: int) -> str:
    """Human-readable size: 512 B, 1.0 KB, 1.5 MB, 1.0 GB, 2.0 TB."""
    if num_bytes < 0:
        num_bytes = 0
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
