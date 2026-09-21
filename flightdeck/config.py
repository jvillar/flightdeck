"""Paths, `config.json` and the `FLIGHTDECK_*` environment overrides.

Everything Flightdeck writes at runtime lives under one state directory, and
everything the user configures lives in one JSON file. Both follow the XDG base
directory spec, which is what makes macOS, Linux and WSL2 behave identically.

Precedence, highest first: the `FLIGHTDECK_*` variable, the `XDG_*` variable,
the home directory. Only four variables CONFIGURE anything (`FLIGHTDECK_CONFIG`,
`FLIGHTDECK_STATE_DIR`, `FLIGHTDECK_PROJECTS_DIR`, `FLIGHTDECK_TMUX_SOCKET`);
everything else is a key in the file, so there is one place to look.

Three more are read elsewhere and are SEAMS rather than configuration -- they do
not change what Flightdeck does, only where a test or a CI job points it:
`FLIGHTDECK_YES` (`install` asks nobody), `FLIGHTDECK_TARBALL_URL` (which tarball
`update` and `install.sh` fetch) and `FLIGHTDECK_FZF_BASE_URL` (where `install.sh`
fetches the fzf archive and its checksums). The tests give the last two a
`file://` URL, so no test reaches GitHub. None of them belongs in `config.json`:
a machine does not have a release URL, a test run does.

`load()` never raises. A `config.json` that is missing, unreadable, not JSON or
not an object degrades to `DEFAULTS`, and the reason is left in `load.error`
(a string, or None when the last load was clean). It is a function attribute
rather than a result object because every caller wants the plain dict back and
only `doctor` and the menu ever look at the error.

**Reading and writing are separate paths on purpose.** `load()` answers "what
is in force right now" and mixes in the defaults; `load_raw()` answers "what
does the user's file actually say" and mixes in nothing. Anything that changes
the config goes through `update()`, which is a read-modify-write over
`load_raw()` and is the one function here that raises (`ConfigError`, when the
file exists but cannot be understood: overwriting it would destroy settings the
user can still fix). Otherwise a `pin add` would write every default and an expanded
`projects_dir` into the file: the user's config would hard-code `/Users/<name>`,
a transient `FLIGHTDECK_PROJECTS_DIR` would become permanent, and a later change
to a default value would never reach them. For the same reason `projects_dir()`,
not `load()`, is where the environment override and `~` expansion happen.

Run as a module it answers one key at a time -- `python3 -m flightdeck.config
menu_port` prints `42707` -- which is how the bash `flightdeck` command reads
its configuration without parsing JSON in shell.
"""

import copy
import json
import os
import sys
from pathlib import Path

_APP = "flightdeck"


class ConfigError(Exception):
    """The config file cannot be used, and guessing would lose the user's data.

    Reading degrades instead of raising (`load()` falls back to `DEFAULTS`);
    this is for the writing side, where carrying on means overwriting.
    """

DEFAULTS = {
    "projects_dir": "~",
    "menu_port": 42707,
    "context_warn_pct": 80,
    "context_rearm_pct": 75,
    "context_show_pct": 50,
    "history_limit": 150,
    "notice_ms": 5000,
    "pins": [],
    # Three modes, one per tool, plus the two keys that shape Flightdeck's own
    # line. Both ship on, because the complete three-line layout IS
    # Flightdeck's line, and both are OPT-OUTS: `lines: 2` brings back the
    # compressed layout shipped first (identity and consumption on one row, a
    # 10-cell context bar), and `account: false` takes the signed-in address
    # and plan off the line -- the key to reach for before sharing a screen.
    "statusline": {"claude": "own", "agy": "own", "codex": "items",
                   "lines": 3, "account": True},
}


def _env(name):
    """An empty variable is an unset variable.

    `FLIGHTDECK_STATE_DIR= flightdeck` is a common way to think you are clearing
    a variable; read literally it would mean `Path("")`, i.e. the working
    directory, and Flightdeck would scatter session cards wherever it was run.
    """
    return os.environ.get(name) or None


def state_dir():
    """Where Flightdeck writes at runtime (session cards, caches, delegates)."""
    override = _env("FLIGHTDECK_STATE_DIR")
    if override:
        return Path(override)
    xdg = _env("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / _APP
    return Path.home() / ".local" / "state" / _APP


def data_dir():
    """Where the INSTALLED code lives, and a downloaded fzf beside it.

    `current/` is the code in use -- the command in `~/.local/bin` links into it
    and the agents' hooks name paths inside it, which is why `update` swaps what
    is behind that name instead of moving it -- `previous/` is the code before
    the last update, and `bin/` holds an fzf the installer downloaded for a
    machine whose own was too old.

    There is no `FLIGHTDECK_*` override on purpose: four variables CONFIGURE
    Flightdeck and no more, so this one moves with `XDG_DATA_HOME`
    like any other program's data directory. (`FLIGHTDECK_TARBALL_URL`, which
    `update` reads, is not one of them: it says where a release comes from, which
    is a test and CI seam -- see the note at the top of this file.) Asking for it does not create it (`ensure_dir`
    does), the same rule the state paths above follow.
    """
    xdg = _env("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _APP
    return Path.home() / ".local" / "share" / _APP


def ensure_dir(path):
    """Create `path` if it is not there, and hand it back either way.

    Asking WHERE something lives and MAKING it are two different things, and
    only the writers need the second: `load_sessions`, the status bar and the
    menu ask on every repaint, and a machine where Flightdeck was never
    installed must not grow a state directory because something read it and
    found nothing.

    The mkdir is swallowed on purpose: the callers are hooks that must exit 0
    whatever happens, and each of them already handles the write that follows
    failing. A full disk is not a reason to take down the session.
    """
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def sessions_dir():
    """Where session cards and their `<id>.ctx.json` context files live."""
    return state_dir() / "sessions"


def cache_dir():
    """Where the disposable caches live (the history feed rebuilds them)."""
    return state_dir() / "cache"


def delegates_dir():
    """Where the user's original status line and codex notify commands live.

    Sacred: this is the only copy. Deleting it degrades their status line and
    nothing can bring it back, since the tool's own settings now point at us.
    """
    return state_dir() / "delegates"


def config_path():
    """Where `config.json` lives."""
    override = _env("FLIGHTDECK_CONFIG")
    if override:
        return Path(override)
    xdg = _env("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _APP / "config.json"
    return Path.home() / ".config" / _APP / "config.json"


def _merge(base, overrides):
    """Shallow, except one level deeper for dict values.

    `{"statusline": {"claude": "wrap"}}` must not wipe the `agy` and `codex`
    modes the user never mentioned: every reader of `cfg["statusline"]` would
    then be reading a dict with one key.

    Used by `load()` over `DEFAULTS` and by `update()` over the user's own file,
    and it has to be the same rule in both places: `update` used a plain
    `dict.update`, so `statusline --mode own` replaced the whole section and
    threw away the modes chosen for the other tools -- silently, and while the
    user was doing something else. Two dicts merge; anything else is replaced,
    which is the only way a value typed as the wrong kind can be corrected.
    """
    for key, value in overrides.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value


def load():
    """`DEFAULTS` updated with `config.json`; never raises.

    Unknown keys in the file are kept (a newer Flightdeck may know them, and
    `save()` must not silently drop them). Values are not type-checked here:
    `doctor` is the place that complains about a `menu_port` spelled as a word.

    `projects_dir` comes back **raw**: exactly the string in the file, or `"~"`.
    Use `projects_dir()` for the usable path. Expanding it here would put an
    absolute home path into anything that writes this dict back to disk.
    """
    cfg = copy.deepcopy(DEFAULTS)
    _merge(cfg, load_raw())
    load.error = load_raw.error
    return cfg


load.error = None


def load_raw():
    """Exactly what `config.json` holds: no defaults, no environment, no expansion.

    `{}` when the file is missing, unreadable or not a JSON object. This is what
    `update()` writes back, so a read-modify-write can only ever add or change
    the keys the user asked for.

    `load_raw.error` says which of those it was: None for a clean read **and for
    a missing file** (no config yet is the normal state, not a fault), a string
    naming the path otherwise. `update()` reads it to tell "there is nothing
    here" apart from "there is something here I cannot understand" -- the whole
    difference between creating a config and destroying one.

    `load.error` is a separate attribute on purpose: it describes the state of
    the last `load()`, which is what `doctor` and the menu report.
    """
    path = config_path()
    load_raw.error = None

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}  # No config file is the normal case, not an error.
    except OSError as exc:
        load_raw.error = "cannot read %s: %s" % (path, exc)
        return {}
    except ValueError as exc:  # Undecodable bytes where text was expected.
        load_raw.error = "cannot read %s: %s" % (path, exc)
        return {}

    try:
        data = json.loads(text)
    except ValueError as exc:
        load_raw.error = "invalid JSON in %s: %s" % (path, exc)
        return {}

    if not isinstance(data, dict):
        load_raw.error = "%s must hold a JSON object, found %s" % (path, type(data).__name__)
        return {}
    return data


load_raw.error = None


def update(changes):
    """Read-modify-write `config.json`: the one entry point for changing it.

    Used by `pin add`/`pin remove` and `statusline --mode`. Returns the raw dict
    that was written.

    Raises `ConfigError` and writes nothing if the file exists but cannot be
    read, cannot be parsed, or is not a JSON object. `load_raw()` reads all
    three as `{}`, so without this guard a `pin add` on a config with one stray
    comma would replace every other setting the user has with the one pin --
    silently, and at the moment they were doing something unrelated. A missing
    file is not an error: it becomes a config holding exactly `changes`.

    A change whose value is a dict merges one level (`_merge`): passing
    `{"statusline": {"claude": "own"}}` changes claude's mode and leaves the
    other tools' alone.
    """
    raw = load_raw()
    if load_raw.error:
        raise ConfigError("%s -- fix the file first; nothing was written" % load_raw.error)
    _merge(raw, changes)
    save(raw)
    return raw


def projects_dir():
    """The effective directory a new shell (Ctrl-N) opens in.

    `FLIGHTDECK_PROJECTS_DIR`, else the file's value, else `~`, expanded. The
    environment wins here because this is the one path a test or a one-off run
    needs to move, and it stays out of `load()` so it can never be written back
    into the user's config as a permanent absolute path.
    """
    value = _env("FLIGHTDECK_PROJECTS_DIR") or load().get("projects_dir")
    return Path(os.path.expanduser(str(value or DEFAULTS["projects_dir"])))


def save(cfg):
    """Write `config.json` atomically (temp file in the same directory + replace).

    Same directory so `os.replace` is a rename and not a copy across
    filesystems: a reader never sees a half-written file, and a crash mid-write
    leaves the previous config intact. The pid is in the temp name so two
    Flightdecks saving at once cannot corrupt each other's temp file.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with open(str(tmp), "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        # Including the serialisation failing halfway: leaving a stray `.tmp`
        # next to a good config.json is how a directory turns into litter.
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def tmux_socket():
    """The `tmux -L <name>` socket to talk to, or None for the default server.

    The test seam: pointing it at a socket nobody listens on is what keeps a
    test suite from sending notices to the tmux server the user is working in.
    """
    return _env("FLIGHTDECK_TMUX_SOCKET")


def code_dir():
    """The root of the installed code (the directory holding the package)."""
    return Path(__file__).resolve().parent.parent


def installed_here():
    """Is the code running right now the one `update` and `uninstall` manage?

    True only for `<data>/current`, the directory the installer extracts into
    and `update` swaps behind. Anything else is a source checkout -- a clone
    somebody runs from, this repository included -- where replacing or deleting
    the code would be taking apart somebody's working copy because they asked to
    update a command. Both commands ask this before they do anything,
    and it lives here because it is a question about where things are.

    `realpath` on both sides: `code_dir()` is resolved and the data path may go
    through a symlink (`/var` is one on macOS), so comparing them unresolved
    would answer "no" on a machine where the answer is yes.
    """
    return (os.path.realpath(str(code_dir()))
            == os.path.realpath(str(data_dir() / "current")))


def version():
    """This Flightdeck's version, from the `VERSION` file at the root.

    One file for both languages: the bash command reads it with `cat` for
    `--version` and python reads it here, so a release cannot stamp one and
    leave the other behind.

    `"unknown"` when the file is missing or unreadable -- a checkout with no
    VERSION, or a half-extracted tarball. Saying "unknown" is a version report
    that is merely unhelpful; raising here would take down `doctor`, whose whole
    job is to survive a broken installation and describe it.
    """
    try:
        return (code_dir() / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except (OSError, ValueError):
        return "unknown"


def _as_text(value):
    """One config value as one line of shell-friendly text."""
    if value is None:
        return ""
    if isinstance(value, bool):  # Before int: bool is a subclass of int.
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def main(argv=None):
    """`flightdeck.config <key>` -> the effective value, one line, exit 0.

    Unknown key: nothing printed, exit 1, so `v=$(... key) || fallback` reads
    naturally in bash. A broken config.json still prints the default value
    (exit 0): the bash command asks for the menu port on every entry and cannot
    be left without an answer because of a typo somewhere else in the file.

    `projects_dir` prints the effective, expanded path: the caller is a shell
    about to `cd` there, and `~` is not a directory.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        sys.stderr.write("usage: python3 -m flightdeck.config <key>\n")
        return 2
    cfg = load()
    key = argv[0]
    if key not in cfg:
        return 1
    value = str(projects_dir()) if key == "projects_dir" else cfg[key]
    sys.stdout.write(_as_text(value) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
