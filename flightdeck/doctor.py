"""`flightdeck doctor`: the one place that decides what "healthy" means.

Everything else degrades quietly on purpose -- a broken `config.json` falls back
to the defaults, a hook that cannot write exits 0, the menu opens on an fzf too
old to reload. That is right while someone is working and wrong when they are
asking why nothing happens, so all of it is said out loud in one place instead
of scattered over the code that shrugs.

Three severities, and the rule behind them:

- `✗` (`ok is False`) means **Flightdeck cannot work**: no tmux or one below
  3.2, no fzf or one below 0.36, python below 3.9, a state directory it cannot
  write into, a hook of ours registered but STALE (it names our script and
  points at a code directory that is not here), the command linked into
  `~/.local/bin` and not resolvable on PATH. Any `✗` and `main()` exits 1.
- `!` (`ok is None`) is everything else: an optional agent missing, an invalid
  config (it takes nothing down), no live tmux server, key bindings
  somebody else has taken, a pin whose tool is gone, a locale that is not UTF-8,
  and the WSL2 note.
- `✓` (`ok is True`) is fine.

**The doctor writes nothing, ever.** It does not create the state directory it
reports on, does not start a tmux server, does not run an agent, does not fix.
A check that cannot answer says so; it never repairs behind the user's back.

Every check is a function of one `Env`, the bundle of injected probes (`which`,
`run`, the environment, the home directory, the platform, the paths). That is
what lets the tests describe a Debian with tmux 3.2 and no fzf while running on
a Mac, and it is why `default_env()` is the only thing here that touches
`shutil.which` or `subprocess.run`. A check NEVER raises: `_guard` turns a probe
that blows up into a warning line carrying the exception, because a traceback in
place of a report is the one answer that helps nobody.
"""

import os
import platform as platform_module
import re
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

from flightdeck import common, config, picker, pins
from flightdeck.install import agy, claude, codex

# One finding. `ok`: True = ✓, None = ! (a warning or a note), False = ✗.
# `fix` is one line of what to do about it, or None when there is nothing to do.
Check = namedtuple("Check", "name ok detail fix")

# Everything a check is allowed to know about the machine. Built by
# `default_env()`; a test replaces the fields it cares about with `_replace`.
Env = namedtuple("Env", "which run environ home platform uname proc_version "
                        "python_version python_path state_dir config_path")

MARKS = {True: "✓", None: "!", False: "✗"}

# The floors, below which Flightdeck does not work at all: tmux 3.2 is where
# `new-session -e` and `display-message -d` arrive, fzf 0.36 is where `--listen`
# does (without it the menu cannot reload while it is open), and 3.9 is the
# python every module here is written against.
TMUX_MIN = (3, 2)
FZF_MIN = (0, 36)
FZF_STABLE_CURSOR = (0, 71)   # `--id-nth`: the cursor follows the same row
PYTHON_MIN = (3, 9)

# The config keys every reader falls back on silently when they are not whole
# numbers. Their docstrings say the doctor is the place that complains, and this
# is that place.
INT_KEYS = ("menu_port", "notice_ms", "context_warn_pct", "context_rearm_pct",
            "context_show_pct", "history_limit")

# The four keys Flightdeck binds, as `tmux list-keys` spells them: the table,
# the key, and the label a person reads.
TARGETS = (("F12", "root", "F12"),
           ("Shift+Enter", "root", "S-Enter"),
           ("prefix j", "prefix", "j"),
           ("prefix n", "prefix", "n"))

# What tmux itself binds them to. `n` is next-window; `j`, F12 and Shift+Enter
# are bound to nothing at all, so their absence is stock and not a collision.
STOCK_BINDINGS = {"prefix n": "next-window"}

# A binding is OURS when its command mentions us: `goto-menu`,
# `flightdeck.handover` and the `@flightdeck_agent` of Shift+Enter all travel
# with the word in them.
OURS_MARK = "flightdeck"

# `bind-key [-r] -T <table> <key> <command...>`, padded with spaces. Measured
# against tmux 3.6a on an isolated server: the flags (`-r` for a repeatable one)
# sit BEFORE the table, and plain `list-keys` prints no `-N` note.
_BIND_RE = re.compile(r"^bind-key\s+(?:-\S+\s+)*?-T\s+(?P<table>\S+)\s+"
                      r"(?P<key>\S+)\s+(?P<command>.+)$")


def _read_proc_version():
    """`/proc/version`, or "" where there is no such file (macOS)."""
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def default_env():
    """The real machine, as an `Env`.

    The paths are resolved HERE rather than inside the checks so that one run of
    the doctor reports on one configuration: `config.state_dir()` reads the
    environment on every call, and a report that asked twice could name two
    different directories in two of its lines.
    """
    return Env(which=shutil.which,
               run=subprocess.run,
               environ=os.environ,
               home=Path.home(),
               platform=sys.platform,
               uname=" ".join(platform_module.uname()),
               proc_version=_read_proc_version,
               python_version=sys.version_info[:2],
               python_path=sys.executable,
               state_dir=config.state_dir(),
               config_path=config.config_path())


# ── 1. the system ────────────────────────────────────────────────────────────

def system(env):
    """`darwin`, `linux`, `wsl2` or `windows`.

    Windows is asked FIRST and by three names, because a python running under
    MSYS or Git Bash reports things that read like a Unix otherwise. WSL2 is a
    Linux whose `/proc/version` carries "microsoft" -- the only difference
    that shows from in here, and the one the F12 note hangs on.
    """
    if (re.search(r"MINGW|MSYS|CYGWIN", env.uname or "", re.IGNORECASE)
            or env.environ.get("OS") == "Windows_NT"
            or str(env.platform).startswith("win")):
        return "windows"
    if str(env.platform).startswith("darwin"):
        return "darwin"
    if "microsoft" in (env.proc_version() or "").lower():
        return "wsl2"
    return "linux"


_SYSTEM_NAMES = {"darwin": "macOS", "linux": "Linux",
                 "wsl2": "WSL2 (Linux inside Windows)"}


def check_system(env):
    kind = system(env)
    if kind == "windows":
        return Check("system", False,
                     "native Windows — Flightdeck is tmux and a POSIX shell, "
                     "and runs inside WSL2",
                     "wsl --install, open Ubuntu, and run the installer in there")
    return Check("system", True, "%s (%s)" % (_SYSTEM_NAMES[kind], env.platform), None)


def _package_hint(env, tool):
    """How this machine installs a command-line tool. Printed, never run."""
    if system(env) == "darwin":
        return "brew install %s" % tool
    return "sudo apt install %s   (or your distribution's package manager)" % tool


# ── 2. tmux ──────────────────────────────────────────────────────────────────

def check_tmux(env):
    """tmux is the cockpit: with none, or one too old, there is nothing to run in."""
    path = env.which("tmux")
    if not path:
        return Check("tmux", False, "not on PATH — Flightdeck is a tmux cockpit",
                     _package_hint(env, "tmux"))
    version = common.tmux_version(run=env.run)
    if version is None:
        return Check("tmux", None,
                     "%s — `tmux -V` says nothing I can read; treated as the "
                     "oldest tmux" % path, None)
    text = "%d.%d" % version
    if version < TMUX_MIN:
        return Check("tmux", False,
                     "%s — %s is below the minimum 3.2 (`new-session -e` and "
                     "`display-message -d`)" % (path, text),
                     _package_hint(env, "tmux"))
    if version < common.LITERAL_NOTICE_FROM:
        return Check("tmux", None,
                     "%s — %s works; floating notices are escaped instead of "
                     "literal (`display-message -l` is 3.4 and newer)" % (path, text),
                     None)
    return Check("tmux", True, "%s — %s" % (path, text), None)


# ── 3. fzf ───────────────────────────────────────────────────────────────────

def check_fzf(env):
    """fzf IS the menu, and which layer you are on depends on its version."""
    path = env.which("fzf")
    if not path:
        return Check("fzf", False, "not on PATH — fzf is the menu itself",
                     _package_hint(env, "fzf"))
    result = env.run(["fzf", "--version"], capture_output=True, text=True, timeout=5)
    # The same parser the picker uses, so the doctor can never report a layer
    # the menu does not put itself on ("0.72.0 (Homebrew)" -> 0.72).
    version = picker._fzf_version(result.stdout or "") if result.returncode == 0 else None
    if version is None:
        return Check("fzf", None,
                     "%s — `fzf --version` says nothing I can read" % path, None)
    text = "%d.%d" % version
    if version < FZF_MIN:
        return Check("fzf", False,
                     "%s — %s is below the minimum 0.36: no live reload "
                     "(`--listen`), so the menu cannot refresh while it is open"
                     % (path, text),
                     _package_hint(env, "fzf"))
    if version < FZF_STABLE_CURSOR:
        return Check("fzf", None,
                     "%s — %s: live reload, without a stable cursor (`--id-nth` "
                     "is 0.71 and newer, and keeps the cursor on the same "
                     "session when the list is reordered)" % (path, text),
                     _package_hint(env, "fzf"))
    return Check("fzf", True,
                 "%s — %s: live reload and a stable cursor (`--id-nth`)"
                 % (path, text), None)


# ── 4. python ────────────────────────────────────────────────────────────────

def check_python(env):
    """3.9 is the floor: everything here is 3.9 standard library and nothing else."""
    text = "%d.%d" % env.python_version
    if env.python_version < PYTHON_MIN:
        return Check("python3", False,
                     "%s is %s, below the minimum 3.9" % (env.python_path, text),
                     "install python 3.9 or newer and put it on PATH")
    return Check("python3", True, "%s — %s" % (env.python_path, text), None)


# ── 5. the agents ────────────────────────────────────────────────────────────

AGENTS = ("claude", "codex", "agy")


def check_agents(env):
    """Which agents are on PATH. All three are optional to Flightdeck itself.

    Missing `claude` is a warning and not a failure: the menu, the pins, the
    handover key and the status bar all work with no agent installed at all --
    what you get is a cockpit with nothing flying in it yet.
    """
    found = [tool for tool in AGENTS if env.which(tool)]
    missing = [tool for tool in AGENTS if tool not in found]
    detail = "found: %s" % ", ".join(found) if found else "none found"
    if missing:
        detail += " — not on PATH: %s" % ", ".join(missing)
    if "claude" not in found:
        return Check("agents", None,
                     detail + " (Flightdeck runs, but Claude Code is what it was "
                              "built around)",
                     "install Claude Code and make sure `claude` is on PATH")
    return Check("agents", True, detail + " (codex and agy are optional)", None)


# ── 6. the command itself ────────────────────────────────────────────────────

def check_command_on_path(env):
    """Can a shell find `flightdeck`? Everything Flightdeck installs calls it.

    The failure that matters is the installer having linked the command into
    `~/.local/bin` while the line it added to the shell's rc file never took:
    the link is there, the shell cannot see it, and the tmux bindings, the state
    hook's menu refresh and the status bar all point at a command that does not
    resolve.
    """
    link = env.home / ".local" / "bin" / "flightdeck"
    linked = link.exists() or link.is_symlink()
    resolved = env.which("flightdeck")
    on_path = str(link.parent) in (env.environ.get("PATH") or "").split(os.pathsep)

    if linked and not resolved:
        return Check("flightdeck on PATH", False,
                     "%s exists but `flightdeck` does not resolve — %s is %s"
                     % (link, link.parent,
                        "on PATH but the link is not usable" if on_path
                        else "not on your PATH"),
                     "add %s to your PATH (the installer's rc line did not take; "
                     "open a new shell after adding it)" % link.parent)
    if resolved:
        through = "through %s" % link.parent if linked else "not through %s" % link.parent
        return Check("flightdeck on PATH", True, "%s (%s)" % (resolved, through), None)
    return Check("flightdeck on PATH", None,
                 "`flightdeck` is not on PATH and there is no link in %s"
                 % link.parent,
                 "run the installer, or link the command yourself: "
                 "ln -s %s ~/.local/bin/flightdeck" % (config.code_dir() / "bin" / "flightdeck"))


# ── 7. the configuration ─────────────────────────────────────────────────────

def wrong_types(raw):
    """The numeric keys whose value is not a whole number. -> [(key, value)]

    `bool` is excluded by hand because it IS an `int` in Python: `"menu_port":
    true` would sail straight through an isinstance check and leave the menu
    listening on a port of 1.
    """
    bad = []
    for key in INT_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            bad.append((key, value))
    return bad


def check_config(env):
    """`config.json` readable, and its numbers actually numbers.

    A warning either way: an invalid config takes nothing down, it
    just means every setting in it is being ignored -- which is precisely the
    kind of thing nobody works out on their own.

    `env.config_path` only NAMES the file in the message; the values come from
    `flightdeck.config`, which resolves the same path from the same environment
    (`default_env` reads it once so the two cannot name different files).
    """
    config.load()
    problems = []
    if config.load.error:
        problems.append(config.load.error)
    for key, value in wrong_types(config.load_raw()):
        problems.append("%s should be a whole number, found %r" % (key, value))
    if problems:
        return Check("config.json", None, "; ".join(problems),
                     "fix %s, or delete it to go back to the defaults" % env.config_path)
    if not Path(env.config_path).exists():
        return Check("config.json", True,
                     "no config file yet (%s) — the defaults are in force"
                     % env.config_path, None)
    return Check("config.json", True, str(env.config_path), None)


# ── 8. the state directory ───────────────────────────────────────────────────

def check_state_dir(env):
    """Can Flightdeck write where it keeps its state? It never creates it here.

    Session cards, the history caches and the delegates all live under it: with
    nowhere to write, the menu shows no state at all and the hooks throw their
    work away in silence. When the directory is not there yet, the question is
    whether the nearest ancestor that IS there can be written into -- asking
    where the state lives must not be what brings it into being.
    """
    path = Path(env.state_dir)
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    if not os.access(str(target), os.W_OK):
        return Check("state directory", False,
                     "%s is not writable" % target,
                     "check who owns %s and its permissions" % target)
    if target != path:
        return Check("state directory", True,
                     "%s (not created yet; %s is writable)" % (path, target), None)
    return Check("state directory", True, str(path), None)


# ── 9-11. what is registered in each agent ───────────────────────────────────

def _stale_detail(stale):
    """One line naming the first stale command; the rest counted."""
    event, command = stale[0]
    more = " (and %d more)" % (len(stale) - 1) if len(stale) > 1 else ""
    return "STALE: %s points somewhere else — %s%s" % (event, command, more)


def check_claude_hooks(env):
    """Claude Code's seven hook events, and whether they point HERE."""
    state = claude.installed_state()
    if state["error"]:
        return Check("Claude Code hooks", None, state["error"],
                     "fix %s by hand; Flightdeck never overwrites one it cannot read"
                     % claude.SETTINGS_PATH)
    if state["stale"]:
        return Check("Claude Code hooks", False, _stale_detail(state["stale"]),
                     "flightdeck install — it replaces the old entries with this "
                     "code directory")
    if not state["live"]:
        return Check("Claude Code hooks", None,
                     "Flightdeck is not registered in %s" % claude.SETTINGS_PATH,
                     "flightdeck install")
    if state["missing"]:
        return Check("Claude Code hooks", None,
                     "registered: %s — missing: %s"
                     % (", ".join(state["live"]), ", ".join(state["missing"])),
                     "flightdeck install")
    if state["matcher"] != claude.PRETOOL_MATCHER:
        return Check("Claude Code hooks", None,
                     "all seven events registered, but PreToolUse's matcher is %r "
                     "and not %r" % (state["matcher"], claude.PRETOOL_MATCHER),
                     "flightdeck install — it brings the matcher up to date")
    return Check("Claude Code hooks", True,
                 "all seven events registered (%s)" % claude.SETTINGS_PATH, None)


# What to say about a file of theirs that is there and cannot be read. Never
# "run flightdeck install": the installers read an unparseable file as an empty
# one, so a reinstall rebuilds it and drops the entries of theirs inside. This
# is the answer `check_claude_hooks` has always given, and the other two tools
# owe the user the same one.
_FIX_BY_HAND = ("fix that file by hand first — a reinstall reads what it cannot "
                "parse as an empty file and would rebuild it, dropping the "
                "entries of yours in there")


def check_codex(env):
    """codex's hooks, its `notify` tee and its status line items."""
    state = codex.installed_state()
    # Stale before unreadable, deliberately: a stale hook is proven and is a ✗,
    # and the error it could hide is the OTHER file (an unreadable hooks.json
    # leaves nothing to call stale). Unreadable before "not registered", because
    # read as empty they look the same and the advice differs completely.
    if state["stale"]:
        return Check("codex", False, _stale_detail(state["stale"]),
                     "flightdeck install")
    if state["error"]:
        return Check("codex", None, state["error"], _FIX_BY_HAND)
    if not state["live"] and state["notify"] != "ours":
        return Check("codex", None,
                     "codex is installed and Flightdeck is not registered in it "
                     "(%s)" % codex.HOOKS_JSON, "flightdeck install")
    notes = []
    if state["missing"]:
        notes.append("events missing: %s" % ", ".join(state["missing"]))
    if state["notify"] == "unreadable":
        notes.append("its `notify` cannot be read off one line, so install will "
                     "not touch %s" % codex.CONFIG_TOML)
    elif state["notify"] == "stale":
        # Ours by name, at a path that is no longer here: codex runs a script
        # that is not there at the end of every turn, so the effect is the same
        # as having no tee at all -- and an install puts it right.
        notes.append("its `notify` names a Flightdeck tee that has moved away, "
                     "so the end of a codex turn goes unnoticed")
    elif state["notify"] != "ours":
        notes.append("`notify` is not Flightdeck's tee, so the end of a codex "
                     "turn goes unnoticed")
    if notes:
        return Check("codex", None, "; ".join(notes), "flightdeck install")
    return Check("codex", True,
                 "notify tee and %d events registered; status line items: %s"
                 % (len(state["live"]), state["status_line_items"]), None)


def check_agy(env):
    """agy's one named hook: present, enabled, and pointing here."""
    state = agy.installed_state()
    if state["stale"]:
        return Check("agy", False, _stale_detail(state["stale"]), "flightdeck install")
    if state["error"]:
        return Check("agy", None, state["error"], _FIX_BY_HAND)
    if not state["present"]:
        return Check("agy", None,
                     "agy is installed and Flightdeck is not registered in it (%s)"
                     % agy.HOOKS_JSON, "flightdeck install")
    if not state["enabled"]:
        return Check("agy", None,
                     "our `%s` hook is registered but disabled in %s"
                     % (agy.NAME, agy.HOOKS_JSON),
                     'set "enabled": true in that file')
    if state["missing"]:
        return Check("agy", None,
                     "registered: %s — missing: %s"
                     % (", ".join(state["live"]), ", ".join(state["missing"])),
                     "flightdeck install")
    return Check("agy", True,
                 "the `%s` hook is registered for %s"
                 % (agy.NAME, ", ".join(state["live"])), None)


def _status_line_check(name, state, error, delegate_path, settings_path, mode_help):
    """The shared reading of a status line: whose it is, and is the delegate there.

    Claude Code's and agy's differ only in which files and which config key they
    use, and their failures are the same three: ours pointing at code that has
    moved, ours registered with the only copy of the user's own line gone, and
    ours not registered at all.

    `error` is passed in rather than read off the state because for agy it is
    not the same error: its hooks and its status line live in two files, and a
    hooks.json nobody can parse says nothing about the line.
    """
    if error:
        return Check(name, None, error, "fix %s by hand" % settings_path)
    if state["status_line"] == "stale":
        return Check(name, False,
                     "it points at a Flightdeck that is not here — %s"
                     % state["status_line_command"], "flightdeck install")
    if state["status_line"] != "ours":
        current = state["status_line_command"] or "none"
        return Check(name, None,
                     "Flightdeck is not painting it (current: %s)" % current,
                     "flightdeck install")
    if not state["delegate_file"]:
        return Check(name, None,
                     "registered, but the delegate file is gone (%s) — it held "
                     "the only copy of the status line you had" % delegate_path,
                     "put your own command back in that file, or reinstall")
    return Check(name, True,
                 "Flightdeck's (mode %s: %s)" % (state["mode"], mode_help), None)


def check_claude_status_line(env):
    """The line under Claude Code's prompt, and the delegate behind it."""
    state = claude.installed_state()
    return _status_line_check("status line (Claude Code)", state, state["error"],
                              claude.delegate_file(), claude.SETTINGS_PATH,
                              claude.MODE_HELP.get(state["mode"], ""))


def check_agy_status_line(env):
    """The same for agy, which has a status line of its own (codex has items)."""
    state = agy.installed_state()
    return _status_line_check("status line (agy)", state,
                              state["status_line_error"],
                              agy.delegate_file(), agy.SETTINGS_PATH,
                              agy.MODE_HELP.get(state["mode"], ""))


# ── 12. the tmux keys ────────────────────────────────────────────────────────

def bindings(list_keys_output):
    """What Flightdeck's four keys are bound to right now. -> {label: command|None}

    Read off the whole `tmux list-keys` listing rather than asked key by key,
    because `install` wants the same answer from the same call. `None` is "bound
    to nothing", which for F12, Shift+Enter and `prefix j` is stock tmux.
    """
    found = {label: None for label, _table, _key in TARGETS}
    for line in (list_keys_output or "").splitlines():
        match = _BIND_RE.match(line.strip())
        if not match:
            continue
        for label, table, key in TARGETS:
            if match.group("table") == table and match.group("key") == key:
                found[label] = match.group("command").strip()
    return found


def key_collisions(list_keys_output):
    """The keys Flightdeck wants that somebody ELSE has taken. -> [(label, command)]

    Not a collision: a key bound to nothing (stock, for F12 and `prefix j`), the
    stock `next-window` on `prefix n`, and anything of ours. Flightdeck
    overrides all four when you enter the menu, so this is what tells you what
    it is overriding -- `flightdeck install` says it before it happens, and the
    doctor says it afterwards. One function, used by both.
    """
    collisions = []
    for label, command in bindings(list_keys_output).items():
        if command is None or OURS_MARK in command:
            continue
        if command == STOCK_BINDINGS.get(label):
            continue
        collisions.append((label, command))
    return collisions


def check_bindings(env):
    """F12, Shift+Enter, `prefix j` and `prefix n`, when there is a server to ask.

    `has-session` FIRST, and it is not a nicety. Measured on tmux 3.6a: with no
    server running, `list-keys` exits 0 and prints tmux's STOCK key table --
    so the answer would be indistinguishable from a live server that has not
    been configured -- and it creates a socket file to do it, spinning up a
    momentary server. The doctor starts nothing. `has-session` answers rc 1
    on a dead socket and leaves nothing behind.
    """
    alive = env.run(common.tmux_bin() + ["has-session"],
                    capture_output=True, text=True, timeout=5)
    if alive.returncode != 0:
        return Check("tmux keys", None,
                     "no live tmux server to ask (the keys live inside the "
                     "server, not in a file)",
                     "start one with `flightdeck` (entering the menu installs "
                     "the keys and the bar)")
    result = env.run(common.tmux_bin() + ["list-keys"],
                     capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return Check("tmux keys", None,
                     "the tmux server did not answer `list-keys`", None)
    collisions = key_collisions(result.stdout or "")
    if collisions:
        return Check("tmux keys", None,
                     "taken by something else: %s"
                     % "; ".join("%s → %s" % pair for pair in collisions),
                     "Flightdeck overrides them when you enter the menu; "
                     "`flightdeck quit` gives tmux its keys back")
    missing = [label for label, command in bindings(result.stdout or "").items()
               if command is None or OURS_MARK not in command]
    if missing:
        return Check("tmux keys", None,
                     "not bound to Flightdeck: %s" % ", ".join(missing),
                     "run `flightdeck` (the menu reapplies them on entry) or "
                     "`flightdeck init`")
    return Check("tmux keys", True,
                 "F12, Shift+Enter, prefix j and prefix n are Flightdeck's", None)


# ── 13. the pins ─────────────────────────────────────────────────────────────

def check_pins(env):
    """A pinned row whose tool is no longer installed opens an empty session."""
    rows = pins.list_rows(config.load(), which=env.which)
    broken = [(name, label) for name, label, installed, pinned in rows
              if pinned and not installed]
    if not broken:
        count = sum(1 for _name, _label, _installed, pinned in rows if pinned)
        return Check("pins", True,
                     ("%d pinned, all installed" % count) if count
                     else "no pinned rows", None)
    hints = [pins.hint_for(name, env.platform) for name, _label in broken]
    return Check("pins", None,
                 "pinned but not installed: %s"
                 % ", ".join("%s (%s)" % (label, name) for name, label in broken),
                 next((hint for hint in hints if hint),
                      "install them, or take the row out with `flightdeck pin "
                      "remove <name>`"))


# ── 14-15. the terminal, and the note for WSL2 ───────────────────────────────

def check_locale(env):
    """A non-UTF-8 locale turns the glyphs the menu is made of into rubbish.

    `LC_ALL` wins over `LC_CTYPE`, which wins over `LANG`: that is the order the
    C library reads them in, and a doctor that looked at the wrong one would
    pass a terminal that is about to print `????` where the badges go.
    """
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.environ.get(name)
        if not value:
            continue
        if "utf-8" in value.lower().replace("utf8", "utf-8"):
            return Check("locale", True, "%s=%s" % (name, value), None)
        return Check("locale", None,
                     "%s=%s is not UTF-8 — the menu's glyphs will not print"
                     % (name, value),
                     "export LANG=en_US.UTF-8 (or your own language) in your "
                     "shell's rc file")
    return Check("locale", None,
                 "no LANG, LC_CTYPE or LC_ALL set — UTF-8 is not guaranteed",
                 "export LANG=en_US.UTF-8 (or your own language) in your shell's "
                 "rc file")


def check_wsl2(env):
    """The two things about WSL2 that are worth knowing, and neither is a fault."""
    return Check("WSL2", None,
                 "you are inside WSL: Claude Code, tmux and Flightdeck all live "
                 "in here, not in Windows — and Windows Terminal does send F12 "
                 "through, so the menu toggle works",
                 None)


# ── running them ─────────────────────────────────────────────────────────────

def _guard(name, function, env):
    """Run one check; a probe that blows up becomes a line, not a traceback.

    A warning and not a failure: a check that crashed has not proved Flightdeck
    broken, it has failed to look -- and `flightdeck install` ends by running
    the doctor, so a bug of ours must not turn a good installation into a
    non-zero exit. The exception is in the line, which is loud enough to report.
    """
    try:
        return function(env)
    except Exception as exc:
        return Check(name, None, "could not check: %s: %s" % (type(exc).__name__, exc),
                     None)


def checks(env):
    """Every check, in the order of the spec. -> [Check]

    The spec lists fifteen concerns; they come out as one line each except for
    two. "hooks for codex and agy if they are there" is a line per tool, and so
    is the status line, which Claude Code and agy each have one of (codex has no
    external status line command, only its own items, which its own line
    reports). One idea per line beats two tools crammed into one.

    The list is not fixed either: codex and agy are asked about only when they
    are on PATH -- a line about a tool nobody has installed is noise on every
    machine that runs Claude Code and nothing else -- and the WSL2 note appears
    only on WSL2.
    """
    planned = [("system", check_system),
               ("tmux", check_tmux),
               ("fzf", check_fzf),
               ("python3", check_python),
               ("agents", check_agents),
               ("flightdeck on PATH", check_command_on_path),
               ("config.json", check_config),
               ("state directory", check_state_dir),
               ("Claude Code hooks", check_claude_hooks)]
    if _which(env, "codex"):
        planned.append(("codex", check_codex))
    if _which(env, "agy"):
        planned.append(("agy", check_agy))
    planned.append(("status line (Claude Code)", check_claude_status_line))
    if _which(env, "agy"):
        planned.append(("status line (agy)", check_agy_status_line))
    planned += [("tmux keys", check_bindings),
                ("pins", check_pins),
                ("locale", check_locale)]
    if _safely(system, env) == "wsl2":
        planned.append(("WSL2", check_wsl2))
    return [_guard(name, function, env) for name, function in planned]


def _which(env, tool):
    """`env.which`, tolerating a probe that raises."""
    return _safely(lambda e: e.which(tool), env)


def _safely(function, env):
    """What `function(env)` answers, or None when it blows up.

    For the two questions that decide WHICH checks are run. They are outside any
    check's own guard, and a probe that raises there would take the whole report
    down before a single line was printed -- the check that uses the same probe
    reports the failure when its turn comes.
    """
    try:
        return function(env)
    except Exception:
        return None


def main(argv=None):
    """`flightdeck doctor` -> one line per check; exit 1 if any of them is ✗."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        sys.stderr.write("usage: flightdeck doctor\n")
        return 2

    env = default_env()
    print("flightdeck %s — doctor" % config.version())
    print("  code: %s" % config.code_dir())
    results = checks(env)
    for check in results:
        print("%s %s: %s" % (MARKS[check.ok], check.name, check.detail))
        if check.fix:
            print("    fix: %s" % check.fix)

    # The exit code is invisible, so the last line says what it means.
    broken = [c for c in results if c.ok is False]
    warnings = [c for c in results if c.ok is None]
    if broken:
        print("\n%s Flightdeck cannot work around%s."
              % (_count(len(broken), "problem"),
                 ", and %s worth a look" % _count(len(warnings), "warning")
                 if warnings else ""))
        return 1
    if warnings:
        print("\nNothing broken; %s worth a look." % _count(len(warnings), "warning"))
        return 0
    print("\nAll good.")
    return 0


def _count(number, thing):
    """`1 problem` / `2 problems`."""
    return "%d %s%s" % (number, thing, "" if number == 1 else "s")


if __name__ == "__main__":
    sys.exit(main())
