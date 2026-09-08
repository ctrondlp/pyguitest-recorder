# Status and known gaps

What has actually been run, and what is still missing. Following pyguitest's
own convention of saying which parts are verified and how, rather than
claiming everything works.

**Early, but the engine is complete and every path has now been run.**

| Part | State |
|------|-------|
| Canonical event model, JSON round-trip | tested, on CI |
| Semantic analyzer (clicks, drags, text, hotkeys, pauses) | tested |
| Synchronization inference | tested |
| Checks (the check key, and what they generate) | tested; not yet run live |
| Script generator + API validation | tested against the installed pyguitest |
| Configuration (TOML, XDG, precedence) | tested |
| CLI (`--doctor`, `--regenerate`) | tested |
| Window/title-drift resolution | tested; drift fix found by a live recording |
| XRecord decoding, keysyms, teardown | tested against synthetic X events |
| XRecord capture of a real application | run live, here and on CI runners |
| AT-SPI element resolution | run live against a real accessibility bus |
| Focus-based targeting for typed text | run live; names a GTK4 field |
| Drag, window switching, save/regenerate | run live |

## The live capture check

`scripts/live-capture-check.py` is what closed the last two rows. It starts a
private Xvfb and a private accessibility bus, puts an application on them,
synthesises real input, and runs the whole pipeline over what comes back —
capture, resolve, normalize, infer, generate, validate. It needs
`xorg-x11-server-Xvfb` (Fedora) or `xvfb` (Debian), and it will not run
against your own session, because XRecord captures every application's
keystrokes and not only the one being recorded. CI runs it on every push, so
the capture backend is not a thing that only works on one developer's machine.

It earns its keep. Running it the first time found a fatal bug no unit test
could have: the RECORD context was created on one display connection and
enabled on the other, which the server refuses with `BadContext`, so capture
had never worked at all. Running it the second time found that the window
context came from the *compositor* rather than the recorded display, so a
recording made on a private X server resolved its clicks onto the editor this
was written in.

**A rootless XWayland is not a substitute for Xvfb.** Verified against a
private headless GNOME session: the RECORD context comes up and disables
cleanly, but neither XTEST nor kernel-level `uinput` can inject into it —
the compositor owns the pointer — so there is no input to record. It is the
same wall described in [architecture.md](architecture.md#why-recording-is-x11-only),
met from the other side.

## Known gaps

- **No UI yet.** The design calls for a timeline, inspector and source preview;
  this is the CLI and the engine underneath it.
- **Only one recording has been made of a real desktop application** — a file
  manager, a text editor and a terminal on GhostBSD. It found three bugs in
  one pass, all now fixed (see the CHANGELOG), the worst of which made any
  recording of an editor fail at replay. The routine live check still uses two
  GTK windows on a private server, so this remains the thinnest-covered part
  of the tool.
- ~~The CI `live` job has never run on a GitHub runner.~~ **Closed:** it now
  runs green on `ubuntu-latest` on every push, so the Ubuntu package names and
  daemon paths are observed rather than reasoned.
- **pyguitest's `Element` has no `double_click`**, only `Session` does. Worked
  around rather than lived with: a double click on a named element emits a
  `double_click_element` helper that looks the element up, reads its rectangle
  *at replay*, and double-clicks there — so the element stays the locator and
  the gesture stays one gesture. It needs the element to have a trustworthy
  rectangle, and falls back to two `Element.click()` calls where it does not,
  which no toolkit is obliged to read as a double click. A triple click has no
  primitive on either path and emits three clicks.
- **Element resolution needs `Capability.ELEMENT_GEOMETRY`**, added upstream
  for this and released in pyguitest 0.4.0. A pyguitest without it declares
  the capability nowhere, so the recorder degrades to coordinates and says so
  rather than failing. (The floor is 0.5.0 for other reasons — see the
  README's install section.)
- **pyguitest cannot look a window up by application id**, only by title
  regex, so a recording whose title drifted carries a small helper function
  into the generated file to do it.
- **pyguitest's X11 backend could not be pointed at a display by argument.**
  Fixed upstream; the recorder still sets `DISPLAY` around the call, which
  works on any version.
- **GTK4 applications cannot be located by AT-SPI hit-testing.** Their
  widgets report a size at the origin and no position, so `element_at`
  returns the frame for essentially every point — measured across
  gnome-calculator (48 of 49 sampled points), baobab (42/49) and
  gnome-text-editor (48/49). Nothing above AT-SPI can recover a position
  that was never published, so **clicks** there come out as coordinates.
  Typed text is the exception and does get named, through focus rather than
  geometry — see
  [architecture.md](architecture.md#typing-goes-where-focus-is-not-where-the-pointer-is).
