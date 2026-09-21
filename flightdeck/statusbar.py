"""Flightdeck's status bar: one line for tmux's `status-right`.

`render_status_line` turns data already gathered into text: no tmux, no session
cards, no history walk -- that wiring lives in `gather`, so the rendering can be
tested with fixtures. It is not quite a pure function, and the exception is
worth knowing: it reads `context_warn_pct` from the config on every repaint
(`_ctx_warn()`), which is what lets an edited `config.json` change the 🧠
threshold without restarting the tmux server. A test that pins a threshold has
to pin `FLIGHTDECK_CONFIG` with it.

Waits only: per-account usage is deliberately not on the bar -- the account
tool's own tab and each session's own status line already show it, and
repeating it here was noise. If something "from the network" ever comes back
to the bar, recover the on-disk cache that used to live here
(`state/usage-cache.json`, 60s TTL): the status line is one-shot per repaint
and cannot go out to the network every time.
"""

import sys

from flightdeck import config

# The states of a session that are a claim on the human: the ones we paint in
# yellow. "asking" (an open form, see the state hook) counts: if it is asking
# you, it is waiting for you -- and there the session really is stopped until
# you answer.
WAITING_STATES = ("awaiting_input", "needs_attention", "asking")

# From what % of spent context the bar says in red that a session is running
# out of brain (time for a /compact or a handover). Higher than the menu's
# threshold (50%): the bar is ALWAYS in sight and can only talk about what is
# urgent. This is the fallback; the value in force comes from the config
# (`context_warn_pct`), read on every repaint.
CTX_WARN = config.DEFAULTS["context_warn_pct"]

# How many sessions at the limit get named in the bar. Two already fill the
# width of a phone; past that, the menu is the place to see the whole list.
MAX_CTX_ENTRIES = 2

# Separator between the sections of the bar (waits | full contexts).
_SEP = " | "


def _ctx_warn():
    """The threshold in force, read from the config on every repaint.

    It falls back to the default instead of raising, because the caller is the
    tmux bar: a `context_warn_pct` the user typed as a word would otherwise
    blank the whole line on every repaint, hiding the waits too. `doctor` is
    the place that complains about a value of the wrong type.
    """
    try:
        return int(config.load()["context_warn_pct"])
    except (KeyError, TypeError, ValueError):
        return CTX_WARN


def _escape(text):
    """Escape for the tmux bar, where `#` opens formatting (`#[`, `#{`).

    It is really needed, not hygiene: real names carry a hash (`dedup_name`
    christens foo#2, foo#3...), and a project called `something#[fg=red]` would
    repaint the whole bar.
    """
    return (text or "").replace("#", "##")


def _name(entry):
    """What that session is called on the bar: the tmux name was chosen by the
    human (it is how they know it); the project is only the fallback."""
    return _escape(entry.get("name") or entry.get("project")) or "?"


def _waiting_section(tmux_entries):
    """`⏳ N waiting: a, b, c` in yellow. "" when nobody is waiting."""
    waiting = [e for e in tmux_entries if e.get("state") in WAITING_STATES]
    n = len(waiting)
    if not n:
        return ""
    return "#[fg=yellow]⏳ %d waiting: %s#[default]" % (
        n, ", ".join(_name(e) for e in waiting[:3]))


def _ctx_sections(tmux_entries):
    """One red `🧠 <session> NN%` section per session with a nearly full context.

    One per session (and not one list, like the waits) because the number is
    what matters: "payments is running low" does not say whether it is 81% or
    97%. The tightest ones go first, and only the first MAX_CTX_ENTRIES are
    named.
    """
    warn = _ctx_warn()
    full = [e for e in tmux_entries
            if isinstance(e.get("ctx_pct"), int) and e["ctx_pct"] >= warn]
    full.sort(key=lambda e: -e["ctx_pct"])
    return ["#[fg=red]🧠 %s %d%%#[default]" % (_name(e), e["ctx_pct"])
            for e in full[:MAX_CTX_ENTRIES]]


def render_status_line(entries):
    """One line for the tmux bar: who is waiting for you and who is running out
    of context.

    Only TMUX sessions count (kind "tmux"): the ones living outside tmux cannot
    be attended to from here and the history ones are not even alive. The waits
    go FIRST: a session stopped and waiting for you is more urgent than one
    working with a full context. With nothing to say, an EMPTY line.

    The `#[fg=yellow]…#[default]` are tmux codes (NOT ANSI): the bar itself
    interprets them.
    """
    tmux_entries = [e for e in entries if e.get("kind") == "tmux"]
    sections = [_waiting_section(tmux_entries)] + _ctx_sections(tmux_entries)
    return _SEP.join(s for s in sections if s)


def gather():
    """Gather the entries the bar looks at, and only those.

    `outside` and `recent` go in EMPTY on purpose: the bar only looks at tmux
    sessions (kind "tmux"), so working them out would be work thrown away. And
    it is not cheap work: walking the transcript history is thousands of files
    (~16k `stat` and ~98 MB read), and this runs again on EVERY repaint of the
    bar, every few seconds.

    The pins are NOT empty, though, even if the bar never paints a pinned row:
    they are what tells `build_entries` which tmux sessions are infrastructure.
    Without them a session pinned as `accounts` would be counted here as
    ordinary work, and the bar would be naming a top or a git viewer among the
    sessions waiting for you.
    """
    from flightdeck.common import list_tmux_sessions, load_sessions
    from flightdeck.picker import build_entries, configured_pins
    return build_entries(list_tmux_sessions(), load_sessions(), [], [],
                         pins=configured_pins())


def main(argv=None):
    """`python3 -m flightdeck.statusbar status` -> the line, one line, exit 0."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd != "status":
        sys.stderr.write("usage: python3 -m flightdeck.statusbar status\n")
        return 1
    try:
        sys.stdout.write(render_status_line(gather()) + "\n")
    except Exception:
        # The tmux bar is painted with this: a one-off failure leaves the line
        # blank, never a traceback inside the status-right.
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
