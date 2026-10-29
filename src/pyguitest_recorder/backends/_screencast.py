"""Absolute cursor position on Wayland, via the ScreenCast portal + PipeWire.

EXPERIMENTAL and OPT-IN (the `screencast_cursor` setting, default off) -- read
this before trusting it.

The evdev backend gives a relative mouse only deltas, so on a bare-metal
machine with no absolute pointing device the absolute cursor position is
unknown (an absolute device -- a tablet, every VM's integration pointer -- is
already exact, and is the common case). The one portable source of the real
position on Wayland is the ScreenCast portal's cursor metadata delivered over
PipeWire: a consented screen-share stream whose buffers carry the cursor's
screen position even in `metadata` mode (no pixels needed).

Two hard facts shaped the design:

- The cursor arrives as a SPA metadata struct on PipeWire buffers. GStreamer's
  `pipewiresrc` does not surface it (it exposes `fd`/`path` only) and there is
  no PipeWire Python binding, so reading it means raw `libpipewire` through
  ctypes -- replicating C struct layouts, where a wrong offset is a *segfault*,
  not an exception. A segfault in the recorder's own process would take the
  whole recording with it.
- So the reader runs in a SEPARATE PROCESS (`_helper_main`, launched as
  `python -m pyguitest_recorder.backends._screencast`). It prints `x y` lines
  on stdout; this `CursorSource` reads them. If the helper segfaults, fails to
  get consent, or never starts, the recorder simply stops getting positions
  and falls back to its delta tracking -- it cannot crash or hang the
  recording. That isolation is what makes carrying a ctypes reader safe at
  all: however well-validated, one compositor's quirk away from a bad offset
  must never cost a recording.

The reader is validated live on GNOME 51 Mutter: real cursor positions stream
end-to-end through this production path (`CursorSource` -> helper ->
`open_cursor_stream` -> `_pipewire.read_cursor`), and a captured click came
back stamped with the live ScreenCast position (see `_screencast_portal` /
`_pipewire`, and developers/status.md for the run record). It is wired into
recording behind the `screencast_cursor` setting (default off): when set,
`recorder._choose_wayland` constructs a `CursorSource`, the
`WaylandCaptureBackend` starts and stops it, and `_stamp()` prefers its live
position over the centre-seeded delta tracking for every click, scroll and
motion. It stays opt-in because starting the stream raises a one-time "Share
your screen?" consent dialog -- it cannot run unattended -- and it is only
worth it for a bare-metal relative-only mouse (every VM's pointer, and any
tablet, is absolute and already exact). Consent denied, portal absent, or the
helper dying all degrade to the delta tracking; nothing here can crash or hang
a recording, which is what the process isolation is for.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading


class CursorSource:
    """The latest absolute cursor position from a process-isolated helper."""

    def __init__(self) -> None:
        """Nothing runs until `start`."""
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._pos: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> bool:
        """Launch the helper. Returns False if it could not even be spawned.

        A True here means the process started, not that consent was granted or
        that any position will ever arrive -- that is deliberately not waited
        on, so a denied or slow portal never blocks the recording.
        """
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "pyguitest_recorder.backends._screencast"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return False
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return True

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:  # ends when the helper exits or is killed
                if self._stop.is_set():
                    break
                parts = line.split()
                if len(parts) == 2:
                    try:
                        x, y = int(parts[0]), int(parts[1])
                    except ValueError:
                        continue
                    with self._lock:
                        self._pos = (x, y)
        finally:
            # The stream has ended -- the helper exited, crashed, or was
            # stopped -- so the last position is now a lie that only gets
            # staler. Clearing it is what actually hands the backend back to
            # its delta tracking: `position()` returning a frozen point would
            # pin every later click, scroll and motion to it for the rest of
            # the recording, strictly worse than no position at all.
            with self._lock:
                self._pos = None

    def position(self) -> tuple[int, int] | None:
        """The most recent absolute cursor position, or None if none yet."""
        with self._lock:
            return self._pos

    def stop(self) -> None:
        """Terminate the helper and stop reading."""
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                # Reap the SIGKILLed helper too; dropping the handle without
                # a wait leaves it a zombie for the life of the session.
                with contextlib.suppress(Exception):
                    self._proc.wait(timeout=1)
            self._proc = None


def _helper_main() -> int:
    """Negotiate the ScreenCast portal and stream cursor positions to stdout.

    Runs only in the isolated subprocess. Any failure -- no portal, consent
    denied, libpipewire missing or its ABI not as expected -- exits non-zero
    with nothing on stdout, which the parent reads as "no positions" and
    ignores. Intentionally self-contained and import-guarded so the parent's
    import of this module never pulls in Gio or ctypes-libpipewire.
    """
    try:
        from ._screencast_portal import open_cursor_stream
    except Exception:  # noqa: BLE001 - the whole helper is best-effort
        return 1
    try:
        return open_cursor_stream(_emit)
    except Exception:  # noqa: BLE001
        return 1


def _emit(x: int, y: int) -> None:
    """Print one position line, flushed, for the parent to read.

    Suppresses a broken pipe: when the parent stops reading (CursorSource.stop
    terminates the helper), a mid-flight flush would otherwise raise inside the
    ctypes callback and print an ignored-exception traceback for nothing.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(f"{x} {y}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(_helper_main())
