"""`flightdeck install`: the one command that sets up every agent on the machine.

It is what `install.sh` runs at the end of a fresh install and what
`flightdeck update` re-runs with `--yes` after downloading new code. It writes
nothing itself: the three installers next door do the writing, `migrate` takes
the pre-release cockpit Flightdeck was ported from back out first, `pins`
writes the config, and `doctor` has the last word.

The order, and why it is that order:

1. **Claude Code**, always -- our hooks live in a settings file, so installing
   on a machine where the binary is not there yet is a perfectly ordinary thing
   to do, and the doctor is what says the binary is missing.
2. **codex**, and 3. **agy**, each only when it is on PATH: a tool nobody has
   installed has no configuration to write into, and one line saying it was
   skipped beats a file appearing in a directory they never made.
3. **The pins**, **the tmux keys**, and then **the doctor**, whose exit code
   joins ours.

And within each tool, the MIGRATION runs before the installer, never after. The
status line the user really has is behind the old cockpit's tee; register ours
first and it would save that tee as its delegate -- a tee calling a tee.
`--keep-legacy` is how that migration is left undone for a while: skipping it
is the only thing the flag does, and the run that finally migrates also takes
the old tees back out of Flightdeck's own delegates, which is where wrapping
them put them.

Two questions, and no more than two (there is no menu here, and no `--dry-run`):
which status line they want for Claude Code, and the same for agy. They are
asked once, after a DEMO of what Flightdeck's line looks like, and never again:
a mode already in `config.json` is a choice somebody made, and `update`
reinstalling must not undo it. With `--yes`, with `FLIGHTDECK_YES=1`, or with
nothing on the other end of stdin, nobody is asked and the installer applies its
own default -- `own` for someone with no status line, `wrap` for someone with
one, because a line nobody was shown the demo for is not replaced behind their
back.
"""

import os
import shutil
import subprocess
import sys

from flightdeck import common, config, doctor, pins, statusline
from flightdeck.install import agy, claude, codex, migrate

USAGE = "usage: flightdeck install [--yes] [--force] [--keep-legacy]\n"

# What `--keep-legacy` is for, said once where it happens.
KEEP_LEGACY_LINE = ("  legacy entries kept: the old cockpit's hooks stay "
                    "beside Flightdeck's")

# The two questions, spelled once.
STATUS_LINE_QUESTION = ("Use the Flightdeck status line for %s? Your current one "
                        "is backed up and restored on uninstall. [Y/n]")
PIN_QUESTION = "Found %s — pin %s to the menu? [Y/n]"

# What each key Flightdeck takes is FOR, for the collision warning. Saying "a
# key is taken" without saying what takes it leaves the reader unable to decide
# whether they mind.
KEY_PURPOSE = {"F12": "menu toggle",
               "Shift+Enter": "new line in Claude Code",
               "prefix j": "jump to the menu",
               "prefix n": "handover"}


def _ask(question):
    """One yes/no question on a real stdin. -> the answer. Enter is yes.

    Injected everywhere else, so no test ever reaches a terminal. Anything that
    is not a plain "no" counts as yes, the end of stdin included: the prompt
    says `[Y/n]`, and what a Ctrl-D takes is the default it is showing.
    A Ctrl-C is not an answer and travels on, to the guard at the bottom of this
    file.
    """
    try:
        answer = input(question + " ").strip().lower()
    except EOFError:
        print()
        return True
    return answer not in ("n", "no")


def _join(words):
    """`["a"]` -> "a"; `["a", "b"]` -> "a and b"; `["a", "b", "c"]` -> "a, b and c"."""
    words = list(words)
    if len(words) < 2:
        return words[0] if words else ""
    return "%s and %s" % (", ".join(words[:-1]), words[-1])


def _heading(text):
    """A blank line and a title, so the run reads as steps rather than a wall."""
    print("\n%s" % text)


# ── the status line question ─────────────────────────────────────────────────

def _mode_for(installer, label, ask, interactive):
    """Which status line mode to install, asking when there is a choice to make.

    -> the mode, or None meaning "installer, you decide" -- which is not the
    same as a mode: given None it reads what the user already had and applies
    `default_mode` itself (`own` with no line of their own, `wrap` with one),
    and that is exactly what the non-interactive answer has to be. A line
    nobody was shown the demo for is not replaced behind their back.

    A mode already in `config.json` short-circuits everything: it was chosen
    once and `flightdeck update` re-runs this.
    """
    chosen = installer.mode_in_file()
    if chosen is not None:
        print("status line (%s): keeping the mode you chose (%s)" % (label, chosen))
        return None
    if not interactive:
        return None
    return "own" if ask(STATUS_LINE_QUESTION % label) else "wrap"


def _show_demo(tools=("claude",)):
    """Print what Flightdeck's line looks like, for the tools that can paint it.

    A picture of what is being offered, once, before the first question. It is
    the same `flightdeck statusline --demo` prints. A demo that blew up must not
    stop an installation, so a failure is swallowed and the question is asked
    anyway -- the answer is about which line they want, not about this one
    having rendered.
    """
    try:
        statusline._demo(tools)
    except Exception as exc:
        print("(the status line demo could not be drawn: %s)" % exc)


# ── the steps ────────────────────────────────────────────────────────────────

def _install_tool(label, migrate_step, install_step):
    """One tool: migrate, then install. -> True when it went in.

    The three installers raise rather than return half an installation, which is
    the right call in a library and the wrong thing to show a person: here it
    becomes one line, the tool is skipped, and the run ends non-zero having
    still reported on the machine.
    """
    try:
        migrate_step()
        install_step()
        return True
    except (config.ConfigError, ValueError, OSError) as exc:
        print("✗ %s: %s" % (label, exc))
        return False


def _migration(step, keep_legacy):
    """That tool's migration step, or one that does nothing. -> a callable.

    `--keep-legacy` is for one machine and one week of it: Flightdeck is
    installed beside the pre-release cockpit it was ported from, both menus stay
    alive while the manual checklist is run, and only then does a reinstall
    WITHOUT the flag take the old entries out. For everybody else there are no
    legacy entries and the flag changes nothing at all.

    Skipping is all this does, but the run it makes is not free. The installer
    that follows wraps the command that is REGISTERED, and on that machine that
    command is the old cockpit's status line tee and its notify tee -- so those
    are what Flightdeck saves as its own delegates. The run that finally
    migrates is therefore a repair as well: `migrate` takes the old tees back
    out of our delegates and puts the user's real ones there. Which is also why
    this flag is for a week and not a way to live.
    """
    return (lambda: None) if keep_legacy else step


def _pins_step(ask, interactive, which):
    """Offer to pin the tools that are on this machine."""
    _heading("Pinned menu rows")
    cfg = config.load()
    already = {pin.get("name") for pin in cfg.get("pins") or []
               if isinstance(pin, dict)}
    found = [name for name in pins.detected(which=which) if name not in already]
    if not found:
        print("pins: nothing new to pin (`flightdeck pin list` shows the catalogue)")
        return
    if interactive and not ask(PIN_QUESTION
                               % (_join(found), "it" if len(found) == 1 else "them")):
        print("pins: left alone — `flightdeck pin add <name>` any time")
        return
    added = []
    for name in found:
        try:
            cfg = pins.add(cfg, name=name)
            added.append(name)
        except pins.PinError as exc:
            print("  %s not pinned: %s" % (name, exc))
    if not added:
        return
    try:
        config.update({"pins": cfg["pins"]})
    except config.ConfigError as exc:
        print("✗ pins: %s" % exc)
        return
    print("✓ pinned %s" % _join(added))


def _switcher_step(which):
    """Say how to get the account switcher when it is not here.

    The `⚙ accounts` row is `cswap`, the `claude-swap` package: the one preset
    people expect to find after reading about accounts, and the one a fresh
    machine is least likely to have. Like every other hint, it is printed and
    never run.
    """
    if which("cswap"):
        return
    print("cswap, the account switcher behind the ⚙ accounts row, is not on this "
          "machine")
    print("  install it with: %s" % pins.hint_for("cswap"))
    print("  then `flightdeck pin add cswap` puts the row in the menu")


def _keys_step(run):
    """Warn about the four tmux keys somebody else has already taken.

    `has-session` FIRST, and it is not a nicety: measured on tmux 3.6a, with no
    server running `list-keys` exits 0, prints tmux's STOCK table -- so the
    answer would look like a live server nobody has configured -- and leaves a
    socket file behind to do it. `flightdeck install` starts no servers.
    """
    _heading("tmux keys")
    try:
        alive = run(common.tmux_bin() + ["has-session"], capture_output=True,
                    text=True, timeout=5)
        if alive.returncode != 0:
            print("no live tmux server to ask — the keys are installed when you "
                  "enter the menu (`flightdeck`)")
            return
        listed = run(common.tmux_bin() + ["list-keys"], capture_output=True,
                     text=True, timeout=5)
        if listed.returncode != 0:
            print("the tmux server did not answer `list-keys`")
            return
        collisions = doctor.key_collisions(listed.stdout or "")
    except (OSError, subprocess.SubprocessError) as exc:
        print("could not read the tmux keys: %s" % exc)
        return
    if not collisions:
        print("F12, Shift+Enter, prefix j and prefix n are free (or already "
              "Flightdeck's)")
        return
    for label, command in collisions:
        print("⚠ %s was bound to `%s`; Flightdeck's %s takes it when you enter "
              "the menu" % (label, command, KEY_PURPOSE.get(label, "binding")))
    print("  `flightdeck quit` gives tmux its own keys back")


def _next_steps(has_codex):
    """What to do now, and the last thing printed when nothing refused.

    Not a word about `~/.local/bin`: whether a line was added to a shell rc is
    something only `install.sh` knows, and it says so itself.
    """
    _heading("Next steps")
    print("  • type `flightdeck` to enter the menu")
    print("  • F12 jumps between the menu and your session; `prefix + n` hands "
          "the conversation over to a fresh one")
    print("  • restart the Claude Code sessions you have open: the hooks are "
          "registered when a session starts")
    if has_codex:
        print("  • the next codex you start will say \"N hooks are new or "
              "changed\" — answer \"Trust all and continue\"; they are these")


# ── the command ──────────────────────────────────────────────────────────────

def main(argv=None, ask=None, isatty=None, which=None, run=None):
    """`flightdeck install [--yes] [--force] [--keep-legacy]`. -> the exit code.

    1 when an installer refused or the doctor found something Flightdeck cannot
    work around; 0 otherwise -- including on a machine with no agent installed
    at all, which is what CI's smoke test runs. 2 when the words do not parse.

    Everything that touches the world outside is injected (`ask`, `isatty`,
    `which`, `run`) so the tests drive the whole command without a terminal, a
    PATH or a tmux server.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    ask = ask or _ask
    isatty = isatty or (lambda: sys.stdin.isatty())
    which = which or shutil.which
    run = run or subprocess.run

    yes = "--yes" in argv
    force = "--force" in argv
    keep_legacy = "--keep-legacy" in argv
    unknown = [word for word in argv
               if word not in ("--yes", "--force", "--keep-legacy")]
    if unknown:
        sys.stderr.write("flightdeck install: unknown option %s\n" % unknown[0])
        sys.stderr.write(USAGE)
        return 2

    # Nobody is asked anything with `--yes`, with `FLIGHTDECK_YES=1` (what
    # `install.sh` and `flightdeck update` set) or with nothing on the other end
    # of stdin -- a pipe, a CI job, a `curl | sh`. A stdin that cannot even
    # answer whether it is a terminal is not one either.
    interactive = not yes and os.environ.get("FLIGHTDECK_YES") != "1"
    if interactive:
        try:
            interactive = bool(isatty())
        except Exception:
            interactive = False

    print("flightdeck %s — install" % config.version())
    print("  code: %s" % config.code_dir())
    if keep_legacy:
        print(KEEP_LEGACY_LINE)

    has_codex = bool(which("codex"))
    has_agy = bool(which("agy"))
    failed = []

    # The demo is shown ONCE, before the first question, and it carries agy's
    # line only when agy is here: a line for a tool they do not have is a
    # picture of nothing they can use.
    if interactive:
        _heading("This is the status line Flightdeck paints")
        _show_demo(("claude", "agy") if has_agy else ("claude",))

    _heading("Claude Code")
    mode = _mode_for(claude, "Claude Code", ask, interactive)
    if not _install_tool("Claude Code",
                         _migration(migrate.migrate_claude, keep_legacy),
                         lambda: claude.install(mode=mode)):
        failed.append("Claude Code")

    _heading("codex")
    if has_codex:
        if not _install_tool("codex",
                             _migration(migrate.migrate_codex, keep_legacy),
                             lambda: codex.install(force=force)):
            failed.append("codex")
    else:
        print("codex: not found, skipped")

    _heading("Antigravity (agy)")
    if has_agy:
        agy_mode = _mode_for(agy, "Antigravity (agy)", ask, interactive)
        if not _install_tool("agy",
                             _migration(migrate.migrate_agy, keep_legacy),
                             lambda: agy.install(mode=agy_mode)):
            failed.append("agy")
    else:
        print("agy: not found, skipped")

    _pins_step(ask, interactive, which)
    _switcher_step(which)
    _keys_step(run)

    # The doctor through its own printer, so what is read here is word for word
    # what `flightdeck doctor` says -- and its exit code is half of ours.
    print()
    unhealthy = doctor.main([])

    # Before the next steps, not after: this is the only line that says part of
    # the install did not happen, and printed last it sat under a cheerful list
    # of things to go and try.
    if failed:
        print("\n%s could not be set up: %s — see the ✗ line for each, then run "
              "`flightdeck install` again."
              % ("One agent" if len(failed) == 1 else "Some agents",
                 _join(failed)))
    _next_steps(has_codex)
    return 1 if (failed or unhealthy) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # A Ctrl-C at a question. Said in one line rather than as a traceback,
        # and 130 is what a shell expects of a command killed by one.
        sys.stderr.write("\nflightdeck install: stopped. Nothing else was "
                         "written; run it again when you are ready.\n")
        sys.exit(130)
