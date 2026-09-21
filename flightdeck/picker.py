"""The menu: a searchable list of AI coding sessions (live + recent) over fzf.

It joins the LIVE tmux sessions (via `flightdeck.common`) with the RECENT
session history (via `flightdeck.history`), formats it for fzf, and turns the
selection into a tmux action: jump to a live one, or bring a closed one back
with its tool's resume command.

The pure logic (build the list, format it, resolve the action) is kept apart
from the effectful wrapper (calling fzf and tmux) so it can be tested.
"""
import os
import re
import subprocess
import sys
import time

from flightdeck import config
from flightdeck.common import (SAFE_ID, dedup_name, human_age, list_tmux_sessions,
                               load_outside_sessions, load_sessions, pin_session,
                               tmux_bin, tmux_safe_name)
from flightdeck.history import list_recent_sessions, title_for_session
from flightdeck.statusline import NO_BRANCH, git_branch, pretty_dir
from flightdeck.tools import glyph, mark_of, resume_argv, supports_fork

# How many recent sessions we load at most (the newest ones). It keeps start-up
# from hanging on thousands of transcripts. This is the fallback; the value in
# force comes from the config (`history_limit`), read when the list is built.
HISTORY_LIMIT = config.DEFAULTS["history_limit"]

# tmux sessions that are Flightdeck's own infrastructure, not work: the menu
# itself. A pin's session is infrastructure too, but which ones those are is
# only known from the config (see `is_reserved`).
RESERVED = ("flightdeck",)


def is_reserved(name, pins=()):
    """Flightdeck's infrastructure, not the user's work.

    `flightdeck` itself, the PER-WINDOW menus (`flightdeck-2`, `flightdeck-3`...)
    the bash command creates when the main one already has a client, and the
    tmux session of every pin: none of them is work and none belongs in the
    green group. The pins arrive as an argument rather than being read here so
    this stays pure -- the menu passes the configured ones.
    """
    if name in RESERVED or _EXTRA_MENU.match(name or ""):
        return True
    return name in _pin_sessions(pins)


_EXTRA_MENU = re.compile(r"^flightdeck-\d+$")

# The states in which a session is asking for its turn (as opposed to
# "working"). "asking" is one of them: with a form open, the tool does not carry
# on by itself.
_WAITING_STATES = ("awaiting_input", "needs_attention", "asking")

# From what % of spent context the 🧠 is shown on a green row. Below it adds
# nothing: a session just opened at 12% has nothing to say, and filling the list
# with numbers makes it unreadable. Fallback for `context_show_pct`.
CTX_SHOW = config.DEFAULTS["context_show_pct"]


def _ctx_show():
    """The 🧠 threshold in force, from the config, read as the list is built.

    Like the status bar's, it falls back to the default instead of raising: a
    `context_show_pct` typed as a word must not take the whole menu down, and
    `doctor` is the place that complains about a value of the wrong type.
    """
    try:
        return int(config.load()["context_show_pct"])
    except (KeyError, TypeError, ValueError):
        return CTX_SHOW


def _history_limit():
    """How many history rows to ask for, from the config. Same fallback rule."""
    try:
        return int(config.load()["history_limit"])
    except (KeyError, TypeError, ValueError):
        return HISTORY_LIMIT


def configured_pins():
    """The pinned rows in force. A config without pins (the default) has none.

    Public because the status bar needs the same answer: it builds its entries
    with `build_entries` too, and without the pins a pinned session would be
    counted there as an ordinary work row.
    """
    pins = config.load().get("pins")
    return pins if isinstance(pins, list) else []


# The session a pin lives in. `common.pin_session` is the one reader of that
# key: `flightdeck pin add` writes it sanitised and the menu has to read it back
# the same way, or the two disagree about which tmux session a row means.
_pin_session = pin_session


def _pin_sessions(pins):
    """The tmux session names of the usable pins."""
    return {s for s in (_pin_session(p) for p in pins or ()) if s}


def _pin_entry(pin):
    """One row out of one pin, or None when the pin cannot be used."""
    session = _pin_session(pin)
    if session is None:
        return None
    label = pin.get("label") or pin.get("name") or session
    return {"kind": "pin", "name": pin.get("name") or session,
            # Everything the row paints or types is forced to text: the values
            # come from a hand-edited file, and a number as a label would blow
            # up the line formatting.
            "label": str(label), "target": session,
            "command": str(pin.get("command") or ""),
            "session_id": None, "project": None, "state": None, "cwd": None,
            "age": ""}


def build_entries(tmux_sessions, live, outside, recent, now=None, pins=()):
    """green (tmux, by activity desc) + pins + dimmed (outside) + grey (history).

    The menu's unit is the TMUX SESSION (green), annotated with the tool running
    inside it (if any: project and state come from its hook card). Behind them
    go the pinned rows, the sessions alive OUTSIDE tmux (informative only) and
    the history of closed ones (which can be brought back).
    """
    if now is None:
        now = time.time()
    # One tmux session can hold several sessions inside (several panes) and the
    # row annotates only one: we sort so the ones WAITING for their turn end up
    # last, which is what stays in the dict. A session waiting for you covered
    # by a "working" is exactly what the menu exists to avoid.
    ordered = sorted(live, key=lambda d: d.get("state") in _WAITING_STATES)
    by_tmux = {d.get("tmux_session"): d for d in ordered if d.get("tmux_session")}
    green = []
    for s in sorted(tmux_sessions, key=lambda s: -s["activity"]):
        if is_reserved(s["name"], pins):
            continue
        d = by_tmux.get(s["name"], {})
        green.append({"kind": "tmux", "name": s["name"], "target": s["id"],
                      "session_id": d.get("session_id"), "project": d.get("project"),
                      "title": d.get("_title"),
                      "state": d.get("state"),
                      # The hook's last event (and WHY it started, if it was a
                      # SessionStart): with both, a freshly started "working" is
                      # painted "open" instead of "working".
                      "last_event": d.get("last_event"),
                      "source": d.get("source"), "cwd": d.get("cwd"),
                      "ctx_pct": d.get("_ctx_pct"),
                      # A conversation sent to the background (`/background` or
                      # the ← arrow): the state above is its job's, and the row
                      # says so (see flightdeck.registry).
                      "parked": d.get("_parked"),
                      "tool": d.get("tool"),
                      "age": human_age(d.get("_age"))})
    pinned = [row for row in (_pin_entry(p) for p in pins or ()) if row]
    out_rows = [{"kind": "outside", "name": d.get("_name_cc"), "target": None,
                 "session_id": d.get("session_id"), "project": d.get("project") or "?",
                 "state": d.get("state"), "cwd": d.get("cwd"), "tool": d.get("tool"),
                 "age": human_age(d.get("_age"))}
                for d in outside]
    grey = [{"kind": "recent", "name": None, "target": None,
             "session_id": m.get("session_id"), "project": m.get("project") or "?",
             "title": m.get("title"), "state": None, "cwd": m.get("cwd"),
             "age": human_age(now - m.get("last_activity", now)),
             "last_activity": m.get("last_activity", now),
             "tool": m.get("tool")}
            for m in recent]
    grey.sort(key=lambda e: -e["last_activity"])
    return green + pinned + out_rows + grey


# The ANSI colours of each group (fzf paints them with --ansi): green = tmux
# session, grey = history, dim = living outside tmux.
_G, _GREY, _DIM, _R = "\033[32m", "\033[90m", "\033[2m", "\033[0m"
# "looping" = it finished a `/loop` round and is waiting for its TIMER, not for
# you (the hook decides it by reading the transcript, see `flightdeck.turn`).
# Neutral on purpose: it is not in `_WAITING_STATES` and the bar does not count it.
_BADGE = {"awaiting_input": "⏳ WAITING FOR YOU", "needs_attention": "⚠ needs attention",
          "asking": "❓ asking you", "working": "● working",
          "looping": "🔁 looping"}


def state_badge(state, last_event=None, source=None):
    """How the row announces the session inside it (or "shell" if there is none).

    A "working" whose LAST event is still the one that started it is not
    working: it is OPEN and still. Starting -- or resuming -- leaves the state at
    "working" because the only event that brings it down to "awaiting_input" is
    the `Stop` at the end of the turn, so "● working" there was a lie. It is
    painted neutral on purpose: it is not a wait, and the bar (which counts
    `state`, not badges) still does not count it.

    With the same exception as the handover, and for that reason: it is the SAME
    signal the handover uses to decide whether it can type the `/exit`
    (`flightdeck.handover.reason_not_to_close`), so the two have to tell the same
    story. The `SessionStart` of a COMPACTION (`source == "compact"`) happens in
    the middle of a turn, so there it IS working.

    The circle is HOLLOW (as opposed to working's `●`) for the same reason it
    carries no bullet: the row already separates label and badge with " · ", and
    a "· open" came out with the dot duplicated ("wolf 4 · · open").
    """
    if (state == "working" and last_event == "SessionStart"
            and source != "compact"):
        return "○ open"
    # No tool inside (or one in a state we do not know): "shell".
    return _BADGE.get(state, "shell")


# Width of the label on ◇ rows: with ~80 columns (a phone) the age and
# "outside tmux (⏳ WAITING FOR YOU)" have to fit as well. fzf cuts long lines at
# the END (with its "··" ellipsis), so without this trim what would be lost on
# narrow screens is the STATE, not the name. Chosen out of three mock-ups: our
# own trim with an ellipsis.
NAME_WIDTH_OUTSIDE = 40


def truncate(text, width):
    """`text` in `width` characters at most, with an ellipsis if it does not fit."""
    text = text or ""
    return text if len(text) <= width else text[:width - 1] + "…"


def glyph_cell(tool, row_colour):
    """The coloured glyph + back to the row's colour, or an aligned blank.

    `tool` None = a row with no tool inside (a shell): two spaces, so the name
    column does not dance between rows. The colour is emitted BEFORE the glyph
    and the row's own is restored behind it (see `flightdeck.tools.GLYPHS` for
    the character and the number of each brand).
    """
    if tool is None:
        return "  "
    mark = glyph(tool)
    char, colour = (mark[0], "\x1b[38;5;%dm" % mark[1]) if mark else ("·", "")
    return "%s%s%s " % (colour, char, row_colour)


def visible_columns(entry):
    """The text that is seen (and searched for) in fzf for one row."""
    k = entry["kind"]
    if k == "tmux":
        inside = state_badge(entry.get("state"), entry.get("last_event"),
                             entry.get("source"))
        # The conversation's title (/rename) says more than the folder's name;
        # the project is the fallback when there is no title.
        label = entry.get("title") or entry.get("project")
        # The tool is seen in the annotation; claude (the common case) carries
        # no mark. "retail fix · codex · ⏳ WAITING FOR YOU".
        mark = mark_of(entry.get("tool"))
        if mark:
            inside = "%s · %s" % (mark, inside)
        note = ("%s · %s" % (label, inside)) if label else inside
        # How much brain is left: only when it is already loaded (see CTX_SHOW).
        pct = entry.get("ctx_pct")
        if isinstance(pct, int) and pct >= _ctx_show():
            note += " · 🧠%d%%" % pct
        if entry.get("parked"):
            note += " · background"
        # With a tool inside, its glyph; a bare shell, a blank.
        tool = (entry.get("tool") or "claude") if entry.get("session_id") else None
        return "%s%s%-16s %5s  %s%s" % (_G, glyph_cell(tool, _G),
                                        entry["name"], entry.get("age") or "", note, _R)
    if k == "pin":
        return entry.get("label") or entry.get("name") or "?"
    if k == "outside":
        label = truncate(entry.get("name") or entry.get("project") or "?",
                         NAME_WIDTH_OUTSIDE)
        mark = mark_of(entry.get("tool"))
        return "%s◇ %s%-16s %5s  %soutside tmux (%s)%s" % (
            _DIM, glyph_cell(entry.get("tool") or "claude", "\x1b[39m" + _DIM),
            label, entry.get("age") or "",
            ("%s · " % mark) if mark else "",
            _BADGE.get(entry.get("state"), "?"), _R)
    title = entry.get("title") or "—"
    mark = mark_of(entry.get("tool"))
    if mark:
        title = "%s · %s" % (title, mark)
    return "%s▹ %s%-16s %6s  %s%s" % (_GREY, glyph_cell(entry.get("tool") or "claude", _GREY),
                                      entry["project"], entry.get("age") or "", title, _R)


def _one_line(text):
    """Collapse tab/CR/newline to spaces: one row = one physical line."""
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


# --- the detail strip -------------------------------------------------------
# Two lines under the list that follow the cursor and say what the row has no
# room for: above all the FOLDER, which is what tells apart two sessions of the
# same project.
#
#     📁 ~/work/pricing · main
#     ✳ claude · ssr cache 2 · open · 42h · id ef1864c2
#
# We do not paint them. They ride in two HIDDEN fields of each line and fzf
# shows the current row's pair as its preview (see `_fzf_args`), so moving the
# cursor costs one `printf` and never a python process rebuilding a 150-row
# list. The folder marker is one glyph so the eye finds line 1 even when a long
# path wraps on a phone.
_FOLDER = "📁"


def _state_words(badge):
    """A badge said in plain words: `⏳ WAITING FOR YOU` -> `waiting for you`.

    Derived from the badge rather than kept in a second table on purpose: the
    strip has to tell exactly the story the row above it tells, and two tables
    drift apart the day a state is added. The first token is dropped only when
    it is a glyph (it does not start with a letter or a digit), so a badge that
    is only words (`shell`) survives whole.
    """
    text = str(badge or "")
    head, _, rest = text.partition(" ")
    if rest and not head[:1].isalnum():
        text = rest
    return text.lower()


def _detail_folder(entry):
    """Line 1: the folder, and the branch when there is a repo.

    The branch is read from `.git/HEAD` without running git
    (`flightdeck.statusline.git_branch`, the same reader the status line uses),
    so a row costs a couple of `stat`s and one small read. `no branch` is left
    OUT rather than printed: most folders are not checkouts and the words would
    be noise on every one of them.
    """
    cwd = entry.get("cwd")
    if not cwd and entry.get("kind") == "tmux" and not entry.get("session_id"):
        # A green row with no agent inside: tmux's session list does not hand
        # out the pane's path, and the projects directory is where Ctrl-N
        # opened it. A row that HAS an agent and still no cwd says `no folder`
        # instead -- there the projects directory would be a guess about a real
        # piece of work.
        cwd = str(config.projects_dir())
    if not cwd:
        return "%s no folder" % _FOLDER
    branch = git_branch(cwd)
    if branch and branch != NO_BRANCH:
        return "%s %s · %s" % (_FOLDER, pretty_dir(cwd), branch)
    return "%s %s" % (_FOLDER, pretty_dir(cwd))


def _detail_agent(entry):
    """Line 2 for a row with an agent: tool, title, state, age and short id.

    Every part is left out when the entry does not carry it, which is what
    keeps a closed conversation from claiming a state it is not in: a grey row
    has no `state`, and the badge's fallback for that is `shell`.
    """
    tool = entry.get("tool") or "claude"
    mark = glyph(tool)
    # An unknown tool is named but borrows nobody's glyph (same rule as the row).
    parts = ["%s %s" % (mark[0], tool) if mark else tool]
    title = entry.get("title") or entry.get("project")
    if title:
        parts.append(str(title))
    if entry.get("state"):
        parts.append(_state_words(state_badge(entry.get("state"),
                                              entry.get("last_event"),
                                              entry.get("source"))))
    if entry.get("parked"):
        parts.append("background")  # where the row says it, and in its words
    age = str(entry.get("age") or "").strip()
    if age:
        parts.append(age)
    sid = entry.get("session_id")
    if sid:
        # The first block of the uuid: enough to grep a transcript or name the
        # conversation in a bug report, short enough not to wrap the line.
        parts.append("id %s" % str(sid)[:8])
    return " · ".join(parts)


def detail_lines(entry):
    """The strip's two lines for one row, as (line 1, line 2).

    Pure but for one filesystem read: the branch of line 1 (see
    `_detail_folder`). Everything else comes out of what the entry already
    carries.
    """
    kind = entry.get("kind")
    if kind == "pin":
        command = str(entry.get("command") or "").strip()
        return ("pin: %s" % (command or "(the default shell)"),
                "pinned · session %s" % (entry.get("target")
                                         or entry.get("name") or "?"))
    first = _detail_folder(entry)
    if kind == "outside":
        # The same badge table the row uses -- `◇` rows never got the `○ open`
        # exception, and the strip must not invent it for them.
        return first, "outside tmux · %s · %s" % (
            entry.get("name") or entry.get("project") or "?",
            _state_words(_BADGE.get(entry.get("state"), "?")))
    if kind == "tmux" and not entry.get("session_id"):
        return first, "shell"  # no agent inside: no tool, no title, no id
    return first, _detail_agent(entry)


def entry_key(entry):
    """A row's STABLE identity: what travels in fzf's hidden field.

    It cannot depend on anything that changes while the menu is open -- and the
    visible text changes by itself, because it carries the age ("12s" becomes
    "15s"). So the key is an id: the tmux session's ('$3'), the session's own id
    (history / outside tmux) or the pin's tmux session.

    Only tabs and newlines are taken out, because those are the line's
    separators (`--delimiter "\\t"`, and `parse_selection` splits on tabs).
    Inner spaces are KEPT: a tmux session name may carry them, and squeezing
    every space out made two pins called "my notes" and "mynotes" the same row
    -- Enter would then open whichever came first.
    """
    k = entry.get("kind")
    if k in ("tmux", "pin"):
        key = entry.get("target")
    else:
        key = entry.get("session_id")
    clean = _one_line(str(key or "?")).strip()
    return clean or "?"


def format_fzf_line(index, entry):
    """One line for fzf: "<index>\\t<key>\\t<visible>\\t<detail 1>\\t<detail 2>".

    Only field 3 is shown (--with-nth 3). The index is a shortcut when
    resolving; the key is the one that rules (see `resolve_selection`) and goes
    in a field of its OWN because it is the identity fzf follows across reloads
    (--id-nth 2): with the `track-current+reload` the hook sends when a state
    changes, the cursor stays on the SAME session even if the list is
    reordered. The index is no identity: it is renumbered on every reload.

    The last two are the detail strip's lines, which fzf previews for whatever
    row the cursor is on. Every field goes through `_one_line`, tabs included:
    a title carrying a tab would shift each field after it, and the two that
    matter most -- the index and the key -- are the ones resolving depends on.
    """
    first, second = detail_lines(entry)
    return "%d\t%s\t%s\t%s\t%s" % (index, entry_key(entry),
                                   _one_line(visible_columns(entry)),
                                   _one_line(first), _one_line(second))


def fzf_input(entries):
    """Every line for fzf, one per entry."""
    return "\n".join(format_fzf_line(i, e) for i, e in enumerate(entries))


def parse_selection(line):
    """(index, key) of the first two hidden fields of the line fzf gives back.

    Only the first two are read, so the detail fields behind the visible text
    are none of its business and a line with five fields parses exactly like the
    three-field one did.

    ValueError if it does not look like `format_fzf_line` (a numeric index and a
    key); the caller decides what to do.
    """
    fields = line.split("\t", 2)
    if len(fields) < 3:
        raise ValueError("line without an index, a key and a text: %r" % line)
    return int(fields[0]), fields[1]


def parse_fzf_output(stdout):
    """(query, key, selection lines) of an fzf run with --print-query --expect.

    With both flags fzf prints: line 0 = the typed query; line 1 = the --expect
    key ("" when it was a plain Enter); lines 2+ = the selection.
    """
    lines = stdout.split("\n")
    query = lines[0] if lines else ""
    key = lines[1].strip() if len(lines) > 1 else ""
    selection = [l for l in lines[2:] if l.strip()]
    return query, key, selection


def resolve_selection(line, fresh):
    """The entry the line fzf gave back points at. None = it is not there any more.

    It is resolved by the KEY of the hidden field, never by the visible text:
    that text carries the session's age, which moves by itself between fzf
    painting the row and the human pressing Enter ("12s" -> "15s"), and
    comparing text made Enter do nothing precisely on freshly opened sessions.

    The index is only a shortcut: while fzf was open, the tmux hook may have
    sent it a `reload` (--listen) and the list is renumbered from 0. If the key
    is not in that slot, it is searched for in the whole fresh list; if it no
    longer shows up (it died in between), we do nothing.
    """
    try:
        i, key = parse_selection(line)
    except ValueError:
        return None
    if 0 <= i < len(fresh) and entry_key(fresh[i]) == key:
        return fresh[i]
    for e in fresh:
        if entry_key(e) == key:
            return e
    return None


def create_argvs(name, existing_names, home=None):
    """A new shell-first session: a shell in the projects directory, and jump to it.

    A SHELL is born, not a tool: if the tool is closed, the pane is still alive
    and usable. The name is sanitised first as tmux would ("example.com" ->
    "example_com", see `tmux_safe_name`) and then deduped against the sessions
    that already exist (foo, foo#2, foo#3...): that way the dedup sees the real
    clashes and the `switch-client` points at the name tmux ends up creating.

    `home` defaults to the configured projects directory, resolved on the call
    and not at import time, so an edited `config.json` (or
    `FLIGHTDECK_PROJECTS_DIR`) takes effect on the next new session.
    """
    home = str(config.projects_dir()) if home is None else home
    n = dedup_name(tmux_safe_name(name) or "session", existing_names)
    return [["new-session", "-d", "-s", n, "-c", home],
            ["switch-client", "-t", n]]


def action_argvs(entry, existing_names, fork=False, taken_names=None):
    """The sequence of tmux argvs for Enter on `entry`. [] = a no-op.

    Pure: it returns the argvs (without the `tmux`/`-L` in front, which the
    runner prepends) in the order they must be run.

    `fork=True` (Ctrl-F, only on the grey history) opens a COPY of that
    conversation instead of carrying it on: `--fork-session` resumes into a NEW
    id and the old transcript is left as it was. On any other row it means
    nothing and returns [] (the warning is `_act`'s job).

    Two different sets of names, on purpose. `existing_names` is what is ALIVE,
    and the pin branch needs exactly that: it is how it decides whether to start
    the pin's session or just jump into it. `taken_names` is what a NEW session
    may not be called (alive + reserved + every pin's session, `_taken_names`)
    and only the revive branch uses it -- a project folder called like a pin
    that is not running yet (`htop`, `k9s`, a custom `notes`) would otherwise
    create a tmux session with that exact name, which `is_reserved` then hides
    from the green group while the pin's row jumps into it. Left out, the alive
    set is used for both, which is what every pure test that does not care
    passes.
    """
    if taken_names is None:
        taken_names = existing_names
    k = entry.get("kind")
    if fork and k != "recent":
        return []
    if k == "tmux":
        # The session already exists: jump by its id ('$3'), not by name (tmux
        # allows repeated names and would resolve to the wrong one).
        return [["switch-client", "-t", entry["target"]]]
    if k == "pin":
        session = entry.get("target")
        if not session:
            return []
        command = entry.get("command")
        # Without a command tmux starts the default shell: a pin whose command
        # was removed still opens something usable.
        create = ["new-session", "-d", "-s", session] + ([command] if command else [])
        seq = [] if session in existing_names else [create]
        return seq + [["switch-client", "-t", session]]
    if k == "recent":
        sid = entry.get("session_id") or ""
        # The id ends up inside a `send-keys` (it is typed into a shell): if it
        # is not a clean id we type nothing, so no commands can be slipped in.
        if not SAFE_ID.match(sid):
            return []
        # The copy (--fork-session) is claude's: on a grey codex/agy row there
        # is no fork to make (flightdeck.tools.supports_fork).
        if fork and not supports_fork(entry.get("tool")):
            return []
        argv = resume_argv(entry.get("tool"), sid)
        if not argv:
            return []
        command = " ".join(argv) + (" --fork-session" if fork else "")
        # The copy is named differently on purpose: in the green list it has to
        # be obvious which is the good conversation and which is the copy.
        return _session_with_command(entry, taken_names, command,
                                     suffix="-fork" if fork else "")
    return []  # outside or others: informative, there is nothing to open


def _session_with_command(entry, existing_names, command, suffix=""):
    """A new tmux session in the project's cwd + type `command` + jump.

    The mould for reviving a grey row, shared with Ctrl-L (`launch_argvs`):
    there the only thing that changes is the COMMAND being typed, so it is a
    parameter instead of a duplicated sequence -- and with it the name
    sanitising, which matters (a project can carry dots, "example.com", and tmux
    turns them into "_" without warning; the dedup then avoids stepping on a
    session that is already called that).

    The command goes in its OWN `send-keys` with `-l --`, and Enter in another.
    Both flags are needed, measured on tmux 3.6a: without `-l` (literal) tmux
    reads the command as KEY NAMES, so a line that is "Enter" types nothing and
    one that is "C-c" sends a real Ctrl-C to the pane (it killed the process in
    the test); without `--` (end of flags), a command starting with a dash --the
    user deletes the "claude" and leaves "--model opus…"-- comes out as `invalid
    flag`, rc 1, with the tmux session ALREADY created, i.e. an orphan. Enter
    goes apart because with `-l` it would also be written literally, as five
    letters.
    """
    base = tmux_safe_name(entry.get("project")) or "session"
    n = dedup_name(base + suffix, existing_names)
    cwd = entry.get("cwd") or os.getcwd()
    return [["new-session", "-d", "-s", n, "-c", cwd],
            ["send-keys", "-t", n, "-l", "--", command],
            ["send-keys", "-t", n, "Enter"],
            ["switch-client", "-t", n]]


# --- Ctrl-L: launch a grey row "your way" ----------------------------------
# Enter and Ctrl-F launch the tool BARE (no flags) on purpose. Ctrl-L is the
# explicit path for launching it with flags: first they are marked in a menu,
# then the whole line is edited. What gets typed is what the user sees.

# A curated catalogue: the flags that actually get used. The note is for the
# human (it is seen in the menu) and does NOT travel into the command -- if it
# did, claude would get "(no" as its first prompt.
#
# `--fork-session` is NOT here on purpose: the copy belongs to Ctrl-F, which
# also christens the session `<project>-fork`. From here a copy would be born
# with the original's name, and in the green list you could no longer tell which
# is the good conversation.
COMMON_FLAGS = (
    ("--dangerously-skip-permissions", "no permission prompts"),
    ("--model fable", ""),
    ("--model opus", ""),
    ("--model sonnet", ""),
    ("--permission-mode plan", "start in plan mode"),
)

# codex's catalogue (0.153's help). Same format and same flow: multi-selection
# plus an editable line.
COMMON_FLAGS_CODEX = (
    ("--dangerously-bypass-approvals-and-sandbox", "no approvals, no sandbox"),
    ("--ask-for-approval never", "never asks; failures go back to the model"),
    ("--sandbox workspace-write", "sandbox with write access to the project"),
    ("--search", "web search"),
)

# And antigravity's (1.1.27's help), what Ctrl-L offers on a grey agy row. Same
# flow: multi-selection plus an editable line.
COMMON_FLAGS_AGY = (
    ("--dangerously-skip-permissions", "no permission prompts"),
    ("--mode plan", "start in plan mode"),
    ("--effort high", ""),
)

_CATALOGUE_BY_TOOL = {"claude": COMMON_FLAGS, "codex": COMMON_FLAGS_CODEX,
                      "agy": COMMON_FLAGS_AGY}


def flag_line(flag, note):
    """A row of the flag menu: the flag and, in brackets, its note."""
    return "%s   (%s)" % (flag, note) if note else flag


def line_flag(line):
    """The flag in a row of the menu: whatever is in front of the bracket.

    Cutting at "(" works because no flag carries brackets, and it also leaves at
    "" the rows that are ONLY an explanation (the "none" one), which then drop
    out by themselves when filtering.
    """
    return line.split("(")[0].strip()


def flag_catalogue(tool=None):
    """That tool's catalogue, one row per line, ready for fzf.
    None if the tool has no catalogue (Ctrl-L warns and does not launch)."""
    table = _CATALOGUE_BY_TOOL.get(tool or "claude")
    if table is None:
        return None
    return "\n".join(flag_line(f, n) for f, n in table)


def command_with_flags(session_id, flags, tool=None):
    """The pre-filled line: `<that tool's resume> <flags>`. "" = not possible
    (an odd id, or a tool with no resume).

    The flags go BEHIND the resume: in front, the parser would eat them (in
    claude, as the value of `--resume`). The same id filter as `action_argvs`,
    and for the same reason: this line ends up in a `send-keys`, i.e. typed into
    a shell, so a strange id builds nothing.
    """
    sid = session_id or ""
    if not isinstance(sid, str) or not SAFE_ID.match(sid):
        return ""
    base = resume_argv(tool, sid)
    if not base:
        return ""
    return " ".join(base + [f for f in flags if f.strip()])


def launch_argvs(entry, existing_names, command, taken_names=None):
    """Ctrl-L's sequence: the SAME one as resuming, with another command inside.

    `command` is the line just edited and it is typed AS IS: it is the user's,
    as if they had written it in their shell, and re-escaping it would break it.
    Empty = they cancelled, and then not even the session is opened.

    `taken_names` names the new session, for the reason given in `action_argvs`;
    left out, the alive set does both jobs.
    """
    if taken_names is None:
        taken_names = existing_names
    if entry.get("kind") != "recent":
        return []
    # Ctrl-L exists where there is a flag catalogue for that tool (claude,
    # codex, agy); without one nothing is launched (the interactive layer warns).
    if flag_catalogue(entry.get("tool")) is None:
        return []
    command = (command or "").strip()
    if not command:
        return []
    return _session_with_command(entry, taken_names, command)


def _live_ids(live):
    return {d.get("session_id") for d in live if d.get("session_id")}


def gather_entries():
    """Gather the menu's sources: tmux sessions + live sessions (inside and
    outside tmux) + the history + the pins, without duplicating what is alive
    into the grey group."""
    live = load_sessions()
    for d in live:
        # The conversation's title (/rename or the AI title) for the green row.
        # Few live sessions and transcripts filtered by needle: cheap.
        #
        # The card's own `title` is the fallback, and only a card synthesised
        # from Claude Code's registry carries one (`common._registry_cards`): a
        # claude that predates the hooks may never have been /renamed, and
        # without this its row would go back to being labelled by its folder
        # instead of by the name Claude Code registers for it.
        d["_title"] = title_for_session(d.get("session_id")) or d.get("title")
    outside = load_outside_sessions()
    alive = _live_ids(live) | _live_ids(outside)
    recent = list_recent_sessions(limit=_history_limit(), exclude_ids=alive)
    return build_entries(list_tmux_sessions(), live, outside, recent,
                         pins=configured_pins())


# --- the effectful loop of the chooser --------------------------------------
# The `flightdeck` tmux session runs this in a loop: show fzf, act on what was
# chosen, and show fzf again. Only Esc gets out of the loop, and it leaves the
# pane as a usable shell, never a dead pane.

def _shell_exec():
    """Replace the pane's process with a usable interactive shell."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        os.execvp(shell, [shell])
    except Exception:
        # If we could not exec, at least we do not leave a dead pane behind.
        sys.stderr.write("Could not start a shell (%s).\n" % shell)


def _run_tmux_seq(seq):
    """Run tmux argvs in order, STOPPING at the first one that fails.

    Stopping is compulsory, not cosmetic: the resume/create sequences point by
    NAME, so if the `new-session` fails (name already taken, a cwd that does not
    exist) carrying on would type the `claude --resume` inside somebody else's
    session that is already called that. True only if all of them exited 0.
    """
    tb = tmux_bin()
    for argv in seq:
        r = subprocess.run(tb + argv, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write("(tmux %s failed: %.100s)\n"
                             % (argv[0], (r.stderr or "").strip() or "no detail"))
            sys.stderr.flush()
            return False
    return True


# Fallback BASE port of fzf's HTTP API (--listen). When you enter a menu, the
# tmux hook the bash command installs sends a `reload` there so the list is
# refreshed without reopening fzf. The value in force is `menu_port` in the
# config, which the bash command reads by running the `flightdeck.config` module
# with `menu_port`: there is one source, not two copies.
LISTEN_PORT = config.DEFAULTS["menu_port"]


def port_for(menu_session):
    """Each menu's --listen port: `flightdeck` -> base, `flightdeck-N` -> +N.

    Since the menu became per-window (`flightdeck-2`, `flightdeck-3`...) there
    are several fzf at once, and two cannot listen on the same port: the second
    came out with `failed to listen on localhost:42707` and its pane fell to a
    shell (measured). The base falls back to the default when the
    config cannot give a number, for the same reason as every other threshold:
    the menu has to open anyway.
    """
    try:
        base = int(config.load()["menu_port"])
    except (KeyError, TypeError, ValueError):
        base = LISTEN_PORT
    m = re.match(r"^flightdeck-(\d+)$", menu_session or "")
    return base + (int(m.group(1)) if m else 0)


def _existing_names():
    """The names of the tmux sessions alive RIGHT NOW.

    Careful: only the ones that really exist go here, because `action_argvs`
    uses this to decide whether a pinned row has to start its session or just
    jump to it. To choose the name of a NEW session use `_taken_names`.
    """
    return {s["name"] for s in list_tmux_sessions()}


def _taken_names(existing=None):
    """Names a new session cannot use: the live ones plus the reserved ones.

    The reserved ones count even if they do not exist yet: a session created by
    hand and called like a pin's would eclipse the pin's (and `flightdeck` the
    chooser itself), and tmux would resolve the repeated name to the wrong one.
    They are deduped to `accounts#2` / `flightdeck#2`.

    `existing` is the live set when the caller already has it, so reviving a row
    does not ask tmux for the session list twice.
    """
    if existing is None:
        existing = _existing_names()
    return existing | set(RESERVED) | _pin_sessions(configured_pins())


def _pause_to_read():
    """Leave the error/warning on screen before fzf covers it."""
    sys.stderr.flush()
    time.sleep(1.5)


def _act(entry, fork=False):
    """Run Enter's action (or Ctrl-F's, with `fork`) on the entry.

    Creating is NEVER Enter's job: that is Ctrl-N, handled earlier in
    `_chooser_once`. Sessions living OUTSIDE tmux (a desktop app) are
    informative only: they cannot be adopted.
    """
    if fork and entry.get("kind") != "recent":
        # Copying a LIVE session does not exist: `--fork-session` starts from a
        # closed transcript. On a green row there is nothing to copy.
        sys.stderr.write("(copy (Ctrl-F) only makes sense on a grey history row)\n")
        _pause_to_read()
        return
    if fork and not supports_fork(entry.get("tool")):
        # Said here and not below: `action_argvs` comes back empty for this row
        # too, and the generic message down there ("no usable session id") blames
        # the ROW for something that is true of the TOOL -- neither codex nor agy
        # has a fork on its command line (`tools.supports_fork`).
        sys.stderr.write("(Ctrl-F copies Claude Code conversations only: "
                         "codex and agy have no fork from the command line)\n")
        _pause_to_read()
        return
    if entry.get("kind") == "outside":
        sys.stderr.write("(that session lives outside tmux — project %s;"
                         " it cannot be adopted)\n" % (entry.get("project") or "?"))
        _pause_to_read()
        return
    existing = _existing_names()
    seq = action_argvs(entry, existing, fork=fork,
                       taken_names=_taken_names(existing))
    if not seq:
        # No sequence = a row that cannot be opened (e.g. a session id that does
        # not pass the send-keys filter). It used to go quiet and look stuck.
        sys.stderr.write("(that row cannot be opened: it has no usable session id)\n")
        _pause_to_read()
        return
    if not _run_tmux_seq(seq):
        _pause_to_read()


# The courtesy row of the flag menu, and why it exists: fzf with -m, if you have
# marked NOTHING with Tab, gives back the row under the cursor (measured on
# 0.72.0). Without this row, entering Ctrl-L and pressing Enter would sneak in
# the first flag of the list -- which is precisely the dangerous one. It goes
# FIRST so the cursor starts there, and `line_flag` leaves it at "" (it starts
# with a bracket).
_NO_FLAGS_ROW = "(none — just edit the command)"


def _fzf_simple(args, stdin_text):
    """An auxiliary fzf (Ctrl-L's steps). None = cancelled (Esc/Ctrl-C).

    Codes 0 and 1 are accepted: the step that edits the command runs with an
    EMPTY list, so Enter exits with 1 ("nothing matches") having printed the line.
    """
    try:
        r = subprocess.run(["fzf"] + args, input=stdin_text, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if r.returncode in (0, 1):
        return r.stdout
    if r.returncode != 130:
        # 130 = Esc/Ctrl-C, i.e. cancelling: there is nothing to report. Any
        # other code is an fzf that DOES NOT EVEN START (a bad flag, a terminal
        # it cannot use), and without saying so Ctrl-L looked like "this key does
        # nothing".
        reason = (r.stderr or "").strip().splitlines()
        sys.stderr.write("(fzf exited with %d: %.100s)\n"
                         % (r.returncode, reason[-1] if reason else "no detail"))
        _pause_to_read()
    return None


def _pick_flags(tool=None):
    """Step 1: a multi-selection (Tab) menu of that tool's catalogue.
    None = cancelled."""
    out = _fzf_simple(
        ["-m", "--no-sort", "--reverse", "--height", "100%", "--prompt", "flags> ",
         "--header", "Tab: pick several · Enter: continue · Esc: cancel"],
        _NO_FLAGS_ROW + "\n" + (flag_catalogue(tool) or ""))
    if out is None:
        return None
    marked = [line_flag(l) for l in out.split("\n") if l.strip()]
    return [f for f in marked if f]


def _edit_command(command):
    """Step 2: the pre-filled, EDITABLE line. None = cancelled.

    It is an fzf with an empty list used only for its search field: `--query`
    brings it pre-filled and `--print-query` gives back what is left after
    editing. It is not readline for a reason measured on a real machine: its
    python3 (3.9.6) carries **libedit**'s readline, where
    `set_startup_hook`/`set_pre_input_hook` do NOT pre-fill (the line comes out
    empty), so one would have to settle for "type whatever you want to ADD".
    With fzf the whole line is edited -- removing a flag included -- and it is
    handled like the rest of the menu, from a phone too.
    """
    return _editable_line(command, "command> ",
                          "Edit the command and press Enter · Esc: cancel")


def _editable_line(query, prompt, header):
    """One line of text asked for with an fzf over an EMPTY list: only its
    search field, pre-filled with `query` and returned with `--print-query`.
    None = cancelled (Esc/Ctrl-C). It is the menu's only line editor: after an
    fzf the tty is left in line mode, and there a `sys.stdin.readline()` does
    NOT interpret the arrows -- a left arrow ended up INSIDE the text as `^[[D`
    (measured with a pty)."""
    out = _fzf_simple(
        ["--print-query", "--query", query, "--reverse", "--height", "100%",
         "--prompt", prompt, "--header", header], "")
    if out is None:
        return None
    return out.split("\n")[0]


def _ask_name():
    """The name of the new session when Ctrl-N arrives with nothing typed.
    None = cancelled; "" = Enter on an empty line (nothing is created either)."""
    line = _editable_line("", "new session name> ",
                          "Type the name and press Enter · Esc: cancel")
    return None if line is None else line.strip()


def _launch_with_flags(entry):
    """Ctrl-L: pick flags, edit the command and launch it. Esc at any step goes
    back to the menu without touching anything."""
    if entry.get("kind") != "recent":
        # On a green row the tool is already running: its flags are what they
        # are. Same criterion (and same warning) as Ctrl-F's copy.
        sys.stderr.write("(launch with flags (Ctrl-L) only makes sense on a grey"
                         " history row)\n")
        _pause_to_read()
        return
    tool = entry.get("tool")
    if flag_catalogue(tool) is None:
        sys.stderr.write("(Ctrl-L does not know that tool's flags)\n")
        _pause_to_read()
        return
    if not command_with_flags(entry.get("session_id"), [], tool=tool):
        sys.stderr.write("(that row cannot be launched: it has no usable session id)\n")
        _pause_to_read()
        return
    flags = _pick_flags(tool)
    if flags is None:
        return
    edited = _edit_command(command_with_flags(entry.get("session_id"), flags, tool=tool))
    if edited is None:
        return
    existing = _existing_names()
    seq = launch_argvs(entry, existing, edited,
                       taken_names=_taken_names(existing))
    if seq and not _run_tmux_seq(seq):
        _pause_to_read()


def _fzf_args(supports_listen, port=LISTEN_PORT, supports_id_nth=False):
    """fzf's command line. --listen and --id-nth only if the version understands them.

    `--id-nth 2` = each row's identity is its key (field 2, hidden): that is what
    lets a `track-current+reload(...)` (the one `flightdeck refresh-menu(s)`
    sends) leave the cursor on the same session after reloading the list.
    Measured on 0.72: without --id-nth the cursor stays in the same POSITION,
    i.e. on another row. An fzf without --id-nth (< 0.71) would reject it and
    not start -- hence the condition.

    The detail strip is fzf's PREVIEW of the current row's two hidden fields,
    which is what keeps it free: fzf substitutes them itself, so moving the
    cursor runs one `printf` and never a python process rebuilding the list.
    Three things were measured on 0.72 before trusting it: `{4}`/`{5}` index
    the ORIGINAL line (`--with-nth` does not renumber the placeholders), fzf
    QUOTES them for the shell (a title carrying `$(...)` or a backtick came out
    literal, nothing ran), and the window's size counts the border (hence the
    `3` below, for two lines). No version gate: `--preview`,
    `--preview-window` and `toggle-preview` are all far below the 0.36 floor
    that `--listen` already sets, which `doctor` treats as the hard minimum.

    `--with-nth` is `3` and not `3..`: with five fields, `3..` would paint the
    strip's own lines into the row as well.

    The header is kept short ON PURPOSE: fzf TRIMS it to the terminal's width,
    it does not wrap it, so whatever is left over is lost off the right -- and
    on the right live exactly the keys for getting out (`F12`, `Esc`), the ones
    you need when you are lost. On a phone the window is about 80 columns, and
    there fzf paints 77 characters of header: the 78th brings the `··` ellipsis
    and eats the end of the line (measured under a pty). The budget is why
    `?: details` cost two things when it joined -- `Enter` left the header
    (it is the obvious key, and it had already lost its verb when Ctrl-L
    joined) and `F12` kept its name but not its verb. Every key whose ACTION
    cannot be guessed still carries one.
    """
    args = ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "3", "--no-sort",
            "--reverse", "--height", "100%", "--print-query",
            "--expect", "ctrl-n,ctrl-f,ctrl-l",
            "--prompt", "session> ",
            "--preview", 'printf "%s\\n" {4} {5}',
            # THREE rows for two lines: the size counts the border, so a `2`
            # here leaves one usable row and line 2 scrolls out of sight
            # (measured on 0.72 -- fzf even prints its `1/2` scroll indicator).
            # Wrapped, so a long folder on an 80-column phone reads instead of
            # being cut; the top border separates the strip from its list.
            "--preview-window", "down,3,wrap,border-top",
            "--bind", "?:toggle-preview",
            "--header", "Ctrl-L: flags · Ctrl-F: copy · Ctrl-N: new · ?: details"
                        " · F12 · Esc: shell"]
    if supports_listen:
        args += ["--listen", str(port)]
    if supports_id_nth:
        args += ["--id-nth", "2"]
    return args


def _fzf_version(out):
    """(major, minor) of `fzf --version`'s output ('0.72.0 (Homebrew)'), or None
    when it does not look like that."""
    try:
        major, minor = (out.split(".") + ["0"])[:2]
        return (int(major), int(minor.split()[0] if " " in minor else minor))
    except Exception:
        return None


def _version_supports_listen(out):
    """Is `fzf --version`'s output >= 0.36?

    0.36 is the first one with --listen. On any odd format we say no: the menu
    works all the same, only refreshing when fzf is reopened.
    """
    v = _fzf_version(out)
    return v is not None and v >= (0, 36)


def _version_supports_id_nth(out):
    """Is `fzf --version`'s output >= 0.71? (the first one with --id-nth).

    Without it the menu works all the same; only a reload with a reordered list
    leaves the cursor in the same position instead of on the same session.
    """
    v = _fzf_version(out)
    return v is not None and v >= (0, 71)


def _fzf_version_output():
    """`fzf --version`'s output, or "" if it is not there or does not answer."""
    try:
        r = subprocess.run(["fzf", "--version"], capture_output=True, text=True)
    except Exception:
        return ""
    return r.stdout or ""


def _chooser_once(supports_listen, port=LISTEN_PORT, supports_id_nth=False):
    """One turn of the chooser. True to carry on, False -> shell."""
    entries = gather_entries()
    try:
        fzf = subprocess.run(_fzf_args(supports_listen, port, supports_id_nth),
                             input=fzf_input(entries),
                             capture_output=True, text=True)
    except FileNotFoundError:
        sys.stderr.write("'fzf' is missing (install it: brew install fzf,"
                         " or sudo apt install fzf).\n")
        return False
    if fzf.returncode == 130:
        return False  # Esc / Ctrl-C -> fall to a shell
    if fzf.returncode not in (0, 1):
        # 0 = a row was chosen, 1 = the filter matches nothing (we stay in the
        # menu there). Any other code is an fzf that DOES NOT EVEN START (it
        # exits 2: a bad flag, an invalid --listen port...) and it would not
        # start on the next turn either: staying in the loop would reopen it at
        # several Hz, burning CPU without saying anything. It is explained and
        # we fall to a shell.
        reason = (fzf.stderr or "").strip().splitlines()
        sys.stderr.write("(fzf exited with %d: %.100s)\n"
                         % (fzf.returncode, reason[-1] if reason else "no detail"))
        _pause_to_read()
        return False
    query, key, selection = parse_fzf_output(fzf.stdout)
    if key == "ctrl-n":
        # Ctrl-N creates with whatever was typed; if nothing was, it asks.
        name = query.strip() or _ask_name()
        if name and not _run_tmux_seq(create_argvs(name, _taken_names())):
            _pause_to_read()
        return True
    if selection:
        # Against the list of NOW, not the one we gave fzf: if there was a
        # `reload` in between, the line's index no longer matches `entries`.
        # Ctrl-F and Ctrl-L pick the row just like Enter; the only difference is
        # what is done with it (a copy, or launching it with flags).
        chosen = resolve_selection(selection[0], gather_entries())
        if chosen:
            if key == "ctrl-l":
                _launch_with_flags(chosen)
            else:
                _act(chosen, fork=(key == "ctrl-f"))
        else:
            # The row existed when fzf painted it and does not any more: without
            # this warning, Enter went quiet and the menu looked stuck.
            sys.stderr.write("(that row is gone: the list refreshed)\n")
            _pause_to_read()
        return True
    # Enter with a filter that matches nothing: there is no row to open, but it
    # is not an order to leave either. The menu is repainted; Esc is the way to
    # the shell.
    return True


def _my_tmux_session():
    """The name of the tmux session this loop runs in ("" when unknown)."""
    try:
        r = subprocess.run(tmux_bin() + ["display-message", "-p", "-t",
                                         os.environ.get("TMUX_PANE", ""),
                                         "#{session_name}"],
                           capture_output=True, text=True, timeout=2)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _report_config_error():
    """Say once, on entering the menu, that `config.json` could not be read.

    Every reader in here degrades to the defaults on a broken config file, which
    is the right thing for a menu that has to open -- but done silently it costs
    the user their pins and their `menu_port` with nothing on screen to explain
    it (a stray comma is enough). So it is said once per menu, before fzf covers
    the pane, which is the half of the warning that `doctor` does not own.

    Once per LOOP, not per round: the message would otherwise come back after
    every selection, and with it the 1.5 s pause.
    """
    try:
        config.load()
        if config.load.error:
            sys.stderr.write("(flightdeck: config.json ignored — %s)\n"
                             % config.load.error)
            _pause_to_read()
    except Exception:
        # Saying so is an extra: it can never be what keeps the menu from
        # opening.
        pass


def chooser_loop():
    """The chooser's loop. A one-off failure does not kill it; Esc -> exec shell."""
    _report_config_error()
    version = _fzf_version_output()  # fzf's version does not change while running
    supports_listen = _version_supports_listen(version)
    supports_id_nth = _version_supports_id_nth(version)
    port = port_for(_my_tmux_session())  # each menu (flightdeck, -2...) its own
    while True:
        try:
            carry_on = _chooser_once(supports_listen, port, supports_id_nth)
        except KeyboardInterrupt:
            break
        except Exception as e:
            # We never die of a one-off failure: it is shown and we try again.
            try:
                sys.stderr.write("(flightdeck: transient failure: %.80s)\n" % str(e))
                sys.stderr.flush()
            except Exception:
                pass
            time.sleep(1)
            continue
        if not carry_on:
            break
    _shell_exec()  # the pane is left as a usable shell (or closes if exec fails)


def main(argv=None):
    """`python3 -m flightdeck.picker [loop|feed]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "loop"
    if cmd == "loop":
        chooser_loop()
        return 0
    if cmd == "feed":
        # Only the list, so fzf can reload it live (`reload`).
        lines = fzf_input(gather_entries())
        sys.stdout.write(lines + "\n" if lines else "")
        return 0
    sys.stderr.write("usage: python3 -m flightdeck.picker [loop|feed]\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
