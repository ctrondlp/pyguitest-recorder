"""Defaults, the user configuration file, and the command line.

Precedence is built-in defaults, then the configuration file, then command
line arguments, each overriding the last -- so a flag always wins and a
default is never silently in force where the user set something.

The file lives at `$XDG_CONFIG_HOME/pyguitest-recorder/config.toml`, which is
`~/.config/...` only when that variable is unset. `~/.pyguitest-recorder.toml`
is read as a fallback for people who prefer a single dotfile.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 predates tomllib; tomli is the same parser
    import tomli as tomllib

__all__ = ["Settings", "config_paths", "load_settings", "ConfigError"]

_SECTIONS = ("capture", "analyzer", "generator", "privacy", "output")


class ConfigError(Exception):
    """The configuration file could not be read, or names something unknown."""


@dataclass
class Settings:
    """Everything the recorder can be told, from any source."""

    # -- capture ---------------------------------------------------------
    display: str | None = None
    screen: int = 0
    backend: str = "auto"
    window_context: bool = True
    element_context: bool = True

    # -- analyzer --------------------------------------------------------
    motion_threshold: int = 8
    click_interval: float = 0.5
    double_click_interval: float = 0.4
    text_idle: float = 1.5
    pause_threshold: float = 1.0
    record_motion: bool = False
    stop_key: str = "Pause"
    """Keysym that ends the recording, so stopping never needs the terminal."""

    sync_inference: bool = True
    """Work out what each pause was waiting for, instead of sleeping for it."""

    infer_idle: bool = True
    """Let an otherwise unexplained pause become `wait_for_idle` on the window.

    Separate from `sync_inference` because it is the one rule that needs
    `WINDOW_PID`, a compositor-tier capability, so it is also the one that can
    make a generated script demand more of a desktop than the rest of it does.
    """

    # -- generator -------------------------------------------------------
    locators: Literal["element", "relative", "absolute"] = "element"
    comments: bool = True
    capability_preamble: bool = True
    default_timeout: float = 10.0
    format_output: bool = True
    function_name: str = "main"

    # -- privacy ---------------------------------------------------------
    sensitive: bool = False
    redact_sensitive: bool = True
    record_raw: bool = False

    # -- output ----------------------------------------------------------
    output: str | None = None
    session_file: str | None = None
    debug: bool = False

    def merged(self, **overrides: Any) -> Settings:
        """Return a copy with the non-None overrides applied."""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update({k: v for k, v in overrides.items() if v is not None})
        return Settings(**data)


def config_paths() -> list[Path]:
    """The configuration files that would be read, most general first."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return [
        base / "pyguitest-recorder" / "config.toml",
        Path.home() / ".pyguitest-recorder.toml",
    ]


def load_settings(path: str | Path | None = None) -> tuple[Settings, Path | None]:
    """Load settings from `path`, or the first configuration file that exists.

    Returns the settings and the file they came from, which is None when no
    file was found -- the caller reports that in `--debug` output so "my
    config is being ignored" is answerable without guessing.
    """
    candidates = [Path(path)] if path is not None else config_paths()
    for candidate in candidates:
        if candidate.is_file():
            return _read(candidate), candidate
    if path is not None:
        raise ConfigError(f"no such configuration file: {path}")
    return Settings(), None


def _read(path: Path) -> Settings:
    """Parse one TOML file into Settings, rejecting unknown keys."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    flat: dict[str, Any] = {k: v for k, v in data.items() if not isinstance(v, dict)}
    for section in _SECTIONS:
        flat.update(data.get(section, {}))
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(flat) - known)
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s) {', '.join(unknown)}; "
            f"known settings are {', '.join(sorted(known))}"
        )
    return Settings(**flat)
