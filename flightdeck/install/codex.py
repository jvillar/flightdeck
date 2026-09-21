"""Installs Flightdeck into codex: the `notify` tee and its hooks.json.

Two pieces, both in files that are NOT Flightdeck's -- hence a timestamped
backup on every write and a `status()`:

1. `~/.codex/config.toml`: `notify` becomes our tee (`codex_notify.py`), and
   whatever notify was there is saved as the `codex-notify` delegate so the tee
   keeps calling it. The same pattern as the Claude Code status line: wrap,
   never replace.
2. `~/.codex/hooks.json`: Flightdeck's events calling
   `session_hook.py <Event> --tool codex`. codex mirrors claude's payload; the
   file's schema was verified live.

The path matters, and it is the one thing that went wrong the first time: the
DOCUMENTED path (learn.chatgpt.com/docs/hooks) is a flat `~/.codex/hooks.json`.
`~/.codex/hooks/hooks.json` is the PLUGINS path, which is why codex validated
and trusted the hooks there but never fired a single one.
"""

import json
import re
from pathlib import Path

from flightdeck import config
from flightdeck.install import backup, read_json_object

CODEX_DIR = Path.home() / ".codex"
# Module attributes so a test can point them somewhere else: none of the three
# is Flightdeck's own file.
CONFIG_TOML = CODEX_DIR / "config.toml"
HOOKS_JSON = CODEX_DIR / "hooks.json"
OLD_PATH = CODEX_DIR / "hooks" / "hooks.json"

TEE = config.code_dir() / "flightdeck" / "hooks" / "codex_notify.py"
HOOK = config.code_dir() / "flightdeck" / "hooks" / "session_hook.py"

# The codex events Flightdeck listens to. No PreToolUse: codex has no
# AskUserQuestion-style form (the `asking` state is claude's), and its permission
# is an event of its own (PermissionRequest), not a Notification.
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse",
          "PermissionRequest", "Stop", "SessionEnd")

TIMEOUT = 5

# The `notify` line in config.toml. TOML's arrays of strings are valid JSON, so
# they are parsed with `json` and no toml dependency (this interpreter has no
# `tomllib`).
#
# The line is matched by its KEY and the rest is read afterwards, never by a
# pattern for the value: a pattern that has to recognise the value decides, by
# failing, that there is no `notify` here -- and the old one then PREPENDED a
# second `notify` key. Two keys is a TOML error and a codex that will not start,
# and it happened with a value over several lines and with a trailing comment
# (`notify = ["/Apps/Sky"]  # mine`), both of them perfectly ordinary files. So:
# the key is found here, `_value_and_comment` reads the value, and anything it
# cannot read is REFUSED rather than written past.
#
# The trailing part is captured too, so a comment of theirs survives being
# wrapped and comes back with the round trip.
_NOTIFY_LINE = r'(?m)^([ \t]*)notify[ \t]*=[ \t]*(.*)$'
_NOTIFY_RE = re.compile(_NOTIFY_LINE)

# codex has no external status line command (measured in 0.153): what it has is
# a list of built-in items. So Flightdeck's line here is the nearest thing --
# codex's own items, in the order of our line: the model, where you are, how
# much context is gone, the two limits, the cost. No glyph and no bars, because
# codex paints this itself and takes nothing but the names.
STATUS_LINE_ITEMS = ["model-with-reasoning", "project-name", "git-branch",
                     "context-used", "five-hour-limit", "weekly-limit",
                     "estimated-thread-cost"]

# `[tui]` and its two keys, read line by line the way `notify` is: a TOML parser
# would have to be written back out, and that would reformat a file that is the
# user's, not ours. Every line we do not touch has to come out byte for byte.
# The spaces inside the brackets are not decoration: `[ tui ]` is valid TOML and
# is the same table. Unrecognised, it earned the file a SECOND `[tui]` table --
# a duplicate table definition, which is the very damage this module reads line
# by line to avoid. The `\r` is there for the same reason and was measured the
# same way: on a config.toml with CRLF line endings (a WSL2 file edited from
# Windows) the header did not match either, and the file got a second table.
# The keys need no such care: they capture to the end of the line and the value
# is read off a stripped copy.
_TUI_HEADER = r'(?m)^[ \t]*\[[ \t]*tui[ \t]*\][ \t\r]*(?:#.*)?$'
_STATUS_LINE = r'(?m)^[ \t]*status_line[ \t]*=[ \t]*(.*)$'
_USE_COLORS = r'(?m)^[ \t]*status_line_use_colors[ \t]*=[ \t]*(.*)$'
# `tui.status_line = [...]` written before any table header is the SAME key as
# ours. Adding a second one inside `[tui]` would be a duplicate key, which codex
# refuses -- taking its whole configuration down for a status line.
_DOTTED = r'(?m)^[ \t]*tui\.status_line[ \t]*='

_ITEMS_LINE = "status_line = %s" % json.dumps(STATUS_LINE_ITEMS)
_COLORS_LINE = "status_line_use_colors = true"


def delegate_file():
    """Where the notify there was before us is kept.

    Resolved on every call: the state directory moves with
    `FLIGHTDECK_STATE_DIR`. It has to be the very file the tee reads, or the
    user's own notify is never called again.
    """
    return config.delegates_dir() / "codex-notify"


def tee_argv():
    """The argv codex is told to run at the end of every turn."""
    return ["python3", str(TEE)]


def _after_line(line, quote, depth):
    """Read one line for what it leaves open. -> (quote, depth).

    `quote` is the triple quote we are inside (or None) and `depth` the number
    of unclosed `[` and `{` of a value. Strings are stepped over rather than
    counted, so a bracket or a `#` inside one is text, not structure.
    """
    index = 0
    while index < len(line):
        if quote is not None:
            if line.startswith(quote, index):
                quote, index = None, index + 3
            else:
                index += 1
            continue
        if line[index] == "#":
            break                       # a comment: the rest of the line is prose
        if line.startswith('"""', index) or line.startswith("'''", index):
            quote, index = line[index:index + 3], index + 3
            continue
        if line[index] in "\"'":
            index = _skip_string(line, index)
            continue
        if line[index] in "[{":
            depth += 1
        elif line[index] in "]}":
            depth = max(0, depth - 1)
        index += 1
    return quote, depth


def _skip_string(line, index):
    """Past the single-line string starting at `index` (unterminated: the line)."""
    delimiter = line[index]
    index += 1
    while index < len(line):
        if delimiter == '"' and line[index] == "\\":
            index += 2                  # only a basic string has escapes
            continue
        if line[index] == delimiter:
            return index + 1
        index += 1
    return index


def _lines(text):
    """`text` cut into lines at `\\n` only, the terminator kept.

    Not `str.splitlines`, which also breaks on U+2028, U+2029, U+0085, `\\v` and
    `\\f`. TOML ends a line at `\\n` (and `\\r\\n`) and nowhere else, so one of
    those inside a string value -- a paragraph separator pasted into somebody's
    prompt -- made the scan below see a line that is not there. What followed
    the phantom break began with `[`, so it read as a table header, the preamble
    ended early and the real `notify` was out of reach: ours got prepended and
    codex was handed a file with two `notify` keys, which is a codex that will
    not start.

    A `\\r` stays at the end of its line, which is what the header pattern
    already expects of a CRLF file.
    """
    parts = text.split("\n")
    for part in parts[:-1]:
        yield part + "\n"
    if parts[-1]:
        yield parts[-1]


def _scan(text):
    """Every line as (offset, line, kind): "header", "top" or "inside".

    "header" is a real table header, "top" a line that begins a top-level
    statement, "inside" a line that is neither -- a continuation of a multi-line
    value or a line of a multi-line string.

    This exists because "the line starts with `[`" is NOT the same question as
    "this is a table header". A `[1, 2]` inside a multi-line array and a
    `[section]` inside a `\"\"\"` string both look like one, and reading either as
    a header put the user's `notify` out of the preamble -- so ours was
    prepended and the file ended up with two `notify` keys, which is a codex
    that will not start. At the top level of valid TOML a value cannot begin a
    line, so once the multi-line constructs are accounted for, a line starting
    with `[` IS a header.
    """
    offset, depth, quote = 0, 0, None
    for line in _lines(text):
        if quote is not None or depth:
            kind = "inside"
        elif line.lstrip(" \t").startswith("["):
            kind = "header"
        else:
            kind = "top"
        yield offset, line, kind
        quote, depth = _after_line(line, quote, depth)
        offset += len(line)


def _preamble(text):
    """Everything before the first table header: the file's top-level keys."""
    for offset, _line, kind in _scan(text):
        if kind == "header":
            return text[:offset]
    return text


def _notify_match(text):
    """The top-level `notify` line, or None.

    Looked for in the PREAMBLE only, line by line. After a table header,
    `notify = ...` is that table's key (`tui.notify`, say) and not codex's own:
    replacing it would break a setting of theirs and leave the tee uninstalled.
    And only on lines that begin a statement, so a `notify = ...` written inside
    somebody's multi-line string is read as the prose it is.
    """
    for offset, line, kind in _scan(text):
        if kind == "header":
            return None
        if kind != "top":
            continue
        # Searched within this line's span, so the offsets are the text's own
        # and every caller can splice with them.
        match = _NOTIFY_RE.search(text, offset, offset + len(line))
        if match is not None:
            return match
    return None


def _value_and_comment(raw):
    """A line's value split from what trails it. -> (list, tail) or (None, None).

    `["/Apps/Sky"]  # mine` comes back as the argv and `"  # mine"`, whitespace
    included, so the comment survives being wrapped and the round trip is byte
    for byte. A `#` INSIDE the value is not a comment: the whole line is tried
    first, and each cut only counts if what is left of it parses.

    (None, None) is the refusal: a value over several lines, a string where an
    array should be, a TOML array written with single quotes. Nothing is written
    past one of those.
    """
    text = raw.rstrip("\n")
    candidates = [(text, "")]
    candidates += [(text[:i], text[i:]) for i, ch in enumerate(text) if ch == "#"]
    for value_text, tail in candidates:
        try:
            value = json.loads(value_text.strip())
        except ValueError:
            continue
        if isinstance(value, list):
            return value, value_text[len(value_text.rstrip()):] + tail
    return None, None


def _tee_in(argv):
    """Where OUR tee sits in that argv: "here", "moved", or "" when it is not ours.

    By the file NAME, the way `is_ours` reads the hook entries -- and for the
    same reason. Read by the whole PATH, our own tee left behind by a move of
    the code directory looked like SOMEBODY ELSE'S notify: `install` saved it as
    the delegate and registered the new tee in front of it, so the tee called
    the tee and the user's real notify sank one level deeper on every move.
    """
    for element in argv or []:
        text = str(element)
        if text == str(TEE):
            return "here"
        if TEE.name in text:
            return "moved"
    return ""


def notify_state(text):
    """Whose top-level `notify` this is.

    "ours" (the tee running now), "stale" (a tee of ours at a path that is no
    longer here), "custom" (theirs), "unreadable" or "none".
    """
    match = _notify_match(text)
    if match is None:
        return "none"
    argv, _tail = _value_and_comment(match.group(2))
    if argv is None:
        return "unreadable"
    placement = _tee_in(argv)
    if placement == "here":
        return "ours"
    return "stale" if placement else "custom"


def _unreadable_notify(match):
    """The refusal, as the one line the user is going to read."""
    return config.ConfigError(
        "%s has a `notify` Flightdeck cannot read (%s): it is not one line of "
        "`notify = [...]`. Put it on one line, or take it out, and run this "
        "again -- nothing was written."
        % (CONFIG_TOML, " ".join(match.group(0).split())))


def register_notify(text):
    """config.toml with `notify` pointing at the tee. -> (new_text, delegate).

    `delegate` is the argv of the PREVIOUS notify (the one to preserve), or None
    when there was none or it was already ours.

    Raises `ConfigError`, having written nothing, when a `notify` key is there
    in a form that cannot be read off one line. The alternative is what used to
    happen: not recognising it, adding ours anyway, and leaving the user with
    two `notify` keys -- a TOML error, a codex that will not start, and their
    own command neither saved nor mentioned.
    """
    match = _notify_match(text)
    new_line = "notify = %s" % json.dumps(tee_argv())
    if match is None:
        return (new_line + "\n" + text, None)
    argv, tail = _value_and_comment(match.group(2))
    if argv is None:
        raise _unreadable_notify(match)
    placement = _tee_in(argv)
    if placement == "here":
        return (text, None)   # already installed: touch nothing, overwrite no delegate
    # A tee of OURS at a path that has gone: the line is brought up to date and
    # the delegate is left exactly as it is -- what it holds is the user's own
    # notify, saved the first time round, and our old tee is not a notify to
    # save on top of it.
    previous = None if placement else (argv or None)
    return (text[:match.start()] + match.group(1) + new_line + tail
            + text[match.end():], previous)


def restore_notify(text, previous=None):
    """config.toml with our `notify` put back to `previous`. -> (new_text, was_ours).

    `was_ours` is False -- and the text untouched -- when the line is missing,
    belongs to someone else, or cannot be read (an uninstall never refuses:
    there is simply nothing of ours in there to take out). With no `previous`
    the line is removed along with its newline, because no notify is what was
    there before us; with one, it is replaced IN PLACE, indentation and trailing
    comment kept, so a file we only ever edited comes back byte for byte.
    """
    match = _notify_match(text)
    if match is None:
        return text, False
    argv, tail = _value_and_comment(match.group(2))
    if not _tee_in(argv):
        return text, False
    if previous:
        return (text[:match.start()] + match.group(1)
                + "notify = %s" % json.dumps(previous) + tail
                + text[match.end():], True)
    end = match.end()
    if text[end:end + 1] == "\n":
        end += 1
    return text[:match.start()] + text[end:], True


def _tui_block(text):
    """Where the `[tui]` table is. -> (header start, body start, body end) or None.

    The body runs from the line after the header to the next table header, so a
    `[tui.colors]` below it is NOT part of it -- keys there belong to that
    sub-table and have nothing to do with the status line.

    Both ends come from `_scan`, never from "the next line starting with `[`":
    a `[tui]` written inside somebody's multi-line string is prose and not a
    table to edit inside, and a `[1, 2]` inside a multi-line value would
    otherwise end the block early and put our keys in the wrong table.
    """
    found = None
    for offset, line, kind in _scan(text):
        if kind != "header":
            continue
        if found is not None:
            return found[0], found[1], offset      # the next table ends ours
        if re.match(_TUI_HEADER, line):
            found = (offset, offset + len(line))
    return None if found is None else (found[0], found[1], len(text))


def _array_on_the_line(raw):
    """The array that value is, or None when it is not one, whole, on one line.

    None is the conservative answer and it is what protects the user: an array
    that runs over several lines, or one written with TOML's single quotes,
    reads as "somebody's own" and is left untouched rather than half-rewritten.
    """
    for candidate in (raw.strip(), raw.split("#")[0].strip()):
        if not candidate.startswith("["):
            return None
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, list):
            return value
    return None


def status_line_state(text):
    """Whose status line items these are: "ours", "custom" or "none"."""
    if re.search(_DOTTED, _preamble(text)):
        return "custom"
    block = _tui_block(text)
    if block is None:
        return "none"
    _, start, end = block
    line = re.search(_STATUS_LINE, text[start:end])
    if line is None:
        return "none"
    return "ours" if _array_on_the_line(line.group(1)) == STATUS_LINE_ITEMS else "custom"


def _drop_line(text, match):
    """That whole line gone, its newline with it."""
    end = match.end() + (1 if text[match.end():match.end() + 1] == "\n" else 0)
    return text[:match.start()] + text[end:]


def register_status_line(toml_text, force=False):
    """config.toml with Flightdeck's status line items in. -> (new_text, changed).

    `changed` is False when there was nothing to do: our items are already
    there, or the user has items of their own (which are theirs, and only
    `force` replaces them). Everything else in the file comes out byte for byte.

    Two things are deliberately never touched. A `status_line_use_colors` that
    is already there is the user's choice, `false` included, and a second copy
    of the key would be a TOML error rather than a preference. And a
    `status_line` that cannot be read off one line -- an array spanning lines, a
    dotted `tui.status_line` -- is left alone even with `force`: where it ends
    cannot be told from one line, and a half-replaced value would leave codex
    with a configuration it refuses to start on.
    """
    if re.search(_DOTTED, _preamble(toml_text)):
        return toml_text, False

    block = _tui_block(toml_text)
    if block is None:
        return toml_text + _new_tui_table(toml_text), True

    _, start, end = block
    if start and toml_text[start - 1] != "\n":
        # `[tui]` as the very last line with no newline after it. Writing our
        # keys from `start` would glue the first one to the header
        # (`[tui]status_line = [...]`), which codex refuses to parse -- and the
        # damage would not heal itself, because the header would no longer be
        # recognised on the way out. Measured, not deduced.
        toml_text += "\n"
        _, start, end = _tui_block(toml_text)
    body = toml_text[start:end]
    line = re.search(_STATUS_LINE, body)
    changed = False
    if line is None:
        body = _ITEMS_LINE + "\n" + body
        changed = True
    else:
        current = _array_on_the_line(line.group(1))
        if current != STATUS_LINE_ITEMS:
            if not force or current is None:
                return toml_text, False
            body = body[:line.start()] + _ITEMS_LINE + body[line.end():]
            changed = True

    if re.search(_USE_COLORS, body) is None:
        # Right after the items, where it is read as belonging to them.
        ours = re.search(_STATUS_LINE, body)
        body = body[:ours.end()] + "\n" + _COLORS_LINE + body[ours.end():]
        changed = True

    return (toml_text[:start] + body + toml_text[end:]) if changed else toml_text, changed


def _new_tui_table(text):
    """The `[tui]` table to append to a config.toml that has none.

    One blank line above it when there is something above, so the file still
    reads as a file. `restore_status_line` takes that line back out, which is
    what makes a round trip byte-equal.
    """
    prefix = "" if not text else ("" if text.endswith("\n") else "\n") + "\n"
    return prefix + "[tui]\n" + _ITEMS_LINE + "\n" + _COLORS_LINE + "\n"


def restore_status_line(toml_text):
    """config.toml with OUR status line items taken out. -> (new_text, removed).

    Only ours: an item list the user changed is theirs and stays, and so does a
    `status_line_use_colors` they set to anything but the `true` we write.

    A `[tui]` left holding nothing goes only when it carries the signature of
    one WE appended: last in the file, and either the whole file or preceded by
    the blank line we write. A `[tui]` the user wrote and left empty is theirs
    and stays empty -- taking it away would be the round trip losing something,
    which is the one thing this module promises not to do.
    """
    block = _tui_block(toml_text)
    if block is None:
        return toml_text, False
    header, start, end = block
    body = toml_text[start:end]
    line = re.search(_STATUS_LINE, body)
    if line is None or _array_on_the_line(line.group(1)) != STATUS_LINE_ITEMS:
        return toml_text, False

    body = _drop_line(body, line)
    colors = re.search(_USE_COLORS, body)
    if colors is not None and colors.group(1).split("#")[0].strip() == "true":
        body = _drop_line(body, colors)

    above, below = toml_text[:header], toml_text[end:]
    if body or below or not (above == "" or above.endswith("\n\n")):
        return toml_text[:start] + body + toml_text[end:], True
    # Nothing left in it, and it is where we would have appended it: ours.
    return (above[:-1] if above.endswith("\n\n") else above) + below, True


def hook_entry(event):
    """One hooks.json entry for `event`, ours by the path of the hook it calls."""
    # `command` is a shell STRING (official docs) and the field is `timeout`.
    return {"hooks": [{"type": "command",
                       "command": "python3 '%s' %s --tool codex" % (HOOK, event),
                       "timeout": TIMEOUT}]}


def entry_commands(entry):
    """Every command in that entry, as a string (an argv list is joined).

    `[]` for anything that is not shaped like an entry: this reads a file the
    user can hand-edit, and a `hooks` holding a number must not take the
    installer down.
    """
    try:
        commands = []
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            commands.append(command if isinstance(command, str)
                            else " ".join(map(str, command or [])))
        return commands
    except Exception:
        return []


def is_ours(entry):
    """Does this entry call Flightdeck's state hook, wherever that hook lives?

    By the file NAME, the way claude's installer reads its own. It used to be by
    the whole PATH, and that is a trap: once the code directory moves, an entry
    of ours points somewhere else, read by path it looked like SOMEBODY ELSE'S,
    and `install` appended a second one -- codex then ran two hooks per event,
    one of them failing. Recognised by name it is ours, so it is replaced; and
    `doctor` is what says the old one was stale (`points_here`).
    """
    return any(HOOK.name in command for command in entry_commands(entry))


def points_here(entry):
    """Does it call the code running right now, and not a copy left elsewhere?"""
    return any(str(HOOK) in command for command in entry_commands(entry))


def hooks_with_flightdeck(existing):
    """The hooks.json dict with our entries in (other people's untouched; our own
    old ones replaced -- so a change of events never duplicates)."""
    data = existing if isinstance(existing, dict) else {}
    hooks = dict(data.get("hooks") or {})
    for event in EVENTS:
        others = [e for e in (hooks.get(event) or []) if not is_ours(e)]
        hooks[event] = others + [hook_entry(event)]
    out = dict(data)
    out["hooks"] = hooks
    return out


def _without_entries(existing, is_target):
    """The hooks.json dict with the entries `is_target` picks out gone.

    -> (a NEW dict, the events touched). The pruning RULE lives here once and is
    called with two predicates: `hooks_without_flightdeck` takes ours out, and
    `migrate` takes the pre-release cockpit Flightdeck was ported from out of
    the same file. That rule -- an event list left empty, and a `hooks` key
    left empty, were ours to create and go with them, so a round trip leaves
    someone else's file byte-equal -- is a decision, and a decision written
    twice is one that will be changed once.
    """
    data = existing if isinstance(existing, dict) else {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        # A `"hooks"` that is a number, a list or a string: this file is edited
        # by hand, and what cannot be understood is left exactly as it is. It
        # used to reach `dict(...)` and raise `TypeError`, which is none of the
        # three exceptions `flightdeck install` catches -- the whole command
        # went down with a traceback over somebody's typo.
        return data, []
    hooks = dict(hooks)
    removed = []
    for event in list(hooks):
        entries = hooks.get(event) or []
        if not isinstance(entries, list):
            continue
        others = [e for e in entries if not is_target(e)]
        if len(others) == len(entries):
            continue
        removed.append(event)
        if others:
            hooks[event] = others
        else:
            del hooks[event]
    if not removed:
        # Same rule as claude's: an empty `hooks` mapping goes only when this
        # pass is what emptied it. One that arrived empty is the user's.
        return data, []
    out = dict(data)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out, removed


def hooks_without_flightdeck(existing):
    """The hooks.json dict with OUR entries taken out. -> (dict, events_removed)."""
    return _without_entries(existing, is_ours)


def is_only_ours(data):
    """Is this hooks.json nothing but Flightdeck's entries?

    The gate on deleting the old plugins path, and it has to be answered by what
    IS in the file, never by what is missing from it. Asking
    `all(any(is_ours(e) for e in entries) for entries in hooks.values())` is
    vacuously TRUE for a file with no `hooks` key at all: a plugins file holding
    `{"plugins": [{"name": "someone-elses"}]}` -- not ours, not even the same
    kind of file -- was unlinked. So: a non-empty `hooks` mapping, at least one
    entry, EVERY entry ours, and nothing else at the top level, since a key we
    never write means the file is somebody's own.
    """
    if not isinstance(data, dict) or set(data) != {"hooks"}:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        return False
    entries = []
    for value in hooks.values():
        if not isinstance(value, list):
            return False
        entries.extend(value)
    return bool(entries) and all(is_ours(e) for e in entries)


def _read_json(path):
    """That file as a dict, `{}` for anything unreadable or odd."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml():
    """config.toml as text. Missing = `""`; unreadable = `ConfigError`.

    The guard is not theoretical: a `config.toml` that is not UTF-8 (a WSL2 file
    edited from Windows) let a `UnicodeDecodeError` traceback out of `status()`
    and `install()`, where the claude and agy readers turn the same case into
    one line. `ValueError` is what an undecodable byte arrives as.
    """
    try:
        return CONFIG_TOML.read_text()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        raise config.ConfigError("cannot read %s: %s -- nothing was written"
                                 % (CONFIG_TOML, exc))


def installed_state(hooks=None, toml_text=None):
    """What is registered in codex right now, as DATA. Never raises.

    The counterpart of `claude.installed_state`, and there for the same reason:
    `status()` prints for a person, `doctor` needs to decide a severity. Given
    `hooks` and `toml_text` it is pure; left out, it reads codex's two files.

    `stale` is an entry of OURS (by file name) that points at a code directory
    which is no longer here -- the hook runs and dies on every event.

    `error` is a file that is THERE and cannot be read, which is a different
    finding from one that is missing: `install` reads both as `{}` and would
    rebuild the file, dropping entries of theirs. Both files' reasons travel,
    because a person fixing this wants to be told about both at once.
    """
    state = {"error": None, "live": [], "stale": [], "missing": [],
             "notify": "none", "delegate_file": False, "status_line_items": "none"}
    errors = []
    if hooks is None:
        hooks, error = read_json_object(HOOKS_JSON)
        if error:
            errors.append(error)
    if toml_text is None:
        try:
            toml_text = _read_toml()
        except config.ConfigError as exc:
            # A config.toml that is not UTF-8 (a Windows editor on a WSL2 file).
            # Here it has to be a finding and not a refusal, because a doctor
            # that dies has told the user nothing.
            errors.append(str(exc))
            toml_text = ""
    state["error"] = "; ".join(errors) or None

    registered = hooks.get("hooks") if isinstance(hooks, dict) else None
    registered = registered if isinstance(registered, dict) else {}
    for event in EVENTS:
        entries = registered.get(event)
        entries = entries if isinstance(entries, list) else []
        ours = [e for e in entries if isinstance(e, dict) and is_ours(e)]
        if any(points_here(entry) for entry in ours):
            state["live"].append(event)
        elif ours:
            state["stale"].append((event, (entry_commands(ours[0]) or [""])[0]))
        else:
            state["missing"].append(event)

    state["notify"] = notify_state(toml_text)
    state["status_line_items"] = status_line_state(toml_text)
    state["delegate_file"] = delegate_file().exists()
    return state


def status():
    """Print whether the notify tee, the delegate and the hooks are in place."""
    try:
        text = _read_toml()
    except config.ConfigError as exc:
        # Said and carried on: the hooks live in the other file and can still be
        # reported. A status that died here would report nothing at all.
        print("config.toml: %s" % exc)
        text = ""
    state = notify_state(text)
    if state == "stale":
        print("notify → Flightdeck's tee: it names our tee at a path that is "
              "not here any more (the code moved); install brings it up to date")
    elif state == "unreadable":
        # Said out loud because it is the one state where `install` will refuse
        # to do anything, and the user is owed the reason before they run it.
        print("notify → Flightdeck's tee: no — a `notify` is there and Flightdeck "
              "cannot read it (not one line of `notify = [...]`); install will "
              "not touch this file until it is")
    else:
        print("notify → Flightdeck's tee: %s" % ("yes" if state == "ours" else "no"))
    print("saved delegate: %s" % ("yes" if delegate_file().exists() else "no"))
    # "custom" is not a fault: it means the user chose their own items and
    # Flightdeck left them, which is the whole point of saying it out loud.
    print("status line items: %s" % status_line_state(text))
    try:
        data = json.loads(HOOKS_JSON.read_text())
        ours = [event for event, entries in (data.get("hooks") or {}).items()
                if any(is_ours(e) for e in entries)]
        print("hooks.json with Flightdeck in: %s" % (", ".join(sorted(ours)) or "none"))
    except Exception:
        print("hooks.json: missing or unreadable (%s)" % HOOKS_JSON)


def install(force=False):
    """Register the notify tee, the status line items and the hook events.

    -> the list of what changed. `force` replaces a `status_line` the user set
    themselves; without it theirs is left exactly as it is.

    Raises `ConfigError` -- one line, and nothing written anywhere, not even the
    hooks -- when `config.toml` holds a `notify` that cannot be read off one
    line, when `config.toml` cannot be decoded at all, or when `hooks.json` is
    there and cannot be parsed. Half an installation is worse than none here:
    the tee is how codex reports the end of a turn, so a run that quietly
    skipped it would leave the menu saying a codex is working long after it
    stopped. The caller (the `flightdeck install` command) prints the message
    and exits non-zero, which is what it already does for a `settings.json` it
    cannot read.
    """
    changed = []

    # BOTH files are read before EITHER is written. hooks.json is read here, at
    # the top, and not where it is used further down: read as `{}` -- which is
    # what a bare `except` did -- an unparseable one would be rebuilt with
    # nothing but our entries in it, and the user's own hooks would survive only
    # in the backup. Discovering that after config.toml had been written would
    # leave exactly the half-installation this function refuses to make.
    existing, error = read_json_object(HOOKS_JSON)
    if error:
        raise config.ConfigError("%s -- fix the file first; nothing was written"
                                 % error)
    text = _read_toml()
    # Both changes are in config.toml, so they are made on the same text and
    # written once: two writes would mean two backups of one file, and the
    # second backup would be of a file we had just edited.
    with_notify, delegate = register_notify(text)
    new_text, items_changed = register_status_line(with_notify, force=force)
    if new_text != text:
        backup(CONFIG_TOML)
        if delegate:
            path = delegate_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(delegate))
        CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_TOML.write_text(new_text)

    if with_notify != text:
        changed.append("notify → tee (previous one saved: %s)" % (delegate or "none"))
        print("✓ " + changed[-1])
    else:
        print("notify: already installed")

    if items_changed:
        changed.append("status line: codex's own items (%s)"
                       % ", ".join(STATUS_LINE_ITEMS))
        print("✓ " + changed[-1])
    elif status_line_state(new_text) == "ours":
        print("status line items: already ours")
    elif force:
        # Asked for and still not done: the value cannot be read off one line
        # (an array over several lines, or a dotted `tui.status_line`), and half
        # a replacement is a config.toml codex refuses to start on.
        print("status line items: left as they are — your `status_line` is not "
              "written on one line, so --force will not touch it either. Change "
              "it by hand in %s." % CONFIG_TOML)
    else:
        print("status line items: left as they are — you have your own "
              "`status_line` (reinstall with --force to use Flightdeck's)")

    # An old installation in the plugins path is retired (only when the whole
    # file is ours): it is a second, dead copy that codex trusts and never fires.
    if is_only_ours(_read_json(OLD_PATH)):
        backup(OLD_PATH)
        OLD_PATH.unlink()
        changed.append("retired the installation in the old path (hooks/hooks.json)")
        print("✓ " + changed[-1])

    combined = hooks_with_flightdeck(existing)
    if combined != existing:
        backup(HOOKS_JSON)
        HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
        HOOKS_JSON.write_text(json.dumps(combined, indent=2) + "\n")
        changed.append("hooks.json: events registered (%s)" % ", ".join(EVENTS))
        print("✓ " + changed[-1])
    else:
        print("hooks.json: already installed")
    return changed


def uninstall():
    """Take Flightdeck out of codex. -> the list of what was removed.

    The notify goes back to the argv saved in the delegate; with no delegate the
    line is removed altogether, because that is what was there before. Our hook
    entries go and everyone else's stay. A hooks.json left with nothing in it
    was ours to create and is deleted, so nothing is left behind that looks like
    a configuration the user made.
    """
    removed = []

    previous = _saved_notify()
    text = _read_toml()
    without_notify, was_ours = restore_notify(text, previous)
    new_text, items_removed = restore_status_line(without_notify)
    if was_ours:
        if previous:
            removed.append("notify: restored %s" % json.dumps(previous))
        else:
            removed.append("notify: removed (there was none before)")
    if items_removed:
        removed.append("status line: our items taken out of [tui]")
    if new_text != text:
        backup(CONFIG_TOML)
        CONFIG_TOML.write_text(new_text)

    existing = _read_json(HOOKS_JSON)
    cleaned, events = hooks_without_flightdeck(existing)
    if events:
        removed.append("hooks.json: %s" % ", ".join(events))
        backup(HOOKS_JSON)
        if cleaned:
            HOOKS_JSON.write_text(json.dumps(cleaned, indent=2) + "\n")
        else:
            HOOKS_JSON.unlink()

    if not removed:
        print("Nothing to remove: Flightdeck is not in %s." % CODEX_DIR)
        return removed
    for line in removed:
        print("✓ removed %s" % line)
    return removed


def _saved_notify():
    """The notify argv saved in the delegate, or None when there is none.

    Anything unusable in there reads as "there was none": the alternative is
    writing a `notify` line codex would choke on at the end of every turn.
    """
    try:
        data = json.loads(delegate_file().read_text())
    except Exception:
        return None
    return data if isinstance(data, list) and data else None
