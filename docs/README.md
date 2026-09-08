# pyguitest-recorder documentation

Record desktop GUI activity and generate [pyguitest](https://github.com/ctrondlp/pyguitest)
scripts from it. For what it is and how to install it, start at the
[project README](../README.md).

## Start here

| File | What's in it |
|------|---------------|
| [getting-started.md](getting-started.md) | From nothing to a test you can run: doctor, record, read what came out, add checks |
| [recipes.md](recipes.md) | Every flag that matters, by the task it serves — regeneration, checks, secrets, key bindings, config |
| [troubleshooting.md](troubleshooting.md) | "Why is my script all coordinates?", and the rest |

## For the applications you record

| File | What's in it |
|------|---------------|
| [testable-guis.md](testable-guis.md) | How to build a GUI that can be tested at all. Written to be handed to application developers — it is the answer when a recording came out as coordinates because of the application rather than the tool |

## Design and internals

| File | What's in it |
|------|---------------|
| [developers/architecture.md](developers/architecture.md) | Why recording is X11 only, why recording and replaying are different questions, and the rules behind every refusal to name an element |
| [developers/status.md](developers/status.md) | What has actually been run and how, plus the known gaps |
