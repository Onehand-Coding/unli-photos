"""Application configuration: TOML persistence, XDG paths, default watch folders."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG_FILE_NAME = "config.toml"
APP_DIR_NAME = "unli-photos"


@dataclass(frozen=True)
class AppConfig:
    """User-managed watch configuration."""

    dirs: tuple[str, ...] = ()
    include_defaults: bool = True


@dataclass(frozen=True)
class Paths:
    """Filesystem layout for config, state, and binaries."""

    config_dir: Path
    config_file: Path
    state_dir: Path
    db_path: Path
    bin_dir: Path
    gotophp_config: Path


def _xdg(env_key: str, fallback: str) -> Path:
    value = os.environ.get(env_key)
    return Path(value) if value else Path.home() / fallback


def default_paths() -> Paths:
    config_dir = _xdg("XDG_CONFIG_HOME", ".config") / APP_DIR_NAME
    state_dir = _xdg("XDG_DATA_HOME", ".local/share") / APP_DIR_NAME
    return Paths(
        config_dir=config_dir,
        config_file=config_dir / CONFIG_FILE_NAME,
        state_dir=state_dir,
        db_path=state_dir / "state.db",
        bin_dir=state_dir / "bin",
        gotophp_config=config_dir / "gotohp.config",
    )


def default_watch_paths() -> tuple[Path, ...]:
    """Builtin watch folders: ~/Pictures only.

    Policy (decided after v1 feedback): default uploads must stay narrow;
    anything else is an explicit user choice via config.toml or the TUI.
    """
    return (Path.home() / "Pictures",)


def load_config(path: Path) -> AppConfig:
    """Read config.toml; missing or unparsable files yield defaults."""
    if not path.is_file():
        return AppConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return AppConfig()
    watch = data.get("watch")
    if not isinstance(watch, dict):
        return AppConfig()

    dirs: tuple[str, ...] = ()
    raw_dirs = watch.get("dirs")
    if isinstance(raw_dirs, list) and all(isinstance(d, str) for d in raw_dirs):
        dirs = tuple(raw_dirs)

    include_defaults = True
    raw_include = watch.get("include_defaults")
    if isinstance(raw_include, bool):
        include_defaults = raw_include

    return AppConfig(dirs=dirs, include_defaults=include_defaults)


def save_config(path: Path, config: AppConfig) -> None:
    """Persist config.toml (creating parent directories as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# unli-photos configuration",
        "",
        "[watch]",
        "# Extra directories to monitor (default folders below are optional).",
        'dirs = [',
    ]
    for d in config.dirs:
        lines.append(f"  {json.dumps(d)},")
    lines.append("]")
    lines.append(
        "# Also monitor standard user folders (Downloads, Pictures, ...)."
    )
    lines.append(f"include_defaults = {'true' if config.include_defaults else 'false'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def replace_dirs(config: AppConfig, dirs: tuple[str, ...]) -> AppConfig:
    """Convenience helper: new AppConfig with different dirs."""
    return replace(config, dirs=dirs)
