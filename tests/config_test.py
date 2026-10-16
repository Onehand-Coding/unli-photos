from pathlib import Path

from unli_photos.config import AppConfig, default_watch_paths, load_config, save_config


def test_load_missing_file_gives_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.dirs == ()
    assert cfg.include_defaults is True


def test_load_garbage_file_gives_defaults(tmp_path: Path):
    bad = tmp_path / "config.toml"
    bad.write_text("[[[ not toml")
    cfg = load_config(bad)
    assert cfg == AppConfig()


def test_roundtrip(tmp_path: Path):
    cfg = AppConfig(dirs=(str(Path.home() / "Cam"),), include_defaults=False)
    p = tmp_path / "config.toml"
    save_config(p, cfg)
    assert load_config(p) == cfg


def test_save_escapes_special_chars(tmp_path: Path):
    cfg = AppConfig(dirs=("/tmp/we'd \"dir\"/photos",))
    p = tmp_path / "config.toml"
    save_config(p, cfg)
    loaded = load_config(p)
    assert loaded.dirs == cfg.dirs


def test_default_watch_paths():
    paths = default_watch_paths()
    assert paths == (Path.home() / "Pictures",)


def test_paths_honor_xdg(monkeypatch):
    home = "/home/tester"
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("XDG_CONFIG_HOME", home + "/cfg")
    monkeypatch.setenv("XDG_DATA_HOME", home + "/data")
    from unli_photos.config import default_paths

    p = default_paths()
    assert p.config_dir == Path(home + "/cfg/unli-photos")
    assert p.state_dir == Path(home + "/data/unli-photos")
    assert p.db_path == p.state_dir / "state.db"
    assert p.gotophp_config == p.config_dir / "gotohp.config"


def test_toml_documented_keys_only(tmp_path: Path):
    content = (
        '[watch]\ndirs = ["/tmp/x"]\ninclude_defaults = true\n'
        '[unknown]\nkey = 1\n'
    )
    p = tmp_path / "config.toml"
    p.write_text(content)
    cfg = load_config(p)
    assert cfg.dirs == ("/tmp/x",)
    assert cfg.include_defaults is True
    # unknown table ignored, not an error
