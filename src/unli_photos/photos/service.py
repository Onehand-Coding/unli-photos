"""systemd user service management (enable/disable/status)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "unli-photos"
UNIT_FILE_NAME = "unli-photos.service"
MODULE_NAME = "unli_photos"


def unit_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / UNIT_FILE_NAME


def unit_text(project_dir: Path, exec_start: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=unli-photos watch-and-upload to Google Photos",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={project_dir}",
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=10",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def derive_exec_start(exe: Path | None = None) -> str:
    resolved = Path(exe) if exe else Path(sys.executable)
    sibling = resolved.parent / "unli"
    if sibling.is_file():
        return f"{sibling} --headless"
    return f"{resolved} -m {MODULE_NAME} --headless"


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _systemctl(*args: str) -> tuple[str, int]:
    proc = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    first_line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return first_line, proc.returncode


def _project_exec_start(project_dir: Path) -> str:
    script = project_dir / ".venv" / "bin" / "unli"
    if script.is_file():
        return f"{script} --headless"
    return f"{Path(sys.executable)} -m {MODULE_NAME} --headless"


def _validate_exec(exec_str: str) -> None:
    if " -m " in exec_str:
        binary = Path(exec_str.split()[0])
        if not binary.is_file():
            raise RuntimeError(f"python executable not found: {binary}")
        return
    binary = Path(exec_str.split()[0])
    if not binary.is_file():
        raise RuntimeError(f"missing executable: {binary}")


def enable(project_dir: Path, exec_start: str | None = None) -> str:
    """Write the unit file, daemon-reload, enable + start. Best-effort linger."""
    exec_str = exec_start or _project_exec_start(project_dir)
    _validate_exec(exec_str)
    unit = unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(project_dir, exec_str))
    _systemctl("daemon-reload")
    _, rc = _systemctl("enable", "--now", UNIT_FILE_NAME)
    loginctl = shutil.which("loginctl")
    if loginctl:
        subprocess.run(
            [loginctl, "enable-linger"],
            capture_output=True,
            check=False,
        )
    return "enabled" if rc == 0 else "systemctl enable failed"


def disable() -> str:
    if not unit_path().exists():
        return "not installed"
    _systemctl("disable", "--now", UNIT_FILE_NAME)
    _systemctl("daemon-reload")
    unit_path().unlink(missing_ok=True)
    return "disabled"


def status() -> str:
    if not systemctl_available():
        return "systemd unavailable"
    if not unit_path().exists():
        return "not installed"
    active, _ = _systemctl("is-active", UNIT_FILE_NAME)
    enabled, _ = _systemctl("is-enabled", UNIT_FILE_NAME)
    return f"{active} ({enabled})"
