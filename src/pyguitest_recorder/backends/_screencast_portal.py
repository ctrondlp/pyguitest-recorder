"""ScreenCast-portal + PipeWire cursor reader. Runs only in the helper process.

Two halves:

- `_negotiate_portal`: CreateSession -> SelectSources (cursor_mode = metadata)
  -> Start (the consent dialog) over the `org.freedesktop.portal.ScreenCast`
  D-Bus interface, then OpenPipeWireRemote for the stream fd and node id. It
  also keeps the D-Bus connection alive (via a background GLib loop) for the
  life of the stream: dropping it closes the portal session, and the PipeWire
  node with it.
- `_stream_cursor`: delegates to `_pipewire.read_cursor`, which binds raw
  libpipewire and reads the cursor position off the stream.

The whole module runs only in the isolated helper subprocess (see
`_screencast.py`), so a mistake in the ctypes reader -- a wrong struct offset
is a segfault, not an exception -- takes down only the helper: the parent just
gets no positions and falls back to its delta tracking.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import Any


class _SessionKeepalive:
    """Holds the portal's D-Bus connection open while the stream runs.

    The portal ties the ScreenCast session to the client's D-Bus connection and
    tears it down (removing the PipeWire node) the moment that connection
    drops. So the negotiating `bus` and its GLib loop must outlive negotiation;
    a daemon thread runs the loop to keep the connection serviced until `close`.
    """

    def __init__(self, bus: Any, loop: Any) -> None:
        self._bus = bus
        self._loop = loop
        self._thread = threading.Thread(target=loop.run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._loop.quit()
        self._thread.join(timeout=2)


# ScreenCast portal enums (xdg-desktop-portal docs).
_MONITOR = 1  # SourceType
_CURSOR_METADATA = 4  # CursorMode: position as metadata, not drawn into pixels


def open_cursor_stream(emit: Callable[[int, int], None]) -> int:
    """Negotiate the portal, stream the cursor, and `emit(x, y)` per update.

    Returns 0 on a clean finish, non-zero on any failure (which the parent
    reads as "no positions").
    """
    fd, node_id, keepalive = _negotiate_portal()
    if fd is None or node_id is None:
        if keepalive is not None:
            keepalive.close()
        return 1
    try:
        return _stream_cursor(fd, node_id, emit)
    finally:
        if keepalive is not None:
            keepalive.close()
        os.close(fd)


# -- portal (Gio D-Bus) -----------------------------------------------------
def _portal_request(
    bus: Any,
    loop: Any,
    unique: str,
    tok: str,
    method: str,
    args_for: Callable[[str], Any],
) -> dict[str, Any] | None:
    """Make one ScreenCast portal request and wait for its `Response`.

    Subscribes to the PREDICTED request path (the connection's unique name
    plus `tok`) BEFORE calling the method -- the fix for the race the portal
    spec warns about. The `Response` to a request that needs no interaction
    (CreateSession, SelectSources) can be emitted before a subscription made
    *after* the call lands; against a fast backend that signal is missed, the
    wait then dies in its timeout, and the sequence reaches no consent dialog
    at all. Also listens on the path the reply itself names, for a portal
    older than the predictable-path scheme.

    Returns the `Response` results dict (empty for a request that carries
    none, e.g. SelectSources), or None on denial, timeout, or any failure.
    Set ``PGT_PORTAL_DEBUG`` to trace each step's outcome on stderr.
    """
    from gi.repository import Gio, GLib  # noqa: PLC0415

    predicted = f"/org/freedesktop/portal/desktop/request/{unique}/{tok}"
    state: dict[str, Any] = {}
    timed_out: list[int] = []

    def on_response(*cb: Any) -> None:
        code, results = cb[5].unpack()
        state["response"] = results if code == 0 else None
        loop.quit()

    def on_timeout() -> bool:
        # The portal never answered -- record it so the finally below does not
        # remove an already-fired source, and stop waiting.
        timed_out.append(1)
        loop.quit()
        return False  # one-shot: do not reschedule

    def listen(path: str) -> Any:
        return bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )

    subs = [listen(predicted)]
    timeout = GLib.timeout_add_seconds(120, on_timeout)
    try:
        reply = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            method,
            args_for(tok),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        returned = str(reply.unpack()[0])
        if returned != predicted:
            subs.append(listen(returned))
        loop.run()
    finally:
        if not timed_out:
            GLib.source_remove(timeout)
        for sub in subs:
            bus.signal_unsubscribe(sub)
    got = state.get("response")
    if os.environ.get("PGT_PORTAL_DEBUG"):
        why = "timeout" if "response" not in state else repr(got)
        print(f"portal {method}: {why}", file=sys.stderr)
    return got


def _negotiate_portal() -> tuple[int | None, int | None, _SessionKeepalive | None]:
    """Run CreateSession -> SelectSources -> Start -> OpenPipeWireRemote.

    Returns (pipewire_fd, node_id, keepalive) on success -- the keepalive holds
    the D-Bus connection open for the stream's life and must be `close`d after.
    Returns (None, None, None) when the portal answers but the flow does not
    complete: consent denied, a response that never arrives, a reply missing
    its stream. A D-Bus-level failure -- no session bus, no portal service at
    all -- raises `GLib.Error` instead, which `_helper_main` catches; either
    way the parent process just sees a helper that produced no positions.
    """
    import gi  # noqa: PLC0415

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib  # noqa: PLC0415

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    unique = bus.get_unique_name().removeprefix(":").replace(".", "_")
    loop = GLib.MainLoop()
    seq = [0]

    def tok() -> str:
        seq[0] += 1
        return f"pgt{seq[0]}"

    def request(method: str, args_for: Callable[[str], Any]) -> dict[str, Any] | None:
        return _portal_request(bus, loop, unique, tok(), method, args_for)

    res = request(
        "CreateSession",
        lambda tok: GLib.Variant(
            "(a{sv})",
            [
                {
                    "handle_token": GLib.Variant("s", tok),
                    "session_handle_token": GLib.Variant("s", "pgtsession"),
                }
            ],
        ),
    )
    if not res:
        return None, None, None
    session = res["session_handle"]

    # SelectSources: monitor, cursor as metadata. Success carries no results,
    # so only an explicit None (denied or timed out) is a failure here.
    selected = request(
        "SelectSources",
        lambda tok: GLib.Variant(
            "(oa{sv})",
            [
                session,
                {
                    "types": GLib.Variant("u", _MONITOR),
                    "cursor_mode": GLib.Variant("u", _CURSOR_METADATA),
                    "handle_token": GLib.Variant("s", tok),
                },
            ],
        ),
    )
    if selected is None:
        return None, None, None

    # Start: the consent dialog.
    res = request(
        "Start",
        lambda tok: GLib.Variant(
            "(osa{sv})",
            [session, "", {"handle_token": GLib.Variant("s", tok)}],
        ),
    )
    if not res:
        return None, None, None
    streams = res.get("streams") or []
    if not streams:
        return None, None, None
    node_id = int(streams[0][0])

    # OpenPipeWireRemote -> fd via the Unix FD list
    reply, fd_list = bus.call_with_unix_fd_list_sync(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.ScreenCast",
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", [session, {}]),
        GLib.VariantType.new("(h)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        None,
    )
    fd_index = reply.unpack()[0]
    fd = fd_list.get(fd_index)
    # Hand back a keepalive that runs `loop` in a background thread, so this
    # bus -- and thus the portal session and its PipeWire node -- outlives the
    # return and stays serviced for the whole stream.
    return fd, node_id, _SessionKeepalive(bus, loop)


# -- PipeWire (cursor reader) -----------------------------------------------
def _stream_cursor(fd: int, node_id: int, emit: Callable[[int, int], None]) -> int:
    """Read SPA cursor metadata off the PipeWire stream on `fd`.

    Delegates to `_pipewire.read_cursor`, which binds raw libpipewire via
    ctypes (imported lazily here so this module imports without the library).
    Validated live on GNOME 51 Mutter: real cursor positions stream off the
    consented monitor. Blocks until the helper process is signalled.
    """
    from ._pipewire import read_cursor  # noqa: PLC0415

    return read_cursor(fd, node_id, emit)
