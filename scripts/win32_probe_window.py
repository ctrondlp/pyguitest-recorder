#!/usr/bin/env python3
"""A native Win32 window with real, driveable controls, for the live win32 check.

Deliberately not an OS application. Notepad, Calculator, and most of Windows
11's own inbox apps are packaged, single-instance MSIX applications now --
launching one a second time hands off to the process already running rather
than opening a new one, which breaks the assumption that the process a script
launched is the process that owns the window. Notepad goes further and
restores its previous draft session even after the process is killed, so
"kill it and relaunch" is not a clean slate there either. A hand-rolled window
using nothing but stock `user32`/`comctl32` classes (EDIT, BUTTON, STATIC,
SysTabControl32, COMBOBOX, SysListView32, a real menu bar) sidesteps all of it,
the same reason the X11 side of this check uses a purpose-built `python-xlib`
window rather than a real X application. Every one of these classes has UI
Automation support built into Windows itself, with nothing extra to install.

Only the controls belonging to the selected tab are shown -- SysTabControl32
does not manage child visibility on its own, so this window does what any
real application using it raw has to do, on `TCN_SELCHANGE`.

    python win32_probe_window.py --title "Probe" --at 100 100 --size 520 340
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("win32_probe_window.py only runs on Windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
comctl32 = ctypes.windll.comctl32

_WS_OVERLAPPEDWINDOW = 0x00CF0000
_WS_VISIBLE = 0x10000000
_WS_CHILD = 0x40000000
_WS_BORDER = 0x00800000
_WS_TABSTOP = 0x00010000
_BS_PUSHBUTTON = 0x00000000
_BS_AUTOCHECKBOX = 0x00000003
_CBS_DROPDOWNLIST = 0x0003
_SW_SHOW = 5
_SW_HIDE = 0
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_WM_NOTIFY = 0x004E
_BN_CLICKED = 0

_TCM_FIRST = 0x1300
_TCM_INSERTITEMW = _TCM_FIRST + 62
_TCM_GETCURSEL = _TCM_FIRST + 11
_TCN_SELCHANGE = (-550 - 1) & 0xFFFFFFFF  # as it arrives in NMHDR.code, unsigned
_TCIF_TEXT = 0x0001

_CB_ADDSTRING = 0x0143
_CB_SETCURSEL = 0x014E

_MF_STRING = 0x00000000
_MF_POPUP = 0x00000010

_ICC_LISTVIEW_CLASSES = 0x00000001
_ICC_TAB_CLASSES = 0x00000002

_LVS_REPORT = 0x0001
_LVM_FIRST = 0x1000
_LVM_INSERTITEMW = _LVM_FIRST + 77
_LVM_INSERTCOLUMNW = _LVM_FIRST + 97
_LVM_SETITEMTEXTW = _LVM_FIRST + 116
_LVM_SETEXTENDEDLISTVIEWSTYLE = _LVM_FIRST + 54
_LVS_EX_FULLROWSELECT = 0x00000020
_LVCF_WIDTH = 0x0002
_LVCF_TEXT = 0x0004
_LVIF_TEXT = 0x0001

_ID_EDIT, _ID_BUTTON, _ID_LABEL, _ID_COMBO, _ID_CHECKBOX = 1, 2, 3, 4, 5
_ID_LISTVIEW, _ID_TAB = 6, 10
_ID_MENU_DO_THING, _ID_MENU_EXIT = 100, 101
_GENERAL_IDS = (_ID_EDIT, _ID_BUTTON, _ID_LABEL)
_ADVANCED_IDS = (_ID_COMBO, _ID_CHECKBOX)
_LIST_IDS = (_ID_LISTVIEW,)
_TAB_IDS = (_GENERAL_IDS, _ADVANCED_IDS, _LIST_IDS)

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)

user32.DefWindowProcW.argtypes = (
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetDlgItem.restype = wintypes.HWND
user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
user32.SetWindowTextW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
user32.CreateWindowExW.restype = wintypes.HWND
user32.SendMessageW.restype = ctypes.c_ssize_t
# No argtypes on SendMessageW: this window sends an int wParam/lParam for most
# messages, a raw string for CB_ADDSTRING, and a struct pointer for
# TCM_INSERTITEMW and the list-view messages -- ctypes' default per-call
# marshaling (str -> wide string pointer, int -> integer, byref() -> pointer)
# handles all three, where one fixed signature would only fit one of them.
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.CreateMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = (
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
)
user32.SetMenu.argtypes = (wintypes.HWND, wintypes.HMENU)
user32.DrawMenuBar.argtypes = (wintypes.HWND,)


class _WndClass(ctypes.Structure):
    """`WNDCLASSW`."""

    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = (ctypes.POINTER(_WndClass),)
user32.RegisterClassW.restype = wintypes.ATOM


class _TcItemW(ctypes.Structure):
    """`TCITEMW`, only the fields this window actually sets."""

    _fields_ = [
        ("mask", ctypes.c_uint),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
    ]


class _LvColumnW(ctypes.Structure):
    """`LVCOLUMNW`, only the fields this window actually sets."""

    _fields_ = [
        ("mask", ctypes.c_uint),
        ("fmt", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("iOrder", ctypes.c_int),
        ("cxMin", ctypes.c_int),
        ("cxDefault", ctypes.c_int),
        ("cxIdeal", ctypes.c_int),
    ]


class _LvItemW(ctypes.Structure):
    """`LVITEMW`, only the fields this window actually sets."""

    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", ctypes.c_uint),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


class _NmHdr(ctypes.Structure):
    """`NMHDR`: enough of `WM_NOTIFY`'s payload to tell tab switches apart."""

    _fields_ = [
        ("hwndFrom", wintypes.HWND),
        ("idFrom", ctypes.c_void_p),
        ("code", ctypes.c_uint),
    ]


class _InitCommonControlsEx(ctypes.Structure):
    """`INITCOMMONCONTROLSEX`, needed once before the common controls exist."""

    _fields_ = [("dwSize", ctypes.c_uint), ("dwICC", ctypes.c_uint)]


comctl32.InitCommonControlsEx.argtypes = (ctypes.POINTER(_InitCommonControlsEx),)

_click_count = 0
_wndproc_ref: object | None = None
"""The `WNDPROC` ctypes callback trampoline, kept alive at module scope.

A local in `_build_window` is not enough: CPython drops its refcount to zero
the moment that function returns, freeing the trampoline while `RegisterClassW`
still holds a raw pointer to it -- caught live as an access violation, before
a single window existed. The class stays registered for the life of the
process, so the callback backing it has to."""


def _show_tab(hwnd: int, index: int) -> None:
    """Show only the controls that belong to tab `index`; hide the rest."""
    for tab_index, ids in enumerate(_TAB_IDS):
        state = _SW_SHOW if tab_index == index else _SW_HIDE
        for control_id in ids:
            user32.ShowWindow(user32.GetDlgItem(hwnd, control_id), state)


def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    """Handle exactly the messages this probe cares about; default the rest."""
    if msg == _WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if msg == _WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == _WM_COMMAND:
        _handle_command(hwnd, wparam)
        return 0
    if msg == _WM_NOTIFY:
        _handle_notify(hwnd, lparam)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _handle_command(hwnd: int, wparam: int) -> None:
    """React to a button click or a menu selection."""
    global _click_count
    control_id = wparam & 0xFFFF
    notify_code = (wparam >> 16) & 0xFFFF
    label = user32.GetDlgItem(hwnd, _ID_LABEL)
    if control_id == _ID_BUTTON and notify_code == _BN_CLICKED:
        _click_count += 1
        user32.SetWindowTextW(label, f"clicked {_click_count}")
    elif control_id == _ID_MENU_DO_THING:
        _click_count += 1
        user32.SetWindowTextW(label, f"menu {_click_count}")
    elif control_id == _ID_MENU_EXIT:
        user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def _handle_notify(hwnd: int, lparam: int) -> None:
    """React to the tab control's selection-changed notification."""
    header = ctypes.cast(lparam, ctypes.POINTER(_NmHdr)).contents
    if header.code == _TCN_SELCHANGE:
        tab = user32.GetDlgItem(hwnd, _ID_TAB)
        _show_tab(hwnd, user32.SendMessageW(tab, _TCM_GETCURSEL, 0, 0))


def _create(hwnd, hinstance, class_name, text, style, box, control_id):
    """One child control at `box` (x, y, width, height), by its class name."""
    x, y, w, h = box
    return user32.CreateWindowExW(
        0,
        class_name,
        text,
        _WS_CHILD | style,
        x,
        y,
        w,
        h,
        hwnd,
        control_id,
        hinstance,
        None,
    )


def _populate_tabs(tab: int) -> None:
    """Give the tab control its three tabs."""
    for index, label in enumerate(("General", "Advanced", "List")):
        item = _TcItemW()
        item.mask = _TCIF_TEXT
        item.pszText = label
        item.cchTextMax = len(label)
        user32.SendMessageW(tab, _TCM_INSERTITEMW, index, ctypes.byref(item))


def _populate_list(listview: int) -> None:
    """Fill the report-view list with two columns and three rows."""
    # Without this a click only selects on the item's *label*: the centre of the
    # row's UI Automation rectangle is blank space to its right, so a recorded
    # coordinate click there selected nothing at record time and, faithfully,
    # nothing at replay either. Real list views nearly always set it.
    user32.SendMessageW(
        listview,
        _LVM_SETEXTENDEDLISTVIEWSTYLE,
        _LVS_EX_FULLROWSELECT,
        _LVS_EX_FULLROWSELECT,
    )
    for index, (heading, width) in enumerate((("Name", 120), ("Value", 100))):
        column = _LvColumnW()
        column.mask = _LVCF_TEXT | _LVCF_WIDTH
        column.cx = width
        column.pszText = heading
        column.cchTextMax = len(heading)
        user32.SendMessageW(listview, _LVM_INSERTCOLUMNW, index, ctypes.byref(column))
    for row, (name, value) in enumerate(
        (("Alpha", "1"), ("Beta", "2"), ("Gamma", "3"))
    ):
        first = _LvItemW()
        first.mask = _LVIF_TEXT
        first.iItem = row
        first.pszText = name
        user32.SendMessageW(listview, _LVM_INSERTITEMW, 0, ctypes.byref(first))
        second = _LvItemW()
        second.mask = _LVIF_TEXT
        second.iItem = row
        second.iSubItem = 1
        second.pszText = value
        user32.SendMessageW(listview, _LVM_SETITEMTEXTW, row, ctypes.byref(second))


def _build_window(title: str, at: tuple[int, int], size: tuple[int, int]) -> int:
    """Register the window class, create every control, and show the window."""
    global _wndproc_ref
    hinstance = kernel32.GetModuleHandleW(None)
    class_name = "PyguitestRecorderProbeWindow"

    _wndproc_ref = _WNDPROC(_wndproc)
    window_class = _WndClass()
    window_class.style = 0
    window_class.lpfnWndProc = _wndproc_ref
    window_class.hInstance = hinstance
    window_class.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
    window_class.hbrBackground = wintypes.HBRUSH(6)  # COLOR_WINDOW + 1
    window_class.lpszClassName = class_name
    user32.RegisterClassW(ctypes.byref(window_class))

    (x, y), (w, h) = at, size
    menu_bar = user32.CreateMenu()
    actions_menu = user32.CreatePopupMenu()
    user32.AppendMenuW(actions_menu, _MF_STRING, _ID_MENU_DO_THING, "Do Thing")
    user32.AppendMenuW(actions_menu, _MF_STRING, _ID_MENU_EXIT, "Exit")
    user32.AppendMenuW(menu_bar, _MF_POPUP, actions_menu, "Actions")
    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        title,
        _WS_OVERLAPPEDWINDOW | _WS_VISIBLE,
        x,
        y,
        w,
        h,
        None,
        menu_bar,
        hinstance,
        None,
    )

    def add(class_name, text, style, box, control_id):
        return _create(hwnd, hinstance, class_name, text, style, box, control_id)

    tab_style = _WS_VISIBLE | _WS_TABSTOP
    _populate_tabs(
        add("SysTabControl32", "", tab_style, (10, 10, w - 40, h - 60), _ID_TAB)
    )

    # General tab, visible to begin with.
    shown = _WS_VISIBLE | _WS_TABSTOP
    add("EDIT", "", shown | _WS_BORDER, (30, 50, w - 100, 30), _ID_EDIT)
    add("BUTTON", "Click Me", shown | _BS_PUSHBUTTON, (30, 100, 120, 30), _ID_BUTTON)
    add("STATIC", "not clicked", _WS_VISIBLE, (170, 105, 250, 20), _ID_LABEL)

    # Advanced and List tabs, hidden until selected.
    combo = add(
        "COMBOBOX", "", _WS_TABSTOP | _CBS_DROPDOWNLIST, (30, 50, 200, 200), _ID_COMBO
    )
    for item_text in ("Alpha", "Beta", "Gamma"):
        user32.SendMessageW(combo, _CB_ADDSTRING, 0, item_text)
    user32.SendMessageW(combo, _CB_SETCURSEL, 0, 0)
    add(
        "BUTTON",
        "Enable feature",
        _WS_TABSTOP | _BS_AUTOCHECKBOX,
        (30, 100, 180, 25),
        _ID_CHECKBOX,
    )
    listview_style = _WS_TABSTOP | _WS_BORDER | _LVS_REPORT
    _populate_list(
        add("SysListView32", "", listview_style, (30, 50, w - 100, 150), _ID_LISTVIEW)
    )

    _show_tab(hwnd, 0)
    user32.ShowWindow(hwnd, _SW_SHOW)
    user32.UpdateWindow(hwnd)
    user32.DrawMenuBar(hwnd)
    return hwnd


def main() -> int:
    """Open one probe window where the caller asked for it, and wait."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Recorder Probe")
    parser.add_argument("--at", nargs=2, type=int, default=(200, 200))
    parser.add_argument("--size", nargs=2, type=int, default=(520, 340))
    args = parser.parse_args()

    icc = _InitCommonControlsEx()
    icc.dwSize = ctypes.sizeof(_InitCommonControlsEx)
    icc.dwICC = _ICC_TAB_CLASSES | _ICC_LISTVIEW_CLASSES
    comctl32.InitCommonControlsEx(ctypes.byref(icc))

    _build_window(args.title, tuple(args.at), tuple(args.size))

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
