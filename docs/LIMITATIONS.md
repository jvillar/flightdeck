# Known limitations

Everything here is known and accepted. Some of it is the price of a design
decision, some of it is a signal the agents do not give, and some of it is code
that works but has never been run on the machine it is for. All of it is written
down rather than discovered.

Limits that belong to one particular tool — Codex and Antigravity have no "asking
you" or "looping" state, Antigravity's grey rows have no titles, and so on — are
in [`TOOLS.md`](TOOLS.md), one section per tool.

## Four keys stop doing what they used to

**F12 no longer reaches the programs inside tmux.** That is the price of the key
working even while an agent is busy: tmux intercepts it first. If something you
use needs F12, take it back with `tmux unbind -n F12` and use `prefix + j` to
reach the menu instead (and Enter in the menu to come back, since `prefix + j`
only goes one way). The binding returns the next time you enter with `flightdeck`.

**`prefix + n` is no longer "next window".** The handover keeps that key. You lose
little: Flightdeck uses one window per session and hides the window list, so
moving between windows led nowhere. If you want it back,
`tmux bind-key -T prefix n next-window` — and it goes back to the handover the
next time you enter with `flightdeck`.

**`prefix + j` is taken too**, for the one-way trip to the menu.

**Shift+Enter goes to Flightdeck first.** In a pane running Claude Code it becomes
the backslash + Enter that Claude Code takes as a new line; in any other pane it
is passed on as Enter, which is what it always was under tmux. To make the
terminal report the key at all, Flightdeck also sets tmux's `extended-keys`
option to `on`: tmux then hands modified keys in their extended form to programs
that ask for them, and to nobody else. If a program of yours behaves differently
with that option, `tmux set -s extended-keys off` puts it back until the next
time you enter the menu.

`flightdeck quit` gives tmux all four back, along with its own status bar and its
`extended-keys` default, and leaves your work sessions running.

## Notices live inside tmux, and nowhere else

When a session finishes, the message appears in tmux's status bar, on every
screen attached to that tmux — your Mac and your phone at once. There are no
macOS notifications, no desktop pop-ups and no push. That is deliberate: a
cockpit you look at is one thing, a program that interrupts you on every device is
another.

If tmux is not running, the notices simply do not happen: silently, with no error,
and without disturbing the agent that triggered them.

## The tmux configuration does not survive a tmux restart

Flightdeck's key bindings, status bar and refresh hook live **inside the running
tmux server**, not in a file. If that server dies — you reboot, or you run
`tmux kill-server` — they go with it.

In practice this does not matter, because `flightdeck` reapplies all of it every
single time you enter the menu. It only bites if you kill the server and then
reconnect with a bare `tmux attach`: no F12, no bar. Type `flightdeck` instead.

This is also why you do not have to touch your own `~/.tmux.conf`.

## Sessions running outside tmux cannot be adopted

An agent that was started outside tmux — from a desktop app, or in a plain
terminal window — cannot be moved into it. Those rows are listed dimmed (`◇`) so
you know they exist, and that is all they are. Pressing Enter on one tells you so.

This is a technical limit, not laziness: a process that already has a terminal
cannot be handed a different one.

## Conversations sent to the background

Claude Code can send a conversation to the background, with `/background` (or
`/bg`) and also — easy to do by accident — with the **`←` arrow pressed while the
text box is empty**, which is its "← for agents" shortcut. From then on the work
runs in a process of Claude Code's own daemon, outside tmux, and what stays in
your pane is a parked agent that only watches.

Flightdeck notices this and shows **the state of the background work** on the
pane's row, marked `· background`. Without that, the row would sit on a stale
state for hours.

Two things follow. The handover on such a pane does not type `/exit` — there, that
opens the agents view, and typing text in that view creates a *new* background
task. It sends `Ctrl-C` as many times as it takes instead, checking between each
one, which does close it. And **the background conversation survives the
handover**: the closing notice tells you its job id, and it reappears in the menu
as `◇ outside tmux`. To bring it back into a terminal, press `←` in any Claude
Code session and Enter on it; to answer it without bringing it back, Space.

Once it is back in a terminal it stops being an `◇ outside tmux` row: Flightdeck
recognises the viewer Claude Code opens for it and paints that pane as the
conversation, with its name, its state and the same `· background` mark. The
handover does not run on a pane like that — how that viewer closes has not been
measured, so it tells you and touches nothing.

## "Looping" is worked out, not announced

A `🔁 looping` row means that agent has finished a round of a self-scheduled loop
and is waiting for **its own timer**, not for you. Claude Code knows which of you
started each turn, but it does not pass that to its hooks in the public versions,
and the loops themselves live in the agent's memory rather than on disk.

So Flightdeck reads the end of the conversation transcript instead: a scheduled
round leaves a trace there, and so does scheduling one. The rule is that a turn is
*looping* if a timer started it or if a new round was scheduled during it, and
*waiting for you* if you typed it yourself (you asked, it answered — that does wait
for you) or if the loop was stopped during it.

If a future version of Claude Code changes what it writes there, this degrades to
`⏳ WAITING FOR YOU`, which is what it said before the state existed. It never
degrades to something worse.

The handover **refuses** on a looping session: the loop lives in the memory of the
agent being closed, and the new one would not inherit it.

## An older tmux or fzf loses a feature each

Neither stops Flightdeck working, and `flightdeck doctor` says which layer you are
on.

**tmux 3.2 and 3.3** (Ubuntu 22.04 and Debian 12) cannot print a floating notice
literally — that arrives in 3.4. Flightdeck escapes the text instead, so a session
name containing tmux's own syntax is *shown* and never *run*. The visible
difference: a `#` in a name appears doubled inside the notice.

**fzf below 0.71** has no way to follow a row's identity across a reload, so when
the list reorders itself while you are looking at it the cursor stays where it is
on screen rather than on the session it was on. **fzf below 0.36** cannot reload at
all while it is open: the list refreshes when you come back to it instead of as
things happen. That is why 0.36 is the floor, and why the installer offers to
download a newer one.

## Uninstalling from inside the menu

`flightdeck uninstall` run from the menu's own shell does only the tmux half of
the job and then stops, because that half kills the session it is running in. Run
it again from an ordinary terminal and it finishes: it is idempotent, so nothing
is done twice.

## Updating a git checkout

`flightdeck update` refuses to run from a clone and tells you to use `git pull`
instead. It manages the installed copy under `~/.local/share/flightdeck` and
nothing else. For the same reason, `flightdeck uninstall` from a clone removes
everything *except* the clone: somebody else's working directory is not an
uninstaller's to delete.

## Written and tested, but never run on the real thing

These work as far as anyone can tell on the machine they were written on, and
nobody has yet run them where they matter. They are listed because you may be the
first.

- **WSL2.** Detecting it, and Windows Terminal actually delivering F12 to tmux,
  have been reasoned through and tested against made-up systems, never on a real
  Windows machine.
- **The migration from the pre-release cockpit Flightdeck was ported from.** The
  code that takes that cockpit's entries back out of the three agents has only
  ever run against made-up copies of its configuration files.

## Out of scope

Not missing — decided against.

- **Native Windows.** Flightdeck is tmux and a POSIX shell. WSL2 is the answer,
  and it is a good one.
- **Package managers as the main way to install.** `curl … | sh` is the route that
  works the same everywhere; a Homebrew formula or a `.deb` may come later, but
  they will never be the thing that has to work.
- **Translating the interface.** English only, including the notices and the
  badges.
- **Switching accounts automatically.** A program that rotates accounts by itself
  to carry on past a usage limit breaks the terms of every provider that has them,
  and it is the pattern that gets accounts banned. Flightdeck tells you where you
  stand and takes you to your own switcher — as a pinned row — and you press the
  button. There is no version of this where the trigger is not human.
- **Integrating more tools.** Claude Code, Codex and Antigravity are what
  Flightdeck understands. Anything else still works inside a Flightdeck session,
  because a session is a shell: you just do not get its state on the row.
