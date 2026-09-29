#!/usr/bin/env python3
"""A native Cocoa window with real, driveable controls, for the live macOS check.

The macOS counterpart of `win32_probe_window.py`. Deliberately smaller than
that one: this is new ground for the recorder rather than an attempt at
parity, so one push button, one checkbox, two radio buttons in a single
group, and one editable text field is enough to exercise named-element
resolution, a click, a toggle, a selection, and text entry. Every one of
these AppKit control types (`NSButton` in its various button types,
`NSTextField`) is Accessibility-visible with no extra work -- see
`docs/testable-guis.md`'s own claims about AppKit -- which is what makes a
hand-rolled window here as cheap as the Win32 one is on that platform.

A plain `NSButton`/`NSTextField` window rather than a packaged application,
for the same reason the Win32 probe avoids Notepad and Calculator: this is a
process the check script launches and kills itself, and a single-instance
packaged app would hand off to one already running instead of giving the
check a fresh window every time.

    python macos_probe_window.py --title "Probe"
"""

from __future__ import annotations

import argparse
import sys

if sys.platform != "darwin":
    sys.exit("macos_probe_window.py only runs on macOS")

import AppKit  # noqa: E402
import objc  # noqa: E402
from Foundation import NSObject  # noqa: E402

# Named rather than imported directly: older PyObjC exposes these AppKit enum
# values only under their classic spellings (`NSSwitchButton`, `NSRadioButton`),
# newer PyObjC under the `NSButtonType*` names -- both are the same numbers
# (3 and 4), so falling back rather than picking one import keeps this probe
# running on whatever PyObjC the machine actually has.
_BUTTON_TYPE_SWITCH = getattr(
    AppKit, "NSButtonTypeSwitch", getattr(AppKit, "NSSwitchButton", 3)
)
_BUTTON_TYPE_RADIO = getattr(
    AppKit, "NSButtonTypeRadio", getattr(AppKit, "NSRadioButton", 4)
)


class _Controller(NSObject):
    """Owns the one piece of state a click changes, and the label that shows it.

    A target/action selector has to live on a real `NSObject`, which a bare
    Python closure cannot be -- this is the smallest thing that can hold both
    the counter and a reference to the label it updates.
    """

    def init(self):
        """Set up the counter and the (not yet known) label to update."""
        self = objc.super(_Controller, self).init()
        if self is None:
            return None
        self.click_count = 0
        self.label = None
        return self

    def clickMe_(self, _sender: object) -> None:
        """Handle the push button: bump the counter and show it in the label."""
        self.click_count += 1
        if self.label is not None:
            self.label.setStringValue_(f"clicked {self.click_count}")


def _add(view, control) -> None:
    """Add one child control to `view`, and return it for chaining."""
    view.addSubview_(control)


def _label(text: str, frame) -> AppKit.NSTextField:
    """A read-only static text field -- AX's `AXStaticText`, role `label`."""
    field = AppKit.NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    return field


def _build_window(
    title: str, at: tuple[int, int], size: tuple[int, int]
) -> tuple[AppKit.NSWindow, _Controller]:
    """Create the window and every control, and show it on screen."""
    (x, y), (w, h) = at, size
    style = (
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskClosable
        | AppKit.NSWindowStyleMaskMiniaturizable
        | AppKit.NSWindowStyleMaskResizable
    )
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(x, y, w, h),
        style,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setTitle_(title)
    content = window.contentView()

    controller = _Controller.alloc().init()

    # An editable text field -- AX's `AXTextField`, role `entry`.
    entry = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, h - 70, 200, 24)
    )
    entry.setEditable_(True)
    entry.setSelectable_(True)
    entry.setBezeled_(True)
    _add(content, entry)

    # A push button -- AX's `AXButton`, role `push button`.
    button = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(240, h - 74, 110, 32)
    )
    button.setTitle_("Click Me")
    button.setBezelStyle_(AppKit.NSBezelStyleRounded)
    button.setTarget_(controller)
    button.setAction_("clickMe:")
    _add(content, button)

    # The status label a click updates -- AX's `AXStaticText`, role `label`.
    status = _label("not clicked", AppKit.NSMakeRect(20, h - 100, 240, 20))
    _add(content, status)
    controller.label = status

    # A checkbox -- AX's `AXCheckBox`, role `check box`.
    checkbox = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, h - 140, 200, 24)
    )
    checkbox.setButtonType_(_BUTTON_TYPE_SWITCH)
    checkbox.setTitle_("Enable feature")
    checkbox.setState_(AppKit.NSControlStateValueOff)
    _add(content, checkbox)

    # Two radio buttons in one group -- AX's `AXRadioButton`, role `radio
    # button`. AppKit ties same-type radio siblings sharing one superview into
    # one mutually-exclusive group on its own, with no extra container needed
    # -- the checkbox and the button above are a different button type, so
    # they do not join it.
    radio_one = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, h - 180, 120, 24)
    )
    radio_one.setButtonType_(_BUTTON_TYPE_RADIO)
    radio_one.setTitle_("One")
    radio_one.setState_(AppKit.NSControlStateValueOn)
    _add(content, radio_one)

    radio_two = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, h - 210, 120, 24)
    )
    radio_two.setButtonType_(_BUTTON_TYPE_RADIO)
    radio_two.setTitle_("Two")
    radio_two.setState_(AppKit.NSControlStateValueOff)
    _add(content, radio_two)

    window.makeKeyAndOrderFront_(None)
    window.setLevel_(AppKit.NSNormalWindowLevel)
    return window, controller


def main() -> int:
    """Open one probe window where the caller asked for it, and run until killed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Recorder Probe")
    parser.add_argument("--at", nargs=2, type=int, default=(200, 200))
    parser.add_argument("--size", nargs=2, type=int, default=(480, 300))
    args = parser.parse_args()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    # Kept alive at function scope for the life of the process -- PyObjC drops
    # an object's refcount to zero the moment nothing in Python still holds
    # it, same hazard the Win32 probe's own `_wndproc_ref` comment describes
    # for its callback trampoline, and the target/action selectors above hold
    # a weak reference from Cocoa's side, not a strong one from this side.
    _window, _controller = _build_window(args.title, tuple(args.at), tuple(args.size))
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
