"""Flightdeck's shared pieces: tmux, session cards and small utilities.

This is what the picker, the status bar and the state hook all use at once:
talking to tmux, reading the session cards the hooks leave in the state
directory (and the context notes the status line tee leaves right next to them)
and formatting ages. It also discovers the live TMUX SESSIONS, which are the
unit of the menu (a window used to be).

Everything is defensive: a tmux that is not running, a corrupt file or a broken
line come back empty, never as an exception.
"""
import datetime
import functools
import json
import os
import re
import subprocess
import time
from pathlib import Path

from flightdeck import config
from flightdeck.registry import read_registry, parked_map, status_by_pid

# A valid session_id is the stem of a file (a UUID). We filter out any stem with
# odd characters BEFORE it can reach the shell of a `--resume`, and before it
# can be used as a file name under the state directory.
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# How long a tee note (`<id>.ctx.json`) is worth something before it counts as
# stale.
#
# 15 minutes, and not two, because of how it gets written: the status line
# repaints on INTERACTION, not on a clock, so a claude in the middle of a long
# turn (subagents working) goes minutes without rewriting its note -- exactly
# when the context is filling up. Measured for real with 120s, 0 of 8 live notes
# passed the cut and the 🧠 never showed. And the figure holds up: the context %
# only goes up, and slowly, so one from 15 minutes ago still says something.
# What we pay if we overshoot is cosmetic: after a /compact or a handover an old
# % can be shown for a few minutes, until the next repaint.
CTX_FRESH_S = 900


def tmux_bin():
    sock = config.tmux_socket()
    return ["tmux"] + (["-L", sock] if sock else [])


def tmux(*args, timeout=3):
    try:
        return subprocess.run(tmux_bin() + list(args), capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


# The first tmux that prints a message literally (`display-message -l`). Below
# it the flag does not exist and the message is read as a FORMAT, so the text has
# to be escaped instead -- see `notice_args`.
#
# It is the ONLY thing Flightdeck asks of a tmux newer than 3.2 (`-d` and
# `new-session -e` are 3.2, `-C` and `#{m/r:...}` are 3.1), which is why
# degrading it is what lets Ubuntu 22.04 (3.2a) and Debian 12 (3.3a) in.
LITERAL_NOTICE_FROM = (3, 4)

# The first number pair in `tmux -V`. It is looked for anywhere in the line so
# that a build between releases (`tmux next-3.5`) answers like the release it is
# heading for, and both numbers are taken whole: 3.10 is above 3.4, not below it.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


@functools.lru_cache(maxsize=None)
def tmux_version(run=subprocess.run):
    """This tmux's `(major, minor)`, or None when it cannot be told.

    Cached for the life of the process: it is read from inside the hooks, which
    fire on every tool use of every session, and a tmux cannot change version
    underneath a running one. (The socket is not part of the key on purpose:
    `tmux -V` asks the BINARY, it never reaches a server.)

    None means "I do not know" -- no tmux on PATH, a build that prints something
    unexpected, a call that timed out -- and every caller has to treat it as the
    OLDEST tmux, which is the safe reading.

    `run` is the seam for the tests: they must answer the same on a machine's
    3.6a and on a CI runner's 3.2a.
    """
    try:
        r = run(tmux_bin() + ["-V"], capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return None
        m = _VERSION_RE.search(r.stdout or "")
    except Exception:
        return None
    return (int(m.group(1)), int(m.group(2))) if m else None


def notice_args(text, version, duration_ms, client=None):
    """The argv of a floating notice (`display-message`) for THIS tmux.

    `-d` because without it tmux uses its stock display-time of 750 ms, a flash
    too short to read; `-C` because without it the pane stops repainting in that
    client for as long as the message lasts (measured in 3.6a: at 750 ms you do
    not notice, at 5 s it would freeze the agent's output on every screen).

    And then the part that varies: from tmux 3.4 the text travels with `-l` and
    is printed exactly as it is. Below that the flag does not exist, so the text
    is escaped the way the status bar escapes its own (`#` -> `##`), which is
    what stops a session called `#(rm -rf /)` from being RUN as a format. An
    unknown version (None) takes that same safe form: losing the `#` of a name is
    cosmetic, running it is not.
    """
    text = text or ""   # never a None inside an argv: that is a TypeError
    args = ["display-message"]
    if client:
        args += ["-c", client]
    args += ["-d", str(duration_ms), "-C"]
    if version is not None and version >= LITERAL_NOTICE_FROM:
        return args + ["-l", text]
    return args + [text.replace("#", "##")]


def pid_alive(pid):
    """Does that process still exist? When in doubt, YES.

    `os.kill(pid, 0)` sends no signal at all: it only asks "does it exist and
    could I signal it?". A pid belonging to another user answers
    PermissionError, which is precisely the proof that it exists.

    The bias is deliberate: hiding a session from the user that is in fact alive
    is far worse than leaving one row too many, so any answer that is not a
    flat "that process does not exist" counts as alive (including an old card
    that carries no pid).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True  # no pid, or junk: the card's freshness decides
    if pid <= 0:
        # In os.kill, 0 and negatives are process GROUPS (it would reach half
        # the system): we do not even ask.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it just belongs to another user
    except Exception:
        return True
    return True


def live_panes():
    r = tmux("list-panes", "-a", "-F",
             "#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_current_command}"
             "\t#{window_name}\t#{pane_pid}")
    panes = {}
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            p = line.split("\t")
            if len(p) >= 5:
                # The pane's own process (its shell). It is here for one reader
                # only -- the walk that looks inside a pane for a `claude attach`
                # viewer (`_attach_viewers`) -- so a tmux that did not give the
                # field, or gave something that is not a number, leaves it None
                # and nothing else about the pane is lost.
                try:
                    pid = int(p[5])
                except (IndexError, ValueError):
                    pid = None
                panes[p[0]] = {"session": p[1], "window": p[2], "cmd": p[3],
                               "window_name": p[4], "pid": pid}
    return panes


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", s))
        except ValueError:
            return None


def human_age(sec):
    if sec is None:
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, _ = divmod(sec, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def read_ctx_pct(session_id, now=None, sess_dir=None):
    """The context % the tee noted for that session. None when it is not known.

    `now` is an epoch in SECONDS (what `time.time()` gives), not a datetime: it
    is compared against the note's `at`, which the tee also writes with
    `time.time()`. By default the clock in here looks it up; it is passed in by
    hand when the caller already has the time (so every card in one pass is
    measured against the same instant). `sess_dir` is only used by the tests.

    None is the NORMAL answer in plenty of cases, not an error: the session may
    have no status line, the note may be from a while ago (claude closed) or the
    file may be half-written -- the tee does not write atomically, so catching it
    torn in half is a matter of time. All of that is "the % is not known", and
    Flightdeck simply shows no number.
    """
    if not session_id or not SAFE_ID.match(str(session_id)):
        # The id ends up being a file name: one with a "/" would send us to read
        # outside the sessions directory.
        return None
    base = config.sessions_dir() if sess_dir is None else Path(sess_dir)
    try:
        d = json.loads((base / ("%s.ctx.json" % session_id)).read_text())
        pct = int(d["pct"])
        at = float(d["at"])
    except Exception:
        return None
    # abs(): a clock jumping backwards (or an `at` from an impossible future)
    # must not leave a % frozen forever.
    if abs((time.time() if now is None else now) - at) >= CTX_FRESH_S:
        return None
    if not 0 <= pct <= 100:
        # A percentage that is not a percentage (negative, 5000...) can only be
        # a broken payload: better to say nothing than to paint "🧠5000%".
        return None
    return pct


def _delete_card(f):
    """Delete the card and, with it, its context note.

    They go together and they die together: `load_sessions` skips the
    `<id>.ctx.json` files by the file NAME, so without this the note of an
    already closed session was looked at by no prune pass at all and stayed in
    the sessions directory forever.
    """
    f.unlink(missing_ok=True)
    f.with_name("%s.ctx.json" % f.stem).unlink(missing_ok=True)


def _raw_cards():
    """Every card in the sessions directory as it is (with `_path`), unfiltered.

    The tee notes (`<id>.ctx.json`) live in this same directory and the glob
    catches them. They are NOT cards: without this skip they would come in as
    cards with no pane, and down that branch `prune` would DELETE them --
    Flightdeck would lose the context % of live sessions.
    """
    out = []
    sess_dir = config.sessions_dir()
    if not sess_dir.exists():
        return out
    for f in sorted(sess_dir.glob("*.json")):
        if f.name.endswith(".ctx.json"):
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        d["_path"] = f
        out.append(d)
    return out


def _fix_with_registry(pane_cards, registry):
    """With `Stop` (awaiting_input) but a `busy` registry, the session is NOT
    waiting for the user: there are agents or delegated tasks running (or the
    model has already been woken up by one of them). It is painted working and
    does not count as a wait -- this was the "N waiting for you" that cleared
    itself a second later. Only the Stop wait is touched: `asking` /
    `needs_attention` / `looping` are exact signals. For a parked card the JOB's
    status decides (`_job_pid`), not the pane's watcher. `_status_cc` is left on
    the card so this can be explained."""
    by_pid = status_by_pid(registry)
    for d in pane_cards:
        pid = d.get("_job_pid") or d.get("pid")
        status = by_pid.get(pid)
        d["_status_cc"] = status
        if status == "busy" and d.get("state") == "awaiting_input":
            d["state"] = "working"


# The registry's tmux destination is spelled "<session>:@<window>.%<pane>". The
# PANE id at the end is the only part of it worth trusting: it is written when
# claude starts, and a session renamed or a window moved underneath it would
# leave the rest stale, while a pane id never changes while the pane lives.
_REG_PANE = re.compile(r"(%\d+)$")

# The same string read for its SESSION NAME, and only as a veto (see
# `_registry_cards`). What it guards against: the destination carries no SOCKET,
# so a claude running inside tmux on ANOTHER server -- the isolated `-L` ones
# this project's own measurements use -- has a pane id that may well exist on
# this one too, and its name and state would be painted on a stranger's pane. A
# name that does not match is the cheap tell. It is a veto and not a source of
# truth because the name IS stale after a rename: there the row loses its
# annotation and goes back to the bare `shell` it was before, which is the safe
# direction.
_REG_SESSION = re.compile(r"^(.*):@\d+\.%\d+$")

# How long after starting a claude that has never had a turn still counts as
# JUST OPENED. Claude Code writes `status: "idle"` the moment it registers
# itself (measured: `statusUpdatedAt` 46 ms after `startedAt`), so an `idle` it
# has never moved from is an empty box, not a session waiting for you. Five
# seconds is roomy on purpose: the cost of overshooting is painting "open" on a
# session that answered in under five seconds, and the cost of undershooting is
# a false "waiting for you", which is the thing this cockpit refuses to do.
REG_JUST_OPENED_MS = 5000

# Claude Code's `status` -> the state a card carries. `waiting` is a dialog
# open (`waiting_for` says which), `shell` is stopped with a background Bash
# still running: both of them are stopped until the human does something.
_REG_STATE = {"busy": "working", "idle": "awaiting_input",
              "waiting": "needs_attention", "shell": "awaiting_input"}


def _epoch_ms_iso(ms):
    """A registry timestamp (epoch MILLISECONDS) as the ISO text a card carries.

    AWARE (`.astimezone()`), and that is not decoration: `load_sessions`
    subtracts this from an aware `now`, and python raises on naive minus aware.
    """
    if ms is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            ms / 1000.0).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _state_from_registry(e):
    """The state keys a row takes from a registry entry's `status`.

    Shared by the two rows built out of Claude Code's registry -- the one for a
    claude that predates the hooks and the one for a conversation being watched
    through `claude attach` -- so the two can never disagree about what `busy`
    or `idle` means. A status we do not know gives back nothing at all:
    inventing one would be inventing a wait.
    """
    out = {}
    status = e.get("status")
    state = _REG_STATE.get(status)
    if state:
        out["state"] = state
    started, changed = e.get("started_at"), e.get("status_updated_at")
    if (status == "idle" and started is not None and changed is not None
            and abs(changed - started) <= REG_JUST_OPENED_MS):
        # Never had a turn: the trio the `○ open` badge reads (see
        # `picker.state_badge`), which is also how a card written by a
        # SessionStart and nothing else looks.
        out.update({"state": "working", "last_event": "SessionStart",
                    "source": "startup"})
    return out


def _registry_cards(panes, taken_panes, taken_ids, now, now_epoch):
    """Cards for the claudes that predate Flightdeck's hooks, from the registry.

    Cards are written by the HOOKS, so a session that has not had a turn since
    the install has none: on the first day of an installation every claude
    already open showed as a bare `shell`, with no state and no age (seen live).
    Claude Code's own registry (`~/.claude/sessions/<pid>.json`) already knows
    the pane, the name, the folder and whether it is busy, so the row is built
    from that instead.

    Synthesised IN MEMORY and never written (`_from_registry` marks them): the
    hook goes on owning the cards, and the moment a real one exists for that
    pane or that session it wins -- those arrive here in `taken_panes` /
    `taken_ids`. A failure of any kind means no synthesised rows, never an
    exception: this is a nicety on top of the cards, not a source of truth.
    """
    try:
        registry = read_registry()
    except Exception:
        return []
    out = []
    for e in registry:
        # `bg` is a conversation parked in a daemon job: it has no pane of its
        # own, and the pane's row is what shows it (see `_link_parked`).
        if e.get("kind") != "interactive":
            continue
        m = _REG_PANE.search(e.get("tmux") or "")
        if not m:
            continue   # no tmux destination: the desktop app, a job, a fork
        pane = m.group(1)
        if pane not in panes or pane in taken_panes:
            continue
        # Same pane id, another tmux server: the name in front of it says so.
        named = _REG_SESSION.match(e.get("tmux") or "")
        if named and named.group(1) != panes[pane].get("session"):
            continue
        sid = e.get("session_id")
        # No id, no row. `read_registry` lets a null `sessionId` through as
        # None, and a row built on one is a crash waiting to happen: the sort
        # at the end of `load_sessions` would compare it with the id of another
        # row in the same tmux session (TypeError, and the menu's loop falls out
        # to a shell). It would be a poor row anyway -- the glyph, the 🧠 and
        # every action hang off the id.
        if not sid or sid in taken_ids:
            continue
        # A live pane does not prove there is a live claude inside it: a
        # `kill -9` leaves the entry behind with the pane running a shell, and a
        # green row over a shell is exactly what `load_sessions` refuses to
        # paint for a card (same bias, same reason).
        if not pid_alive(e.get("pid")):
            continue
        cwd = e.get("cwd")
        d = {"session_id": sid, "pid": e.get("pid"), "tmux_pane": pane,
             # The tmux session's name comes from the SERVER, not from the
             # registry's stale copy of it.
             "tmux_session": panes[pane].get("session"),
             "tool": "claude",   # this registry is Claude Code's own
             "cwd": cwd, "project": Path(cwd).name if cwd else None,
             # The name Claude Code registers (`claude -n`, `/rename`, its own
             # default) is the only human label these rows can have: the picker
             # falls back to it when the transcript holds no title.
             "title": e.get("name"),
             "_from_registry": True}
        d.update(_state_from_registry(e))
        upd = _epoch_ms_iso(e.get("updated_at"))
        if upd:
            d["updated_at"] = upd
        parsed = parse_iso(upd)
        d["_age"] = (now - parsed).total_seconds() if parsed else None
        d["_pane_info"] = panes.get(pane, {})
        d["_window_name"] = panes.get(pane, {}).get("window_name")
        # The status line tee writes `<id>.ctx.json` with no hook involved, so
        # one of these rows can show its 🧠 from the very first repaint.
        d["_ctx_pct"] = read_ctx_pct(sid, now=now_epoch)
        out.append(d)
    return out


# How many levels below the pane's own process the viewer is looked for.
# Measured: the pane's shell (pid 5116) is the DIRECT parent of
# `claude attach 23a53419` (pid 5168), so one level is what the real shape
# needs; the second is there for a shell that wraps the command (a `sh -c`, a
# launcher). It stops there on purpose -- this is a bounded walk over a list of
# processes, not a search of the whole tree.
ATTACH_DEPTH = 2


def process_table(run=subprocess.run):
    """Every process as `(pid, ppid, command line)`. [] when it cannot be read.

    ONE `ps` per pass (not one per pane), and the flags are the ones both
    families understand: BSD's `ps` on macOS and procps' on Linux both take
    `-ax -o pid=,ppid=,args=`, and the empty `=` headers leave the output as
    three bare columns with no title line to skip.

    The command line arrives FLATTENED, exactly as in `handover.argv_of_pid`:
    the spaces inside an argument cannot be told from the ones separating
    arguments. Its one reader here (`attach_id`) only looks at the first three
    tokens of a line it has already recognised, so that costs nothing.

    Any failure comes back empty rather than raising: without this table a pane
    simply goes on showing what it showed before.
    """
    try:
        r = run(["ps", "-ax", "-o", "pid=,ppid=,args="],
                capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        out.append((pid, ppid, parts[2]))
    return out


def attach_id(argv):
    """The conversation a `claude attach <id>` process is SHOWING, or None.

    Claude Code 2.1.26x+ opens a conversation that lives in a background job in
    a pane of its own with `claude attach <short id>`: that pane is a WINDOW
    onto the job, not a session of its own.

    Strict on purpose. The subcommand has to be the first token after the binary
    (the shape measured), argv[0] has to read as a claude -- the name `claude`,
    or the binary christened with its version, `2.1.269`, the same reading
    `tools.is_argv_of` makes -- and the id has to pass `SAFE_ID`.
    What this decides is which conversation gets painted on a pane, and the row
    it replaces (a bare `shell`) is not worth a guess.
    """
    if not argv or len(argv) < 3 or argv[1] != "attach":
        return None
    try:
        name = Path(str(argv[0])).name
    except TypeError:
        return None
    if not (name.startswith("claude") or name[:1].isdigit()):
        return None
    ident = str(argv[2])
    return ident if SAFE_ID.match(ident) else None


def _viewer_in(pid, by_pid, children):
    """The id the `claude attach` of that pane is showing, or None.

    The pane's OWN process is read first: `#{pane_pid}` is the pane's first
    process, which in a Flightdeck session is the shell (shell-first, so the
    viewer is a child of it, which is the shape measured) but IS the command
    itself in a pane somebody opened with one.
    """
    if not pid:
        return None
    ident = attach_id((by_pid.get(pid) or "").split())
    if ident:
        return ident
    frontier = [pid]
    for _ in range(ATTACH_DEPTH):
        below = []
        for parent in frontier:
            for child, args in children.get(parent, ()):
                ident = attach_id(args.split())
                if ident:
                    return ident
                below.append(child)
        frontier = below
    return None


def _attach_viewers(panes, table):
    """{pane id: the short id of the conversation that pane is WATCHING}."""
    if not panes or not table:
        return {}
    children, by_pid = {}, {}
    for pid, ppid, args in table:
        children.setdefault(ppid, []).append((pid, args))
        by_pid[pid] = args
    out = {}
    for pane, info in panes.items():
        ident = _viewer_in(info.get("pid"), by_pid, children)
        if ident:
            out[pane] = ident
    return out


def _job_of_viewer(ident, jobs):
    """The background job a `claude attach <id>` is showing, or None.

    `claude attach` is given the JOB's short id, so an entry whose `job_id` is
    exactly that is the match and wins; one that carries none is matched on a
    `session_id` that STARTS with it (the short id is that id's first eight
    characters). Anything ambiguous answers None instead of guessing: painting
    the wrong conversation on a pane is worse than leaving the row the bare
    `shell` it was.
    """
    exact = [e for e in jobs if e.get("job_id") == ident]
    if exact:
        return exact[0] if len(exact) == 1 else None
    prefix = [e for e in jobs if (e.get("session_id") or "").startswith(ident)]
    return prefix[0] if len(prefix) == 1 else None


def _attach_rows(panes, taken_panes, taken_ids, cards, now, now_epoch):
    """Rows for the panes that are WATCHING a conversation in a background job.

    The case (measured): pane `%2` runs `claude attach 23a53419`, and the
    registry holds ONE entry for that conversation, the JOB's (`kind: "bg"`,
    `jobId`, no `tmux`), which `_registry_cards` rightly skips because a job
    has no pane of its own. With nothing linking the two, the pane read `shell`
    -- no name, no state, no age -- while the same conversation was listed
    apart as `◇ outside tmux`.

    What links them is the process table: the pane's own process is the viewer's
    parent. From the job then comes the row, either its real card (the hook did
    fire for it) or one synthesised from the registry entry, exactly as
    `_registry_cards` does it. The pane is grafted on (`tmux_pane`,
    `tmux_session`), `pid` is the JOB's -- the viewer comes and goes, and what
    has to be alive for the row to mean anything is the conversation -- and
    `_parked` carries the short id so the row says `· background` with nothing
    changed in the picker. `_attach` is what tells the handover to keep its
    hands off (see `handover.reason_not_to_close`).

    -> (rows, {session_id of the conversations shown this way}), the second one
    so `load_outside_sessions` does not list them a second time. Any failure
    means no rows, never an exception: this is a nicety on top of the cards.
    """
    free = {p: info for p, info in (panes or {}).items()
            if p not in taken_panes and info.get("pid")}
    if not free:
        return [], set()
    try:
        jobs = [e for e in read_registry() if e.get("kind") == "bg"]
        # The `ps` is only asked for when there IS a background job to link to:
        # with none -- the common case, and every machine that never presses ←
        # -- this whole path costs one registry read.
        viewers = _attach_viewers(free, process_table()) if jobs else {}
    except Exception:
        return [], set()
    by_id = {c.get("session_id"): c for c in (cards or [])}
    rows, shown = [], set()
    for pane, ident in sorted(viewers.items()):
        job = _job_of_viewer(ident, jobs)
        if job is None or not pid_alive(job.get("pid")):
            continue
        sid = job.get("session_id")
        # No id, no row: same reason as in `_registry_cards` (the sort at the
        # end of `load_sessions` compares ids, and the glyph, the 🧠 and every
        # action hang off it). And a conversation already on a row of its own
        # stays where it is.
        if not sid or sid in taken_ids or sid in shown:
            continue
        card = by_id.get(sid)
        if (card is not None and card.get("state") != "ended"
                and pid_alive(card.get("pid"))):
            # The hook's card always wins -- it knows about turns, forms and
            # loops, which a `status` does not. Copied, because what we are
            # about to write on it (the pane) belongs to this view of it only.
            d = dict(card)
            d.pop("_path", None)
        else:
            cwd = job.get("cwd")
            d = {"session_id": sid, "pid": job.get("pid"),
                 "tool": "claude",   # this registry is Claude Code's own
                 "cwd": cwd, "project": Path(cwd).name if cwd else None,
                 "title": job.get("name"), "_from_registry": True}
            d.update(_state_from_registry(job))
            upd = _epoch_ms_iso(job.get("updated_at"))
            if upd:
                d["updated_at"] = upd
        d["tmux_pane"] = pane
        # From the tmux SERVER, like everywhere else: the registry's own copy of
        # a session name is the one written when that claude started.
        d["tmux_session"] = panes[pane].get("session")
        d["_parked"] = ident
        d["_attach"] = ident
        parsed = parse_iso(d.get("updated_at"))
        d["_age"] = (now - parsed).total_seconds() if parsed else None
        d["_pane_info"] = panes[pane]
        d["_window_name"] = panes[pane].get("window_name")
        d["_ctx_pct"] = read_ctx_pct(sid, now=now_epoch)
        rows.append(d)
        shown.add(sid)
    return rows, shown


def _link_parked(pane_cards, raw):
    """Puts on every PARKED pane card (`/background` or the ← arrow, see
    `flightdeck.registry`) the state of its JOB, which is the one really
    working: `state`/`last_event`/`source`/`turn` and the activity
    (`updated_at`) are the job's, the card is still the pane's (id, title, the
    tee's 🧠...) and `_parked` holds the job's session_id. Returns the set of
    job session_ids represented this way (so they are not listed separately)."""
    try:
        registry = read_registry()
        mapping = parked_map(pane_cards, registry, all_cards=raw)
    except Exception:
        registry, mapping = [], {}
    if not mapping:
        _fix_with_registry(pane_cards, registry)
        return set()
    by_id = {c.get("session_id"): c for c in raw}
    represented = set()
    for d in pane_cards:
        job_id = mapping.get(d.get("session_id"))
        job = by_id.get(job_id)
        if job is None or job.get("state") == "ended" or not pid_alive(job.get("pid")):
            continue
        d["_parked"] = job_id
        d["_job_pid"] = job.get("pid")
        for k in ("state", "last_event", "source", "turn"):
            d[k] = job.get(k)
        if job.get("updated_at"):
            d["updated_at"] = job["updated_at"]
        represented.add(job_id)
    _fix_with_registry(pane_cards, registry)
    return represented


def load_sessions(prune=False):
    panes = live_panes()
    now = datetime.datetime.now().astimezone()
    now_epoch = now.timestamp()
    out = []
    raw = _raw_cards()
    for d in raw:
        f = d.pop("_path")
        if d.get("state") == "ended":
            if prune:
                _delete_card(f)
            continue
        pane = d.get("tmux_pane")
        if not pane or pane not in panes:
            if prune:
                _delete_card(f)
            continue
        # The pane is still alive, but the claude inside it may not be: a
        # `kill -9` (or a crash) sends no SessionEnd, so its card keeps saying
        # "● working" on top of a shell, forever. The pid is the proof of life,
        # with the same bias as in load_outside_sessions: with no pid, it is
        # listed. Here it is NOT deleted, not even with prune -- the pane is
        # alive and the file belongs to a claude that may be starting up right
        # now.
        if not pid_alive(d.get("pid")):
            continue
        upd = parse_iso(d.get("updated_at"))
        d["_age"] = (now - upd).total_seconds() if upd else None
        d["_pane_info"] = panes.get(pane, {})
        # The name of the tmux window this claude lives in. Informative: the
        # unit of the menu is the tmux SESSION, so build_entries does not read
        # it (it paints the session name + the project of the claude inside).
        d["_window_name"] = panes.get(pane, {}).get("window_name")
        # How much brain that claude has left (noted by the status line tee).
        # One small open per live session; no reading of transcripts here, since
        # the status bar calls this every few seconds too.
        if d.get("tool") == "codex":
            # Codex's 🧠 does not come through claude's tee: it is read from its
            # rollout.
            from flightdeck.tools import codex_working, ctx_pct_codex
            d["_ctx_pct"] = ctx_pct_codex(d.get("session_id"))
            # And neither does "working": with no hooks firing (0.153), a turn
            # in flight shows up in the rollout's mtime (see codex_working).
            if d.get("state") == "awaiting_input" and codex_working(d):
                d["state"] = "working"
                d["last_event"] = "PostToolUse"
        else:
            d["_ctx_pct"] = read_ctx_pct(d.get("session_id"), now=now_epoch)
        out.append(d)
    # A claude that was already open when the hooks went in has no card at all:
    # its row comes from Claude Code's own registry, and only for the panes no
    # card above already speaks for.
    out += _registry_cards(panes,
                           {d.get("tmux_pane") for d in out},
                           {d.get("session_id") for d in out},
                           now, now_epoch)
    # And a pane can be a WINDOW onto a conversation that lives in a background
    # job (`claude attach <id>`): there is neither a card nor an interactive
    # registry entry naming it, so the link goes through the process table.
    out += _attach_rows(panes,
                        {d.get("tmux_pane") for d in out},
                        {d.get("session_id") for d in out},
                        raw, now, now_epoch)[0]
    # Parked conversations: the pane's row tells what its job is doing.
    if _link_parked(out, raw):
        for d in out:
            if d.get("_parked"):
                upd = parse_iso(d.get("updated_at"))
                d["_age"] = (now - upd).total_seconds() if upd else d.get("_age")
    # `or ""` and not `get(k, "")` on the id: a card can carry the key with a
    # null VALUE (an old one, a half-written file), and the default only covers
    # a key that is missing. Two rows in one tmux session and the comparison
    # raises -- the status bar swallows that, the menu's loop does not.
    out.sort(key=lambda d: (d.get("tmux_session") or d.get("project") or "",
                            d.get("session_id") or ""))
    return out


def parse_tmux_sessions(stdout):
    """Parses `list-sessions -F name\\tid\\tactivity\\tattached`. A broken line is skipped."""
    out = []
    for line in (stdout or "").splitlines():
        p = line.split("\t")
        if len(p) < 4:
            continue
        try:
            out.append({"name": p[0], "id": p[1], "activity": float(p[2]),
                        "attached": p[3] not in ("0", "")})
        except ValueError:
            continue
    return out


def list_tmux_sessions():
    """Live tmux sessions. [] when tmux is not running or fails."""
    r = tmux("list-sessions", "-F",
             "#{session_name}\t#{session_id}\t#{session_activity}\t#{session_attached}")
    if not r or r.returncode != 0:
        return []
    return parse_tmux_sessions(r.stdout)


def load_outside_sessions(now=None, max_age_s=21600):
    """Hook cards of live sessions OUTSIDE tmux (the desktop app, etc.).

    state != ended, no live pane in tmux, and updated less than max_age_s ago
    (a stale card from a crash is not a live session). Informative only.
    """
    if now is None:
        now = datetime.datetime.now().astimezone()
    panes = live_panes()
    out = []
    raw = _raw_cards()
    # The jobs of PARKED conversations are already represented by the pane's row
    # (see `_link_parked`): they are not listed again here.
    in_pane = [d for d in raw if d.get("state") != "ended"
               and d.get("tmux_pane") in panes and pid_alive(d.get("pid"))]
    represented = set(_link_parked([dict(d) for d in in_pane], raw))
    # And so is the conversation a pane is WATCHING through `claude attach`: its
    # job's card has no pane of its own, so without this the same conversation
    # was listed here as well as on the pane's row (see `_attach_rows`).
    # Only the second half of that answer is used here -- which conversations a
    # pane is already showing -- so the rows it builds on the way are dated
    # against this module's own clock, not against the caller's `now` (which is
    # a seam for measuring card freshness and nothing else).
    at = datetime.datetime.now().astimezone()
    represented |= _attach_rows(panes,
                                {d.get("tmux_pane") for d in in_pane},
                                {d.get("session_id") for d in in_pane},
                                raw, at, at.timestamp())[1]
    # The human name of these sessions (daemon jobs, ⑂ forks, the app) is the
    # one Claude Code itself registers: the basename of the cwd may be "claude"
    # and says nothing (a live ⑂ fork was unrecognisable in the menu).
    try:
        names = {e["pid"]: e.get("name") for e in read_registry()
                 if e.get("pid") is not None}
    except Exception:
        names = {}
    for d in raw:
        d.pop("_path", None)
        if d.get("state") == "ended":
            continue
        if d.get("session_id") in represented:
            continue
        pane = d.get("tmux_pane")
        if pane and pane in panes:
            continue  # that one belongs to tmux: the green group lists it
        # A claude that died without saying goodbye (crash, kill -9, closing the
        # app) never gets to send SessionEnd, so its card keeps saying
        # "working": the pid is the proof of life. We do NOT rewrite it to ended
        # here (a reader does not mutate state): the ghost simply is not listed.
        # An old card with no pid -> pid_alive says True and freshness decides.
        if not pid_alive(d.get("pid")):
            continue
        upd = parse_iso(d.get("updated_at"))
        if not upd or (now - upd).total_seconds() > max_age_s:
            continue
        d["_age"] = (now - upd).total_seconds()
        d["_name_cc"] = names.get(d.get("pid"))
        out.append(d)
    return out


def dedup_name(base, existing):
    """foo -> foo / foo#2 / foo#3... depending on clashes with `existing` (a set of names)."""
    if base not in existing:
        return base
    n = 2
    while "%s#%d" % (base, n) in existing:
        n += 1
    return "%s#%d" % (base, n)


def tmux_safe_name(name):
    """The name exactly as tmux would leave it: "." and ":" become "_".

    tmux does NOT reject those characters in a session name, it replaces them
    silently (session_check_name), so a project called "example.com" ends up as
    the session "example_com". We sanitise ourselves so we compare like with
    like.
    """
    return (name or "").replace(".", "_").replace(":", "_")


def pin_session(pin):
    """The tmux session a pin lives in, spelled as tmux would spell it.

    None when the pin cannot be used: `config.json` is edited by hand, and a pin
    that is not an object, or with no session to switch to, is skipped rather
    than becoming a row that does nothing when you press Enter on it.

    It lives here, and not in either of its two callers, because both of them
    have to answer the same: `flightdeck pin add` sanitises on the way IN, and
    the menu reads the file back. With the menu only stripping, a hand-written
    `"session": "my.notes"` had it asking tmux for `my.notes` while tmux had
    made `my_notes` -- `new-session` reported the session as already there and
    the `switch-client` behind it went somewhere else.
    """
    session = pin.get("session") if isinstance(pin, dict) else None
    if not (isinstance(session, str) and session.strip()):
        return None
    return tmux_safe_name(session.strip()) or None
