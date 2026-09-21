"""`flightdeck uninstall`: take Flightdeck back off this machine.

The half you are reading is everything that is not tmux. The tmux half -- the
stock options, the three key bindings, the hook, the menus -- happens in the bash
command BEFORE this module is started, because it is exactly what `quit` does and
because no bash may still be reading a script file that is about to be deleted.

What it removes, in this order and for these reasons:

1. **The three agents** (`install.claude`, `install.codex`, `install.agy`): our
   hooks out, their status line and their codex `notify` restored from
   `delegates/`. Each one is asked separately and a refusal is one line: a
   `~/.codex/config.toml` nobody can parse must not leave Claude Code's hooks
   registered for ever.
2. **The links** `~/.local/bin/flightdeck` and `~/.local/bin/fld`, and only when
   they resolve into the code directory being removed. `fld` is a short name and
   somebody else's may be sitting there.
3. **The `# flightdeck` line** in the shell rc files, with a timestamped backup
   and the rest of the file byte for byte as it was.
4. **The state and the config**, but only with `--purge`. They are kept by
   default because `delegates/` is the only copy of the user's original status
   line and notify commands -- the installers have just restored from them, and
   somebody who reinstalls tomorrow wants them there.
5. **The code**, last of all, and only when this Flightdeck IS the installed one
   (`<data>/current`). Run from a source checkout it says so and removes
   nothing: a clone is not ours to delete. The fzf downloaded into `<data>/bin`
   goes with the code, since it was put there for Flightdeck and nothing else.

The code directory is last because the process is running out of it: everything
else has to have been done, and printed, before it disappears. Every module this
file needs is imported at the top for the same reason -- a lazy import after the
removal would have nothing to read.

What it never touches: the work sessions and the agents running in them (they are
tmux's, and `quit`'s promise is this one's too), the pins' sessions (the user's
own commands), and anything inside another program's configuration that was not
Flightdeck's to begin with.
"""

import os
import shutil
import sys
from pathlib import Path

from flightdeck import config
from flightdeck.install import agy, backup, claude, codex

USAGE = "usage: flightdeck uninstall [--purge]\n"

# The comment `install.sh` leaves at the END of the PATH line it adds, and the
# only thing that says a line in somebody's rc file is ours (`is_our_line`).
RC_MARK = "# flightdeck"

# Where that line can be, in the order they are looked at. Relative to the home
# directory, fish included: it is the one of the four that does not live at the
# top level.
RC_FILES = (".zshrc", ".bashrc", ".bash_profile", ".config/fish/config.fish")

# The commands the installer links into `~/.local/bin`.
LINKS = ("flightdeck", "fld")

# The three tools, in the order they are asked. Claude Code first for the same
# reason `install` does it first: it is the one everybody has.
AGENTS = (("Claude Code", claude), ("codex", codex), ("agy", agy))


def _heading(text):
    """A blank line and a title, so the run reads as steps rather than a wall."""
    print("\n%s" % text)


# ── the agents ───────────────────────────────────────────────────────────────

def remove_from_agents():
    """Ask the three installers to take our entries out. -> the labels that failed.

    They raise rather than return half a job, which is right in a library and
    wrong to show a person: here it becomes one line, the next tool is still
    asked, and the exit code carries the news.
    """
    failed = []
    for label, module in AGENTS:
        # A heading each, the same shape `flightdeck install` prints: the three
        # installers say "status line: restored ..." in the same words, and read
        # one after another with nothing in between there is no telling whose
        # status line came back.
        _heading(label)
        try:
            module.uninstall()
        except (config.ConfigError, ValueError, OSError) as exc:
            print("✗ %s: %s" % (label, exc))
            failed.append(label)
    return failed


# ── the links ────────────────────────────────────────────────────────────────

def link_paths(home=None):
    """The two paths the installer links the command into."""
    home = Path(home) if home else Path.home()
    return [home / ".local" / "bin" / name for name in LINKS]


def points_into(link, code):
    """Does `link` land inside `code`?

    What makes a link ours is where it LANDS, not what it is called: `fld` is a
    short name, the installer only creates it when it is free, and somebody
    else's `fld` in the same directory must survive this command.

    `os.path.realpath` and not `Path.resolve(strict=True)` because a link whose
    target has already gone is still ours and still breaks `command -v`; realpath
    answers for a path that is not there.
    """
    target = Path(os.path.realpath(str(link)))
    return target == code or code in target.parents


def remove_links(code):
    """Take out the links that point at `code`. -> the labels that failed."""
    failed = []
    for link in link_paths():
        if not (link.is_symlink() or link.exists()):
            print("no link at %s" % link)
            continue
        if not points_into(link, code):
            print("%s left alone: it does not point at Flightdeck's code" % link)
            continue
        try:
            link.unlink()
        except OSError as exc:
            print("✗ %s: %s" % (link, exc))
            failed.append(str(link))
            continue
        print("✓ removed the link %s" % link)
    return failed


# ── the shell rc line ────────────────────────────────────────────────────────

def is_our_line(line):
    """Is this line the one the installer wrote?

    It has to END with the mark, not merely carry it. `install.sh` writes
    `export PATH="$HOME/.local/bin:$PATH"  # flightdeck` (and fish's equivalent,
    ending the same way), so the mark is a trailing comment and nothing else.
    Matching the words anywhere in the line would take a heading of theirs
    (`# flightdeck notes to self`), an alias whose comment mentions us, or a line
    that merely prints the word -- lines nobody would think to look for, and only
    findable afterwards by diffing a backup they do not know exists.

    `rstrip()` first because an rc file is hand-edited: trailing spaces after the
    mark, and the line ending itself, do not make the line somebody else's.
    """
    return line.rstrip().endswith(RC_MARK)


def without_our_lines(text):
    """`text` with the installer's own lines gone. -> (the text, those lines)

    `splitlines(True)` keeps the line endings, so what comes back differs from
    what went in by exactly those lines: a CRLF file stays CRLF and a file with
    no trailing newline does not grow one. The rc file is the user's, and the
    only thing we are entitled to change in it is the line we put there.
    """
    kept, removed = [], []
    for line in text.splitlines(True):
        (removed if is_our_line(line) else kept).append(line)
    return "".join(kept), [line.strip() for line in removed]


def _read_keeping_newlines(path):
    """The file as text with its line endings exactly as they are on disk.

    `Path.read_text()` translates them: a CRLF rc file (WSL2, an editor coming
    from Windows) is read as LF and written back as LF, so taking out one line
    would silently rewrite every other line in the file. `newline=""` turns the
    translation off in both directions.
    """
    with open(str(path), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_keeping_newlines(path, text):
    """The counterpart: write `text` with whatever endings it carries."""
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def clean_rc_files(home=None):
    """Take our PATH line out of the shell rc files. -> the labels that failed."""
    home = Path(home) if home else Path.home()
    failed = []
    touched = False
    for name in RC_FILES:
        path = home / name
        try:
            text = _read_keeping_newlines(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:   # ValueError: undecodable bytes
            print("✗ %s: %s" % (path, exc))
            failed.append(str(path))
            continue
        kept, removed = without_our_lines(text)
        if not removed:
            # Not written at all, not even backed up: rewriting a file byte for
            # byte still moves its mtime, and a `.bak-flightdeck-` beside an
            # untouched rc file is a puzzle for whoever finds it.
            continue
        try:
            copy = backup(path)
            _write_keeping_newlines(path, kept)
        except OSError as exc:
            print("✗ %s: %s" % (path, exc))
            failed.append(str(path))
            continue
        touched = True
        print("✓ removed the PATH line from %s" % path)
        # The line itself, and not just the file name: this is somebody else's
        # file, and "I changed your .zshrc" without saying how is an invitation
        # to go and diff a backup they did not know was there.
        for line in removed:
            print("    %s" % line)
        if copy:
            print("  backup: %s" % copy)
    if not touched and not failed:
        print("no Flightdeck line in %s"
              % ", ".join("~/" + name for name in RC_FILES))
    return failed


# ── the state and the config ─────────────────────────────────────────────────

def purge_state():
    """Remove the state directory and `config.json`. -> the labels that failed.

    Only ever reached with `--purge`. It runs AFTER the three installers, which
    is what makes it safe: `delegates/` is the only copy of the user's original
    status line and notify commands, and by now they have been put back where
    they came from.
    """
    failed = []
    state = config.state_dir()
    if state.is_dir():
        try:
            shutil.rmtree(str(state))
            print("✓ removed the state in %s (session cards, caches, delegates)"
                  % state)
        except OSError as exc:
            print("✗ %s: %s" % (state, exc))
            failed.append(str(state))
    else:
        print("nothing to remove in %s" % state)

    path = config.config_path()
    if path.exists():
        try:
            path.unlink()
            print("✓ removed the config %s" % path)
        except OSError as exc:
            print("✗ %s: %s" % (path, exc))
            failed.append(str(path))
    else:
        print("nothing to remove at %s" % path)
    return failed


# ── the code ─────────────────────────────────────────────────────────────────

def remove_code():
    """Remove the installed code. -> the labels that failed.

    `current` (this very code), `previous` (what the last update replaced) and
    `bin` (an fzf the installer downloaded, which exists for Flightdeck and for
    nothing else). The data directory itself goes when there is nothing left in
    it, and stays when the user put something there.
    """
    data = config.data_dir()
    failed = []
    what = {"current": "the code in %s",
            "previous": "the code the last update replaced, %s",
            "bin": "the fzf downloaded into %s"}
    for name in ("current", "previous", "bin"):
        path = data / name
        if not path.exists():
            continue
        try:
            shutil.rmtree(str(path))
        except OSError as exc:
            print("✗ %s: %s" % (path, exc))
            failed.append(str(path))
            continue
        print("✓ removed " + what[name] % path)
    try:
        os.rmdir(str(data))
    except OSError:
        # Not empty, not there, or not ours to remove: all three are fine.
        pass
    return failed


def main(argv=None):
    """`flightdeck uninstall [--purge]`. -> the exit code.

    1 when anything refused to come out (the line says which), 0 otherwise. 2
    when the words do not parse -- and then nothing at all has been removed,
    which is why the arguments are read before the first step.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    purge = "--purge" in argv
    unknown = [word for word in argv if word != "--purge"]
    if unknown:
        sys.stderr.write("flightdeck uninstall: unknown option %s\n" % unknown[0])
        sys.stderr.write(USAGE)
        return 2

    # Resolved once, here: it is compared against where a link lands and against
    # `<data>/current`, and on macOS one side of that comparison comes through a
    # symlink (`/var` -> `/private/var`) often enough to matter.
    code = Path(os.path.realpath(str(config.code_dir())))
    print("flightdeck %s — uninstall" % config.version())
    print("  code: %s" % code)

    failed = []
    failed += remove_from_agents()

    _heading("The command")
    failed += remove_links(code)
    failed += clean_rc_files()

    _heading("State and configuration")
    if purge:
        failed += purge_state()
    else:
        print("kept: %s (session cards, caches and the delegates -- the only "
              "copy of your own status line)" % config.state_dir())
        print("kept: %s" % config.config_path())
        print("  `flightdeck uninstall --purge` removes both")

    # Last, and after everything else has been printed: this is the directory
    # the process is running out of.
    _heading("The code")
    if config.installed_here():
        failed += remove_code()
    else:
        print("running from a source checkout: the code is left where it is (%s)"
              % code)

    print("\nFlightdeck is off this machine. Your tmux work sessions and the "
          "agents in them were never touched.")
    if failed:
        print("Some of it would not come out: %s — see the ✗ lines above."
              % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
