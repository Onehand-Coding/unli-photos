from pathlib import Path

import pytest

from unli_photos.photos.service import (
    SERVICE_NAME,
    derive_exec_start,
    disable,
    enable,
    status,
    systemctl_available,
    unit_path,
    unit_text,
)


def test_unit_text_fields(tmp_path: Path):
    exec_start = str(tmp_path / ".venv" / "bin" / "unli --headless")
    text = unit_text(tmp_path, exec_start)
    assert f"ExecStart={exec_start}" in text
    assert f"WorkingDirectory={tmp_path}" in text
    assert "After=network-online.target" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=10" in text
    assert "WantedBy=default.target" in text


def test_derive_exec_start_uses_venv_script(tmp_path: Path):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "unli").touch()
    exe = venv_bin / "python"
    assert derive_exec_start(exe) == f"{venv_bin / 'unli'} --headless"


def test_derive_exec_start_module_fallback(tmp_path: Path):
    exe = tmp_path / "python"
    assert derive_exec_start(exe) == f"{exe} -m unli_photos --headless"


def install_fake_systemctl(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "fakesbin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    log.write_text("")
    for name, body in (
        (
            "systemctl",
            "#!/bin/sh\n"
            f'printf "%s|" "$@" >> {log}\necho "" >> {log}\n'
            "sub=$2\n"
            'case "$sub" in\n'
            '  is-active) echo "${FAKE_ACTIVE:-active}" ;;\n'
            '  is-enabled) echo "${FAKE_ENABLED:-enabled}" ;;\n'
            "esac\n",
        ),
        ("loginctl", f'#!/bin/sh\necho "loginctl $@" >> {log}\necho "" >> {log}\n'),
    ):
        script = bin_dir / name
        script.write_text(body)
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return log


@pytest.fixture
def service_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def test_systemctl_available(tmp_path: Path, monkeypatch):
    install_fake_systemctl(tmp_path, monkeypatch)
    assert systemctl_available() is True
    monkeypatch.setenv("PATH", "/nonexistent")
    assert systemctl_available() is False


def test_enable_writes_unit_and_activates(
    tmp_path: Path, service_home: Path, monkeypatch
):
    log = install_fake_systemctl(tmp_path, monkeypatch)
    project_dir = tmp_path / "proj"
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "unli").touch()

    message = enable(project_dir)

    unit = unit_path()
    assert unit.exists()
    text = unit.read_text()
    assert f"ExecStart={venv_bin / 'unli'} --headless" in text
    assert f"WorkingDirectory={project_dir}" in text
    assert message == "enabled"
    calls = log.read_text().splitlines()
    assert any("daemon-reload" in c for c in calls)
    assert any(f"enable|--now|{SERVICE_NAME}.service" in c for c in calls)
    assert any("loginctl enable-linger" in c for c in calls)
    assert status() == "active (enabled)"


def test_enable_falls_back_to_module_form(
    tmp_path: Path, service_home: Path, monkeypatch
):
    install_fake_systemctl(tmp_path, monkeypatch)
    project_dir = tmp_path / "proj"
    (project_dir / ".venv" / "bin").mkdir(parents=True)
    assert enable(project_dir) == "enabled"
    text = unit_path().read_text()
    assert "-m unli_photos --headless" in text


def test_disable_removes_unit(tmp_path: Path, service_home: Path, monkeypatch):
    log = install_fake_systemctl(tmp_path, monkeypatch)
    project_dir = tmp_path / "proj"
    (project_dir / ".venv" / "bin").mkdir(parents=True)
    (project_dir / ".venv" / "bin" / "unli").touch()
    enable(project_dir)
    assert unit_path().exists()
    assert disable() == "disabled"
    assert not unit_path().exists()
    calls = log.read_text().splitlines()
    assert any(f"disable|--now|{SERVICE_NAME}.service" in c for c in calls)
    assert status() == "not installed"


def test_status_states(tmp_path: Path, service_home: Path, monkeypatch):
    install_fake_systemctl(tmp_path, monkeypatch)
    assert status() == "not installed"
    unit_path().parent.mkdir(parents=True)
    unit_path().touch()
    monkeypatch.setenv("FAKE_ACTIVE", "failed")
    monkeypatch.setenv("FAKE_ENABLED", "disabled")
    assert status() == "failed (disabled)"


def test_status_without_systemd(tmp_path: Path, service_home: Path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert status() == "systemd unavailable"
