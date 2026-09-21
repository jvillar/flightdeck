"""What Flightdeck writes into each agent's own configuration.

One module per tool: `claude` (hooks + the status line tee), `codex` (the
`notify` tee + its hooks.json) and `agy` (one named hook). All three follow the
same three rules:

- **Wrap, never replace.** Whatever was already there -- a status line, a codex
  `notify`, other people's hooks -- keeps working: it is saved and called
  through, or simply left alone next to ours.
- **Idempotent.** A second run writes nothing, so `flightdeck update` can always
  run all three.
- **Reversible.** Each has an `uninstall()` that takes out only our entries and
  puts back what it replaced.

Two more modules live beside them and write nothing of their own: `migrate`,
which takes the pre-release cockpit Flightdeck was ported from back out of the
three agents before they are installed into, and `__main__`, the `flightdeck
install` command that runs the lot in order and asks the two questions.

The backup lives here instead of three times over because these are one
package. As three standalone scripts with no shared import they had to spell
the mark out three times -- the same kind of split that needed a cross-check
test to keep the three notice durations equal.
"""

import datetime
import json
import shutil

# The mark on every backup Flightdeck leaves beside a file it did not create.
# Spelled once: `uninstall` and `doctor` recognise their own backups by it.
BACKUP_MARK = ".bak-flightdeck-"


def read_json_object(path):
    """One of the agents' JSON files. -> (the dict, an error or None)

    "There is nothing here" and "there is something here I cannot read" are
    OPPOSITE findings, and the readers the installers write with (`_read_json`,
    a bare `except`) answer `{}` to both. That is right for an installer about
    to write the file and wrong for a report: read as empty, a corrupt
    `hooks.json` comes out as "Flightdeck is not registered", and the action
    that follows from it -- a reinstall -- rebuilds the file and drops the
    entries of theirs it could not read.

    So this is the reading `doctor` does, and it is the same rule
    `claude._read_settings` and `config.load_raw` already follow: a missing file
    is `({}, None)`; anything else unusable carries the reason.
    """
    try:
        # UTF-8 and not the locale's encoding, the same way `config.load_raw`
        # reads ours: JSON is UTF-8 by definition, and on a machine whose locale
        # is `C` a perfectly good `hooks.json` with an accent in a path would
        # come back as "cannot read" -- sending its owner to fix a file that has
        # nothing wrong with it.
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:   # ValueError: undecodable bytes
        return {}, "cannot read %s: %s" % (path, exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, "invalid JSON in %s: %s" % (path, exc)
    if not isinstance(data, dict):
        return {}, ("%s must hold a JSON object, found %s"
                    % (path, type(data).__name__))
    return data, None


def backup(path):
    """Copy `path` beside itself with a timestamp. -> the copy, or None.

    None when there was no file to copy: creating a configuration the tool did
    not have yet is not something anyone needs to undo.
    """
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = path.with_name(path.name + BACKUP_MARK + stamp)
    shutil.copy2(str(path), str(destination))
    return destination
