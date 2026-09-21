"""Safety net for the whole suite: no test may write into the real home.

unittest's discovery imports this package before importing any test module, so
this is in place before the first `from flightdeck import ...` -- which matters,
because the three installers work out which files they write at IMPORT time,
from `Path.home()`.

The `FLIGHTDECK_*` seams below are defaults: a test that patches the environment
itself (most of them do) still wins, and so does a developer or CI job that
exports its own. HOME and the three XDG variables are SET, overriding whatever
the shell had -- see the comments on each for the escape each one closes.

Without this, one forgotten `mock.patch.dict` is enough for a test to create
session cards in `~/.local/state/flightdeck`, rewrite the developer's own
`~/.claude/settings.json` or reach a live tmux server.
"""

import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="flightdeck-tests-")

# A home for the scripts the tests run as SUBPROCESSES (the hooks). They look
# things up in `~/.claude` -- the conversation's title, Claude Code's registry --
# and `HOME=<this>` keeps that away from the real one. It is deliberately NOT one
# of a test's own temporary directories: this interpreter puts its bytecode cache
# under `$HOME/Library/Caches`, so a per-test home would make every subprocess
# recompile the package, and the cache being written afterwards raced with the
# directory being cleaned up.
SUBPROCESS_HOME = os.path.join(_SANDBOX, "home")
os.makedirs(SUBPROCESS_HOME, exist_ok=True)

# And it is THIS process's home too, set like the XDG trio below and for the
# same kind of reason. The three installers read `Path.home()` when they are
# imported (`~/.claude/settings.json`, `~/.codex/config.toml`,
# `~/.gemini/config/hooks.json`), so an in-process test that forgets its fakes
# does not fall back on "nothing happens": it reaches the developer's own agent
# configuration and writes it. That nearly happened once, and it was survivable
# only by luck -- the pre-release cockpit Flightdeck was ported from spells its
# file names with hyphens. A test that wants a home of its own still moves HOME
# itself; no test may UNSET it.
os.environ["HOME"] = SUBPROCESS_HOME

os.environ.setdefault("FLIGHTDECK_STATE_DIR", os.path.join(_SANDBOX, "state"))
os.environ.setdefault("FLIGHTDECK_CONFIG", os.path.join(_SANDBOX, "config.json"))
# A socket name nothing listens on: tmux commands fail instead of reaching the
# server the developer is actually working in.
os.environ.setdefault("FLIGHTDECK_TMUX_SOCKET", "flightdeck-tests-no-such-socket")

# The XDG paths are SET, not defaulted, which is the one place this file does not
# let the outside win -- and deliberately so. Every path Flightdeck reads falls
# back to one of these when its `FLIGHTDECK_*` variable is missing, so on a
# machine that exports them (routine on Linux and in CI) a test that only
# overrode HOME still reached the developer's real config, data and state. The
# one that bites hardest is `XDG_CONFIG_HOME`: `flightdeck quit` sources
# `$XDG_CONFIG_HOME/tmux/tmux.conf` when the home has no `.tmux.conf`, so their
# real tmux config -- `run-shell` lines and all -- would be executed on the test
# server. A test that wants a particular XDG path still sets it itself.
for _name, _tail in (("XDG_CONFIG_HOME", ("config",)),
                     ("XDG_DATA_HOME", ("data",)),
                     ("XDG_STATE_HOME", ("xdg-state",))):
    os.environ[_name] = os.path.join(_SANDBOX, *_tail)

# COLUMNS is REMOVED, for the same reason the XDG trio is overridden: it is read
# from the outside and it changes what the code produces. Claude Code exports it
# to the status line command (from its own stdout), so `render_claude` trims to
# it -- which means that on a machine whose shell exports COLUMNS, every test
# asserting on a whole status line would silently be asserting on a trimmed one,
# and how badly would depend on how wide the developer's window happened to be.
# A test that wants a width sets it itself; the suite's baseline is "no terminal".
os.environ.pop("COLUMNS", None)

atexit.register(shutil.rmtree, _SANDBOX, True)


def broken_package_copy(root, source):
    """Copy one hook script next to a `flightdeck` package that cannot be imported.

    The hooks are launched by absolute path and put their OWN root on
    `sys.path` to find the package (two levels up from the file). So a broken
    install is simulated by giving a copy of the script a root of its own whose
    `flightdeck/__init__.py` raises: the import fails for real, exactly as it
    would with a half-written file, instead of through a flag the code could
    recognise and treat gently.

    `root` is a directory of the test's own; returns the path to run.
    """
    import pathlib

    pkg = pathlib.Path(root) / "flightdeck"
    (pkg / "hooks").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        'raise RuntimeError("this package is broken on purpose")\n')
    (pkg / "hooks" / "__init__.py").write_text("")
    target = pkg / "hooks" / pathlib.Path(source).name
    shutil.copyfile(str(source), str(target))
    return target
