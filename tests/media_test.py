from pathlib import Path

from unli_photos.media import MEDIA_EXTENSIONS, format_size, is_media_file


def test_is_media_pass_lower_and_upper():
    assert is_media_file(Path("a.jpg"))
    assert is_media_file(Path("IMG_0001.JPG"))
    assert is_media_file(Path("IMG_0001.Jpeg"))


def test_is_media_reject_non_media():
    assert not is_media_file(Path("a.txt"))
    assert not is_media_file(Path("noext"))
    assert not is_media_file(Path("archive.zip"))


def test_media_covers_full_original_list():
    expected = {
        # standard images
        ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp",
        ".tiff", ".tif", ".jfif", ".avif", ".svg", ".ico",
        # camera RAW
        ".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
        ".pef", ".srw", ".raf",
        # videos
        ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".3g2", ".wmv",
        ".webm", ".mpg", ".mpeg", ".mts", ".m2ts", ".ts", ".flv", ".vob",
    }
    assert MEDIA_EXTENSIONS == frozenset(expected)
    assert len(MEDIA_EXTENSIONS) >= 37


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(int(1.5 * 1024 * 1024)) == "1.5 MB"
    assert format_size(1024**3) == "1.0 GB"
    assert format_size(2 * 1024**4) == "2.0 TB"
