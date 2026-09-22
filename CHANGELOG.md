# Changelog

What changed in each release, in the words of somebody using it. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions are
[semantic](https://semver.org/spec/v2.0.0.html).

The heading of a released version carries its date. `release.yml` reads the
section for the tag it is building and makes it the release notes, so a version
with no section here does not get released.

## v0.1.1 — 2026-09-22

### Fixed

- **Shift+Enter in Claude Code inside tmux sent the prompt** instead of adding a
  line. Claude Code tells the two keys apart with a keyboard protocol it asks the
  terminal for, and tmux neither speaks it nor passes the request on. Entering
  the menu now sets tmux's `extended-keys` on, so the terminals tmux recognises
  (iTerm2, kitty, WezTerm, Ghostty, foot) report Shift+Enter as its own key, and
  binds it, in a pane running Claude Code, to the backslash + Enter that Claude
  Code takes as a new line anywhere. Other panes still get Enter; `quit` and
  `uninstall` undo both. Terminal.app cannot tell the keys apart: there it is
  Ctrl+J, which works everywhere.

### Added

- **`flightdeck install` says how to get the account switcher** when `cswap` is
  not on the machine: it is the `claude-swap` package, and the hint the doctor
  gives for a pinned-but-missing `cswap` now says so too, instead of calling it a
  personal tool.

## v0.1.0 — 2026-09-22

The first release.

### Added

- **A menu of every session you have open**, full screen in tmux, reached with
  F12 or `flightdeck`. It lists your live tmux sessions, your past
  conversations and the agents running outside tmux, and Enter opens one.
- **What each agent is doing**, on its row and in the tmux bar: waiting for you,
  working, asking you a question, going round a loop, or sent to the background.
- **How much room the conversation has left** (🧠), on the row and in the bar,
  with a notice when it passes 80 % so a handover is your decision and not a
  surprise.
- **Handover on `prefix+n`**: it closes the agent in this pane and opens the next
  one, numbered ("pricing 3"), with the same flags the old one was running.
- **The history, three ways**: Enter carries on with that conversation, Ctrl-F
  copies it so you can look at a frozen point without touching it, and Ctrl-L
  picks flags from a list and lets you edit the whole command first.
- **Three tools**, side by side and each with its own mark: Claude Code, Codex
  and Antigravity.
- **One status line** for Claude Code and Antigravity, in the mode you choose:
  Flightdeck's own, wrapped around the one you already had, or both stacked.
- **A status line of three lines**: who you are (the model, the account you are signed in with, the folder and
  branch), what you have spent (a fifteen-cell context bar, then every rate
  limit with its own bar and reset time), and the tokens. Each line fits itself
  to the pane on its own, so a narrow terminal loses the branch and the reset
  times rather than a bar. `"statusline": {"lines": 2}` in `config.json` brings
  back the compressed two-line layout.
- **The account you are signed in with, in the status line**: `👤` and your
  address after the model, with your plan beside it. The address comes from
  `~/.claude.json` and the plan from your Claude Code credentials (the keychain
  on macOS); Antigravity sends both itself, so one key covers both lines.
  Neither value is ever written down or put in a notice, and on a narrow pane
  this is the last thing the first line drops. A status line does end up in
  screenshots and screen shares: `"statusline": {"account": false}` takes it
  off.
- **Two lines under the menu that follow the cursor**: the folder of the
  highlighted session and its git branch, then the tool, the conversation's
  whole title, its state in plain words, its age and the first eight characters
  of its id. It is there so two sessions of the same project stop looking
  alike. `?` hides it and shows it again; while it is bound to the strip, a
  question mark can no longer be typed into the search box.
- **Pinned rows**: fixed entries for the commands you keep open — right after
  your live sessions and before the history — with presets so `flightdeck pin add
  htop` is all you have to type.
- **Configuration in one file** (`config.json`), and everything else under the
  standard XDG directories.
- **Installation in one line** (`install.sh`), and `flightdeck install`,
  `doctor`, `update` and `uninstall` to set up, check, upgrade and remove it.

### Requirements

- tmux 3.2 or newer (3.4 and newer print the floating notices more faithfully)
  and fzf 0.36 or newer (0.71 keeps the cursor on the same session when the list
  is reordered). Below each of those the feature degrades; `flightdeck doctor`
  says which layer you are on.
- macOS, Linux and WSL2. On Windows, Flightdeck runs inside WSL2.
