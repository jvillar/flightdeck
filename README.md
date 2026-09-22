<p align="center">
  <img src="docs/img/icon.png" width="112" alt="">
</p>

<h1 align="center">flightdeck</h1>

<p align="center"><b>One screen for every AI coding session you have open.</b></p>

<p align="center"><a href="https://github.com/jvillar/flightdeck/actions/workflows/ci.yml"><img src="https://github.com/jvillar/flightdeck/actions/workflows/ci.yml/badge.svg" alt="CI"></a></p>

![Flightdeck in use](docs/img/demo.gif)

## Why Flightdeck

- 🖥 &nbsp;**A multi-agent terminal.** Claude Code ✳, Codex ⬡ and Antigravity ✦ on one screen.

- 📱 &nbsp;**Sessions that persist across devices.** Close the laptop, open the phone, still running.

- 👤 &nbsp;**Several accounts at once.** See which account every session is spending.

- 🪟 &nbsp;**The same session from several devices at the same time.**

- 🔒 &nbsp;**Remote access that survives an account switch.**

- 🔁 &nbsp;**Sessions that cycle themselves.** A fresh conversation before the old one runs out.

- ⏳ &nbsp;**Know who needs you.** Waiting, asking, working: at a glance, and in the tmux bar.

- 🔌 &nbsp;**Nothing else to run.** tmux, fzf and python. Uninstall gives everything back.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/jvillar/flightdeck/main/install.sh | sh
```

Then open a new shell and type `flightdeck`. The installer never runs a package
manager and never asks for `sudo`; `--yes` takes the default answer to every
question. It needs tmux 3.2, fzf 0.36 and python 3.9 or newer, on macOS, Linux
or WSL2. Everything else is in [`docs/INSTALL.md`](docs/INSTALL.md).

## The keys

| Key | What it does |
|---|---|
| **Enter** | jump into a live session, or resume a past conversation in a new one |
| **Ctrl-F** | open a *copy* of a past Claude Code conversation; the original stays frozen |
| **Ctrl-L** | resume with flags of your choice, editing the whole command first |
| **Ctrl-N** | a new, empty session: a shell you launch whatever you like in |
| **F12** | the switch between the menu and the session you came from |
| **prefix + n** | the handover: close the agent in this pane, open a fresh one in its place |
| **Shift+Enter** | a new line in Claude Code, inside tmux too |

`prefix` is tmux's leading key, `Ctrl-b` out of the box.

## Documentation

- [`docs/GUIDE.md`](docs/GUIDE.md) — the menu, the keys, the context gauge, the
  handover, the notices.
- [`docs/INSTALL.md`](docs/INSTALL.md) — installing, updating, removing; every
  command and flag; the status line.
- [`docs/TOOLS.md`](docs/TOOLS.md) — what Flightdeck sees of Claude Code, Codex
  and Antigravity.
- [`docs/REMOTE.md`](docs/REMOTE.md) — from a phone or another machine, over SSH.
- [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — what it does not do, and why.
- [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) — the code and the tests.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each release.

MIT licence — see [`LICENSE`](LICENSE).
