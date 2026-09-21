# Working on Flightdeck

Flightdeck is python 3.9 and its standard library, plus one bash command and one
POSIX shell installer. There is nothing to build and nothing to `pip install`.
Clone it and run it:

```sh
git clone https://github.com/jvillar/flightdeck
cd flightdeck
make test
bin/flightdeck --help
```

Read [the safety rules](#verifying-without-doing-harm) before you run anything
that touches tmux or an agent. They are short, and every one of them is there
because somebody broke something.

## The layout

| Path | What it is |
|---|---|
| `bin/flightdeck` | the one command, and the only bash in the project. Entering the menu, the tmux configuration, `restart`, `quit`, and thin branches that hand `doctor`, `pin`, `statusline`, `install`, `update` and `uninstall` to python |
| `flightdeck/config.py` | paths, `config.json` and the four `FLIGHTDECK_*` overrides. Reading never raises; writing is one function |
| `flightdeck/common.py` | the shared pieces: talking to tmux, reading session cards, ages, safe and deduplicated names, and building a floating notice for this tmux version |
| `flightdeck/picker.py` | the menu: building the list, painting it for fzf, turning what you chose into tmux commands, and the loop |
| `flightdeck/history.py` | past conversations from all three tools, ranked by how recently they were touched, with two disposable caches |
| `flightdeck/tools.py` | everything that varies between Claude Code, Codex and Antigravity, in one place |
| `flightdeck/handover.py` | `prefix + n`: close the agent in a pane, wait for the shell, start the next one numbered and with the same flags |
| `flightdeck/turn.py` | reads a transcript backwards to decide whether a turn ended in a loop |
| `flightdeck/registry.py` | Claude Code's own session registry, read only. It is what links a pane to a conversation sent to the background |
| `flightdeck/statusbar.py` | the right-hand side of tmux's status bar: who is waiting, and whose context is nearly full |
| `flightdeck/statusline.py` | Flightdeck's own status line, the one the agent paints under its pane, plus `--demo` and the mode flags |
| `flightdeck/pins.py` | pinned menu rows and the preset catalogue |
| `flightdeck/doctor.py` | the one place that decides what "healthy" means, and the only one that says out loud what the rest of the code degrades around |
| `flightdeck/update.py`, `flightdeck/uninstall.py` | `flightdeck update` and `flightdeck uninstall` |
| `flightdeck/hooks/` | what the agents run: the state hook (`session_hook.py`), the status line tees (`context_tee.py`, `agy_statusline.py`) and codex's notify (`codex_notify.py`) |
| `flightdeck/install/` | one installer per agent (`claude.py`, `codex.py`, `agy.py`), the `flightdeck install` command (`__main__.py`) and the migration from the pre-release cockpit Flightdeck was ported from (`migrate.py`) |
| `install.sh` | the `curl … \| sh` installer, and the only POSIX sh in the project |
| `scripts/` | developer tools that are not part of the product: the demo-GIF pipeline and the terminal-to-HTML converter it uses |
| `tests/` | the suite. One file per module |
| `docs/` | the user guides, this file, and the pictures the README shows |

Two habits worth copying. Anything that varies per tool goes in `tools.py` and
nowhere else. And the pure logic is separated from the effects on purpose: the
functions that build a list, decide a badge or parse fzf's output are tested, and
the thin layer that actually runs tmux is not.

## Tests

```sh
make test          # the suite
make lint          # shellcheck: bin/flightdeck as bash, install.sh as sh
make tarball       # the release archive, into dist/
make clean
```

`make test` is `python3 -m unittest discover -s tests -t . -q` with a tmux socket
nothing listens on. **`pytest` is not used and is not a dependency** — do not add
it.

`SHELLCHECK=<path>` points `lint` at a shellcheck that is not on your PATH, and
`DIST=<dir>` keeps `tarball` from leaving a `dist/` in your working tree.

What is tested is the pure logic: building and ordering entries, colours by group,
parsing fzf's output, resolving a selection, deduplicating and sanitising names,
rendering the bar and the status line, the history, and every edit the installers
make to a settings file. The hooks and the bash command are run for real, as
subprocesses, against temporary directories and a dead socket.

### If you test by mutation, clear the bytecode cache first

Change a line, run the suite, see whether a test catches it — and make sure you
are running the code you think you are. On a Mac, python's compiled cache is not
in a `__pycache__` beside the code, so the usual `find . -name __pycache__` clears
nothing. Worse, the cache is keyed on the file's size and its timestamp **in whole
seconds**, so restoring a file in the same second as the change, at the same size,
leaves python running the changed version.

The symptom wastes an afternoon: the source you print looks right while the
function behaves wrong. Before each pass:

```sh
find "$(python3 -c 'import sys;print(sys.pycache_prefix)')" \
     -path "*flightdeck*" -name "*.pyc" -delete
```

(On Linux, where `pycache_prefix` is usually unset, clearing `__pycache__`
directories is enough.) This kind of contamination can only produce a false
failure, never a false pass — but you will hunt a bug that does not exist.

## Re-rendering the demo GIF

The GIF at the top of the README, and the two stills the docs link to, are not
drawn: they are photographs of Flightdeck running. If you change what the menu
paints, the badges, the status bar or the status line, re-render them:

```sh
python3 scripts/demo_gif.py --out docs/img
```

It writes `docs/img/demo.gif`, `menu.png` and `statusline.png`, and takes about a
minute. It needs **Google Chrome** (headless, for the screenshots) and **Pillow**
(for the GIF only). Pillow is not a dependency of Flightdeck and must not become
one — point the script at an interpreter that has it and it re-invokes itself
there for that one step:

```sh
python3 scripts/demo_gif.py --out docs/img --python /path/to/venv/bin/python
```

What it does is build a whole throwaway world — a tmux server on the socket
`fdshots`, a home directory, a config file, invented session cards and an
invented history — attach a real client to it inside a second tmux server it can
photograph, and then walk the storyboard pressing real keys. The floating notice
frame is the real state hook, fed a synthetic `Stop`. Nothing in a frame is drawn
by hand; the one piece of scenery is the conversation excerpt in the session
pane, and it is written to read as a demo.

It touches nothing of yours: your tmux server, `~/.claude`, `~/.codex` and
`~/.gemini` are never read or written, and both servers and its own `sleep`
processes are killed on the way out, including after a failure. Everything
captured is run through a masking pass (e-mail addresses become
`user1@example.com`, any real home becomes `~`) before it is rendered.

If only one frame needs redoing, name it and the other nine stay as they are:

```sh
python3 scripts/demo_gif.py --out docs/img --only accounts
```

The walk still happens in full — there is no way to stand in front of the
accounts pane without opening the menu and pressing Enter on its row — but only
that frame is photographed, and it is spliced back into its place in the
existing GIF. That is what keeps a one-frame fix from re-rolling every age,
clock time and cursor position in the other nine. The frame names are `menu`,
`cursor`, `session`, `notice`, `back`, `pin`, `accounts`, `filter`, `flags` and
`statusline`, and a still is refreshed only when its own frame was re-shot.

The storyboard is data at the top of the script: `GREEN` is the live sessions,
`HISTORY` the closed ones, `EXCERPT` the conversation. `scripts/ansi2html.py`,
which turns a captured screen into HTML, is the one piece of this with tests
(`tests/test_ansi2html.py`) — the pipeline itself needs Chrome and a tmux server,
so it sits in the thin-layer-without-tests half of the project, like the picker's
loop.

## Verifying without doing harm

Flightdeck touches live things: a tmux server with real work in it, running
agents, other programs' settings files, a history of real conversations. During
the project this went wrong repeatedly — four times in one day, an attempt to
"just check something" started a real agent from the history (at a real cost in
tokens) or sent invented notices to somebody's status bar. These rules came out of
that.

- **Always use an isolated tmux socket.** `tmux -L <name>` on every command, or
  `FLIGHTDECK_TMUX_SOCKET=<name>`, and `kill-server` on that socket when you are
  done. Your real tmux server is for reading only (`ls`, `list-keys`,
  `show-options`).
- **Never start a real agent to verify something.** A stub earlier on `PATH` does
  not work: the pane's interactive shell rebuilds `PATH` from your own startup
  file and eats it. What does work is a tmux pane whose command is `cat`, which
  runs nothing and records what was typed into it. If a real agent is genuinely
  needed, say so first, do it once, in a scratch directory, and close it.
- **Every test carries its own home directory.** `tests/__init__.py` sets `HOME`,
  the three `XDG_*` variables, the state directory, the config file and a dead
  tmux socket, all inside a sandbox, and no test may unset them. The installers
  work out which files they write from the home directory when they are
  *imported*, which happens before any test can patch anything — so a run that
  forgot its fakes would not quietly do nothing, it would rewrite your real
  `~/.claude/settings.json`. One test exists purely to keep that net in place.
- **Read an agent's binary, never run it.** Flag tables, tool names and the values
  a payload can carry are read out of the binaries with `strings` and a regular
  expression. That is the source of truth, and it costs nothing.
- **Only the installers write another program's configuration**, and always with a
  timestamped backup. Everything else reads.
- **Mutation testing on a copy in a scratch directory**, never on the repository.

## Continuous integration and releases

`ci.yml` runs on every push and pull request, in three jobs:

- **tests** on Ubuntu 22.04, Ubuntu 24.04 and macOS, against python 3.9 and 3.12.
- **lint**, which is `make lint`.
- **smoke**, a real installation on a clean Ubuntu 22.04. That runner is the worst
  case on purpose — its tmux is 3.2a and its fzf 0.29, below the floor — so the
  installer takes its own offer and downloads the pinned fzf with its checksum.
  Then it checks the version, runs the doctor on a machine with no agent installed
  at all (a missing agent is a warning, never a failure), and uninstalls.

`release.yml` runs on a `v*` tag. It **refuses a tag that disagrees with the
`VERSION` file**, so a release can never claim a different version from the one
people download; it builds the archive with `make tarball`; and it takes the
release notes from the matching `## v<version>` section of `CHANGELOG.md` — **a
version with no section there does not get released**.

Two things not to change casually. The release asset is named
`flightdeck.tar.gz`, and both `install.sh` and `flightdeck update` ask for it by
that name: renaming it breaks updating for every installation already out there.
And `make tarball` packs a **commit**, not your working tree, so an uncommitted
change is invisible to it.
