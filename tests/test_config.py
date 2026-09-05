import pytest

from pyguitest_recorder.config import ConfigError, Settings, config_paths, load_settings


def test_defaults_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    settings, source = load_settings()
    assert source is None
    assert settings.locators == "element"
    assert settings.motion_threshold == 8


def test_xdg_config_home_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_paths()[0] == tmp_path / "cfg" / "pyguitest-recorder" / "config.toml"


def test_config_home_defaults_under_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_paths()[0].is_relative_to(tmp_path / ".config")


def test_sections_are_flattened(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        "[analyzer]\nmotion_threshold = 25\n\n[generator]\nlocators = 'absolute'\n"
    )
    settings, source = load_settings(path)
    assert source == path
    assert settings.motion_threshold == 25
    assert settings.locators == "absolute"


def test_top_level_keys_also_work(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("record_raw = true\n")
    settings, _ = load_settings(path)
    assert settings.record_raw is True


def test_unknown_key_is_rejected_with_the_known_list(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[analyzer]\nnonsense = 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(path)


def test_malformed_toml_is_reported(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[unclosed\n")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no such configuration file"):
        load_settings(tmp_path / "absent.toml")


def test_command_line_overrides_the_file():
    settings = Settings(locators="absolute", motion_threshold=25)
    merged = settings.merged(locators="element", display=None)
    assert merged.locators == "element"
    assert merged.motion_threshold == 25


def test_none_overrides_are_ignored():
    settings = Settings(display=":1")
    assert settings.merged(display=None).display == ":1"
