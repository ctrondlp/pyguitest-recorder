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
    hover_threshold: float = 0.3
    """Seconds the pointer must rest somewhere for that to record as a hover.

    Hovering is how menus open their submenus and how tooltips appear, and
    none of it is a click -- see NormalizerOptions.hover_threshold. Set to 0
    to record nothing but clicks, drags and keys, as this did before.
    """
    stop_key: str = "Escape"
    """Key that ends the recording, so stopping never needs the terminal.

    Takes the same `+` chord syntax as `check_key`, so `ctrl+Escape` works
    if a bare one is wanted by the application. `Pause` was the old default
    and still suits a keyboard that has one -- with `stop_key_presses = 1`,
    since nothing else uses it. Many laptops have no Pause key at all, which
    is why it is no longer the default.
    """

    stop_key_presses: int = 2
    """How many times `stop_key` must be pressed in a row to stop.

    Two, because the default is Escape and a single Escape belongs to the
    application being recorded -- swallowing it would make closing a dialog
    unrecordable. Presses that do not complete the run are passed through to
    the recording, so a lone Escape is still captured as one.
    """

    stop_key_interval: float = 2.0
    """Seconds within which those presses have to arrive to count as a run.

    1.0 measured live as too tight for how people actually press it:
    pressing once, seeing no visible effect, and pausing to check before
    pressing again is a natural response with no feedback that the first
    press was seen -- and that pause reliably lands just over 1 second (a
    real capture: 1.333s between the release of the first press and the
    start of the second). 2.0 gives that unhurried cadence real room without
    opening the window so wide that two genuinely unrelated presses of the
    application's own become likely to be mistaken for a deliberate run.
    """

    check_key: str = "ctrl+1"
    """Key that records a check on whatever the pointer is over.

    This is what makes a recording a test rather than a replay: without it a
    generated script asserts nothing and passes as long as it does not raise.
    Empty records no checks and lets the key through to the application.

    Modifiers are written with `+`, as in `ctrl+F9` or `ctrl+shift+c`, and
    must match exactly -- `ctrl+F9` does not fire on `ctrl+shift+F9`, so the
    combinations near it stay usable in the application being recorded. A
    bare `F9` is still accepted and then fires only with no modifier held.

    `ctrl+F1` was the old default and is no longer used: measured live, a
    laptop's bare F1 key commonly sends a hardware media keysym
    (`XF86_AudioMute`, confirmed live) rather than the literal `F1` X11
    calls it, which the matcher never sees -- silently making the check key
    unusable with no error at all, every single press recorded as an
    ordinary keystroke instead. Function keys generally carry this risk, so
    the default moved to a plain digit, which keyboards do not remap.
    Setting `check_key` to an unmodified printable character (a bare letter
    or digit) has its own real cost either way: that character can then
    never be typed into the recorded application again, since every press
    of it is swallowed as a check instead of text -- `ctrl+1` sidesteps that
    by being a chord no ordinary typing produces.
    """

    announce_checks: bool = True
    """Print each check to the terminal the moment it is recorded.

    Pressing `check_key` has no other visible effect at all, so without this
    the only way to find out whether a check actually recorded something
    useful -- rather than nothing being identified under the pointer -- is
    to read the generated script afterward. On by default for the same
    reason `on_stop_progress` exists: silent success and silent failure look
    identical while recording, and there is no cost to saying which one just
    happened.
    """

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

    suppress_keymap_warning: bool = False
    """Silence pyguitest's KeymapWarning in generated scripts. Off by
    default: real signal the first time (record/replay keyboard layout
    mismatch types the wrong characters silently), noise on repeat runs."""

    suppress_atspi_chatter: bool = False
    """Silence GLib's "dbind" log domain in generated scripts -- AT-SPI's
    own registry chatter, not pyguitest's. Off by default: a process-wide
    GLib log handler with no way to undo it, so it should be opted into,
    not assumed safe for every desktop."""

    include_header: bool = True
    """Write the docstring saying where the script came from. On by default.

    It carries the recorder and pyguitest versions, the desktop recorded on,
    and a pointer to the notes at the end -- which is what makes a generated
    script answerable months later. Off produces a bare script.
    """

    header: str = ""
    """Text of your own to open the file with, above the generated detail.

    For a licence, a ticket number, a team convention -- whatever a generated
    file is expected to carry where you work. Multi-line is fine; a TOML
    triple-quoted string is the natural way to write one. The "Generated by
    pyguitest-recorder" line and the version block still follow it, so
    provenance is never lost by setting this.
    """

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
