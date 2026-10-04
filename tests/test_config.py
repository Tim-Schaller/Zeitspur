import pytest

from zeitspur.config import Config, ConfigError, load_config, save_config


def test_defaults_match_spec():
    cfg = Config()
    assert cfg.capture_interval_seconds == 5
    assert cfg.idle_pause_minutes == 3
    assert cfg.retention_days == 14
    assert cfg.max_image_width == 1920
    assert cfg.webp_quality == 75
    assert cfg.ocr_language == "deu+eng"
    assert "KeePass.*" in cfg.excluded_process_regex
    assert any("Inkognito" in p for p in cfg.excluded_title_regex)
    assert cfg.interval_ms == 5000
    assert cfg.max_block_ms == 10 * 60_000


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg == Config()


def test_load_overrides_and_ignores_unknown_keys(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("capture_interval_seconds: 10\nretention_days: 7\nunknown_key: 1\n"
                 "excluded_process_regex:\n  - 'keepass.*'\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.capture_interval_seconds == 10
    assert cfg.retention_days == 7
    assert cfg.excluded_process_regex == ["keepass.*"]
    assert cfg.webp_quality == 75  # Default bleibt


@pytest.mark.parametrize("field,value", [
    ("capture_interval_seconds", 1),
    ("capture_interval_seconds", 61),
    ("webp_quality", 100),
    ("max_image_width", 100),
    ("retention_days", 0),
    ("ocr_language", "de"),
    ("log_level", "LOUD"),
    ("change_threshold", 2.0),
    ("excluded_process_regex", ["("]),
    ("excluded_title_regex", "kein-list"),
    ("db_path", ""),
])
def test_invalid_values_raise(field, value):
    cfg = Config()
    setattr(cfg, field, value)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_invalid_yaml_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- nur eine liste\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_save_roundtrip(tmp_path):
    cfg = Config(capture_interval_seconds=7, excluded_title_regex=[".*Geheim.*"], log_level="debug")
    p = save_config(cfg, tmp_path / "config.yaml")
    assert p.exists() and not (tmp_path / "config.yaml.tmp").exists()
    loaded = load_config(p)
    assert loaded.capture_interval_seconds == 7
    assert loaded.excluded_title_regex == [".*Geheim.*"]
    assert loaded.log_level == "DEBUG"


def test_default_db_path_follows_data_dir(data_dir):
    cfg = Config()
    assert cfg.resolved_db_path == data_dir / "zeitspur.db"  # Default folgt dem (ggf. ueberschriebenen) Datenordner


def test_custom_db_path_expands_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = Config(db_path="%LOCALAPPDATA%/Anderswo/zeitspur.db")
    assert cfg.resolved_db_path == tmp_path / "Anderswo" / "zeitspur.db"
    cfg2 = Config(db_path="D:/Daten/zeitspur.db")
    assert cfg2.resolved_db_path.as_posix().lower() == "d:/daten/zeitspur.db"


def test_data_dir_override(data_dir):
    from zeitspur import config as c

    assert c.data_dir() == data_dir
    assert c.config_path() == data_dir / "config.yaml"
    assert c.key_path() == data_dir / "key.bin"
    assert c.is_first_run()


def test_patterns_are_case_insensitive():
    cfg = Config(excluded_process_regex=["keepass.*"], excluded_title_regex=[".*inkognito.*"])
    assert cfg.process_patterns()[0].fullmatch("KeePass.exe")
    assert cfg.title_patterns()[0].fullmatch("Bank - Inkognito - Firefox")


def test_catastrophic_regex_is_rejected():
    from zeitspur.config import Config, ConfigError
    import pytest as _pytest
    with _pytest.raises(ConfigError):
        Config(excluded_title_regex=["(a+)+$"]).validate()   # katastrophales Backtracking
    with _pytest.raises(ConfigError):
        Config(excluded_process_regex=["(.*a){1,30}$"]).validate()
    # harmlose Standardmuster bleiben zulaessig
    Config().validate()


def test_teams_identity_config():
    from zeitspur.config import Config, ConfigError
    import pytest as _pytest
    cfg = Config(teams_user_id="obj-id", teams_user_names=["Mustermann, Max"]).validate()
    assert cfg.teams_user_id == "obj-id" and cfg.teams_user_names == ["Mustermann, Max"]
    with _pytest.raises(ConfigError):
        Config(teams_user_names="keine-liste").validate()
    with _pytest.raises(ConfigError):
        Config(teams_user_id=123).validate()


def test_update_idle_minutes_range():
    import pytest as _pytest

    from zeitspur.config import Config, ConfigError
    assert Config().validate().update_idle_minutes == 5           # Standard: 5 Minuten
    assert Config(update_idle_minutes=240).validate().update_idle_minutes == 240
    for bad in (0, 241):
        with _pytest.raises(ConfigError):
            Config(update_idle_minutes=bad).validate()
