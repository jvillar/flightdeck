"""Installs Flightdeck into agy (Antigravity): a named hook, and the status line.

Two pieces, in two files that are NOT Flightdeck's -- hence a timestamped backup
on every write and a `status()`:

`~/.gemini/config/hooks.json`, agy's SHARED hooks file (1.1.27, schema read from
the documentation embedded in the binary). Its shape is neither claude's nor
codex's: the top-level keys are hook NAMES -- a user can have several, each with
its own `enabled` and its own arrays per event -- so Flightdeck lives under its
own key and reinstalling overwrites only that one: hooks of theirs sitting
beside it are left intact.

Inside a hook, the TOOL events (`PreToolUse`, `PostToolUse`) are arrays of
groups `{"matcher": ..., "hooks": [...]}` (`"*"` matches everything), while the
turn events (`PreInvocation`, `PostInvocation`, `Stop`) are FLAT arrays of
handlers. A handler is `{"type": "command", "command": <sh string>, "timeout":
<seconds>}`.

`PreToolUse` is deliberately not registered: agy expects its answer to carry a
`decision` (allow or deny the tool) and Flightdeck is not the one to decide
that -- it watches, it does not rule. `PostInvocation` is not registered either:
measured, it arrives just before `Stop` with the same content, so it would be
one more write of the session card per turn and no new state.

`~/.gemini/antigravity-cli/settings.json`, agy's OWN settings, where the status
line command goes (measured: `/statusline <command>` writes
`{"type": "command", "command": ...}` under a `statusLine` key -- Claude Code's
exact shape -- plus `stack_with_default` when both lines are to be shown). That
file also holds the user's colour scheme and their trusted workspaces, so it is
never rewritten when it cannot be parsed, and whatever status line they had is
saved in `delegates/agy-statusline` BEFORE ours goes in.
"""

import json
from pathlib import Path

from flightdeck import config
from flightdeck.install import backup, read_json_object

# A module attribute so a test can point it somewhere else: it is a live file of
# the user's, with hooks of their own possibly in it.
HOOKS_JSON = Path.home() / ".gemini" / "config" / "hooks.json"

# The same, for agy's own settings. Note the directory: 1.1.28 writes its
# conversations under `~/.gemini/antigravity-cli/` and reports a transcript path
# under `~/.gemini/antigravity/`; the settings are in the first.
SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

HOOK = config.code_dir() / "flightdeck" / "hooks" / "session_hook.py"
STATUS_LINE = config.code_dir() / "flightdeck" / "hooks" / "agy_statusline.py"

# Ours is recognised by the file NAME and not by the whole path, so that code
# moved to another directory is still recognised (and replaced) instead of
# being saved as if it were the user's own line.
STATUS_LINE_MARKER = STATUS_LINE.name

# WHICH line agy paints, kept in Flightdeck's config as `statusline.agy`. The
# hook is registered the same way in all three: it always writes the context %
# down and always sings the handover notice, so the menu and the tmux bar see
# no difference. What changes is only what reaches the user's eyes.
MODES = ("own", "wrap", "stack")
MODE_HELP = {
    "own": "Flightdeck's line",
    "wrap": "the line you already had (ours only reads the context %)",
    "stack": "Flightdeck's line, and agy's own default underneath it",
}

# Our key. Everything under it is ours to replace; everything beside it is not.
NAME = "flightdeck"

EVENTS = ("PreInvocation", "PostToolUse", "Stop")
_GROUPED = ("PreToolUse", "PostToolUse")   # the tool events carry a matcher

TIMEOUT = 5


def handler(event):
    """One handler for `event`, the way agy runs it."""
    # `exec`: agy runs this with `sh -c`; without exec the hook's parent would be
    # that short-lived sh and the session card would be born with a dead pid.
    return {"type": "command",
            "command": "exec python3 '%s' %s --tool agy" % (HOOK, event),
            "timeout": TIMEOUT}


def hooks_with_flightdeck(existing):
    """The hooks.json dict with OUR key in (other people's untouched; our own old
    one replaced whole -- so a change of events never duplicates)."""
    data = dict(existing) if isinstance(existing, dict) else {}
    ours = {"enabled": True}
    for event in EVENTS:
        one = handler(event)
        ours[event] = ([{"matcher": "*", "hooks": [one]}]
                       if event in _GROUPED else [one])
    data[NAME] = ours
    return data


def _handlers(value):
    """An event's handlers, flat (turn) or grouped by matcher (tool) alike:
    `status()` looks inside both the same way."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("hooks"), list):
            out.extend(item["hooks"])
        else:
            out.append(item)
    return out


def is_ours(hook):
    """Does this handler call Flightdeck's state hook?"""
    return isinstance(hook, dict) and str(HOOK) in str(hook.get("command") or "")


def installed_state(hooks=None, settings=None):
    """What is registered in agy right now, as DATA. Never raises.

    The counterpart of `claude.installed_state` and `codex.installed_state`:
    `status()` prints for a person, `doctor` needs a severity and an exit code.
    Given `hooks` and `settings` it is pure; left out, it reads agy's two files.

    `stale` is an event of ours that carries handlers none of which points at
    the code running now -- the key still looks whole while every hook fails in
    silence, which is exactly the state nobody notices without being told.

    `error` is a file that is THERE and cannot be read, which is a different
    finding from one that is missing: this hooks.json holds the user's OTHER
    named hooks beside ours, and `install` reads an unparseable one as `{}` and
    writes a file with nothing but our key in it.
    """
    state = {"present": False, "enabled": False, "error": None,
             "status_line_error": None,
             "live": [], "stale": [], "missing": list(EVENTS),
             "status_line": "none", "status_line_command": "",
             "delegate_file": False, "delegate": "", "mode": configured_mode()}
    errors = []

    if hooks is None:
        hooks, error = read_json_object(HOOKS_JSON)
        if error:
            errors.append(error)
    ours = hooks.get(NAME) if isinstance(hooks, dict) else None
    if isinstance(ours, dict):
        state["present"] = True
        state["enabled"] = bool(ours.get("enabled", True))
        state["missing"] = []
        for event in EVENTS:
            handlers = _handlers(ours.get(event))
            if any(is_ours(handler) for handler in handlers):
                state["live"].append(event)
            elif handlers:
                first = handlers[0]
                state["stale"].append(
                    (event, str(first.get("command") if isinstance(first, dict) else first)))
            else:
                state["missing"].append(event)

    if settings is None:
        try:
            settings = _read_settings()
        except config.ConfigError as exc:
            # Kept apart as well as counted in: agy is the one tool whose hooks
            # and whose status line live in two different files, and the status
            # line check must not report a hooks.json problem as its own.
            state["status_line_error"] = str(exc)
            errors.append(str(exc))
    state["error"] = "; ".join(errors) or None
    if settings is not None:
        current = current_status_line(settings)
        state["status_line_command"] = current
        if STATUS_LINE_MARKER in current:
            state["status_line"] = "ours" if str(STATUS_LINE) in current else "stale"
        elif current:
            state["status_line"] = "other"

    # The file existing and what it holds are two different facts: empty is what
    # the installer writes for someone who had no line of their own; gone is the
    # loss of the only copy of one they did have.
    state["delegate_file"] = delegate_file().exists()
    state["delegate"] = saved_delegate()
    return state


# ── the status line ──────────────────────────────────────────────────────────

def delegate_file():
    """Where the `/statusline` command agy had before us is kept.

    Resolved on every call: the state directory moves with
    `FLIGHTDECK_STATE_DIR`. Its own file, not claude's: someone can perfectly
    well have a custom line in both tools, and one file would mean each
    installer overwriting the other's only copy.
    """
    return config.delegates_dir() / "agy-statusline"


def saved_delegate():
    """The command saved in that file ("" when there is none).

    UnicodeDecodeError rides along with OSError on purpose: a binary file in
    there must not take the installer down with a traceback.
    """
    try:
        return delegate_file().read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def status_line_command():
    """The shell string agy runs on every repaint.

    Quoted, and with `exec` for the same reason as the hook handler: agy runs
    this through `sh -c`, so one space in the install path would leave the user
    with no status line at all.
    """
    return "exec python3 '%s'" % STATUS_LINE


def current_status_line(settings):
    """The status line command agy has configured right now ("" when none).

    The measured shape is `{"type": "command", "command": "..."}`, but a bare
    string is respected too: whatever is not read here is lost the moment ours
    goes in its place.
    """
    value = settings.get("statusLine")
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    return (value.get("command") or "").strip()


def _save_delegate(current):
    """Keep the command that was there. -> False when it could not be saved.

    The order matters: the command of a lifetime is written to the delegate
    FIRST, and only if that worked is ours registered. The other way round, a
    failed write would point agy at us with their own line gone for good.
    """
    path = delegate_file()
    already = saved_delegate()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if current:
            path.write_text(current + "\n")
        elif already:
            # agy's settings carry no statusLine (it happens after a `delete`,
            # or when an old backup is restored) but the delegate already holds
            # one: writing an empty file here would lose it for good.
            print("⚠ agy had no statusLine; keeping the one already saved: %s"
                  % already)
        else:
            path.write_text("")
    except OSError as exc:
        print("⚠ status line untouched: could not save the delegate (%s)" % exc)
        return False
    return True


def register_status_line(settings, mode):
    """Put our command into agy's `statusLine`. -> (settings, the one saved).

    `saved` is the command that was there before and is now in the delegate file
    -- "" when there was none, when ours was already registered (nothing is
    saved then: a delegate holding our own command would make the status line
    call itself for ever), or when the delegate could not be written, in which
    case nothing is registered either.

    `stack_with_default` is written only for `stack`. Measured: agy omits the
    key when it is false (the field is `omitempty` in its own tags), so writing
    `false` would vanish on its next save and read afterwards as somebody having
    edited the file by hand.
    """
    settings = dict(settings) if isinstance(settings, dict) else {}
    current = current_status_line(settings)
    ours_already = STATUS_LINE_MARKER in current
    previous = "" if ours_already else current
    if not ours_already and not _save_delegate(previous):
        return settings, ""

    line = settings.get("statusLine")
    # Keys of theirs inside that object (a future agy setting) are kept: only
    # the three that are ours to decide are written.
    line = dict(line) if isinstance(line, dict) else {}
    line["type"] = "command"
    line["command"] = status_line_command()
    if mode == "stack":
        line["stack_with_default"] = True
    else:
        line.pop("stack_with_default", None)
    settings["statusLine"] = line
    return settings, previous


def mode_in_file():
    """The mode the user's `config.json` actually holds, or None.

    None both for "the key is not there" and for "the key says something that is
    not a mode": in either case nobody has made a choice worth honouring, so the
    installer is free to write the default.
    """
    section = config.load_raw().get("statusline")
    mode = section.get("agy") if isinstance(section, dict) else None
    return mode if mode in MODES else None


def configured_mode():
    """The mode in force right now: the file's, else the default.

    The same reading the hook does on every repaint, so `status` and the line
    can never disagree about which one is being painted.
    """
    return mode_in_file() or config.DEFAULTS["statusline"]["agy"]


def set_mode(mode):
    """Write `statusline.agy` AND keep agy's settings.json in step. -> the mode.

    Two files, because for agy the mode is half a key in a file that is not
    ours: `stack` is this config value *and* `stack_with_default: true` beside
    our command in agy's own settings. Written in only one of the two, both
    halves of the mistake are silent -- a `stack` that paints exactly like `own`
    (agy stacks nothing), or an `own` with agy still stacking its default line
    underneath one that no longer expects it.

    When our command is not registered in agy's settings there is nothing to
    keep in step: the config key is written and it says so, because the mode
    only takes effect once `flightdeck install` has put our line in.

    `ValueError` for a mode that does not exist, raised BEFORE anything is
    written: a typo must not leave the user believing they changed something.
    """
    _write_mode(mode)
    _sync_stack_flag(mode)
    return mode


def _write_mode(mode):
    """Write `statusline.agy` into Flightdeck's config.json and nothing else.

    The half of `set_mode` the INSTALL needs on its own: there agy's settings
    are written by `register_status_line`, which puts `stack_with_default` in
    with the rest of the line, so the sync afterwards would have nothing to do
    -- and, run before our command is registered, it would tell the user agy is
    not painting our line while an install is busy putting it there.
    """
    if mode not in MODES:
        raise ValueError("unknown status line mode %r: pick %s"
                         % (mode, ", ".join(MODES)))
    # Only agy's key travels: `config.update` merges one level, so claude's and
    # codex's modes stay exactly as the user left them.
    config.update({"statusline": {"agy": mode}})
    return mode


def _sync_stack_flag(mode):
    """Put `stack_with_default` in step with `mode`. -> was anything written?

    Only touches agy's settings when OUR command is the one registered there: a
    status line of theirs is not ours to add keys to, and a machine where
    Flightdeck is not installed yet has nothing to keep in step.
    """
    try:
        settings = _read_settings()
    except config.ConfigError as exc:
        print("⚠ agy's settings.json left as it was: %s" % exc)
        return False
    if STATUS_LINE_MARKER not in current_status_line(settings):
        print("agy is not painting Flightdeck's line yet — `flightdeck install` "
              "puts it in, and the mode applies then.")
        return False
    # Same writer as the install, so the two can never drift apart. Ours is
    # already registered, so no delegate is touched and nothing is saved.
    updated, _previous = register_status_line(settings, mode)
    if updated.get("statusLine") == settings.get("statusLine"):
        return False
    copy = _write_settings(updated)
    # Not indented under anything: this is printed BEFORE the caller says which
    # mode it settled on, so it has to read on its own.
    print("✓ agy's settings.json: stack_with_default %s"
          % ("on" if mode == "stack" else "off"))
    if copy:
        print("  backup: %s" % copy)
    return True


def status_line_registered():
    """Is Flightdeck's status line the command agy runs right now?

    Raises `ConfigError` when the file cannot be read, rather than answering
    "no": the honest answer is "I cannot tell", and a report that says no where
    it means that is a report that misleads.
    """
    return STATUS_LINE_MARKER in current_status_line(_read_settings())


def restore():
    """Put the `/statusline` command agy had back. -> it, or "".

    The state hook stays registered: it is not the status line, and taking it
    out is what `uninstall` is for. The delegate file is KEPT, the same way
    `uninstall` keeps it -- it is the only copy of that command anywhere, the
    state directory is only cleared by `--purge`, and a later `install` finds it
    and wraps the same line again.

    The mode in `config.json` is deliberately left alone: with our line no
    longer registered there is nothing for it to describe, and a user who
    reinstalls gets back the choice they made rather than a value invented here.
    """
    settings = _read_settings()
    if STATUS_LINE_MARKER not in current_status_line(settings):
        print("Flightdeck's status line is not registered in %s: nothing to "
              "restore." % SETTINGS_PATH)
        return ""
    previous = saved_delegate()
    if not previous:
        print("Nothing to restore: agy had no status line command before Flightdeck.")
        print("  `flightdeck uninstall` takes ours out and agy goes back to "
              "painting its own line.")
        return ""
    settings["statusLine"] = {"type": "command", "command": previous}
    copy = _write_settings(settings)
    print("✓ status line: restored %s" % previous)
    print("  the saved copy is kept in %s" % delegate_file())
    if copy:
        print("  backup: %s" % copy)
    return previous


def default_mode(previous):
    """The mode for someone who has not chosen: `wrap` if they had a line.

    Nobody loses the status line they already had unless they say so.
    With no line of their own there is nothing to keep, so they get ours -- and
    that is the common case, since agy's default line is built in rather than a
    command of theirs.
    """
    return "wrap" if previous else "own"


def _read_settings():
    """agy's settings.json as a dict. Missing = `{}`; unusable = `ConfigError`.

    A file that exists but cannot be parsed is never overwritten: it holds their
    colour scheme and every workspace they have trusted, and a stray comma is
    something they can still fix by hand. A missing file is the normal state of
    a machine where agy has not been started yet.
    """
    try:
        text = SETTINGS_PATH.read_text()
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise config.ConfigError("cannot read %s: %s -- nothing was written"
                                 % (SETTINGS_PATH, exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise config.ConfigError("invalid JSON in %s: %s -- fix the file first; "
                                 "nothing was written" % (SETTINGS_PATH, exc))
    if not isinstance(data, dict):
        raise config.ConfigError("%s must hold a JSON object, found %s -- "
                                 "nothing was written"
                                 % (SETTINGS_PATH, type(data).__name__))
    return data


def _write_settings(settings):
    """Back agy's settings up (if they existed) and write them. -> the backup."""
    copy = backup(SETTINGS_PATH)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    return copy


def _install_status_line(mode):
    """Register the status line and settle the mode. -> what changed.

    A settings.json that cannot be read is reported and skipped rather than
    raised: the hooks have just gone in and are worth keeping, and the person
    can fix their file and run `flightdeck install` again.
    """
    try:
        settings = _read_settings()
    except config.ConfigError as exc:
        print("⚠ status line untouched: %s" % exc)
        return []

    was_ours = STATUS_LINE_MARKER in current_status_line(settings)
    # The line they had, whether it is still in settings.json or was already
    # saved by an earlier install: it is what `default_mode` asks about.
    previous = saved_delegate() if was_ours else current_status_line(settings)
    before = settings.get("statusLine")

    # The mode lives in Flightdeck's own config.json, written apart from agy's
    # settings: `flightdeck update` must not put somebody back on the default
    # because they happen to have a line of their own.
    in_file = mode_in_file()
    chosen = mode if mode is not None else in_file
    if chosen is None:
        chosen = default_mode(previous)
    # And it is written FIRST, the way the claude installer does it. The other
    # way round, a config.json that cannot be parsed (so cannot be updated) left
    # agy's own settings already carrying `stack_with_default` for a mode the
    # config still did not know about -- the two files out of step, silently.
    mode_written = False
    if chosen != in_file:
        try:
            _write_mode(chosen)
        except config.ConfigError as exc:
            print("⚠ status line untouched: %s" % exc)
            return []
        mode_written = True

    settings, _saved = register_status_line(settings, chosen)

    changed = []
    if settings.get("statusLine") != before:
        copy = _write_settings(settings)
        changed.append("status line: agy now paints through %s" % STATUS_LINE)
        print("✓ " + changed[-1])
        if not was_ours and saved_delegate():
            print("  your usual agy status line is saved in: %s" % delegate_file())
        if copy:
            print("  backup: %s" % copy)
    if mode_written:
        changed.append("status line mode: %s — %s" % (chosen, MODE_HELP[chosen]))
        print("✓ " + changed[-1])
    return changed


def _uninstall_status_line():
    """Put back the status line agy had. -> what was removed.

    It comes back in the documented `{"type": "command", ...}` shape even if the
    user had written it as a bare string: that is the form agy itself writes, so
    this is a normalisation and not a loss.
    """
    try:
        settings = _read_settings()
    except config.ConfigError as exc:
        print("⚠ status line left as it was: %s" % exc)
        return []
    if STATUS_LINE_MARKER not in current_status_line(settings):
        return []
    previous = saved_delegate()
    if previous:
        settings["statusLine"] = {"type": "command", "command": previous}
        line = "status line: restored %s" % previous
    else:
        # Nothing was there before us, so nothing goes back: leaving our command
        # would point agy at code that is being removed. agy falls back to its
        # own built-in line, which is what they had.
        settings.pop("statusLine", None)
        line = "status line: removed (agy goes back to its own)"
    copy = _write_settings(settings)
    print("✓ removed %s" % line)
    if copy:
        print("  backup: %s" % copy)
    return [line]


def status():
    """Print what is registered right now: the named hook and the status line."""
    _status_hooks()
    settings = None
    try:
        settings = _read_settings()
    except config.ConfigError as exc:
        print("statusLine: cannot tell — %s" % exc)
    if settings is not None:
        print("statusLine: %s" % (current_status_line(settings) or "—"))
        print("  delegate (the status line agy had before): %s"
              % (saved_delegate() or "—"))
    mode = configured_mode()
    print("  mode (config statusline.agy): %s — %s" % (mode, MODE_HELP[mode]))


def _status_hooks():
    """Print whether our named hook is there, and whether it still points here."""
    try:
        data = json.loads(HOOKS_JSON.read_text())
        ours = data.get(NAME) if isinstance(data, dict) else None
        if not ours:
            print("agy's hooks.json: no Flightdeck in it (%s)" % HOOKS_JSON)
            return
        # "Registered" is not enough: if the code moved (or someone edited the
        # command), the key is still whole and every hook would fail in silence.
        # Installed = the command points at OUR session_hook.py.
        live = [event for event in EVENTS
                if any(is_ours(h) for h in _handlers(ours.get(event)))]
        stale = [event for event in EVENTS
                 if event not in live and ours.get(event)]
        note = ""
        if stale:
            note += " (STALE: points somewhere else: %s)" % ", ".join(stale)
        if not ours.get("enabled", True):
            note += " (DISABLED)"
        print("agy's hooks.json with Flightdeck in: %s%s"
              % (", ".join(live) or "none", note))
    except Exception:
        print("agy's hooks.json: missing or unreadable (%s)" % HOOKS_JSON)


def install(mode=None):
    """Register our named hook and the status line. -> the list of what changed.

    An empty list means everything was already in place and nothing was written.

    `mode` is which line agy paints (`own`, `wrap`, `stack`). Given, it is
    written. Not given, a mode already in the config file is left exactly as it
    is -- `flightdeck update` reinstalls -- and only when there is none does
    `default_mode` decide.
    """
    changed = _install_hooks() + _install_status_line(mode)
    if not changed:
        print("  status line mode: %s — %s"
              % (configured_mode(), MODE_HELP[configured_mode()]))
    return changed


def _install_hooks():
    """Register our named hook. -> the list of what changed.

    Raises `ConfigError`, having written nothing, when the file is there and
    cannot be read. It holds the user's OTHER named hooks beside ours: read as
    empty it would be rebuilt with nothing but our key in it, and theirs would
    live on only in the timestamped backup. A missing file is the normal state
    of a machine where nobody has ever written an agy hook.
    """
    existing, error = read_json_object(HOOKS_JSON)
    if error:
        raise config.ConfigError("%s -- fix the file first; nothing was written"
                                 % error)
    combined = hooks_with_flightdeck(existing)
    if combined == existing:
        print("agy's hooks.json: already installed")
        return []
    backup(HOOKS_JSON)
    HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_JSON.write_text(json.dumps(combined, indent=2) + "\n")
    changed = ["hooks.json: Flightdeck registered (%s) in %s"
               % (", ".join(EVENTS), HOOKS_JSON)]
    print("✓ " + changed[0])
    return changed


def uninstall():
    """Take Flightdeck out of agy. -> the list of what was removed.

    Both files: our key in its hooks.json and, in its settings.json, the status
    line command (which gives them back the one they had).
    """
    removed = _uninstall_hooks() + _uninstall_status_line()
    if not removed:
        print("Nothing to remove: Flightdeck is not in %s." % HOOKS_JSON)
    return removed


def _uninstall_hooks():
    """Remove our key from agy's hooks.json. -> the list of what was removed.

    Only our key goes, stale or not: a key of ours left behind would make agy
    run a hook that no longer exists on every turn. A file left with nothing in
    it was ours to create and is deleted, so nothing stays that looks like a
    configuration the user made.
    """
    try:
        existing = json.loads(HOOKS_JSON.read_text())
    except Exception:
        existing = {}
    if not isinstance(existing, dict) or NAME not in existing:
        return []
    cleaned = dict(existing)
    del cleaned[NAME]
    backup(HOOKS_JSON)
    if cleaned:
        HOOKS_JSON.write_text(json.dumps(cleaned, indent=2) + "\n")
    else:
        HOOKS_JSON.unlink()
    print("✓ removed hooks.json: the %s key" % NAME)
    return [NAME]
