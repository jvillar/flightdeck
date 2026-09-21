"""Pinned rows: fixed menu entries that open a tool of the user's own.

A pin is a row the menu always paints, between the live tmux sessions and the
history, which switches to (or creates) one permanent tmux session running one
command: a process viewer, a git UI, an account switcher, a notes file. It lives
in `config.json` under `pins`, and there are none by default.

The catalogue of `PRESETS` is there so the common ones do not have to be typed:
`flightdeck pin add htop` knows the label, the command, the tmux session and --
when the tool is missing -- how that machine installs it. The hint is **printed,
never run**: what to install is the user's decision, and an installer that ran
package managers on their behalf would be a very different program.

Detection is `shutil.which` on the FIRST WORD of the command, so `cswap tui` is
found by looking for `cswap`. Everything here is pure except `main`, which is the
only part that reads or writes the config file.

Three rules a pin has to obey, all enforced in `add`:

- its tmux session cannot be one of Flightdeck's own (`flightdeck`,
  `flightdeck-N`) nor another pin's -- those sessions are infrastructure, hidden
  from the green group, and two pins sharing one would fight over it;
- its name is unique, because that is how `remove` finds it;
- it has a command, because a row that types nothing on Enter is worse than no
  row at all.

Run as a module it is the `flightdeck pin` command: `list`, `add`, `remove`.
"""

import shutil
import sys
import unicodedata

from flightdeck import config
from flightdeck.common import pin_session, tmux_safe_name
from flightdeck.picker import is_reserved


class PinError(Exception):
    """The pin cannot be made, and the reason is worth one sentence.

    Separate from `_Usage` (which is about the words typed on the command line)
    so the CLI can answer a bad pin with one line and exit 1, and a bad command
    line with the usage and exit 2.
    """


class _Usage(Exception):
    """The command line does not parse. Answered with the usage text, exit 2."""


# The ready-made pins. Each one carries what the menu paints (`label`), what
# the session runs (`command`), the tmux session it lives in (`session`) and
# the install hint per platform.
#
# The three process viewers share one glyph on purpose: it marks the CATEGORY,
# and they are interchangeable -- the word after it is what tells them apart.
# Labels are kept short (under 16 characters) because the menu paints them
# whole, and the menu is half read on a phone-width terminal.
#
# The hints are honest rather than uniform. `brew install <x>` is right for all
# of them on macOS, but on Linux only `htop` (and `procps`, which is already
# there) is reliably one `apt install` away: `lazygit`, `lazydocker` and `k9s`
# are not in the Debian/Ubuntu archives that ship with the distributions this
# targets, so their hint is the project's own installation page, which stays
# right as the packaging changes.
PRESETS = {
    "top": {
        "label": "📊 top",
        "command": "top",
        "session": "top",
        "hint": {"darwin": "top ships with macOS",
                 "linux": "sudo apt install procps"},
    },
    "htop": {
        "label": "📊 htop",
        "command": "htop",
        "session": "htop",
        "hint": {"darwin": "brew install htop",
                 "linux": "sudo apt install htop"},
    },
    "btop": {
        "label": "📊 btop",
        "command": "btop",
        "session": "btop",
        "hint": {"darwin": "brew install btop",
                 "linux": "sudo apt install btop (Debian 12 / Ubuntu 22.04 and "
                          "newer), else https://github.com/aristocratos/btop#installation"},
    },
    "cswap": {
        # A personal account switcher. It opens its TUI and nothing else: a
        # Flightdeck that rotated accounts by itself would be automating around
        # a provider's limits, which is vetoed and stays vetoed.
        "label": "⚙ accounts",
        "command": "cswap tui",
        "session": "cswap",
        "hint": {"darwin": "cswap is a personal account switcher, not a package "
                           "-- pin your own tool with --label and --command",
                 "linux": "cswap is a personal account switcher, not a package "
                          "-- pin your own tool with --label and --command"},
    },
    "lazygit": {
        "label": "🌿 lazygit",
        "command": "lazygit",
        "session": "lazygit",
        "hint": {"darwin": "brew install lazygit",
                 "linux": "https://github.com/jesseduffield/lazygit#installation"},
    },
    "lazydocker": {
        "label": "🐳 lazydocker",
        "command": "lazydocker",
        "session": "lazydocker",
        "hint": {"darwin": "brew install lazydocker",
                 "linux": "https://github.com/jesseduffield/lazydocker#installation"},
    },
    "k9s": {
        "label": "☸ k9s",
        "command": "k9s",
        "session": "k9s",
        "hint": {"darwin": "brew install k9s",
                 "linux": "https://k9scli.io/topics/install/"},
    },
}


def _first_word(command):
    """What `which` is asked about: `cswap tui` is on the PATH as `cswap`."""
    parts = str(command or "").split()
    return parts[0] if parts else ""


def _installed(command, which):
    """Is the command's binary on the PATH?"""
    word = _first_word(command)
    return bool(word and which(word))


def detected(presets=PRESETS, which=shutil.which):
    """The preset names whose tool is installed, in catalogue order.

    What the installer offers to pin ("Found htop and cswap -- pin them?") and
    what `pin list` marks as installed.
    """
    return [name for name, preset in presets.items()
            if _installed(preset.get("command"), which)]


def hint_for(name, platform=None):
    """How to install a preset's tool on this platform. Empty for a custom pin.

    An unknown platform gets the Linux advice: WSL2 already reports `linux`, and
    on anything else an apt line is a better answer than a blank space where the
    install command should be.
    """
    preset = PRESETS.get(name)
    if not preset:
        return ""
    hints = preset.get("hint") or {}
    key = sys.platform if platform is None else platform
    return hints.get(key) or hints.get("linux") or ""


def _pins_of(cfg):
    """The pins in a config, refusing a `pins` that is not a list.

    Same rule as `config.update`: something that cannot be understood is not
    quietly replaced. A `"pins": "htop"` typed by hand is a mistake the user can
    still fix, and writing over it would take their other pins with it. The
    menu, which only reads, degrades instead (`picker.configured_pins`).
    """
    pins = cfg.get("pins")
    if pins is None:
        return []
    if not isinstance(pins, list):
        raise PinError('"pins" in the config is not a list -- fix the file '
                       'first; nothing was written')
    return pins


def _name_of(pin):
    """A pin's name, falling back to its session, or None if it has neither."""
    if not isinstance(pin, dict):
        return None
    for key in ("name", "session"):
        value = pin.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _session_of(pin):
    """A pin's tmux session as tmux would spell it, or None.

    `common.pin_session` is the shared reader of that key -- the menu reads it
    with the same one, so the two can never disagree about which session a pin
    means. What is added here is the fallback to the NAME, which only this side
    needs: a hand-written pin with no `session` still occupies the session its
    name would take, and without the fallback a `pins.a.b` already in the file
    would not be seen to collide with an `a_b` being added.
    """
    session = pin_session(pin)
    if session:
        return session
    name = _name_of(pin)
    return tmux_safe_name(name) if name else None


def _usable(pins):
    """The pins that can be acted on: an object, with a name (config is hand-edited)."""
    return [p for p in pins if _name_of(p)]


def _slug(label):
    """A name out of a label: `📝 My Notes` -> `my-notes`.

    Only the word characters survive, so the glyph that leads every label does
    not end up in a tmux session name.
    """
    word = []
    words = []
    for char in str(label or ""):
        if char.isalnum() or char in "._-":
            word.append(char)
        elif word:
            words.append("".join(word))
            word = []
    if word:
        words.append("".join(word))
    return "-".join(words).strip("-").lower()


def add(cfg, name=None, label=None, command=None, session=None):
    """A NEW config with one more pin. Raises `PinError` if it cannot be made.

    `name` alone is a preset (`add(cfg, name="htop")`); any of `label`,
    `command` and `session` given with it override that preset's. Without a
    preset it is a pin of the user's own, which needs at least a command and
    something to take a name from.

    The config is copied rather than edited: the caller writes the result
    through `config.update`, and a validation that failed half way through must
    not leave the dict it was handed in a state nobody asked for.
    """
    existing = _pins_of(cfg)
    preset = PRESETS.get(name) if name else None
    if name and preset is None and command is None:
        # Not "unknown preset": `pin add --name scratch` never asked for one.
        # What that user is missing is `--command`, and the message has to be
        # the one that says so -- while still pointing a `pin add htpo` at the
        # catalogue, which is where the typo shows up.
        raise PinError('"%s" is not a preset — add --command for a pin of your '
                       'own, or see: flightdeck pin list' % name)
    if preset:
        label = label or preset["label"]
        command = command or preset["command"]
        session = session or preset["session"]

    command = str(command or "").strip()
    if not command:
        raise PinError("a pin needs a command: "
                       "flightdeck pin add --label L --command C")

    session = tmux_safe_name(str(session or "").strip())
    # The name follows what was given, in order of how explicit it is: what was
    # typed as a name, else the session it lives in, else the label's words.
    name = str(name or "").strip() or session or _slug(label)
    if not name:
        raise PinError("a pin needs a name: pass --session, or a --label with "
                       "letters in it")
    session = session or tmux_safe_name(name)

    if name in {_name_of(p) for p in _usable(existing)}:
        raise PinError('a pin named "%s" already exists' % name)
    owner = next((_name_of(p) for p in _usable(existing)
                  if _session_of(p) == session), None)
    if owner:
        raise PinError('session "%s" is already used by the pin "%s"' % (session, owner))
    if is_reserved(session):
        raise PinError('session "%s" is reserved' % session)

    # Exactly the four keys `picker._pin_entry` reads. Any other spelling would
    # be a row that paints and does nothing when you press Enter on it.
    pin = {"name": name, "label": str(label or name), "session": session,
           "command": command}
    updated = dict(cfg)
    updated["pins"] = list(existing) + [pin]
    return updated


def remove(cfg, name):
    """A NEW config without the pin called `name`. Raises `PinError` if there is none."""
    existing = _pins_of(cfg)
    kept = [p for p in existing if _name_of(p) != name]
    if len(kept) == len(existing):
        raise PinError('no pin named "%s" — try: flightdeck pin list' % name)
    updated = dict(cfg)
    updated["pins"] = kept
    return updated


def list_rows(cfg, which=shutil.which):
    """`(name, label, installed, pinned)` for the whole catalogue, then the custom pins.

    The presets always show, pinned or not: the list is how the user finds out
    what there is to pin. A pinned preset shows the label from the CONFIG, which
    is the one painted in the menu and may have been changed.

    Unlike `add` and `remove` this only reads, so a `pins` key that makes no
    sense degrades to no pins instead of raising: a hand-edited mistake must not
    be what stops you from looking at the catalogue.
    """
    try:
        configured = _usable(_pins_of(cfg))
    except PinError:
        configured = []
    by_name = {}
    for pin in configured:
        by_name.setdefault(_name_of(pin), pin)

    rows = []
    for name, preset in PRESETS.items():
        pin = by_name.get(name)
        label = (pin.get("label") if pin else None) or preset["label"]
        command = (pin.get("command") if pin else None) or preset["command"]
        rows.append((name, str(label), _installed(command, which), pin is not None))
    for name, pin in by_name.items():
        if name in PRESETS:
            continue
        rows.append((name, str(pin.get("label") or name),
                     _installed(pin.get("command"), which), True))
    return rows


USAGE = """usage: flightdeck pin list
       flightdeck pin add <preset>
       flightdeck pin add --label L --command C [--session S] [--name N]
       flightdeck pin remove <name>"""


def _display_width(text):
    """How many terminal columns `text` takes.

    An emoji is two cells wide and one character long, so padding the label
    column with `%-16s` would leave the rows of the listing a column short of
    each other for every glyph. Combining marks and the emoji variation selector
    add nothing.
    """
    width = 0
    for char in text:
        if char == "️" or unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text, width):
    """`text` followed by enough spaces to fill `width` columns, always at least one."""
    return text + " " * max(1, width - _display_width(text))


def _cmd_list(args):
    """The catalogue and the custom pins, one per line, with the missing ones' hint."""
    if args:
        raise _Usage("list takes no arguments")
    cfg = config.load()
    if config.load.error:
        # Reading degrades to the defaults, so the catalogue still prints: the
        # user is told in one line why their pins are not in it.
        sys.stderr.write("warning: %s -- using the defaults\n" % config.load.error)
    # `which` is passed rather than left to the default: the default is bound
    # when this module is imported, and the CLI has to use the one in force when
    # it runs (which is also what lets a test drive the listing with a fake).
    for name, label, installed, pinned in list_rows(cfg, which=shutil.which):
        line = "%s%s%s%s" % (_pad(name, 13), _pad(label, 17),
                             _pad("installed" if installed else "not installed", 15),
                             "pinned" if pinned else "")
        sys.stdout.write(line.rstrip() + "\n")
        hint = "" if installed else hint_for(name)
        if hint:
            sys.stdout.write("    %s\n" % hint)
    return 0


def _parse_add(args):
    """`add`'s words: one preset name and/or `--label`, `--command`, `--session`, `--name`."""
    values = {"name": None, "label": None, "command": None, "session": None}
    positional = None
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            key, equals, value = token[2:].partition("=")
            key = key.replace("-", "_")
            if key not in values:
                raise _Usage("unknown option --%s" % key.replace("_", "-"))
            if not equals:
                index += 1
                if index >= len(args):
                    raise _Usage("--%s needs a value" % key)
                value = args[index]
            values[key] = value
        elif positional is None:
            positional = token
        else:
            raise _Usage('add takes one preset name, not "%s" as well' % token)
        index += 1
    if positional is not None:
        if values["name"] is not None:
            raise _Usage("add takes a preset name or --name, not both")
        values["name"] = positional
    if not any(values.values()):
        raise _Usage("add needs a preset name, or --label and --command")
    return values


def _cmd_add(args):
    """Add one pin and write it, touching nothing else in the config file."""
    values = _parse_add(args)
    updated = add(config.load(), **values)
    config.update({"pins": updated["pins"]})
    pin = updated["pins"][-1]
    sys.stdout.write('pinned %s: %s in the tmux session "%s"\n'
                     % (pin["name"], pin["command"], pin["session"]))
    # Pinning a tool that is not installed is allowed -- you may be about to
    # install it -- but saying nothing leaves a row that opens a session, fails
    # to find the command and dies, with no clue anywhere as to why. `pin list`
    # prints the hint already; this is where the user actually is. Printed,
    # never run: what gets installed is their decision.
    if not _installed(pin["command"], shutil.which):
        hint = hint_for(pin["name"])
        if hint:
            sys.stdout.write("  %s is not installed: %s\n" % (pin["name"], hint))
    return 0


def _cmd_remove(args):
    """Take one pin out and write the rest back."""
    if len(args) != 1:
        raise _Usage("remove takes the name of one pin")
    updated = remove(config.load(), args[0])
    config.update({"pins": updated["pins"]})
    sys.stdout.write("unpinned %s\n" % args[0])
    return 0


def main(argv=None):
    """`flightdeck pin [list|add|remove]`. No arguments is `list`.

    Exit codes: 0 done, 1 the pin could not be made or the config could not be
    written (one sentence on stderr, never a traceback), 2 the command line does
    not parse.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "list"
    rest = argv[1:]
    handlers = {"list": _cmd_list, "add": _cmd_add, "remove": _cmd_remove}
    handler = handlers.get(command)
    try:
        if handler is None:
            raise _Usage("" if not command else 'unknown command "%s"' % command)
        return handler(rest)
    except _Usage as exc:
        if str(exc):
            sys.stderr.write("%s\n" % exc)
        sys.stderr.write(USAGE + "\n")
        return 2
    except PinError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    except config.ConfigError as exc:
        # The file exists and cannot be understood. `config.update` refused to
        # write, which is the point: the user's other settings are still there.
        sys.stderr.write("cannot write config: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
