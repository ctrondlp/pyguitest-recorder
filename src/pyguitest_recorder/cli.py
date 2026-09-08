"""The command-line entry point.

Three modes, because a recorder that can only record is hard to trust:
`--doctor` says whether this machine can record at all and why not, the
default records, and `--regenerate` re-renders a saved recording without
touching the desktop. The last is what makes the generator profile useful --
a recording made today can be re-rendered against a later pyguitest without
recording it again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .analyzer import SyncOptions, infer_synchronization
from .backends.base import CaptureUnavailable
from .config import ConfigError, Settings, config_paths, load_settings
from .generator import PROFILE, GeneratorOptions, generate, validate
from .model import Event, Origin, Recording
from .recorder import (
    ContextReport,
    Recorder,
    choose_backend,
    describe_environment,
    probe_context,
)

__all__ = ["main", "build_parser"]


def _parse_indices(value: str) -> frozenset[int]:
    """Parse `--drop`'s comma-separated index list, e.g. "3,7,12"."""
    try:
        return frozenset(int(piece) for piece in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--drop wants comma-separated indices, e.g. 3,7,12 (got {value!r})"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="pyguitest-recorder",
        description="Record GUI activity and generate a pyguitest script.",
    )
    parser.add_argument("--version", action="version", version=_version_string())

    capture = parser.add_argument_group("capture")
    capture.add_argument("--display", help="X display to record (default: $DISPLAY)")
    capture.add_argument("--screen", type=int, help="screen number (default: 0)")
    capture.add_argument(
        "--stop-key",
        metavar="KEY",
        help="key that ends the recording, e.g. Escape or ctrl+Escape"
        " (default: Escape)",
    )
    capture.add_argument(
        "--stop-presses",
        dest="stop_key_presses",
        type=int,
        metavar="N",
        help="how many stop-key presses in a row end the recording (default: 2,"
        " since a single Escape belongs to the application)",
    )
    capture.add_argument(
        "--check-key",
        metavar="KEY",
        help="key that records a check on whatever the pointer is over,"
        " e.g. ctrl+F1 (default: ctrl+F1)",
    )
    capture.add_argument(
        "--no-checks",
        dest="check_key",
        action="store_const",
        const="",
        help="record no checks; the check key types into the application instead",
    )
    capture.add_argument(
        "--record-motion",
        action="store_true",
        default=None,
        help="record pointer movement in its own right, not only as part of a"
        " drag; needed for anything driven by hovering, such as opening a menu"
        " without clicking it",
    )
    capture.add_argument(
        "--no-window-context",
        dest="window_context",
        action="store_false",
        default=None,
        help="do not open a pyguitest session to identify windows",
    )
    capture.add_argument(
        "--no-element-context",
        dest="element_context",
        action="store_false",
        default=None,
        help="do not resolve clicks to accessible elements through AT-SPI",
    )

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", metavar="FILE.py", help="generated script")
    output.add_argument(
        "--save-session", metavar="FILE.json", help="also save the raw recording"
    )
    output.add_argument(
        "--regenerate",
        metavar="FILE.json",
        help="re-render a saved recording and exit, without recording anything",
    )
    output.add_argument(
        "--from",
        dest="trim_from",
        type=float,
        metavar="SECONDS",
        help="drop every event before this many seconds into the recording"
        " -- works with --regenerate, or right after recording",
    )
    output.add_argument(
        "--to",
        dest="trim_to",
        type=float,
        metavar="SECONDS",
        help="drop every event at or after this many seconds into the recording",
    )
    output.add_argument(
        "--drop",
        type=_parse_indices,
        metavar="N[,N...]",
        help="drop events by their 0-based index in the recording, e.g. 3,7,12"
        " -- indices are into the original recording, applied before --from/--to",
    )
    output.add_argument("--config", metavar="FILE", help="configuration file to use")

    render = parser.add_argument_group("generation")
    render.add_argument(
        "--absolute-coordinates",
        dest="locators",
        action="store_const",
        const="absolute",
        default=None,
        help="always emit screen coordinates, never elements or window offsets",
    )
    render.add_argument(
        "--relative-coordinates",
        dest="locators",
        action="store_const",
        const="relative",
        help="prefer window-relative coordinates over accessible elements",
    )
    render.add_argument(
        "--no-sync-inference",
        dest="sync_inference",
        action="store_false",
        default=None,
        help="keep every pause as a sleep instead of working out what it waited for",
    )
    render.add_argument(
        "--no-idle-inference",
        dest="infer_idle",
        action="store_false",
        default=None,
        help="never turn an unexplained pause into wait_for_idle (needs WINDOW_PID)",
    )
    render.add_argument(
        "--no-header",
        dest="include_header",
        action="store_false",
        default=None,
        help="omit the docstring naming the versions and desktop recorded on",
    )
    render.add_argument(
        "--header",
        metavar="TEXT",
        help="open the generated file with text of your own, above that"
        " docstring; a licence or a ticket number, say",
    )
    render.add_argument(
        "--no-comments",
        dest="comments",
        action="store_false",
        default=None,
        help="omit explanatory comments from the generated script",
    )

    privacy = parser.add_argument_group("privacy")
    privacy.add_argument(
        "--sensitive",
        action="store_true",
        default=None,
        help="treat all typed text as secret; never write it to disk",
    )
    privacy.add_argument(
        "--no-redact",
        dest="redact_sensitive",
        action="store_false",
        default=None,
        help="write text from password fields into the script as a literal",
    )
    privacy.add_argument(
        "--record-raw",
        action="store_true",
        default=None,
        help="keep the raw event stream for diagnostics (includes keystrokes)",
    )

    parser.add_argument(
        "--doctor",
        action="store_true",
        help="report whether this session can be recorded, and exit",
    )
    parser.add_argument("--debug", action="store_true", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the recorder. Returns the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings, source = load_settings(args.config)
    except ConfigError as exc:
        print(f"pyguitest-recorder: {exc}", file=sys.stderr)
        return 2
    settings = settings.merged(**_overrides(args))
    if args.debug:
        _report_settings(settings, source)
    if args.doctor:
        return _doctor(settings)
    try:
        if args.regenerate:
            return _regenerate(
                args.regenerate, settings, args.trim_from, args.trim_to, args.drop
            )
        return _record(settings, args.trim_from, args.trim_to, args.drop)
    except ValueError as exc:
        print(f"pyguitest-recorder: {exc}", file=sys.stderr)
        return 2


def _overrides(args: argparse.Namespace) -> dict[str, object]:
    """The settings the command line actually set, ignoring the rest."""
    names = (
        "display",
        "screen",
        "stop_key",
        "stop_key_presses",
        "check_key",
        "record_motion",
        "window_context",
        "element_context",
        "output",
        "session_file",
        "locators",
        "comments",
        "include_header",
        "header",
        "sync_inference",
        "infer_idle",
        "sensitive",
        "redact_sensitive",
        "record_raw",
        "debug",
    )
    values = {name: getattr(args, name, None) for name in names}
    values["session_file"] = args.save_session
    return values


def _sync_options(settings: Settings) -> SyncOptions:
    """Translate settings into the analyzer's inference switches."""
    return SyncOptions(
        enabled=settings.sync_inference,
        idle=settings.infer_idle,
        min_timeout=settings.default_timeout,
    )


def _generator_options(settings: Settings) -> GeneratorOptions:
    """Translate settings into generator options."""
    return GeneratorOptions(
        locators=settings.locators,
        comments=settings.comments,
        capability_preamble=settings.capability_preamble,
        function_name=settings.function_name,
        default_timeout=settings.default_timeout,
        redact_sensitive=settings.redact_sensitive,
        format_output=settings.format_output,
        include_header=settings.include_header,
        header=settings.header,
    )


def _record(
    settings: Settings,
    trim_from: float | None = None,
    trim_to: float | None = None,
    drop: frozenset[int] | None = None,
) -> int:
    """Record until the stop key or Ctrl-C, then generate."""
    try:
        recorder = Recorder(settings=settings)
        recorder.start()
    except CaptureUnavailable as exc:
        print(f"pyguitest-recorder: {exc}", file=sys.stderr)
        return 1
    print(f"Recording. {_stop_hint(settings)} to stop.", file=sys.stderr)
    if settings.check_key:
        print(
            f"Point at something and press {settings.check_key} to check it.",
            file=sys.stderr,
        )
    print(
        "Everything you type is captured, including from other windows.",
        file=sys.stderr,
    )
    try:
        recording = recorder.run()
    finally:
        recorder.stop()
    if recorder.unstopped_presses:
        # Pressing the stop key once when two are needed does nothing visible,
        # so the natural next move is to press it again -- and the first press
        # is by then a keystroke in the script. Handing it on is the right
        # default, but it is worth saying out loud the once.
        presses = recorder.unstopped_presses
        print(
            f"note: {presses} {settings.stop_key} press"
            f"{'es were' if presses > 1 else ' was'} recorded as "
            f"{'keystrokes' if presses > 1 else 'a keystroke'}, not as a stop. "
            f"Stopping takes {settings.stop_key_presses} within "
            f"{settings.stop_key_interval:g}s; delete "
            f"{'them' if presses > 1 else 'it'} if that was a missed attempt.",
            file=sys.stderr,
        )
    return _emit(recording, settings, trim_from=trim_from, trim_to=trim_to, drop=drop)


def _regenerate(
    path: str,
    settings: Settings,
    trim_from: float | None = None,
    trim_to: float | None = None,
    drop: frozenset[int] | None = None,
) -> int:
    """Re-render a saved recording without touching the desktop."""
    try:
        recording = Recording.load(path)
    except (OSError, ValueError) as exc:
        print(f"pyguitest-recorder: {exc}", file=sys.stderr)
        return 2
    return _emit(
        recording,
        settings,
        save_session=False,
        trim_from=trim_from,
        trim_to=trim_to,
        drop=drop,
    )


def _trimmed_events(
    events: list[Event],
    trim_from: float | None,
    trim_to: float | None,
    drop: frozenset[int] | None,
) -> list[Event]:
    """Keep only events inside [trim_from, trim_to) and not in `drop`.

    `drop` indexes the *original* list, before any of this runs -- so
    `--from 8 --drop 0` always means "the very first event", not "whatever
    is first among the survivors of --from", which would make the meaning
    of one flag depend on whether another happened to run first.

    An out-of-range index raises rather than being silently ignored: a
    recording that turned out to have fewer events than expected is worth
    noticing, not trimming around quietly.
    """
    if drop:
        invalid = sorted(i for i in drop if i < 0 or i >= len(events))
        if invalid:
            raise ValueError(
                f"--drop index out of range for a {len(events)}-event "
                f"recording: {', '.join(map(str, invalid))}"
            )
    kept = []
    for index, event in enumerate(events):
        if drop and index in drop:
            continue
        if trim_from is not None and event.timestamp < trim_from:
            continue
        if trim_to is not None and event.timestamp >= trim_to:
            continue
        kept.append(event)
    return kept


def _emit(
    recording: Recording,
    settings: Settings,
    save_session: bool = True,
    trim_from: float | None = None,
    trim_to: float | None = None,
    drop: frozenset[int] | None = None,
) -> int:
    """Generate, validate and write the script; report what was produced.

    Synchronization is inferred here rather than during capture, and what is
    saved is what was *observed*. A recording is then re-analyzed every time it
    is rendered -- so `--regenerate` picks up better inference rules without
    re-recording anything, and rendering the same file twice cannot compound.

    Trimming happens first, ahead of both analysis and `--save-session`, so a
    trimmed recording is what gets re-analyzed *and* what a saved copy holds
    -- the whole point being to fix the recording once rather than repeating
    the same --from/--drop on every future --regenerate.
    """
    if trim_from is not None or trim_to is not None or drop:
        kept = _trimmed_events(recording.events, trim_from, trim_to, drop)
        print(
            f"Trimmed {len(recording.events) - len(kept)} of "
            f"{len(recording.events)} events",
            file=sys.stderr,
        )
        recording = Recording(
            events=kept,
            environment=recording.environment,
            raw=recording.raw,
            started_at=recording.started_at,
        )
    analyzed = _analyze(recording, settings)
    source = generate(analyzed, _generator_options(settings))
    problems = validate(source)
    if save_session and settings.session_file:
        Recording(
            events=recording.events,
            environment=recording.environment,
            raw=recording.raw if settings.record_raw else [],
            started_at=recording.started_at,
        ).save(settings.session_file)
        print(f"Recording saved to {settings.session_file}", file=sys.stderr)
    if settings.output:
        Path(settings.output).write_text(source, encoding="utf-8")
        print(f"Script written to {settings.output}", file=sys.stderr)
    else:
        sys.stdout.write(source)
    _summarize(analyzed, problems)
    return 1 if problems else 0


def _analyze(recording: Recording, settings: Settings) -> Recording:
    """Return the recording with synchronization inferred into it."""
    return Recording(
        events=infer_synchronization(recording.events, _sync_options(settings)),
        environment=recording.environment,
        started_at=recording.started_at,
    )


def _summarize(recording: Recording, problems: list[str]) -> None:
    """Print what was recorded and anything that needs the user's attention."""
    observed = sum(1 for e in recording.events if e.origin is Origin.OBSERVED)
    inferred = len(recording.events) - observed
    print(
        f"{len(recording.events)} events ({observed} observed, {inferred} inferred)",
        file=sys.stderr,
    )
    for note in recording.environment.notes:
        print(f"note: {note}", file=sys.stderr)
    for problem in problems:
        print(f"INVALID: {problem}", file=sys.stderr)


def _doctor(settings: Settings) -> int:
    """Report whether this session can be recorded, and what would degrade."""
    print(_version_string())
    print(f"generator profile: {PROFILE}")
    try:
        backend = choose_backend(settings)
        print(f"capture:           {backend.name} (available)")
        capture_ok = True
    except CaptureUnavailable as exc:
        print(f"capture:           unavailable\n                   {exc}")
        capture_ok = False
    environment = describe_environment(None, "n/a")
    print(f"session:           {environment.session_type or 'unknown'}")
    print(f"compositor:        {environment.compositor or 'unknown'}")
    print(f"display:           {environment.display or 'unset'}")
    print(f"pyguitest:         {environment.pyguitest_version or 'not importable'}")

    # Capture answers "can input be seen"; this answers what the generated
    # script will look like, which is the part worth knowing before spending
    # ten minutes on a recording that turns out to be all coordinates.
    context = probe_context(settings) if capture_ok else ContextReport()
    print(f"window context:    {'yes' if context.windows else 'no'}")
    print(f"element context:   {'yes' if context.elements else 'no'}")

    for note in environment.notes + context.notes:
        print(f"note:              {note}")
    print("config searched:")
    for path in config_paths():
        print(f"  {'*' if path.is_file() else '-'} {path}")
    print(f"verdict:           {_verdict(capture_ok, context)}")
    return 0 if capture_ok else 1


def _stop_hint(settings: Settings) -> str:
    """How to describe the stop key to someone about to need it."""
    if not settings.stop_key:
        return "Press Ctrl-C"
    times = {1: "", 2: " twice", 3: " three times"}.get(
        settings.stop_key_presses, f" {settings.stop_key_presses} times"
    )
    return f"Press {settings.stop_key}{times}"


def _verdict(capture_ok: bool, context: ContextReport) -> str:
    """The one line `--doctor` exists to print.

    It reported facts and left the reader to draw the conclusion, which is a
    poor trade when the whole question is "can this machine record?" -- and
    the answer has three outcomes, not two. Recording with no element context
    works and produces a far more fragile script, so saying only "yes" would
    be true and misleading.
    """
    if not capture_ok:
        return "cannot record -- see the capture line above"
    if context.elements:
        return "ready to record, and clicks will be named"
    if context.windows:
        return (
            "can record, but no click will be named -- the script will use "
            "window-relative coordinates"
        )
    return "can record, but every click will be a bare screen coordinate"


def _report_settings(settings: Settings, source: Path | None) -> None:
    """Print the settings actually in force, and where they came from."""
    print(f"config: {source or 'defaults only'}", file=sys.stderr)
    for name, value in sorted(vars(settings).items()):
        print(f"  {name} = {value!r}", file=sys.stderr)


def _version_string() -> str:
    """The version line shared by --version and --doctor."""
    return f"pyguitest-recorder {__version__}"
