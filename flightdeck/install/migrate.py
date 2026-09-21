"""Taking the pre-release cockpit Flightdeck was ported from back out of the agents.

It concerns one machine and one moment: the first `flightdeck install` on a
machine where that older cockpit is still registered in Claude Code, codex and
agy. Its hooks are the SAME events under a different path, so leaving them
there would mean two state hooks per event, two status line tees and two
notify tees, each writing into a different state directory.

Three rules, and they are the whole design:

- **The old checkout is READ and never written to.** It is still working, and
  it keeps working until its owner switches it off by hand, once Flightdeck has
  passed the checklist. Two files inside it are read -- `state/statusline-delegate` and
  `state/codex-notify-delegate`, which happen to be in exactly the formats
  Flightdeck's own `delegates/` uses -- and nothing else, ever.
- **It runs BEFORE each installer, never after.** The status line the user
  really had is behind the old tee; put our tee in first and it would save the
  OLD TEE as its delegate -- a tee calling a tee, with their real line buried
  one level deeper. So: take the old one out, put their command back, and only
  then let `claude.install` wrap it the ordinary way.
- **Where the old checkout lives is read off the commands themselves**
  (`old_root`). Nothing here assumes a path: the cockpit is a git checkout and
  can be anywhere, and the one thing that certainly names it is the command the
  agent was told to run.

Everything is built on the installers' own readers and writers -- they are
siblings in this package -- so a file is never parsed one way here and written
another way there. Anything of theirs that cannot be parsed is refused with one
line and nothing is written, the same rule the installers follow.
"""

import json
import re
from pathlib import Path

from flightdeck import config
from flightdeck.install import agy, backup, claude, codex, read_json_object

# What makes a command the old cockpit's: its checkout directory is called
# `session-manager`, and every hook, tee and notify it registers is a file
# directly inside it.
OLD_MARK = "session-manager/"
OLD_HOOK = "session-hook.py"          # the state hook (claude, codex and agy)
OLD_TEE = "context-tee.py"            # the status line tee (claude)
OLD_NOTIFY_TEE = "codex-notify-tee.py"

# The two files inside that checkout Flightdeck is allowed to read, and what
# each becomes here. Same formats: one command line, and a JSON argv list.
OLD_STATUS_LINE_DELEGATE = "state/statusline-delegate"
OLD_NOTIFY_DELEGATE = "state/codex-notify-delegate"

# The path inside a command, tried QUOTED first. codex's and agy's commands
# quote it (`python3 '/x/session-manager/session-hook.py' Stop --tool codex`)
# and a quoted path may hold spaces, which the unquoted form cannot tell from
# the next word.
_QUOTED = re.compile(r"""['"]([^'"]*""" + OLD_MARK + r"""[^'"]*)['"]""")


def old_root(command):
    """The old checkout's directory, read off a command of its own. "" when none.

    `python3 /home/u/dev/session-manager/session-hook.py Stop` ->
    `/home/u/dev/session-manager`.
    """
    text = str(command or "")
    at = text.find(OLD_MARK)
    if at < 0:
        return ""
    quoted = _QUOTED.search(text)
    if quoted:
        path = quoted.group(1)
        return path[:path.find(OLD_MARK) + len(OLD_MARK) - 1]
    # Unquoted: the path is the word the marker sits in.
    start = at
    while start > 0 and text[start - 1] not in " \t":
        start -= 1
    return text[start:at + len(OLD_MARK) - 1]


def _is_old(command, script=None):
    """Does this command run the old cockpit (optionally, that script of it)?"""
    return OLD_MARK + (script or "") in str(command or "")


def _old_file(root, relative):
    """The text of one of the two files read inside the old checkout, or None.

    None both for "it is not there" and for "it cannot be read": this is a
    courtesy copy of something the user can recreate, and nothing here is worth
    stopping a migration for.
    """
    try:
        return (Path(root) / relative).read_text()
    except (OSError, ValueError):
        return None


def _copy_delegate(text, destination):
    """Put the old delegate's text where Flightdeck's tee looks. -> was it copied?

    Never on top of one of ours: if Flightdeck has a delegate already, it holds
    a command saved from a live settings file, which is at least as fresh as the
    old cockpit's copy of it.
    """
    if text is None or destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    return True


def _replace_delegate(destination, text):
    """Write `text` over a delegate of OURS. -> was it written?

    The opposite rule to `_copy_delegate`, and the one case that earns it: it
    is only ever called for a delegate that holds an OLD TEE, which is not a
    command of the user's at all. See the note above `_report`.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    except OSError as exc:
        print("⚠ delegate left as it is: could not write %s (%s)"
              % (destination, exc))
        return False
    return True


# Why the two `_old_tee_out_of_*` repairs below exist, said once.
#
# They undo what the `--keep-legacy` run makes necessary.
# Installing BESIDE the old cockpit does what installing always does: our tee is
# put in front of the command that is REGISTERED, and on that machine the
# registered command is the old cockpit's tee -- which is therefore saved as our
# delegate. Nothing else takes it back out later: every branch above keys on the
# old tee being the command registered, and by then ours is, with theirs behind
# it. Left there, our tee would call the retired cockpit's tee for ever (still
# writing to its state directory, still firing its own notices) and `uninstall`
# would one day hand THAT back as the user's own.
#
# What goes in its place is the old cockpit's own delegate, one level further
# down: the command the user really had. Missing or unusable means they had
# nothing before either cockpit, which the installers already spell -- an empty
# status line delegate, and no notify delegate at all.
#
# This is the ONLY place Flightdeck overwrites a delegate of its own, and the
# guard is what makes it safe: a delegate holding anything that is not an old tee
# is the user's, saved off a live file, and is never touched here.


def _report(changed):
    """Print one line per change, the way the installers do. -> `changed`."""
    for line in changed:
        print("✓ migrated %s" % line)
    return changed


# ── Claude Code ──────────────────────────────────────────────────────────────

def _is_old_claude_entry(entry):
    """Is this settings.json hook entry one the old cockpit registered?

    One hook to an entry is the shape it writes, so the whole entry goes -- and
    the user's own `Bash` guard, which lives in the same `PreToolUse` list, does
    not.
    """
    return any(_is_old(command, OLD_HOOK)
               for command in claude.hook_commands([entry]))


def without_old_claude_hooks(settings):
    """`settings` with the old cockpit's hook entries gone. -> (settings, events)

    Pure. The pruning is `claude._without_entries`, the same one `uninstall`
    uses, called with a different predicate: what an emptied event list means is
    the installer's decision and is written down in one place.
    """
    return claude._without_entries(settings, _is_old_claude_entry)


def _old_tee_out_of_the_status_line_chain():
    """The old tee out of Flightdeck's status line delegate. -> lines to report.

    Nothing to do on any ordinary machine; see the note above `_report`.
    """
    saved = claude.saved_delegate()
    if not _is_old(saved, OLD_TEE):
        return []
    root = old_root(saved)
    previous = (_old_file(root, OLD_STATUS_LINE_DELEGATE) or "").strip()
    if not _replace_delegate(claude.delegate_file(),
                             previous + "\n" if previous else ""):
        return []
    return ["status line delegate: the old tee in %s is out of the chain "
            "(now: %s)" % (root, previous or "no status line")]


def migrate_claude():
    """Take the old cockpit out of `~/.claude/settings.json`. -> what changed.

    Raises `ConfigError`, having written nothing, when that file cannot be
    parsed -- `claude._read_settings`'s rule, and the right one: it holds the
    user's whole Claude Code configuration.
    """
    settings = claude._read_settings()
    settings, events = without_old_claude_hooks(settings)
    changed = []
    if events:
        changed.append("Claude Code hooks: %s" % ", ".join(events))

    current = claude.current_statusline(settings)
    root = old_root(current) if _is_old(current, OLD_TEE) else ""
    if root:
        previous = _old_file(root, OLD_STATUS_LINE_DELEGATE)
        command = (previous or "").strip()
        # Only a real command is worth copying and worth a line: the old cockpit
        # writes an EMPTY delegate for someone who never had a status line, and
        # "your status line was saved" about nothing is a puzzle, not a report.
        if command and _copy_delegate(previous, claude.delegate_file()):
            changed.append("status line saved in %s" % claude.delegate_file())
        if command:
            claude._with_command(settings, command)
            changed.append("status line: back to %s (was the old tee in %s)"
                           % (command, root))
        else:
            # The old tee with no delegate behind it: there is no command to
            # give back, so the setting goes and the installer starts from a
            # machine with no status line -- which is what this one now is.
            claude._without_command(settings)
            changed.append("status line: the old tee in %s removed (it had "
                           "nothing saved behind it)" % root)

    # settings.json is written for what changed IN IT, and only then: the
    # delegate repair below touches a file of ours and must not cost the user a
    # rewrite (and a backup) of their whole Claude Code configuration.
    copy = claude._write_settings(settings) if changed else None

    changed += _old_tee_out_of_the_status_line_chain()
    if changed:
        _report(changed)
        if copy:
            print("  backup: %s" % copy)
    return changed


# ── codex ────────────────────────────────────────────────────────────────────

def _is_old_codex_entry(entry):
    """Is this hooks.json entry one the old cockpit registered in codex?"""
    return any(_is_old(command) for command in codex.entry_commands(entry))


def without_old_codex_hooks(hooks):
    """codex's hooks.json with the old cockpit's entries gone. -> (dict, events)

    Pure. The pruning is `codex._without_entries`, the same one
    `hooks_without_flightdeck` uses, called with a different predicate: other
    people's entries on the same event stay, an event list left empty goes, and
    a `hooks` key left empty goes with it -- decided once, over there.
    """
    return codex._without_entries(hooks, _is_old_codex_entry)


def _notify_argv(text):
    """The argv inside an old `codex-notify-delegate`, or None.

    The same rule `codex._saved_notify` follows: anything that is not a
    non-empty list reads as "there was no notify", because the alternative is
    writing a `notify` line codex would choke on at the end of every turn.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) and data else None


def old_notify(text):
    """The old cockpit's `notify` line in that config.toml. -> (match, root) or (None, "")

    The line is found with codex's own reader, never with a TOML parser: this
    file is the user's and every line we do not touch has to come out of it byte
    for byte.
    """
    match = codex._notify_match(text)
    if match is None:
        return None, ""
    argv, _tail = codex._value_and_comment(match.group(2))
    for element in argv or []:
        if _is_old(element, OLD_NOTIFY_TEE):
            return match, old_root(element)
    return None, ""


def _old_tee_out_of_the_notify_chain():
    """The old tee out of Flightdeck's notify delegate. -> lines to report.

    Nothing to do on any ordinary machine; see the note above `_report`.
    """
    argv = codex._saved_notify() or []
    old = [element for element in argv if _is_old(element, OLD_NOTIFY_TEE)]
    if not old:
        return []
    root = old_root(old[0])
    previous = _notify_argv(_old_file(root, OLD_NOTIFY_DELEGATE))
    path = codex.delegate_file()
    if previous:
        if not _replace_delegate(path, json.dumps(previous)):
            return []
    else:
        # No notify behind the old tee: ours forwards to nobody, and a delegate
        # that is not there is how the tee already reads that.
        try:
            path.unlink()
        except OSError as exc:
            print("⚠ delegate left as it is: could not remove %s (%s)"
                  % (path, exc))
            return []
    return ["codex notify delegate: the old tee in %s is out of the chain "
            "(now: %s)" % (root, json.dumps(previous) if previous
                           else "no notify")]


def migrate_codex():
    """Take the old cockpit out of codex's two files. -> what changed.

    Raises `ConfigError`, having written nothing, when either file is there and
    cannot be read. Both are read first, for the reason `codex.install` reads
    them first: half a migration would leave codex calling a notify that is gone
    while its hooks still point at the old checkout.
    """
    existing, error = read_json_object(codex.HOOKS_JSON)
    if error:
        raise config.ConfigError("%s -- fix the file first; nothing was written"
                                 % error)
    text = codex._read_toml()
    changed = []

    match, root = old_notify(text)
    if match is not None:
        previous = _old_file(root, OLD_NOTIFY_DELEGATE)
        argv = _notify_argv(previous)
        if argv and _copy_delegate(previous, codex.delegate_file()):
            changed.append("codex notify saved in %s" % codex.delegate_file())
        if argv:
            indent, tail = match.group(1), codex._value_and_comment(match.group(2))[1]
            new_text = (text[:match.start()] + indent
                        + "notify = %s" % json.dumps(argv) + (tail or "")
                        + text[match.end():])
            changed.append("codex notify: back to %s (was the old tee in %s)"
                           % (json.dumps(argv), root))
        else:
            # No saved notify behind the old tee: nothing to give back, so the
            # line goes, which is the state codex was in before that cockpit.
            new_text = codex._drop_line(text, match)
            changed.append("codex notify: the old tee in %s removed (it had "
                           "nothing saved behind it)" % root)
        backup(codex.CONFIG_TOML)
        codex.CONFIG_TOML.write_text(new_text)

    cleaned, events = without_old_codex_hooks(existing)
    if events:
        changed.append("codex hooks: %s" % ", ".join(events))
        backup(codex.HOOKS_JSON)
        if cleaned:
            codex.HOOKS_JSON.write_text(json.dumps(cleaned, indent=2) + "\n")
        else:
            # Nothing of anyone's left in it: the old cockpit created this file,
            # and an empty one would look like a configuration the user made.
            codex.HOOKS_JSON.unlink()

    changed += _old_tee_out_of_the_notify_chain()
    return _report(changed)


# ── agy ──────────────────────────────────────────────────────────────────────

def without_old_agy_hooks(hooks):
    """agy's hooks.json without the NAMED hooks that call the old cockpit.

    -> (dict, the names removed). Pure. It is what a key RUNS that decides, not
    what it is called: the old cockpit's key is called `session-cockpit`, but a
    hook of somebody else's by that name, calling something else, stays.
    """
    data = dict(hooks) if isinstance(hooks, dict) else {}
    removed = []
    for name in list(data):
        value = data.get(name)
        if not isinstance(value, dict):
            continue
        commands = []
        for event, handlers in value.items():
            if event == "enabled":
                continue
            for handler in agy._handlers(handlers):
                if isinstance(handler, dict):
                    commands.append(str(handler.get("command") or ""))
        if any(_is_old(command) for command in commands):
            removed.append(name)
            del data[name]
    return data, removed


def migrate_agy():
    """Take the old cockpit's named hook out of agy's hooks.json. -> what changed.

    agy's status line is not migrated because there is nothing to migrate: the
    old cockpit never wrote one (agy had no 🧠 there at all -- its transcript
    carries no token count, measured).
    """
    existing, error = read_json_object(agy.HOOKS_JSON)
    if error:
        raise config.ConfigError("%s -- fix the file first; nothing was written"
                                 % error)
    cleaned, names = without_old_agy_hooks(existing)
    if not names:
        return []
    backup(agy.HOOKS_JSON)
    if cleaned:
        agy.HOOKS_JSON.write_text(json.dumps(cleaned, indent=2) + "\n")
    else:
        agy.HOOKS_JSON.unlink()
    return _report(["agy hooks: the %s key" % ", ".join(names)])
