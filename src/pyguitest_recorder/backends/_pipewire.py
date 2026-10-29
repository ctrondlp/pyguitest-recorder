"""Read the cursor position off a PipeWire ScreenCast stream, via raw ctypes.

Runs only in the isolated helper subprocess (see `_screencast.py`): a wrong
struct offset here is a segfault, and the whole point of the subprocess is that
such a crash takes down only the helper, never the recorder.

There is no PipeWire Python binding and GStreamer's `pipewiresrc` does not
surface the cursor, so this binds `libpipewire-0.3.so.0` directly and
hand-builds the SPA pods the stream negotiation needs. The ABI constants and
struct offsets below are the stable SPA 0.2 / PipeWire 0.3 layout, corrected
against the live library (the headers' enums start at a `START=0` sentinel, so
e.g. `SPA_TYPE_Object` is 15, not 14 -- getting this wrong makes
`pw_stream_update_params` reject every pod with `!spa_pod_is_object`).

The flow mirrors a minimal screen-cast consumer (OBS, Mutter's own test
client): connect a core over the portal's fd, round-trip it so the target node
is known, connect an input stream to that node with `media.*` capture props,
and in `param_changed(Format)` request `SPA_META_Cursor` on the buffers. The
one non-obvious requirement is the cursor-meta *size*: Mutter allocates a fixed
`CURSOR_META_SIZE(384, 384)` (~590 KB), so the requested size range has to
reach at least that or the meta is silently dropped from the buffer. Each
buffer then carries a `struct spa_meta_cursor` whose `position` is emitted.
"""

from __future__ import annotations

import ctypes as C
import os
import struct
from collections.abc import Callable
from typing import Any

# -- SPA / PipeWire ABI constants (stable layout, verified against the lib) --
SPA_TYPE_Id = 3
SPA_TYPE_Int = 4
SPA_TYPE_Rectangle = 10
SPA_TYPE_Fraction = 11
SPA_TYPE_Object = 15
SPA_TYPE_Choice = 19

SPA_TYPE_OBJECT_Format = 0x40003
SPA_TYPE_OBJECT_ParamBuffers = 0x40004
SPA_TYPE_OBJECT_ParamMeta = 0x40005

SPA_PARAM_EnumFormat = 3
SPA_PARAM_Format = 4
SPA_PARAM_Buffers = 5
SPA_PARAM_Meta = 6

SPA_FORMAT_mediaType = 1
SPA_FORMAT_mediaSubtype = 2
SPA_FORMAT_VIDEO_format = 0x20001
SPA_FORMAT_VIDEO_size = 0x20003
SPA_FORMAT_VIDEO_framerate = 0x20004

SPA_MEDIA_TYPE_video = 2
SPA_MEDIA_SUBTYPE_raw = 1
# A broad set of raw formats so the EnumFormat intersects whatever is produced.
_VIDEO_FORMATS = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]  # RGBx .. BGR

SPA_PARAM_META_type = 1
SPA_PARAM_META_size = 2
SPA_META_Cursor = 5
SPA_PARAM_BUFFERS_dataType = 6

SPA_CHOICE_Range = 1
SPA_CHOICE_Enum = 3

PW_VERSION_STREAM_EVENTS = 2
PW_VERSION_CORE_EVENTS = 0
PW_VERSION_REGISTRY = 3
PW_ID_CORE = 0
PW_DIRECTION_INPUT = 0
PW_STREAM_FLAG_AUTOCONNECT = 1 << 0
PW_STREAM_FLAG_MAP_BUFFERS = 1 << 2


# -- hand-built SPA pods ----------------------------------------------------
def _pad8(b: bytes) -> bytes:
    return b + b"\x00" * ((-len(b)) % 8)


def _pod(body: bytes, typ: int) -> bytes:
    """A pod is [uint32 size][uint32 type][body], padded to 8 bytes."""
    return _pad8(struct.pack("<II", len(body), typ) + body)


def _pod_id(v: int) -> bytes:
    return _pod(struct.pack("<I", v), SPA_TYPE_Id)


def _pod_int(v: int) -> bytes:
    return _pod(struct.pack("<i", v), SPA_TYPE_Int)


def _choice(choice_type: int, child_type: int, child_size: int, els: bytes) -> bytes:
    body = struct.pack("<II", choice_type, 0)
    body += struct.pack("<II", child_size, child_type)  # the child pod header
    body += els
    return _pod(body, SPA_TYPE_Choice)


def _choice_enum_id(values: list[int]) -> bytes:
    els = b"".join(struct.pack("<I", v) for v in values)
    return _choice(SPA_CHOICE_Enum, SPA_TYPE_Id, 4, els)


def _choice_range_rect(
    d: tuple[int, int], mn: tuple[int, int], mx: tuple[int, int]
) -> bytes:
    els = struct.pack("<IIIIII", d[0], d[1], mn[0], mn[1], mx[0], mx[1])
    return _choice(SPA_CHOICE_Range, SPA_TYPE_Rectangle, 8, els)


def _choice_range_fraction(
    d: tuple[int, int], mn: tuple[int, int], mx: tuple[int, int]
) -> bytes:
    els = struct.pack("<IIIIII", d[0], d[1], mn[0], mn[1], mx[0], mx[1])
    return _choice(SPA_CHOICE_Range, SPA_TYPE_Fraction, 8, els)


def _choice_range_int(d: int, mn: int, mx: int) -> bytes:
    return _choice(SPA_CHOICE_Range, SPA_TYPE_Int, 4, struct.pack("<iii", d, mn, mx))


def _prop(key: int, value_pod: bytes) -> bytes:
    """A property: [uint32 key][uint32 flags][value pod]."""
    return struct.pack("<II", key, 0) + value_pod


def _obj(obj_type: int, obj_id: int, props: bytes) -> bytes:
    return _pod(struct.pack("<II", obj_type, obj_id) + props, SPA_TYPE_Object)


def _enum_format_pod() -> bytes:
    props = b"".join(
        [
            _prop(SPA_FORMAT_mediaType, _pod_id(SPA_MEDIA_TYPE_video)),
            _prop(SPA_FORMAT_mediaSubtype, _pod_id(SPA_MEDIA_SUBTYPE_raw)),
            _prop(SPA_FORMAT_VIDEO_format, _choice_enum_id(_VIDEO_FORMATS)),
            _prop(
                SPA_FORMAT_VIDEO_size,
                _choice_range_rect((320, 240), (1, 1), (16384, 16384)),
            ),
            _prop(
                SPA_FORMAT_VIDEO_framerate,
                _choice_range_fraction((30, 1), (0, 1), (1000, 1)),
            ),
        ]
    )
    return _obj(SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat, props)


def _cursor_meta_size(w: int, h: int) -> int:
    # sizeof(spa_meta_cursor) 28 + sizeof(spa_meta_bitmap) 20 + w*h*4 pixels.
    return 28 + 20 + w * h * 4


def _buffers_pod() -> bytes:
    data_types = (1 << 1) | (1 << 2) | (1 << 3)  # MemPtr | MemFd | DmaBuf
    return _obj(
        SPA_TYPE_OBJECT_ParamBuffers,
        SPA_PARAM_Buffers,
        _prop(SPA_PARAM_BUFFERS_dataType, _pod_int(data_types)),
    )


def _meta_cursor_pod() -> bytes:
    # The size range MUST reach Mutter's fixed CURSOR_META_SIZE(384, 384)
    # (~590 KB); a smaller max leaves the sizes unable to agree and the cursor
    # meta is silently dropped from the buffer.
    props = _prop(SPA_PARAM_META_type, _pod_id(SPA_META_Cursor)) + _prop(
        SPA_PARAM_META_size,
        _choice_range_int(
            _cursor_meta_size(64, 64),
            _cursor_meta_size(1, 1),
            _cursor_meta_size(1024, 1024),
        ),
    )
    return _obj(SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta, props)


# -- libpipewire binding (lazy, so importing this module never needs the lib) --
_CB = C.CFUNCTYPE
_EV_VOID = _CB(None, C.c_void_p)
_EV_STATE = _CB(None, C.c_void_p, C.c_int, C.c_int, C.c_char_p)
_EV_U32_PTR = _CB(None, C.c_void_p, C.c_uint32, C.c_void_p)
_EV_IO = _CB(None, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32)
_EV_PTR = _CB(None, C.c_void_p, C.c_void_p)
_CE_DONE = _CB(None, C.c_void_p, C.c_uint32, C.c_int)
_CE_ERROR = _CB(None, C.c_void_p, C.c_uint32, C.c_int, C.c_int, C.c_char_p)
_CE_U32 = _CB(None, C.c_void_p, C.c_uint32)
_CE_BOUND = _CB(None, C.c_void_p, C.c_uint32, C.c_uint32)
_CE_ADDMEM = _CB(None, C.c_void_p, C.c_uint32, C.c_uint32, C.c_int, C.c_uint32)
_CE_BOUNDP = _CB(None, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p)


class _StreamEvents(C.Structure):
    _fields_ = [  # noqa: RUF012 - a fixed C ABI layout, not mutable state
        ("version", C.c_uint32),
        ("destroy", _EV_VOID),
        ("state_changed", _EV_STATE),
        ("control_info", _EV_U32_PTR),
        ("io_changed", _EV_IO),
        ("param_changed", _EV_U32_PTR),
        ("add_buffer", _EV_PTR),
        ("remove_buffer", _EV_PTR),
        ("process", _EV_VOID),
        ("drained", _EV_VOID),
        ("command", _EV_PTR),
        ("trigger_done", _EV_VOID),
    ]


class _CoreEvents(C.Structure):
    _fields_ = [  # noqa: RUF012 - a fixed C ABI layout, not mutable state
        ("version", C.c_uint32),
        ("info", _EV_PTR),
        ("done", _CE_DONE),
        ("ping", _CE_DONE),
        ("error", _CE_ERROR),
        ("remove_id", _CE_U32),
        ("bound_id", _CE_BOUND),
        ("add_mem", _CE_ADDMEM),
        ("remove_mem", _CE_U32),
        ("bound_props", _CE_BOUNDP),
    ]


_pw_lib: Any = None


def _pw() -> Any:
    """Load and bind `libpipewire-0.3.so.0` once; raise OSError if absent."""
    global _pw_lib
    if _pw_lib is not None:
        return _pw_lib
    pw = C.CDLL("libpipewire-0.3.so.0", use_errno=True)
    void, i32, u32, size = C.c_void_p, C.c_int, C.c_uint32, C.c_size_t
    sig = {
        "pw_init": ([void, void], None),
        "pw_main_loop_new": ([void], void),
        "pw_main_loop_get_loop": ([void], void),
        "pw_main_loop_run": ([void], i32),
        "pw_main_loop_quit": ([void], i32),
        "pw_main_loop_destroy": ([void], None),
        "pw_context_new": ([void, void, size], void),
        "pw_context_connect_fd": ([void, i32, void, size], void),
        "pw_stream_new": ([void, C.c_char_p, void], void),
        "pw_stream_add_listener": ([void, void, void, void], None),
        "pw_stream_connect": ([void, i32, u32, u32, C.POINTER(void), u32], i32),
        "pw_stream_dequeue_buffer": ([void], void),
        "pw_stream_queue_buffer": ([void, void], i32),
        "pw_stream_update_params": ([void, C.POINTER(void), u32], i32),
        "pw_proxy_add_object_listener": ([void, void, void, void], None),
        "pw_core_sync": ([void, u32, i32], i32),
        "pw_core_get_registry": ([void, u32, size], void),
    }
    for name, (argtypes, restype) in sig.items():
        fn = getattr(pw, name)
        fn.argtypes = argtypes
        fn.restype = restype
    pw.pw_properties_new.restype = void  # variadic: leave argtypes unset
    _pw_lib = pw
    return pw


def _u32(addr: int, off: int) -> int:
    return int(C.c_uint32.from_address(addr + off).value)


def _i32(addr: int, off: int) -> int:
    return int(C.c_int32.from_address(addr + off).value)


def _ptr(addr: int, off: int) -> int:
    return C.c_void_p.from_address(addr + off).value or 0


def read_cursor(fd: int, node_id: int, emit: Callable[[int, int], None]) -> int:
    """Stream node `node_id` on `fd` and `emit(x, y)` each cursor update.

    Blocks until the process is killed: the parent's `CursorSource.stop()`
    sends SIGTERM, whose *default* action ends the helper -- deliberately no
    Python handler, because one would only run between bytecodes and
    `pw_main_loop_run` can block in C indefinitely (a stream that never
    connects fires no callbacks), turning graceful shutdown into the parent's
    2s-timeout-then-SIGKILL path every time. The process is sacrificial;
    nothing here needs an orderly teardown. `fd` is dup'd for PipeWire's own
    ownership; the caller keeps and closes the original.
    """
    pw = _pw()
    pw.pw_init(None, None)
    loop = pw.pw_main_loop_new(None)
    context = pw.pw_context_new(pw.pw_main_loop_get_loop(loop), None, 0)
    core = pw.pw_context_connect_fd(context, os.dup(fd), None, 0)
    if not core:
        return 1

    refs: list[Any] = []  # keep ctypes callbacks alive for the loop's lifetime
    last: dict[str, tuple[int, int]] = {}

    def on_process(_data: Any) -> None:
        buf = pw.pw_stream_dequeue_buffer(stream)
        if not buf:
            return
        try:
            spabuf = _ptr(buf, 0)  # pw_buffer->buffer
            metas = _ptr(spabuf, 8)
            for i in range(_u32(spabuf, 0)):  # n_metas
                base = metas + i * 16
                data = _ptr(base, 8)
                if _u32(base, 0) == SPA_META_Cursor and data and _u32(data, 0):
                    pos = (_i32(data, 8), _i32(data, 12))  # spa_meta_cursor.position
                    if pos != last.get("pos"):
                        last["pos"] = pos
                        emit(pos[0], pos[1])
        finally:
            pw.pw_stream_queue_buffer(stream, buf)

    def on_param(_data: Any, pid: int, param: int) -> None:
        if pid != SPA_PARAM_Format or not param:
            return
        params = _pod_array([_buffers_pod(), _meta_cursor_pod()], refs)
        pw.pw_stream_update_params(stream, params, 2)

    props = pw.pw_properties_new(
        b"media.type",
        b"Video",
        b"media.category",
        b"Capture",
        b"media.role",
        b"Screen",
        None,
    )
    stream = pw.pw_stream_new(core, b"pyguitest-recorder-cursor", props)

    events = _StreamEvents(version=PW_VERSION_STREAM_EVENTS)
    refs += [_EV_STATE(lambda *a: None), _EV_U32_PTR(on_param), _EV_VOID(on_process)]
    events.state_changed, events.param_changed, events.process = refs[-3:]
    # The spa_hook must outlive the call: PipeWire keeps a pointer into it and
    # writes to it, so a GC'd hook (or events struct) is a use-after-free.
    stream_hook = C.create_string_buffer(256)
    refs += [events, stream_hook]
    pw.pw_stream_add_listener(stream, stream_hook, C.byref(events), None)

    connected = {"done": False}

    def on_done(_data: Any, cid: int, _seq: int) -> None:
        # Connect only after a core round-trip, so the registry (and the target
        # node) is populated -- else AUTOCONNECT errors "no target node".
        if cid == PW_ID_CORE and not connected["done"]:
            connected["done"] = True
            fmt = _pod_array([_enum_format_pod()], refs)
            pw.pw_stream_connect(
                stream,
                PW_DIRECTION_INPUT,
                node_id,
                PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS,
                fmt,
                1,
            )

    core_events = _CoreEvents(version=PW_VERSION_CORE_EVENTS)
    refs.append(_CE_DONE(on_done))
    core_events.done = refs[-1]
    core_hook = C.create_string_buffer(256)
    refs += [core_events, core_hook]
    pw.pw_proxy_add_object_listener(core, core_hook, C.byref(core_events), None)
    pw.pw_core_get_registry(core, PW_VERSION_REGISTRY, 0)
    pw.pw_core_sync(core, PW_ID_CORE, 0)

    # No SIGTERM handler, on purpose: the default action terminates the
    # process even while pw_main_loop_run blocks in C, where a Python-level
    # handler would never get a bytecode boundary to run on. See the
    # docstring.
    pw.pw_main_loop_run(loop)
    return 0


def _pod_array(pods: list[bytes], refs: list[Any]) -> Any:
    """A `const struct spa_pod **` from pod byte strings, kept alive in `refs`."""
    bufs = [C.create_string_buffer(p, len(p)) for p in pods]
    refs.extend(bufs)
    arr = (C.c_void_p * len(bufs))(*(C.cast(b, C.c_void_p) for b in bufs))
    refs.append(arr)
    return arr
