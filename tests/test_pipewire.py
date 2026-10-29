"""The PipeWire reader's hand-built SPA pods, byte for byte.

`read_cursor` itself needs libpipewire and a live consented stream, so it is
exercised by the live GNOME/Mutter validation, not here. But the SPA pods it
sends are pure byte layout, and getting a single ABI constant wrong (the real
bug this pins: the `spa_type` enums start at a START=0 sentinel, so
`SPA_TYPE_Object` is 15, not 14) makes `pw_stream_update_params` reject every
pod with `!spa_pod_is_object`. These check the exact bytes so that regresses
loudly here rather than silently on a desktop.

The module must also import with no libpipewire present (the binding is lazy),
since the helper's import guard depends on it.
"""

from __future__ import annotations

import struct

from pyguitest_recorder.backends import _pipewire as pw


def _header(pod: bytes) -> tuple[int, int]:
    """A pod's (body size, type) from its 8-byte header."""
    return struct.unpack("<II", pod[:8])


def test_every_pod_is_eight_byte_aligned():
    for pod in (
        pw._pod_id(5),
        pw._pod_int(42),
        pw._enum_format_pod(),
        pw._buffers_pod(),
        pw._meta_cursor_pod(),
    ):
        assert len(pod) % 8 == 0


def test_an_id_pod_has_the_right_type_and_value():
    pod = pw._pod_id(5)
    size, typ = _header(pod)
    assert (size, typ) == (4, pw.SPA_TYPE_Id)
    assert pw.SPA_TYPE_Id == 3  # the corrected ABI value
    assert struct.unpack("<I", pod[8:12])[0] == 5


def test_enum_format_is_a_format_object():
    pod = pw._enum_format_pod()
    _size, typ = _header(pod)
    assert typ == pw.SPA_TYPE_Object == 15  # NOT 14 (that is Struct)
    obj_type, obj_id = struct.unpack("<II", pod[8:16])
    assert obj_type == pw.SPA_TYPE_OBJECT_Format == 0x40003
    assert obj_id == pw.SPA_PARAM_EnumFormat == 3


def test_meta_cursor_requests_the_cursor_with_a_big_enough_size():
    pod = pw._meta_cursor_pod()
    _size, typ = _header(pod)
    assert typ == pw.SPA_TYPE_Object
    obj_type, obj_id = struct.unpack("<II", pod[8:16])
    assert obj_type == pw.SPA_TYPE_OBJECT_ParamMeta == 0x40005
    assert obj_id == pw.SPA_PARAM_Meta == 6
    # The requested cursor-meta type is Cursor (5), and the size range's max
    # must reach Mutter's fixed CURSOR_META_SIZE(384, 384); a smaller max is
    # exactly what made Mutter drop the meta.
    assert pw.SPA_META_Cursor == 5
    mutter_fixed = pw._cursor_meta_size(384, 384)
    assert pw._cursor_meta_size(1024, 1024) >= mutter_fixed
    assert mutter_fixed in range(
        pw._cursor_meta_size(1, 1), pw._cursor_meta_size(1024, 1024) + 1
    )


def test_buffers_pod_is_a_parambuffers_object():
    obj_type, obj_id = struct.unpack("<II", pw._buffers_pod()[8:16])
    assert obj_type == pw.SPA_TYPE_OBJECT_ParamBuffers == 0x40004
    assert obj_id == pw.SPA_PARAM_Buffers == 5


def test_binding_the_library_is_lazy():
    # Importing the module (done above) must not have loaded libpipewire: the
    # helper's import guard, and any no-PipeWire box, depends on that.
    assert pw._pw_lib is None
