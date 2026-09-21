"""Installs Flightdeck into Claude Code: the state hooks and the status line tee.

It writes `~/.claude/settings.json`, a file that is NOT Flightdeck's -- so every
write leaves a timestamped backup and there is a `status()`. The hook entries go
in WITHOUT touching the ones already there (a user's own `PreToolUse` guards
live in the same list), and the tee goes in FRONT of whatever status line was
configured: theirs keeps painting exactly the same, the tee only reads the
context % on the way past.

Re-runnable: with everything in place, nothing is written.
"""

import json
from pathlib import Path

from flightdeck import config
from flightdeck.install import backup

# The file, as a module attribute so a test can point it somewhere else. It is
# the only path here that is not Flightdeck's own.
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
          "Notification", "SessionEnd"]

# `PreToolUse` is kept apart from the six above because it is the ONLY one with
# a matcher. A PreToolUse matcher is the tool's name (verified in the 2.1.233
# binary, in its own table: "PreToolUse | Tool name | Run before tool, can
# block"), and only the two tools that ask a question and then wait are of any
# interest. Without a matcher the hook would fire on EVERY tool of EVERY
# session, which is a lot of noise for nothing.
#
# And mind that "can block": a PreToolUse hook exiting with code 2 BLOCKS the
# tool. That is why the state hook's contract -- whatever happens, exit 0 and
# nothing on stdout -- is also what guarantees Flightdeck can never get in the
# way of someone's work.
PRETOOL_MATCHER = "AskUserQuestion|ExitPlanMode"

HOOK = config.code_dir() / "flightdeck" / "hooks" / "session_hook.py"
TEE = config.code_dir() / "flightdeck" / "hooks" / "context_tee.py"

# An entry is recognised as ours by the file name and not by the whole path, so
# that code moved to another directory is still recognised (and replaced)
# instead of being registered a second time alongside the old one.
HOOK_MARKER = HOOK.name
TEE_MARKER = TEE.name

# Seconds Claude Code gives the hook before giving up on it. The hook's worst
# case is a Stop with several tmux clients attached; in practice tmux answers in
# milliseconds.
TIMEOUT = 5

# WHICH line the tee paints, kept in the config as `statusline.claude`. The tee
# is registered the same way in all three: it always reads the context % and
# always sings the handover notice, so the menu and the tmux bar see no
# difference. What changes is only what reaches the user's eyes.
MODES = ("own", "wrap", "stack")
MODE_HELP = {
    "own": "Flightdeck's line",
    "wrap": "the line you already had (the tee only reads the context %)",
    "stack": "Flightdeck's line, and yours underneath it",
}


def delegate_file():
    """Where the status line there was before us is kept.

    Resolved on every call: the state directory moves with
    `FLIGHTDECK_STATE_DIR`. It has to be the very file the tee reads, or the
    user's own status line silently stops being painted.
    """
    return config.delegates_dir() / "statusline"


def command_for(event):
    """The command line Claude Code runs for one event.

    The path is quoted, the same way the codex and agy installers quote theirs:
    Claude Code hands this to a shell, so one space anywhere in the install path
    would silently break every hook.
    """
    return "python3 '%s' %s" % (HOOK, event)


def has_our_hook(entries):
    """Is Flightdeck's hook in this list of entries?"""
    for entry in entries or []:
        for hook in entry.get("hooks", []):
            if HOOK_MARKER in hook.get("command", ""):
                return True
    return False


def entries_for(hooks, event):
    """That event's entries. An empty list when there are none (or something odd)."""
    entries = hooks.get(event) or []
    return entries if isinstance(entries, list) else []


def entry_for(event, matcher=None):
    """One hook entry, ready to go into the event's list."""
    entry = {"hooks": [{"type": "command", "command": command_for(event),
                        "timeout": TIMEOUT}]}
    if matcher is not None:
        # The matcher goes FIRST in the dict purely for how settings.json reads
        # by eye: "for these tools, this".
        entry = {"matcher": matcher, **entry}
    return entry


def our_entry(entries):
    """Our entry inside that list, or None."""
    return next((e for e in entries if has_our_hook([e])), None)


def points_here(entry):
    """Does this entry of ours call the code running right now?

    `has_our_hook` says "ours" by file NAME, which is what stops a moved code
    directory being registered a second time beside the old one. This is the
    other half of that reading: ours, but naming a directory that is not this
    one, is an entry that runs NOTHING -- every hook fails in silence and the
    menu paints a session that stopped moving hours ago. Same pair codex has
    (`is_ours` / `points_here`), and `doctor` reports the difference.
    """
    for hook in entry.get("hooks", []) or []:
        if str(HOOK) in (hook.get("command") or ""):
            return True
    return False


def _drop_stale(entries):
    """Take the entries that are OURS but name another code directory out of `entries`.

    Edited IN PLACE, because what `register_hooks` holds is the list inside the
    caller's own settings dict; everybody else's entries stay exactly where they
    were, ours included when it points here.
    """
    entries[:] = [e for e in entries if not has_our_hook([e]) or points_here(e)]


def stale_events(settings):
    """The events carrying an entry of ours that points somewhere else.

    Read BEFORE `register_hooks` repairs them, so `install` can say what it did.
    """
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    return [event for event in EVENTS + ["PreToolUse"]
            if any(has_our_hook([e]) and not points_here(e)
                   for e in entries_for(hooks, event))]


def _without_entries(settings, is_target):
    """`settings` with the hook entries `is_target` picks out gone.

    -> (a NEW settings dict, the events touched). The pruning RULE lives here
    once and is called with two predicates: `uninstall` takes ours out, and
    `migrate` takes the pre-release cockpit Flightdeck was ported from out of
    the same file. That rule -- only the matching entries go, an event list
    left empty goes with them (it was ours to create), and so does a `hooks`
    key with nothing left in it -- is a decision, and a decision written twice
    is one that will be changed once.

    The settings are copied rather than edited, so a caller that decides not to
    write is left holding exactly what it was given.
    """
    settings = dict(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, []
    hooks = dict(hooks)
    removed = []
    for event in list(hooks):
        entries = entries_for(hooks, event)
        kept = [entry for entry in entries if not is_target(entry)]
        if len(kept) == len(entries):
            continue
        removed.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not removed:
        # Nothing of ours was here, so nothing of theirs may go: an empty
        # `"hooks": {}` that arrived empty is the user's own and is not a leftover
        # of a list this pass emptied. Without this the migration rewrote that
        # key out of somebody's settings while it was there to remove something
        # else entirely.
        return settings, []
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings, removed


def register_hooks(settings):
    """Put our entries into `settings["hooks"]`. -> the same settings.

    Idempotent and respectful: ours is never duplicated, and other people's
    entries (the `PreToolUse` guards that live in the same list) are never
    touched -- one more entry is added beside them.

    An entry of OURS naming another code directory is REPLACED, not left alone
    (`_drop_stale`): it points at code that is not here, so it runs nothing, and
    `doctor` already tells the user that `flightdeck install` brings it back --
    this is what makes that true. The same repair codex's `hooks_with_flightdeck`
    has always done.

    `PreToolUse` gets one extra treatment: if our entry's matcher differs from
    the current one (or is missing, which is the dangerous case -- it would fire
    on every tool), it is corrected in place rather than left stale or added a
    second time.
    """
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        _drop_stale(entries)
        if not has_our_hook(entries):
            entries.append(entry_for(event))
    entries = hooks.setdefault("PreToolUse", [])
    _drop_stale(entries)
    ours = our_entry(entries)
    if ours is None:
        entries.append(entry_for("PreToolUse", PRETOOL_MATCHER))
    elif ours.get("matcher") != PRETOOL_MATCHER:
        ours["matcher"] = PRETOOL_MATCHER
    return settings


def current_statusline(settings):
    """The status line command configured right now ("" when there is none)."""
    value = settings.get("statusLine")
    # The normal shape is {"type": "command", "command": "..."}, but a bare
    # string has to be respected too: whatever is not read here is lost the
    # moment the tee goes in front.
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    return (value.get("command") or "").strip()


def _with_command(settings, command):
    """`settings` with `statusLine` pointing at `command`, the rest of it kept.

    The setting is an OBJECT and `command` is only one of its keys: the
    documented `padding` lives in there too, set by people who want the left
    margin gone. Replaced wholesale it would survive only in the timestamped
    backup, so what is not ours to decide is copied across. A `statusLine`
    written as a bare string has nothing else in it to keep.

    Same shape agy's installer writes, and for the same reason.
    """
    line = settings.get("statusLine")
    line = dict(line) if isinstance(line, dict) else {}
    line["type"] = "command"
    line["command"] = command
    settings["statusLine"] = line
    return settings


def _without_command(settings):
    """`settings` with the two keys that are ours taken out of `statusLine`.

    The whole key goes only when nothing of theirs is left under it: a
    `statusLine` that still carries a `padding` of theirs is a setting they
    made, and deleting it would be taking out more than Flightdeck put in.
    """
    line = settings.get("statusLine")
    line = dict(line) if isinstance(line, dict) else {}
    line.pop("type", None)
    line.pop("command", None)
    if line:
        settings["statusLine"] = line
    else:
        settings.pop("statusLine", None)
    return settings


def saved_delegate():
    """The command saved in the delegate file ("" when there is none).

    UnicodeDecodeError rides along with OSError on purpose: a binary file in
    there must not take the installer down with a traceback.
    """
    try:
        return delegate_file().read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def register_statusline(settings):
    """Put the tee in front of the current status line. -> (settings, saved).

    `saved` is the command that was there before and is now in the delegate file
    -- "" when there was none, when the tee was already registered (nothing is
    touched then: a delegate overwritten with the tee would make the status line
    call itself for ever), or when the delegate could not be written.

    The order matters: the command of a lifetime is written to the delegate
    FIRST and only if that works is the tee registered. The other way round, a
    failed write would leave the tee in place and the user's status line lost.
    """
    current = current_statusline(settings)
    if TEE_MARKER in current:
        if str(TEE) not in current:
            # A tee of OURS at a path that has gone (a moved code directory).
            # The command is brought up to date and the delegate is left exactly
            # as it is: what it holds is the user's own status line, saved the
            # first time round, and our old tee is not a status line to save on
            # top of it. Same rule as codex's `notify` when it has "moved".
            _with_command(settings, "python3 '%s'" % TEE)
        return settings, ""
    already_saved = saved_delegate()
    path = delegate_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if current:
            path.write_text(current + "\n")
        elif already_saved:
            # settings with no statusLine (it happens when an old backup is
            # restored) but the delegate already holds one: writing an empty
            # file here would lose it for good, so what is there is kept.
            print("⚠ settings carried no statusLine; keeping the one already "
                  "saved: %s" % already_saved)
        else:
            path.write_text("")
    except OSError as exc:
        print("⚠ status line untouched: could not save the delegate (%s)" % exc)
        return settings, ""
    # Quoted for the same reason as the hook command: a space in the install
    # path would leave the user with no status line at all.
    _with_command(settings, "python3 '%s'" % TEE)
    return settings, current


def tee_registered():
    """Is Flightdeck's tee the status line command in settings.json right now?

    Raises `ConfigError` when the file cannot be read, rather than answering
    "no": the honest answer is "I cannot tell", and a report that says no where
    it means that is a report that misleads.
    """
    return TEE_MARKER in current_statusline(_read_settings())


def mode_in_file():
    """The mode the user's `config.json` actually holds, or None.

    None both for "the key is not there" and for "the key says something that
    is not a mode": in either case nobody has made a choice that is worth
    honouring, so the installer is free to write the default.
    """
    raw = config.load_raw()
    section = raw.get("statusline")
    mode = section.get("claude") if isinstance(section, dict) else None
    return mode if mode in MODES else None


def configured_mode():
    """The mode in force right now: the file's, else the default.

    The same reading the tee does on every repaint, so `status` and the bar can
    never disagree about which line is being painted.
    """
    return mode_in_file() or config.DEFAULTS["statusline"]["claude"]


def set_mode(mode):
    """Write `statusline.claude` and nothing else. -> the mode written.

    `ValueError` for a mode that does not exist, raised BEFORE anything is
    written: a typo must not leave the tee reading a key it will fall back on
    anyway, with the user believing they changed something.
    """
    if mode not in MODES:
        raise ValueError("unknown status line mode %r: pick %s"
                         % (mode, ", ".join(MODES)))
    # Only claude's key travels: `config.update` merges one level, so agy's and
    # codex's modes stay exactly as the user left them.
    config.update({"statusline": {"claude": mode}})
    return mode


def default_mode(previous):
    """The mode for someone who has not chosen: `wrap` if they had a line.

    Nobody loses the status line they already had unless they say so.
    With no line of their own there is nothing to keep, so they get ours.
    """
    return "wrap" if previous else "own"


def restore():
    """Put the user's own status line command back into settings.json. -> it, or "".

    The hooks stay registered: they are not the status line, and taking them out
    is what `uninstall` is for. The delegate file is KEPT, the same way
    `uninstall` keeps it -- it is the only copy of that command anywhere, the
    state directory is only cleared by `--purge`, and a later `install` finds it
    and wraps the same line again.

    The mode in `config.json` is deliberately left alone: with the tee no longer
    registered there is nothing for it to describe, and a user who reinstalls
    gets back the choice they made rather than a value invented here.
    """
    settings = _read_settings()
    if TEE_MARKER not in current_statusline(settings):
        print("Flightdeck's status line tee is not registered in %s: nothing to "
              "restore." % SETTINGS_PATH)
        return ""
    previous = saved_delegate()
    if not previous:
        print("Nothing to restore: there was no status line before Flightdeck.")
        print("  `flightdeck uninstall` takes ours out and leaves you with none.")
        return ""
    _with_command(settings, previous)
    copy = _write_settings(settings)
    print("✓ status line: restored %s" % previous)
    print("  the saved copy is kept in %s" % delegate_file())
    print("  (the state hooks stay; `flightdeck install` puts the tee back)")
    if copy:
        print("  backup: %s" % copy)
    return previous


# What is lost when the delegate file goes, per mode. The consequence is
# different in each, and saying `wrap`'s in all three was saying something false
# to the two modes that do not paint the user's own line at all.
LOST_DELEGATE = {
    "wrap": "the line you had cannot be painted: the bar falls back to the "
            "tee's minimal line (Ctx NN%).",
    "stack": "Flightdeck's line is painted alone: there is nothing left to "
             "stack underneath it.",
    "own": "your bar looks the same today, but `flightdeck uninstall` has "
           "nothing to put back.",
}


def warn_lost_delegate(settings):
    """Shout if the tee is registered and the file holding their own line has GONE.

    The state directory is disposable (it can be cleaned out without fear), but
    the user's own status line lives in there, and that file is the only copy of
    it anywhere -- it is what `uninstall` puts back. Without this warning the
    loss would be silent and permanent.

    A file that EXISTS and is empty is not that: it is what the installer writes
    for somebody who had no status line at all, which is most people on their
    first run. Warning about it meant an alarm about nothing on every `install`,
    every `update` and every `status`. The same distinction `installed_state`
    and the doctor make (`delegate_file` against `delegate`).
    """
    if TEE_MARKER not in current_statusline(settings) or delegate_file().exists():
        return
    print("⚠ the tee is registered but the saved status line is gone (%s):"
          % delegate_file())
    print("  " + LOST_DELEGATE[configured_mode()])
    print("  Fix: put your status line command back in there, for example")
    print("    echo 'sh ~/.claude/statusline-command.sh' > %s" % delegate_file())
    print("  (never had one? `: > %s` is the empty file the installer writes.)"
          % delegate_file())


def _read_settings():
    """settings.json as a dict. Missing = `{}`; unusable = `ConfigError`.

    A file that exists but cannot be parsed is never overwritten: it holds the
    user's whole Claude Code configuration (permissions, environment, model) and
    a stray comma is something they can still fix by hand. Same rule as
    `config.update()`. A missing file, on the other hand, is the normal state of
    a machine where Claude Code has not been started yet.
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
    """Back the file up (if it existed) and write it. -> the backup, or None."""
    copy = backup(SETTINGS_PATH)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    return copy


def install(mode=None):
    """Register the hooks and the status line tee. -> the list of what changed.

    An empty list means everything was already in place and nothing was written.

    `mode` is which line the tee paints (`own`, `wrap`, `stack`) and it is the
    ONE thing here that goes into Flightdeck's own `config.json` rather than
    into settings.json. Given, it is written. Not given, a mode already in the
    config file is left exactly as it is -- `flightdeck update` reinstalls, and
    putting somebody back on `wrap` because they happen to have a status line
    would undo their choice behind their back -- and only when there is none
    does `default_mode` decide.
    """
    settings = _read_settings()
    hooks = settings.setdefault("hooks", {})

    # What was already there, read BEFORE registering: `register_hooks` fixes a
    # stale matcher in place, so the old value has to be copied out first.
    already = [ev for ev in EVENTS + ["PreToolUse"]
               if has_our_hook(entries_for(hooks, ev))]
    old_entry = our_entry(entries_for(hooks, "PreToolUse")) or {}
    old_matcher = old_entry.get("matcher")
    # Ours, but naming a code directory that is not this one: `register_hooks`
    # and `register_statusline` replace them, and this is read first so the user
    # is told which ones moved.
    was_stale = stale_events(settings)
    current = current_statusline(settings)
    was_teed = TEE_MARKER in current
    tee_was_stale = was_teed and str(TEE) not in current
    # The line they had, whether it is still in settings.json or already saved
    # by an earlier install. It is what `default_mode` asks about, so it has to
    # be read before `register_statusline` puts the tee in its place.
    previous = saved_delegate() if was_teed else current_statusline(settings)

    register_hooks(settings)
    settings, _saved = register_statusline(settings)

    changed = []
    added = [ev for ev in EVENTS + ["PreToolUse"] if ev not in already]
    if added:
        changed.append("hooks added: %s" % ", ".join(added))
    if was_stale:
        changed.append("stale entries repointed at this code directory: %s"
                       % ", ".join(was_stale))
    if "PreToolUse" in already and old_matcher != PRETOOL_MATCHER:
        changed.append("matcher brought up to date (%s): PreToolUse" % PRETOOL_MATCHER)
    tee_added = not was_teed and TEE_MARKER in current_statusline(settings)
    if tee_added:
        changed.append("status line: now goes through %s" % TEE)
    elif tee_was_stale:
        changed.append("status line: repointed at %s" % TEE)

    # config.json, a file of ours, is written apart from settings.json, a file
    # of theirs: each is only touched when it has something to say. The mode
    # goes first on purpose. `set_mode` refuses a config.json it cannot parse,
    # and a refusal has to leave the user with the status line they had rather
    # than with our tee painting whatever the default happens to be. (One thing
    # has been written by then: `register_statusline` saved their command to the
    # delegate. That is theirs being kept safe, and the retry wants it there.)
    settings_changed = list(changed)
    in_file = mode_in_file()
    chosen = mode if mode is not None else in_file
    if chosen != in_file or chosen is None:
        chosen = set_mode(chosen if chosen is not None else default_mode(previous))
        changed.append("status line mode: %s — %s" % (chosen, MODE_HELP[chosen]))

    if not changed:
        print("Nothing to do: state hooks and status line tee are already installed.")
        print("  status line mode: %s — %s" % (chosen, MODE_HELP[chosen]))
        warn_lost_delegate(settings)
        return changed

    copy = _write_settings(settings) if settings_changed else None
    for line in changed:
        print("✓ " + line)
    if tee_added and saved_delegate():
        print("  your usual status line is saved in: %s" % delegate_file())
        print("  change which line you see with: flightdeck statusline --mode %s"
              % "|".join(MODES))
    if copy:
        print("  backup: %s" % copy)
    if settings_changed:
        # Only the hooks and the registration need a restart: the mode is read
        # from config.json on every repaint, so it takes effect where the user
        # is looking.
        print("  (affects NEW Claude Code sessions; restart the open ones to "
              "register them)")
    warn_lost_delegate(settings)
    return changed


def hook_commands(entries):
    """Every command string in those entries, ours and everyone else's.

    Defensive about the shape because settings.json is hand-edited and the
    doctor is exactly what somebody runs when theirs is a mess: an entry that is
    not shaped like one contributes nothing instead of taking the reader down.
    """
    commands = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict):
                commands.append(str(hook.get("command") or ""))
    return commands


def installed_state(settings=None):
    """What is registered right now, as DATA. Never raises.

    `status()` prints for a person; this answers for `doctor`, which has to
    decide a severity and an exit code. Re-parsing printed text would make the
    two drift apart the first time a line is reworded.

    `settings` given is read as it is (that is the pure way in, and what the
    tests use); left out, the file is read, and one that cannot be parsed comes
    back as `error` rather than as an exception -- "I cannot tell" is a finding,
    not a crash.

    The key distinction is `live` against `stale`. An entry is OURS by the file
    name (`HOOK_MARKER`), and it is LIVE only when it names the path of the code
    running right now. A moved code directory leaves entries that are still ours
    and point at nothing: every hook fails in silence, so the menu and the bar go
    on painting a session that stopped moving hours ago.
    """
    empty = {"error": None, "live": [], "stale": [], "missing": [],
             "matcher": None, "status_line": "none", "status_line_command": "",
             "delegate_file": False, "delegate": "", "mode": configured_mode()}
    if settings is None:
        try:
            settings = _read_settings()
        except config.ConfigError as exc:
            return dict(empty, error=str(exc))

    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    state = dict(empty)
    for event in EVENTS + ["PreToolUse"]:
        ours = [c for c in hook_commands(entries_for(hooks, event)) if HOOK_MARKER in c]
        if any(str(HOOK) in command for command in ours):
            state["live"].append(event)
        elif ours:
            state["stale"].append((event, ours[0]))
        else:
            state["missing"].append(event)

    # Our PreToolUse entry found the same way as above rather than through
    # `our_entry`, which would reach into an entry of any shape at all.
    ours = next((e for e in entries_for(hooks, "PreToolUse")
                 if any(HOOK_MARKER in c for c in hook_commands([e]))), {})
    state["matcher"] = ours.get("matcher") if isinstance(ours, dict) else None

    current = current_statusline(settings)
    state["status_line_command"] = current
    if TEE_MARKER in current:
        state["status_line"] = "ours" if str(TEE) in current else "stale"
    elif current:
        state["status_line"] = "other"
    # The file EXISTING and what it holds are two different facts: an empty file
    # is what the installer writes for someone who had no status line at all,
    # while a file that has gone is the loss of the only copy there was.
    state["delegate_file"] = delegate_file().exists()
    state["delegate"] = saved_delegate()
    return state


def status():
    """Print what is registered right now, our entries and everyone else's."""
    settings = _read_settings()
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    for event in EVENTS + ["PreToolUse"]:
        # With its matcher in front when it has one: in `PreToolUse` ours lives
        # beside the user's own guards, and without seeing it there is no
        # telling which is which, nor whether ours still watches the tools that
        # ask.
        commands = [("[%s] " % entry["matcher"] if entry.get("matcher") else "")
                    + hook.get("command", "")
                    for entry in entries_for(hooks, event)
                    for hook in entry.get("hooks", [])]
        print("%s: %s" % (event, commands if commands else "—"))
    print("statusLine: %s" % (current_statusline(settings) or "—"))
    print("  delegate (the status line there was before): %s" % (saved_delegate() or "—"))
    mode = configured_mode()
    print("  mode (config statusline.claude): %s — %s" % (mode, MODE_HELP[mode]))
    warn_lost_delegate(settings)


def uninstall():
    """Take Flightdeck out of settings.json. -> the list of what was removed.

    Only our entries go: other hooks on the same events stay exactly where they
    are, and the status line saved in the delegate is put back (as the
    documented `{"type": "command", ...}` shape -- a bare string comes back as a
    command entry). An event list left empty, and a `hooks` key left empty, were
    ours to create and are removed with them, so a round trip leaves a file that
    had no hooks byte for byte as it was.

    The delegate file itself is left in place: the state directory is only
    cleared by `--purge`, and a reinstall just saves the live command again.
    """
    settings, events = _without_entries(_read_settings(),
                                        lambda entry: has_our_hook([entry]))
    removed = ["hook: %s" % event for event in events]

    if TEE_MARKER in current_statusline(settings):
        previous = saved_delegate()
        if previous:
            _with_command(settings, previous)
            removed.append("status line: restored %s" % previous)
        else:
            # Nothing was there before us, so nothing goes back: leaving the
            # tee's command would point Claude Code at code being removed.
            _without_command(settings)
            removed.append("status line: removed (there was none before)")

    if not removed:
        print("Nothing to remove: Flightdeck is not in %s." % SETTINGS_PATH)
        return removed

    copy = _write_settings(settings)
    for line in removed:
        print("✓ removed %s" % line)
    if copy:
        print("  backup: %s" % copy)
    return removed
