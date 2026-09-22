# Installing Flightdeck

Everything about getting Flightdeck onto a machine, checking it, updating it and
taking it back off. If you only want to get going, the first two sections are
enough.

- [What you need](#what-you-need)
- [The one-line install](#the-one-line-install)
- [What `install.sh` does, step by step](#what-installsh-does-step-by-step)
- [The lifecycle commands](#the-lifecycle-commands)
- [The status line](#the-status-line)
- [Pinned rows](#pinned-rows)
- [Where everything lives](#where-everything-lives)
- [Installing from a git checkout](#installing-from-a-git-checkout)

## What you need

Five things, and on most machines you already have four of them.

| What | Minimum | Recommended | Why |
|---|---|---|---|
| tmux | 3.2 | 3.4 | every session lives inside tmux |
| fzf | 0.36 | 0.71 | fzf *is* the menu |
| python3 | 3.9 | any newer | Flightdeck is python and its standard library, nothing else |
| curl | any | — | how the release is fetched |
| tar | any | — | how the release is unpacked |

Installing the two that are usually missing:

```sh
brew install tmux fzf          # macOS
sudo apt install tmux fzf      # Debian, Ubuntu
sudo dnf install tmux fzf      # Fedora, RHEL
```

Flightdeck never runs any of those for you. It prints the line for your machine
and stops; installing software is your decision, not an installer's.

On **macOS**, `python3` comes with the Command Line Tools. If `python3` is not
found, run `xcode-select --install` once.

Your terminal also needs a **UTF-8 locale**, because the menu is made of glyphs
(⏳ ❓ 🧠 and the tool marks). If neither `LANG` nor `LC_ALL` says UTF-8 they come
out as question marks, which looks like a broken install and is not one. The fix
is one line in your shell's startup file:

```sh
export LANG=en_US.UTF-8
```

### What each system ships, and what that means

| System | tmux | fzf | Verdict |
|---|---|---|---|
| macOS (Homebrew) | 3.6a | 0.72 | everything works |
| Ubuntu 24.04 | 3.4 | 0.44 | fine; the cursor does not follow a row when the list is reordered |
| Ubuntu 22.04 | 3.2a | 0.29 | tmux works with the escaped-notice fallback; fzf is **below the minimum**, so the installer offers to download one |
| Debian 12 | 3.3a | 0.38 | tmux works with the fallback; fzf is fine but without the stable cursor |
| Debian 13 | 3.5a | 0.60 | fine; no stable cursor |

Two things degrade rather than break, and it is worth knowing which is which.

**tmux below 3.4** cannot print a floating notice literally (`display-message
-l` arrives in 3.4). Flightdeck escapes the text instead, so a session called
`#(something)` is shown and never *run*. What you lose: a `#` in a session name
is shown doubled in the notice. Nothing else.

**fzf below 0.71** has no `--id-nth`, so when the list reorders itself while you
are looking at it, the cursor stays where it is on screen instead of following
the row it was on. **fzf below 0.36** has no `--listen` either, and then the menu
cannot refresh while it is open at all: it repaints when you come back to it.
That is why 0.36 is the floor and the installer offers a download.

### Windows

Native Windows is not supported: Flightdeck is tmux and a POSIX shell, and
neither exists there. It runs inside **WSL2**, which is a real Linux running on
Windows:

```powershell
wsl --install
```

Open Ubuntu from the Start menu and run the install command in there. Everything
— Claude Code, tmux, fzf, python3 and Flightdeck — lives inside WSL2 and not in
Windows. Windows Terminal sends F12 through to it, so the menu key works.

If you run `install.sh` from Git Bash, MSYS or a Windows command prompt it stops
and tells you this.

## The one-line install

```sh
curl -fsSL https://raw.githubusercontent.com/jvillar/flightdeck/main/install.sh | sh
```

Then **open a new shell** (the installer may have just added a line to your
shell's startup file) and type `flightdeck`.

Its options:

| Option | What it does |
|---|---|
| `--yes` | take the default answer to every question. `FLIGHTDECK_YES=1` in the environment does the same |
| `--version vX.Y.Z` | install that release instead of the newest one |
| `--force` | let the agent setup replace items somebody else put in codex's status line |
| `--keep-legacy` | leave an older cockpit's entries in place (see below) |

Options go after `-s --`, which is how `sh` is told they are for the script and
not for itself:

```sh
curl -fsSL https://raw.githubusercontent.com/jvillar/flightdeck/main/install.sh | sh -s -- --yes
curl -fsSL https://raw.githubusercontent.com/jvillar/flightdeck/main/install.sh | sh -s -- --version v0.1.0
```

**About `--keep-legacy`.** If you are moving from the pre-release cockpit
Flightdeck was ported from, `--keep-legacy` installs Flightdeck beside it
without migrating its entries, so both run at once while you satisfy yourself
the new one works; run `flightdeck install` again *without* the flag when you
are ready to switch, and do not run `flightdeck update` in between, because an
update migrates. On any other machine there are no legacy entries and the flag
does nothing.

## What `install.sh` does, step by step

Six steps, in this order, and it says each one out loud as it goes.

**1. This machine.** macOS, Linux or WSL2. Native Windows stops here.

**2. What Flightdeck needs.** tmux, python3, curl and tar are checked and a
missing one is the end of the run, with the exact command for your machine. A
tmux whose version cannot be read (one built from master, say) is a warning and
the install carries on. `claude` not being on PATH is also only a warning:
Flightdeck installs fine, it is just a cockpit with nothing flying in it yet.

Then **fzf**. If yours is missing or older than the recommended 0.71, you are
offered the official fzf release:

```
Download fzf 0.74.4 from its GitHub release to ~/.local/share/flightdeck/bin? [Y/n]
```

It is a pinned version, downloaded from fzf's own GitHub release and **checked
against its published sha256** before anything is unpacked. A download that does
not match its checksum is deleted and the install stops. It lands in
`~/.local/share/flightdeck/bin`, which only the `flightdeck` command puts on its
own PATH — nothing goes into a system directory, nothing goes behind your package
manager's back, and `flightdeck uninstall` takes it away with the code.

Saying no is fine when you already have an fzf of at least 0.36: you get a
warning and the install carries on. Saying no with no fzf at all, or with one
below 0.36, stops the install, because there is no menu to draw without one.

When there is **no terminal to ask on** — a scripted install, a CI job — the
prompt's own default is taken, which is yes. The only thing that question ever
downloads is fzf's official release, checksummed, into a directory that only
Flightdeck sees, so a headless install simply works.

**3. The terminal.** The UTF-8 check described above.

**4. The code.** The release tarball is downloaded and unpacked into
`~/.local/share/flightdeck/current`. What the archive holds is read *before* a
byte of it is written: anything with an absolute path, anything climbing out with
`..`, any link pointing outside, anything that is not a plain file, a directory or
a symlink — any one of those and nothing is extracted at all. The swap into place
is two renames
inside one directory, so `current` is never half a Flightdeck, and if the second
one fails the old code goes back.

**5. The command.** `~/.local/bin/flightdeck` is linked to the code, and so is the
short name `fld` — unless something else already answers to `fld`, in which case
yours is left exactly where it is and you are told so.

Then, **only if `~/.local/bin` is not already on your PATH**, one line is added to
your shell's startup file, ending in a marker so it can be found and removed
later:

```sh
export PATH="$HOME/.local/bin:$PATH"  # flightdeck
```

Which file: `~/.zshrc` for zsh; `~/.bashrc` on Linux and `~/.bash_profile` on
macOS for bash (a login shell there reads that one); `~/.config/fish/config.fish`
for fish, in fish's own spelling. Any other shell is told the line and nothing is
written. There is a timestamped backup before the first write, and a second
install writes nothing and makes no second backup.

The line does not apply to the shell you are in. **Open a new one**, or `source`
the file, before typing `flightdeck`.

**6. Setting up the agents.** The script hands over to `flightdeck install`, which
is the next section.

## The lifecycle commands

### The menu itself: `flightdeck`, `restart`, `init`, `quit`

Four verbs, and none of them ever touches your work sessions or the agents running
inside them.

**`flightdeck`**, with no arguments, is the normal way in. It creates the tmux
session called `flightdeck` if it is not there, configures tmux (the keys, the
status bar, the hook that keeps the list up to date) and puts you inside. It
reapplies that configuration **every** time you enter, which is why the next two
are rarely needed.

**`flightdeck restart`** gives you a fresh menu, running the code that is on disk
now. Use it after updating Flightdeck by hand: the menu you are looking at runs the
code its pane started with, and F12 will not rebuild it while it is alive. Your
work sessions and your pinned sessions carry on untouched — a pin runs a command
of yours, and killing it to pick up code of ours is not the same trade.

**`flightdeck init`** does only the tmux configuration step, and **needs a tmux
server already running**: with none there is nowhere to put the keys and bindings,
so it says so and exits with an error rather than pretending. In practice you do
not need it; it is for when you are already inside tmux and want to refresh the
keys without going through the menu.

That configuration lives inside the **running tmux server**, not in a file. If the
server dies — you reboot, or you run `tmux kill-server` — it goes with it, and the
next `flightdeck` puts it back. That is why you never have to touch your own
`~/.tmux.conf`.

**`flightdeck quit`** turns the cockpit off: the menus are killed and tmux gets its
own status bar and its own keys back (`prefix + n` is "next window" again; F12,
Shift+Enter and `prefix + j` are unbound), and if you have a `~/.tmux.conf` of your own it is
sourced again so your settings come back on top. Your work sessions, their agents
and your pinned sessions keep running, and `flightdeck` turns the cockpit back on
whenever you want.

### `flightdeck install`

Sets up every agent on the machine. `install.sh` runs it for you at the end; you
run it yourself after changing something, or to repair an installation. It is
idempotent — running it again is always safe — and every file it writes outside
Flightdeck's own directories gets a timestamped backup first
(`<file>.bak-flightdeck-<timestamp>`).

What happens, in order:

1. **The demo.** It shows you what the Flightdeck status line looks like, on your
   own terminal, before asking anything about it.
2. **Claude Code**, always. Seven hook events go into `~/.claude/settings.json`,
   and the status line setting is handled according to your answer to the one
   question it asks:

   ```
   Use the Flightdeck status line for Claude Code? Your current one is backed up
   and restored on uninstall. [Y/n]
   ```

   Yes means Flightdeck's line; no means the line you already had, with
   Flightdeck reading the context percentage through it. Whatever you had is
   saved before anything is registered.
3. **codex**, and **Antigravity (`agy`)**, each only when it is on PATH. A tool you
   do not have is skipped with one line. agy gets the same status line question as
   Claude Code; codex has no external status line command, so it gets its own
   built-in items arranged in the same order instead.
4. **Pinned rows.** It notices the tools on your machine that have a ready-made
   pin and offers to add them (see [Pinned rows](#pinned-rows)).
5. **The tmux keys.** If a live tmux server is running, it reads your bindings and
   warns about any of F12, Shift+Enter, `prefix j` and `prefix n` that somebody else has
   already taken, saying what each one was bound to. It does not start a tmux
   server to find out.
6. **The doctor**, whose report is printed in full and whose verdict is half of
   the command's exit code.

Three questions at most, and they are asked **once**: the status line for each tool
that has one (Claude Code, and Antigravity if you have it), and the pins. A mode
already recorded in your `config.json` is a choice you made, and reinstalling —
which is what `flightdeck update` does — never undoes it.
With `--yes`, with `FLIGHTDECK_YES=1`,
or with nothing on the other end of stdin, nobody is asked anything: the status
line becomes Flightdeck's for someone who had none and stays theirs for someone
who had one — a line nobody was shown the demo for is not replaced behind their
back — and the pins it found are added.

Its own options: `--yes`, `--force` (replace codex's status line items even if you
set your own) and `--keep-legacy`.

After a first install, restart the Claude Code sessions you already have open:
hooks are registered when a session starts. Until you do, those sessions still
get a row of their own — Flightdeck reads Claude Code's own register of open
sessions for their name, their state and their age — but nothing about them
updates: no floating notices, no list redrawing itself when one of them starts
waiting for you, and no `🧠` unless Flightdeck's status line is already running
there. And the next codex you start will say
"N hooks are new or changed" — answer "Trust all and continue"; those are these.
agy does not ask.

### `flightdeck doctor`

Reads the machine and prints one line per check. It **writes nothing, ever**: it
does not create directories, does not start a tmux server, does not run an agent
and does not repair anything behind your back.

It checks: the system; tmux, fzf and python3 with their versions; which agents are
installed; whether the `flightdeck` command resolves on your PATH; `config.json`;
the state directory; Claude Code's hooks; codex's and agy's hooks, when those
tools are there; the status line for Claude Code and for agy; the four tmux keys;
your pins; the locale; and a note when you are on WSL2.

Each line carries one of three marks, and the difference matters:

| Mark | Means |
|---|---|
| `✓` | fine |
| `!` | worth a look, but Flightdeck works. An agent you have not installed, an unreadable `config.json` (the defaults are used), no tmux server running, a key somebody else took, a pin whose tool is gone, a locale that is not UTF-8 |
| `✗` | **Flightdeck cannot work like this.** No tmux or one below 3.2, no fzf or one below 0.36, python below 3.9, a state directory it cannot write into, one of our hooks registered but pointing at code that is no longer there, or the command linked but not resolvable |

The last line says which it found, and the command **exits non-zero only when
there is a `✗`**. Most lines with a mark other than `✓` also carry a `fix:` line
saying what to do. One of those is worth knowing: when a tool's own configuration
file cannot be parsed at all, the fix is to repair it **by hand**, never to
reinstall — an installer reads an unreadable file as an empty one and would
rebuild it, dropping whatever of yours was inside.

### `flightdeck update [--version vX.Y.Z]`

Fetches the newest release (or the one you name), puts it in place, reinstalls and
restarts the menus. If you are already on that version it says so and swaps
nothing.

The code is replaced *behind* the name `current`, because the command link and the
paths in the three agents' settings all point inside it — so an update never has
to touch a hook. The code it replaced is kept as `previous`.

Run from a git checkout it refuses and tells you to use `git pull`: it manages the
installed copy and nothing else.

### `flightdeck uninstall [--purge]`

Puts the machine back, and prints every single thing it removes.

In order: tmux's own options, keys and bar come back and the menus are killed —
**your work sessions and the agents inside them carry on**, and so do your pinned
sessions; then Flightdeck's hooks come out of Claude Code, codex and agy, with
**your original status line and your original codex notify command restored from
the saved copies**; then the `~/.local/bin` links, but only the ones that land
inside Flightdeck's own code, so somebody else's `fld` survives; then the
`# flightdeck` line in your shell startup file, with a backup and not one other
byte of that file changed; and last the code itself.

It **keeps** your state and your `config.json` unless you pass `--purge`. That is
deliberate: the saved copies of your own status line live in there, and somebody
who reinstalls tomorrow wants them.

Run from a git checkout it does everything else and leaves the code where it is: a
clone is not an installer's to delete.

One thing to know: **run it from a shell outside the menu**. Started from the
menu's own shell, the tmux half kills the session it is running in and the rest
never happens; run it again from an ordinary terminal and it finishes the job.

## The status line

![Flightdeck status line](img/statusline.png)

Flightdeck's status line is **three lines**, each answering one question.

```
✳ Fable 5.1 · xhigh │ 👤 you@example.com max │ ~/…/claude/flightdeck │ main
Ctx █████████████░░ 88% │ 5h █░░░░░░░░░ 11% ↻06:30 │ 7d ███░░░░░░░ 34% ↻23/09 22:00
Tokens: msg 1.1k │ cache 882.9k │ session 884.0k │ turns 716
```

The first is who you are: the tool's mark, the model and how hard it is
thinking, the account you are signed in with, the folder and the git branch. The
second is what you have spent: a fifteen-cell bar of context with its
percentage, then every rate limit with its own bar and the time it resets. The
third is the tokens — plus, for Claude Code, how many turns the conversation has
had. Antigravity does not publish that number, so its third line is the tokens
alone.

Each line fits itself to the pane on its own. A narrow terminal loses the branch
before the folder and the reset times before a rate limit, and it never loses
the context bar. The third line is the exception: once the turn count has gone
there is nothing left to shed but the three figures themselves, so below about
48 columns your terminal wraps it rather than Flightdeck cutting a number in
half.

Two things about it can be turned off, and nothing has to be turned on:

| In `config.json` | What it does |
|---|---|
| `{ "statusline": { "lines": 2 } }` | the older, compressed layout: the first two lines on one row, with a ten-cell context bar |
| `{ "statusline": { "account": false } }` | takes the `👤` segment off — the key to reach for before sharing a screen |

See it without installing anything:

```sh
flightdeck statusline --demo
```

Three modes, and you can change your mind at any time:

| Mode | What you see |
|---|---|
| `own` | Flightdeck's line |
| `wrap` | the line you already had; Flightdeck only reads the context percentage as it passes through |
| `stack` | Flightdeck's line, with yours underneath it |

```sh
flightdeck statusline --mode own              # for Claude Code
flightdeck statusline --mode stack --tool agy # for Antigravity
flightdeck statusline --status                # which mode, what is saved, what is registered
flightdeck statusline --restore               # put your own line back, keeping the saved copy
```

Without `--tool`, these act on Claude Code.

**codex is different**: it has no way to run an external command for its status
line, only a list of its own built-in items. So Flightdeck arranges codex's own
items in the order of its line and turns its colours on, written into the `[tui]`
section of `~/.codex/config.toml`. If you had already chosen items of your own
they are left alone, unless you install with `--force`. There is no bar and no
tool mark there: it is codex's own text, and there is nothing more to be had.

**Wherever the line comes from, Flightdeck reads the context percentage through
it.** That is where the 🧠 on the menu row and in the tmux bar comes from, and the
notice when a conversation passes 80 %. It works in all three modes.

### The account you are signed in with

The first line shows the account you are signed in with, right after the model:
`👤 you@example.com max`, the address in blue and the plan beside it. It is
**on** by default, because knowing which account is spending is part of what
the line is for. A status line does end up in screenshots and screen shares, so
there is one key that takes it off:

```json
{ "statusline": { "account": false } }
```

The address comes from `~/.claude.json`, and the plan from wherever Claude Code
keeps your credentials: the file `~/.claude/.credentials.json` on Linux, the
login keychain on macOS (the item `Claude Code-credentials`). Antigravity sends
both in its own status line data, so the same switch covers its line too, and
codex has no way to show them at all. Both values are read fresh each time the
line is painted and are never written down, logged or put in a notice; if either
cannot be read, that half is simply left out — with no address there is no
segment at all. On a narrow pane it is the **last** thing the first line drops,
after the branch, the folder and the effort: the point of it is knowing which
account is live, so what outlives it is only the tool's mark and the model.

See it with `flightdeck statusline --demo`, which shows Flightdeck's line as it
ships whatever your `config.json` says.

## Pinned rows

Fixed rows in the menu, right after your live sessions and before the history, for the commands you keep open — a process
viewer, a git interface, whatever you leave running. Enter on one opens it in its
own tmux session; Flightdeck never counts it as work and never kills it.

```sh
flightdeck pin list
flightdeck pin add htop
flightdeck pin remove htop
```

The ready-made ones:

| Preset | Row | Runs |
|---|---|---|
| `top` | 📊 top | `top` |
| `htop` | 📊 htop | `htop` |
| `btop` | 📊 btop | `btop` |
| `cswap` | ⚙ accounts | `cswap tui` |
| `lazygit` | 🌿 lazygit | `lazygit` |
| `lazydocker` | 🐳 lazydocker | `lazydocker` |
| `k9s` | ☸ k9s | `k9s` |

`flightdeck install` notices which of these you have on your machine and offers to
pin them. A preset whose tool is not installed can still be pinned; the doctor
warns about it and tells you how that machine installs it. The hint is **printed,
never run**.

`cswap` is [`claude-swap`](https://github.com/realiti4/claude-swap), a
multi-account switcher for Claude Code, and the one preset a fresh machine is
least likely to have: `pipx install claude-swap` (or `uv tool install claude-swap`,
or `python3 -m pip install --user claude-swap`), then `flightdeck pin add cswap`.
`flightdeck install` says exactly this when it does not find it.

Anything else works too:

```sh
flightdeck pin add --label "📓 notes" --command "nvim ~/notes.md"
```

## Where everything lives

Standard XDG directories, which is what makes macOS, Linux and WSL2 behave
identically.

| Path | What |
|---|---|
| `~/.local/share/flightdeck/current` | the code in use |
| `~/.local/share/flightdeck/previous` | the code the last update replaced |
| `~/.local/share/flightdeck/bin` | an fzf the installer downloaded, if it did |
| `~/.local/state/flightdeck/sessions` | one small file per session, written as agents work |
| `~/.local/state/flightdeck/cache` | history caches, disposable |
| `~/.local/state/flightdeck/delegates` | **the only copy of your original status line and codex notify commands** |
| `~/.config/flightdeck/config.json` | your settings |
| `~/.local/bin/flightdeck`, `~/.local/bin/fld` | the command |

`$XDG_DATA_HOME`, `$XDG_STATE_HOME` and `$XDG_CONFIG_HOME` move all of those if
you have set them.

**Do not delete `delegates/`.** It holds the only copy of the status line command
you had before Flightdeck, because your agent's own settings now point at
Flightdeck instead. Lose it and nothing can bring that line back.

Outside those directories, Flightdeck writes the three tools' own configuration —
five files, and every one of them gets a timestamped backup first:

- `~/.claude/settings.json` — seven hook events and the status line setting
- `~/.codex/config.toml` and `~/.codex/hooks.json` — the notify command, six hook
  events and the status line items
- `~/.gemini/config/hooks.json` and
  `~/.gemini/antigravity-cli/settings.json` — one named key, three hook events and
  the status line

Plus the one marked line in your shell's startup file. And that is all: the rest
of what Flightdeck does is talking to tmux.

### `config.json`

Written by the installer and by `flightdeck pin`; you can edit it by hand.

| Key | Default | What |
|---|---|---|
| `projects_dir` | `~` | where Ctrl-N opens a new shell |
| `menu_port` | `42707` | the port the menu listens on so it can refresh itself |
| `context_warn_pct` | `80` | where the 🧠 notice and the red bar entry start |
| `context_rearm_pct` | `75` | below this, the notice is armed again |
| `context_show_pct` | `50` | below this, the 🧠 is not shown on a row at all |
| `history_limit` | `150` | how many past conversations the menu lists |
| `notice_ms` | `5000` | how long a floating notice stays up |
| `pins` | `[]` | your pinned rows |
| `statusline` | `own` for Claude Code and agy, `items` for codex | the status line mode per tool |
| `statusline.lines` | `3` | how many lines Flightdeck's own line has; `2` is the older compressed layout |
| `statusline.account` | `true` | show the signed-in address and plan in the line |

Four environment variables override paths, for when you want a second
installation or a sandbox: `FLIGHTDECK_CONFIG`, `FLIGHTDECK_STATE_DIR`,
`FLIGHTDECK_PROJECTS_DIR` and `FLIGHTDECK_TMUX_SOCKET`. Everything else that can
be configured is a key in the file, so there is one place to look.

## Installing from a git checkout

To run Flightdeck from a clone — to work on it, or to try a branch:

```sh
git clone https://github.com/jvillar/flightdeck
cd flightdeck
bin/flightdeck install      # set the agents up, pointing at this checkout
bin/flightdeck              # enter the menu
```

`bin/flightdeck` works from anywhere, so you can link it into your own PATH if you
like. Two commands behave differently there, on purpose: `flightdeck update`
refuses and tells you to `git pull`, and `flightdeck uninstall` takes everything
else out but leaves the clone alone.

If you later install through `install.sh` as well, run `flightdeck install` once
more afterwards: the agents' settings name a code directory, and the second run
points them at the installed copy.
