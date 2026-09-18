"""Where the config file is found, and what wins when two places disagree.

Defaults with no file at all, the XDG lookup, keys given at the top level
rather than in a section, and the precedence between file and command line
are four separate mechanisms, and they fail in four different ways.
"""

from pathlib import Path

import pytest

from pyguitest_recorder.config import ConfigError, Settings, config_paths, load_settings
from pyguitest_recorder.config import settings as settings_module


class _FakePlatform:
    """Stands in for the `sys` module, with only `platform` pinned.

    `config.settings` reads `sys.platform` to choose where configuration lives,
    and patching the attribute on the real `sys` would change what `pathlib`,
    `os` and every import does inside the same test. Swapping the module
    reference the one function looks it up on is the narrower move -- the
    shape pyguitest's own `_platform()` seam exists for.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform


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
    # Path.home() reads USERPROFILE on Windows and ignores HOME, so the
    # platform is pinned rather than inherited: setting HOME and asserting the
    # result followed it was a Linux-only assumption, and it failed on
    # Windows for that reason rather than for a fault in config_paths.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(settings_module, "sys", _FakePlatform("linux"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert config_paths()[0].is_relative_to(tmp_path / ".config")


def test_config_home_is_appdata_on_windows(tmp_path, monkeypatch):
    # ~/.config is neither conventional nor discoverable on Windows: `~` there
    # is the profile root a user sees in Explorer, and %APPDATA% is the
    # directory Windows itself backs up and roams.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(settings_module, "sys", _FakePlatform("win32"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert config_paths()[0] == (
        tmp_path / "Roaming" / "pyguitest-recorder" / "config.toml"
    )


def test_xdg_config_home_still_wins_on_windows(tmp_path, monkeypatch):
    # An explicit instruction, and a Cygwin or MSYS2 session that sets it
    # means it -- so the platform default never overrides it.
    monkeypatch.setattr(settings_module, "sys", _FakePlatform("win32"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_paths()[0] == tmp_path / "cfg" / "pyguitest-recorder" / "config.toml"


def test_windows_without_appdata_falls_back_rather_than_failing(tmp_path, monkeypatch):
    # %APPDATA% is set on every ordinary login, but a stripped service
    # environment can lack it, and "no config file anywhere" is a worse
    # answer than the path every other platform uses.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(settings_module, "sys", _FakePlatform("win32"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
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


def test_the_check_key_is_configurable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[analyzer]\ncheck_key = "F10"\n')
    settings, _ = load_settings(path)
    assert settings.check_key == "F10"
