"""Flightdeck's status line: the line the agent paints under its own pane.

This is a port of the pre-release cockpit's `statusline-command.sh` (251 lines
of POSIX sh + jq), which is the reference look for the whole project: tool
glyph, model, folder and branch, a bar of context with its percentage, the rate
limits with their own bars and reset times, and underneath the tokens and the
turns. It replaces `jq`, `awk`, `sed`, `date` and `git` with the standard
library. That is not tidiness: those five are what tied the line to a Mac with
Homebrew, and Flightdeck has to paint the same line on Linux and WSL2.

**That layout, complete, is what everybody gets**: THREE lines, each answering
one question, exactly as the reference printed them.

    line 1  who you are:   glyph, model, effort, account, folder, branch
    line 2  what you have spent: the 15-cell context bar, then every rate-limit
            bucket with its own 10-cell bar and reset time
    line 3  the tokens and the turns

The compressed form -- lines 1 and 2 on one row, with a 10-cell context bar,
and no account segment at all -- is what the two OPT-OUTS bring back:
`"statusline": {"lines": 2}` in config.json restores that layout byte for byte,
and `{"account": false}` takes the address off the identity line. There is no
third layout and no new switch.

Each line is trimmed to the pane's width on its own (`_fit` and one ladder per
line), because they no longer compete with each other for it: line 1 sheds the
branch, then the folder, then the effort, and the account LAST; line 2 sheds the
reset times and then rate-limit buckets from the end, never the context bar;
line 3 can only shed the turn count.

**The thresholds and the colours are measured taste, not convention** (green
under 20% of context, orange from 40, red from 70; a rate limit does not turn
yellow until 50). They come from a line that was read every day for months, and
they are pinned test by test. Do not "round" them.

Three things the reference does are deliberately NOT here, and each for a reason
of its own:

- **The weekly per-model buckets from the `/usage` API.** That meant a keychain
  token, `curl`, a lock and a cache: network inside a status line. What IS
  ported is the dynamic part -- EVERY bucket in `rate_limits` gets painted, with
  its short name -- so the day Claude Code sends those buckets in the payload
  they appear here with no release of ours.
- **The three-second cache of the branch.** It existed because asking git meant
  forking a process on every repaint; reading `.git/HEAD` does not.
- **The dump of every payload to `/tmp/claude-statusline-input.json`.** A
  debugging aid on a predictable, world-readable path, carrying the session id,
  the transcript path and the working directory of every session on the machine.

A fourth, **the account's e-mail address and its plan**, is here and ON: it is
part of the reference line, so it is part of the line Flightdeck paints. The
worry about it -- an address in somebody else's screenshot -- is answered by
`"statusline": {"account": false}`, one key for a shared screen, rather than by
leaving the segment out of everybody's line.
The rules that come with it are part of the feature: the two values are read
afresh on every repaint and are never written down, logged, put on a session
card or spoken in a notice, and every way of failing to read them ends in
"unknown" rather than in an error.

The one rule that overrides everything: **this may never raise**. It runs on
every repaint of every session, and an exception here is a traceback painted
where the status bar should be. Every reader coerces defensively and `main`
catches everything and exits 0.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flightdeck import config
from flightdeck.tools import glyph

# ── colours ──────────────────────────────────────────────────────────────────
# Plain ANSI, no curses and no terminfo: the agent hands this straight to the
# terminal, exactly as the reference's `printf` did.
RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
ORANGE = "\033[38;5;208m"  # 256-colour: there is no 16-colour orange
RED = "\033[31m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BLUE = "\033[34m"       # the reference's colour for the account's address

# ── the line's shape ─────────────────────────────────────────────────────────
BAR_WIDTH = 10          # every rate-limit bucket's bar, in both layouts
# The context bar is WIDER than the rest, as the reference had it: it is the one
# number somebody acts on (a full context means a handover), and two extra cells
# per 10% are what make 88% and 95% look different at a glance. It only fits
# where the context has a line of its own, so the compressed two-line layout
# keeps it at BAR_WIDTH -- there the three bars are side by side and a bar of a
# different length would read as a different scale.
CONTEXT_BAR_WIDTH = 15
FULL_CELL = "█"
EMPTY_CELL = "░"
SEP = " │ "             # the reference's separator
NO_BRANCH = "no branch"  # what git itself said when it could not answer
DIR_MAX = 34            # past this the folder is shortened to `~/…/parent/leaf`
LIMITS_NA = "limits n/a"

# ── the signed-in account (on by default) ────────────────────────────────────
# `👤 user@example.com max`, where the reference put it: straight after the
# model. Switched OFF with `"statusline": {"account": false}` in config.json,
# which is the line to reach for before sharing a screen.
ACCOUNT_MARK = "👤"

# The one character in this line that is not one cell wide. Everything else it
# paints (`█ ░ │ ↻ ✳ ⬡ ✦`) is a single cell, so `_visible_len` counts
# characters; 👤 is an emoji and terminals give it two, and measured short the
# line would wrap in a pane the ladder had just decided it fitted.
WIDE = frozenset(ACCOUNT_MARK)

# Where the two values come from, exactly as the reference read them:
#   the address -> `~/.claude.json`, key `oauthAccount.emailAddress`
#   the plan    -> the JSON Claude Code keeps its credentials in, key
#                  `claudeAiOauth.subscriptionType` (`max`, `pro`, `team`, ...)
# That credentials JSON is a FILE on Linux and a KEYCHAIN ITEM on macOS, and the
# file is tried first on both. Three reasons, in this order: it is the
# cross-platform half; it is under HOME, which is what lets the demo and the
# tests answer with a home of their own and never go near anybody's keychain;
# and it costs 0.5 ms against the keychain's 15 (measured on a Mac with a
# 155 KB `~/.claude.json` and `security` warm).
CREDENTIALS_FILE = (".claude", ".credentials.json")
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT = 2    # seconds; this runs inside a repaint of the bar

# The bands, as `(below this, colour)` pairs, most permissive first; anything
# past the last pair is red. Three different scales on purpose: 55% of context
# is comfortable, 55% of a five-hour limit is not, and 55 turns is nothing.
CONTEXT_BANDS = ((20, GREEN), (40, YELLOW), (70, ORANGE))
RATE_BANDS = ((50, GREEN), (75, YELLOW), (90, ORANGE))
TURN_BANDS = ((60, GREEN), (80, YELLOW), (100, ORANGE))

# The reasoning effort, coloured by how expensive it is to think that hard.
EFFORT_COLOURS = {"max": MAGENTA, "xhigh": MAGENTA, "high": CYAN,
                  "medium": GREEN, "low": DIM}

# What the line sheds, in order, when it does not fit the width it was given.
# The idea: first what can be read somewhere else (the branch is in the shell's
# own prompt, the folder is in the tmux session's name), last what only exists
# here. The context bar is never dropped -- it is the reason the line is painted
# at all -- and neither are claude's rate limits, whose line fits a 100-column
# pane once these rungs are spent.
TRIM_SHARED = ("branch", "resets", "effort", "dir_short", "dir")

# agy's line does NOT fit once they are spent: it is ~106 cells with a long
# model name ("Gemini 3.8 Flash (High)"), two quota buckets and the word the
# agent is doing, and a 100-column pane wrapped six cells of it -- in the demo
# GIF, which is the first thing anybody sees. So agy's ladder carries on where
# claude's stops, in this order: what the agent is DOING goes first (it is one
# word, it changes on its own, and the row in the menu says the same thing), and
# then one quota bucket at a time FROM THE END, so the bucket the line listed
# first is the one that survives. The cost is not a rung: it has never been seen
# in a real payload and it is four characters. What is left at the bottom is the
# model and the context bar. `agy_ladder` builds it, because how many bucket
# rungs there are depends on how many buckets arrived.
TRIM_TAIL_AGY = ("mode", "bucket")

# And under everything, on EVERY line, the account. Somebody who switched that
# segment on did so to see WHICH account is signed in, and for a person with two
# of them that is the one thing on the line they cannot work out from anywhere
# else on screen -- the branch is in their prompt, the folder is in the tmux
# session's name. So it goes only when what is left is the model and the context
# bar, which are never shed at all: on claude's line that means under the
# folder, and on agy's under the folder AND under agy's own rungs, the agent
# state and the quota buckets.
ACCOUNT_RUNG = "account"


def with_account_last(rungs):
    """A trim ladder: those rungs, and the account under all of them.

    The one place that knows where the account is shed, because "the last thing
    the line drops" has to mean the same on both lines. Spelling it into each
    ladder separately is exactly how agy's came to shed the account BEFORE its
    own quota buckets while the documentation said otherwise.
    """
    return tuple(rungs) + (ACCOUNT_RUNG,)


TRIM_LADDER = with_account_last(TRIM_SHARED)

# ── the three-line layout's ladders, one per line ────────────────────────────
# With a line each, the pieces stop competing: line 1 has room to spend rungs
# the compressed line could not afford, and line 2 never has to choose between a
# bucket and the folder. So the shared ladder is SPLIT rather than reused --
# `resets` and the buckets belong to line 2, everything else to line 1 -- and
# each line is fitted on its own by `_fit`.
#
# Line 1's order is the same rule as ever ("first what can be read somewhere
# else, last what only exists here"), applied with the extra room: the branch is
# in the shell's own prompt and the folder is in the tmux session's name, so
# they go before the effort, which is written down nowhere else on screen. The
# account is under all of them (`with_account_last`), which is the one place
# that decides where it goes.
TRIM_IDENTITY = with_account_last(("branch", "dir_short", "dir", "effort"))

# Line 3 can shed exactly one thing. The three token figures ARE the line, and
# the turn count is the piece that is also painted in the menu's row, so it is
# the one that goes -- and only when the line would otherwise wrap.
TRIM_TOKENS = ("turns",)

# How many lines the layout has. `3` is the reference layout, `2` the
# compressed one, and `1` is for callers with a single row to spare.
LINES_DEFAULT = 3
LINES_ALLOWED = (1, 2, 3)


def consumption_ladder(parts):
    """Line 2's ladder: the reset times, agy's agent state, then its buckets.

    Built rather than declared for the same reason `agy_ladder` is: how many
    `bucket` rungs there are depends on how many buckets arrived. claude's line
    gets rungs for its `5h` and `7d` too -- on a 60-column phone the two of them
    plus the bar do not fit, and a bucket half painted is worse than one fewer.

    The context bar is not a rung in it: it is the reason the line is painted.
    """
    buckets = parts.get("buckets") or ()
    quotas = sum(1 for b in buckets if b.get("kind", "quota") == "quota")
    mode = ("mode",) if any(b.get("kind") == "mode" for b in buckets) else ()
    return ("resets",) + mode + ("bucket",) * quotas


# How many turns the demo shows. The demo has no transcript to count (and must
# not write one), and a line with no turns at all would be selling the tool
# short at the very moment the user is deciding whether to use it.
DEMO_TURNS = 37


def _colour_256(number):
    return "\033[38;5;%dm" % number


def tool_colour(tool):
    """The tool's brand colour, or dim when we have no glyph for it."""
    mark = glyph(tool)
    return _colour_256(mark[1]) if mark else DIM


def _band(value, bands):
    for limit, colour in bands:
        if value < limit:
            return colour
    return RED


def context_colour(pct):
    """Green under 20%, yellow under 40, orange under 70, red past it."""
    return _band(_number(pct, 0), CONTEXT_BANDS)


def rate_colour(pct):
    """Green under 50%, yellow under 75, orange under 90, red past it."""
    return _band(_number(pct, 0), RATE_BANDS)


def turns_colour(turns):
    """Green under 60 turns, yellow under 80, orange under 100, red past it."""
    return _band(_number(turns, 0), TURN_BANDS)


def effort_colour(level):
    """An unknown level is dim rather than absent: a new effort name must show
    up in the line, just without a colour of its own."""
    return EFFORT_COLOURS.get(str(level or "").strip().lower(), DIM)


def _number(value, default=None):
    """`value` as a float, or `default`. Never raises.

    Text is accepted because the reference put everything through jq's
    `tostring` and a payload that sends `"42"` must not blank the bar. Booleans
    are not: `True` as a percentage is a payload bug, not 1%.
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default  # NaN and infinity: json.loads accepts both
    return number


def whole_percent(value):
    """A percentage as a whole number, or None when there is not one.

    floor(x + 0.5), which is what the reference's `awk '{printf "%d", $1+0.5}'`
    did and what the tee writes into `<id>.ctx.json`: the number in this line
    and the one in the menu's row have to agree, and they are rounded in two
    different files.
    """
    number = _number(value)
    return None if number is None else int(number + 0.5)


def bar(pct, width=BAR_WIDTH):
    """`████░░░░░░`: the percentage as `width` cells, rounded to the nearest one.

    Clamped to 0-100 (a payload at 103% would otherwise paint a longer bar and
    move every column to its right) and empty when there is no usable number.
    """
    number = _number(pct, 0)
    number = max(0.0, min(100.0, number))
    filled = int((number * width + 50) // 100)
    filled = max(0, min(width, filled))
    return FULL_CELL * filled + EMPTY_CELL * (width - filled)


def format_tokens(count):
    """118 -> `118`, 798298 -> `798.3k`, 1500000 -> `1.5M`."""
    number = _number(count, 0)
    if number >= 1000000:
        return "%.1fM" % (number / 1000000.0)
    if number >= 1000:
        return "%.1fk" % (number / 1000.0)
    return "%d" % number


def pretty_dir(cwd, home=None):
    """The folder as the eye wants it: `~/…/parent/leaf`.

    The home becomes `~`, and a path longer than DIR_MAX keeps only its last two
    names, so line 1 has a stable height (a deep path used to wrap the whole
    line on a phone).

    The home is only replaced at a separator. The reference replaced a bare
    prefix, which turned `/home/username2` into `~name2` -- invisible on most
    machines, wrong on anyone's whose home is a prefix of another.
    """
    if not cwd:
        return ""
    text = str(cwd)
    home = str(Path.home()) if home is None else str(home)
    if home and home.strip(os.sep):  # a home of "/" would tilde every path
        if text == home:
            text = "~"
        elif text.startswith(home.rstrip(os.sep) + os.sep):
            text = "~" + text[len(home.rstrip(os.sep)):]
    if len(text) > DIR_MAX:
        names = [n for n in text.split("/") if n]
        if len(names) >= 2:
            text = "~/…/%s/%s" % (names[-2], names[-1])
    return text


def dir_leaf(pretty):
    """Just the last name of an already prettified folder (`api`).

    The last thing tried before the folder is dropped altogether on a narrow
    terminal: the leaf is the bit that identifies the project.
    """
    names = [n for n in str(pretty or "").split("/") if n and n != "…"]
    return names[-1] if names else ""


def _head_path(cwd):
    """The `HEAD` file that answers for `cwd`, or None when there is no repo.

    Walks up looking for `.git`. A DIRECTORY is the ordinary repo; a FILE is a
    worktree or a submodule and says `gitdir: <path>` -- whose own HEAD is the
    one that answers for this directory, which is the whole point of a worktree.
    """
    if not cwd:
        return None
    try:
        start = Path(str(cwd))
    except (TypeError, ValueError):
        return None
    # Relative paths are refused rather than resolved: this process's working
    # directory is not the session's, so `Path("x").resolve()` would answer with
    # the branch of whatever repo Flightdeck itself was launched from.
    if not start.is_absolute():
        return None

    for directory in [start] + list(start.parents):
        dot_git = directory / ".git"
        try:
            if dot_git.is_dir():
                return dot_git / "HEAD"
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    target = Path(text[len("gitdir:"):].strip())
                    if not target.is_absolute():
                        target = directory / target
                    return target / "HEAD"
        except (OSError, ValueError):
            return None
    return None


def git_branch(cwd):
    """The branch of the repo `cwd` is in, WITHOUT running git.

    `no branch` outside a repo, which is the word the reference printed when
    `git rev-parse` failed.

    A detached HEAD comes out as the short sha (`17bdb2a`). Git itself prints
    the word `HEAD` there (`rev-parse --abbrev-ref`), which names nothing: since
    the branch is being read by hand anyway, the sha is the same length and
    actually says where you are: a deliberate difference from the reference.
    """
    head = _head_path(cwd)
    if head is None:
        return NO_BRANCH
    try:
        text = head.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return NO_BRANCH
    if not text:
        return NO_BRANCH
    if text.startswith("ref:"):
        ref = text[len("ref:"):].strip()
        # `refs/heads/feature/x` is the branch `feature/x`: the prefix goes, the
        # slashes inside the name stay.
        if ref.startswith("refs/heads/"):
            ref = ref[len("refs/heads/"):]
        return ref or NO_BRANCH
    return text[:7]


def bucket_label(key):
    """`five_hour` -> `5h`, `seven_day_opus` -> `7d-opus`, `gemini-weekly` -> `gemini-7d`.

    One rule for every bucket, known or not: the reference listed the four it
    had seen and then fell back to this same substitution, which produces the
    identical answer for all four. A bucket nobody has seen yet is painted under
    its own name rather than hidden.

    `weekly` is agy's word for the same span (its buckets are `3p-weekly` and
    `gemini-weekly`, measured); it shortens to claude's `7d` so the
    two lines say a week the same way.
    """
    text = str(key or "")
    return (text.replace("seven_day", "7d").replace("five_hour", "5h")
            .replace("weekly", "7d").replace("_", "-"))


def epoch_from_iso(text):
    """`2026-09-23T04:55:56Z` -> epoch seconds, or None when it is not a date.

    agy dates its quota resets with an RFC3339 string where claude sends an
    epoch (measured), so this is what lets `format_reset` paint the
    same `↻` for both. A string with no timezone is read as UTC, which is what
    agy sends; the `Z` is swapped for `+00:00` because `fromisoformat` does not
    accept it before python 3.11 and Flightdeck runs on 3.9.
    """
    if not isinstance(text, str):
        return None
    stamp = text.strip()
    if not stamp:
        return None
    if stamp[-1] in "Zz":
        stamp = stamp[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        return when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def format_reset(epoch, key=""):
    """`↻07:50` for the five-hour bucket, `↻17/09 20:00` for the rest.

    In LOCAL time: the person reading is deciding whether to wait for it. The
    five-hour one only needs the time of day; a weekly one that says `20:00`
    without a date would be read as "tonight".
    """
    seconds = _number(epoch)
    if seconds is None or seconds <= 0:
        return ""
    try:
        when = datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return ""
    return when.strftime("%H:%M" if key == "five_hour" else "%d/%m %H:%M")


# The marker of an assistant row in the transcript. The reference counted them
# with `grep -c`, i.e. LINES containing it, and Claude Code writes its JSONL
# without spaces after the colons, so the plain substring is enough.
_ASSISTANT = b'"type":"assistant"'


def count_turns(path):
    """How many turns this conversation has had, or None when it cannot be read.

    One sequential read of the transcript per repaint, which is what the
    reference's `grep` cost. Read as bytes and line by line: transcripts reach
    megabytes, and a tool result with odd bytes in it must not raise here.
    """
    if not path:
        return None
    try:
        with open(str(path), "rb") as handle:
            return sum(1 for line in handle if _ASSISTANT in line)
    except (OSError, TypeError, ValueError):
        return None


def _get(payload, *keys, **kw):
    """`payload["a"]["b"]`, or the default as soon as anything is not a dict."""
    default = kw.get("default")
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def _text(value):
    """A stripped string, or "" for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def _json_file(path):
    """A JSON object out of `path`, or `{}` for every way that can go wrong.

    Missing, unreadable, not JSON, JSON that is not an object: all the same
    answer. Nothing this file reads is worth an exception in a status line.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _current_config():
    """The config in force, degrading to the defaults. Never raises.

    One read, handed to everything in a render that needs it. Two readers each
    calling `config.load()` would mean two file reads per repaint of every
    session, and worse, a config.json edited BETWEEN them could paint a line
    half in one shape and half in the other.
    """
    try:
        return config.load()
    except Exception:
        # `config.load` documents that it never raises, so this is the belt to
        # its braces: the one rule here is that a repaint may not raise.
        return config.DEFAULTS


def account_enabled(cfg=None):
    """Is the `👤` segment switched on? `config.json`'s `statusline.account`.

    The default is `true` and it lives where defaults live, in
    `config.DEFAULTS`: what `config.load()` hands over already says `true` for
    somebody who never wrote the key, which is why the test here is still for
    exactly the boolean. That test is what privacy leans on for the degraded
    case -- a `statusline` section the user typed as something other than an
    object, or an `account` written as the STRING "false", leaves the address
    unpainted rather than guessing. Reaching for the safe side of a value
    nobody can read costs one config key; the other way round costs somebody
    their address in a screen share.

    `cfg` left out is read from the config in force. That is one small file
    read per repaint on top of what the tee already does, and it is what makes
    an edited config.json take effect without restarting a thing.
    """
    try:
        cfg = _current_config() if cfg is None else cfg
        return cfg["statusline"]["account"] is True
    except (KeyError, TypeError, IndexError):
        return False


def configured_lines(cfg=None):
    """How many lines the layout has: `config.json`'s `statusline.lines`.

    `3` (the reference layout) unless the file says `2` or `1`. Anything else --
    a `4`, a word, a `true`, a `statusline` section of the wrong kind, a
    config.json that could not be read at all -- is the default, which is the
    rule the status line modes already follow: a typo leaves the user with the
    line everybody else has, and never with a blank bar.
    """
    try:
        cfg = _current_config() if cfg is None else cfg
        value = cfg["statusline"]["lines"]
    except (KeyError, TypeError, IndexError):
        return LINES_DEFAULT
    number = _number(value)     # `_number` refuses booleans: `true` is not 1 line
    # A whole number and nothing else: `2.5` is not "two lines, roughly", it is
    # a value somebody got wrong, and the default is what a wrong value gets.
    if number is None or number != int(number) or int(number) not in LINES_ALLOWED:
        return LINES_DEFAULT
    return int(number)


def account_email(home=None):
    """The signed-in address from `~/.claude.json`, or "" when there is none."""
    home = Path.home() if home is None else Path(home)
    return _text(_get(_json_file(home / ".claude.json"),
                      "oauthAccount", "emailAddress"))


def _security(argv):
    """Ask the macOS keychain, and hand back what it printed (or "").

    `subprocess` is imported HERE and not at the top of the file. This module
    runs on every repaint of every session, and the keychain is reached on a
    fraction of those: not at all off darwin, not at all with the segment
    switched off, and not at all on a Mac whose credentials file answers. The
    import is deferred to the repaint that actually asks.

    What that repaint pays is ~15 ms (measured on a Mac with `security` warm)
    against the credentials file's 0.5 -- the same price the reference script
    paid for the same segment, every repaint, with no file tried first.
    """
    import subprocess

    done = subprocess.run(argv, capture_output=True, timeout=KEYCHAIN_TIMEOUT)
    if done.returncode != 0:
        return ""       # the item is not in the keychain: rc 44, nothing printed
    return (done.stdout or b"").decode("utf-8", "replace")


def account_plan(home=None, run=None):
    """The subscription word (`max`, `pro`, `team`, ...), or "" when unknown.

    The credentials FILE first and the macOS keychain second (see
    CREDENTIALS_FILE above for why that way round). `run` is the seam: it is
    what asks the keychain, and a test hands in its own so that no test of this
    suite can ever reach the developer's.

    Every failure is the same silent "": no file, no keychain item, a
    `security` that is not there (Linux, where it is not tried at all), one
    that hangs past its timeout, an answer that is not JSON, a key that moved.
    """
    home = Path.home() if home is None else Path(home)
    plan = _text(_get(_json_file(home.joinpath(*CREDENTIALS_FILE)),
                      "claudeAiOauth", "subscriptionType"))
    if plan:
        return plan
    if run is None:
        if sys.platform != "darwin":
            return ""   # `security` is a Mac program: do not fork for it
        run = _security
    try:
        answer = run(["security", "find-generic-password",
                      "-s", KEYCHAIN_SERVICE, "-w"])
    except Exception:
        # Broad on purpose, and the only broad catch in this file: it wraps
        # somebody else's program (a missing binary, a timeout, a kill) and the
        # one rule here is that a repaint may not raise.
        return ""
    try:
        data = json.loads(answer)
    except (TypeError, ValueError):
        return ""
    # `_get` answers with the default as soon as anything is not a dict, so a
    # keychain holding a list or a bare string needs no guard of its own.
    return _text(_get(data, "claudeAiOauth", "subscriptionType"))


def claude_account(cfg=None, home=None, run=None):
    """What claude's line paints after the model, or None.

    None both when the segment is off and when there is no address: an empty
    `👤` says nothing and costs a separator. The switch is looked at FIRST, so
    with the segment off nothing at all is read -- no file, no keychain.
    """
    if not account_enabled(cfg):
        return None
    email = account_email(home)
    if not email:
        return None
    return {"email": email, "plan": account_plan(home, run)}


def agy_account(payload, cfg=None):
    """The same segment for agy, whose payload carries both values itself.

    agy sends `email` and `plan_tier` on every repaint (measured in 1.1.28), so
    there is no file and no keychain on this side -- but it is the SAME switch.
    One option called `statusline.account` that answered in one tool's line and
    not in the other's would read as a bug in whichever one stayed quiet.
    """
    if not account_enabled(cfg):
        return None
    payload = payload if isinstance(payload, dict) else {}
    email = _text(payload.get("email"))
    if not email:
        return None
    return {"email": email, "plan": _text(payload.get("plan_tier"))}


def account_text(account):
    """`👤 user@example.com max`, in the reference's own colours.

    The mark and the address in blue, the plan dimmed beside them, both inside
    one segment. "" when there is no address, which is what leaves the segment
    and its separator out of the line altogether.
    """
    account = account if isinstance(account, dict) else {}
    email = _text(account.get("email"))
    if not email:
        return ""
    plan = _text(account.get("plan"))
    return ("%s%s %s%s" % (BLUE, ACCOUNT_MARK, email, RESET)
            + (" %s%s%s" % (DIM, plan, RESET) if plan else ""))


def _visible_len(text):
    """The width on screen: the escape sequences do not take up a cell.

    Every character the line paints (`█ ░ │ ↻ ✳ …`) is one cell wide except the
    account's `👤`, which is an emoji and gets two (see WIDE), so after dropping
    the escapes this is the width.
    """
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            end = text.find("m", i)
            if end == -1:
                break
            i = end + 1
            continue
        out += 2 if text[i] in WIDE else 1
        i += 1
    return out


def terminal_width(environ=None):
    """How wide the pane Claude Code is painting in is, or None.

    Claude Code sends the status line command no width of its own: there is
    nothing in its payload answering to agy's `terminal_width` (checked against
    the JSON schema its own binary documents, 2.1.274, and against a real
    payload). The `terminal_width_bucket` that turns up elsewhere in that binary
    is analytics and not a width to paint to -- do not reach for it. What Claude
    Code does do is export `COLUMNS`, taken from `process.stdout.columns`, into
    the environment of every command it launches -- the status line among them,
    because in the binary it goes through the same runner as the hooks, the one
    that also sets `CLAUDE_PROJECT_DIR`. So this is a MEASUREMENT of the pane
    and not a default of ours.

    `shutil.get_terminal_size()` is not an alternative and must not be tried:
    this process's stdout is the pipe back to the agent, so it would answer with
    its own 80-column fallback on every repaint.

    None when there is no usable number, and then the line is left whole -- what
    it did before it could ask. That is the case when Claude Code's own stdout
    is not a terminal. A guessed default would be worse than nothing: it would
    hide segments on a pane with room to show them.
    """
    environ = os.environ if environ is None else environ
    width = _number(environ.get("COLUMNS"))
    # A width under one cell is not a width: 0, a negative, and the `0` a shell
    # leaves in COLUMNS when it cannot size the terminal all mean "do not trim".
    return int(width) if width is not None and width >= 1 else None


def agy_width(payload):
    """The width agy's line fits itself to: its payload's, else `COLUMNS`.

    agy IS the tool that says how wide its pane is -- `terminal_width` was
    measured in a real payload (1.1.28) -- so that number wins whenever it is a
    usable one. The fallback exists for a payload nobody's pane produced: the
    `--demo` line is built here, carries no width, and came out at 178 cells,
    which the terminal then wrapped. `terminal_width` is the same reader
    `render_claude` uses, so the two lines answer to the same environment.

    None when neither says anything, and then nothing is trimmed -- what agy's
    line did before, and still the answer when a payload arrives without the key
    outside a terminal.
    """
    payload = payload if isinstance(payload, dict) else {}
    width = _number(payload.get("terminal_width"))
    # The same floor as `terminal_width`: a width under one cell is not a width,
    # and agy sending 0 means it cannot size its pane either.
    if width is not None and width >= 1:
        return int(width)
    return terminal_width()


def _model_name(payload):
    return str(_get(payload, "model", "display_name")
               or _get(payload, "model", "id") or "?")


def _buckets(payload):
    """One painted segment per rate-limit bucket, in the payload's own order.

    Two versions of each: `full` with the reset time and `short` without, which
    is what a narrow terminal gets.
    """
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    out = []
    for key, value in limits.items():
        if not isinstance(value, dict):
            continue
        # A bucket with no percentage is painted at 0%, as the reference did:
        # the one thing worse than a wrong-looking 0% is a limit that silently
        # disappears from the line.
        pct = whole_percent(value.get("used_percentage")) or 0
        body = "%s %s%s%s %d%%" % (bucket_label(key), rate_colour(pct),
                                   bar(pct), RESET, pct)
        reset = format_reset(value.get("resets_at"), key)
        out.append({"kind": "quota", "short": body,
                    "full": body + (" %s↻%s%s" % (DIM, reset, RESET) if reset else "")})
    return out


# agy's own state, in the slot where claude paints its reasoning effort's
# neighbours. `working` earns a colour because it is the one that changes while
# you watch; anything else (a state agy adds later) is dim rather than absent.
AGENT_STATE_COLOURS = {"working": GREEN, "idle": DIM}


def _quota_segments(payload):
    """agy's quota buckets, painted like claude's rate limits.

    `remaining_fraction` is what is LEFT (0 to 1) and the bar shows what is
    SPENT, the way claude's `used_percentage` does: two status lines reading in
    opposite directions would be worse than one of them missing.

    A bucket with no fraction is left OUT, where claude's are painted at 0%:
    there a bucket without its percentage is still a limit that exists, here the
    fraction is the only thing the bucket says.
    """
    quota = payload.get("quota")
    if not isinstance(quota, dict):
        return []
    out = []
    for key, value in quota.items():
        if not isinstance(value, dict):
            continue
        fraction = _number(value.get("remaining_fraction"))
        if fraction is None:
            continue
        pct = whole_percent((1 - max(0.0, min(1.0, fraction))) * 100)
        body = "%s %s%s%s %d%%" % (bucket_label(key), rate_colour(pct),
                                   bar(pct), RESET, pct)
        reset = format_reset(epoch_from_iso(value.get("reset_time")), key)
        out.append({"kind": "quota", "short": body,
                    "full": body + (" %s↻%s%s" % (DIM, reset, RESET) if reset else "")})
    return out


def _mode_segments(payload):
    """What agy is doing (`idle`/`working`) and whether it is boxed in.

    The sandbox shows only when it is ON: a cell spent saying "sandbox: no" on
    every repaint buys nothing, and the line is already competing for width.
    """
    out = []
    state = payload.get("agent_state")
    if isinstance(state, str) and state.strip():
        text = state.strip()
        body = "%s%s%s" % (AGENT_STATE_COLOURS.get(text.lower(), DIM), text, RESET)
        out.append({"kind": "mode", "short": body, "full": body})
    if _get(payload, "sandbox", "enabled") is True:
        body = "%ssandbox%s" % (CYAN, RESET)
        out.append({"kind": "mode", "short": body, "full": body})
    return out


def _cost_segments(payload):
    """What this conversation has cost, when agy says.

    NEVER MEASURED: nothing came through on the plan the payload was captured
    on (an Antigravity Starter Quota), and the field is `omitempty` in
    agy's own struct tags, where its type carries an `estimated`. So both shapes
    the binary suggests are accepted -- a bare number and an object -- and
    anything else paints nothing at all rather than a figure about money that
    might be wrong.
    """
    cost = payload.get("cost")
    number = None
    if isinstance(cost, dict):
        for key in ("estimated", "total", "amount"):
            number = _number(cost.get(key))
            if number is not None:
                break
    else:
        number = _number(cost)
    if number is None:
        return []
    body = "%s$%.2f%s" % (DIM, number, RESET)
    return [{"kind": "cost", "short": body, "full": body}]


def _limits(payload, tool):
    """The limits slot: claude's rate buckets, or agy's quota + state + cost."""
    if tool == "agy":
        return (_quota_segments(payload) + _mode_segments(payload)
                + _cost_segments(payload))
    return _buckets(payload)


def _effort_level(payload, tool):
    """Where each tool keeps the reasoning effort.

    claude puts it in `effort.level`, agy in `model.effort` (measured). agy's
    model name already ends in `(High)`, so its line says the word twice; the
    slot is kept anyway, because it is the one the eye reads for the colour and
    a future model name may not carry the effort in it.
    """
    if tool == "agy":
        return _get(payload, "model", "effort")
    return _get(payload, "effort", "level")


def _context_pct(payload):
    """The context %, from `used_percentage` or from what is left of it.

    agy sends both halves and claude only the first, so the fallback costs
    nothing and keeps the bar alive if a version ever sends only the remainder.
    """
    pct = whole_percent(_get(payload, "context_window", "used_percentage"))
    if pct is not None:
        return pct
    remaining = _number(_get(payload, "context_window", "remaining_percentage"))
    return None if remaining is None else whole_percent(100 - remaining)


def _parts(payload, turns, tool="claude", account=None, ctx_cells=BAR_WIDTH):
    """Every piece the line can show, on its own, ready to be assembled.

    Split like this so `columns` can leave pieces out without any of them being
    rendered twice. `tool` changes only two things -- where the effort lives and
    what goes in the limits slot -- which is the whole distance between the
    claude line and the agy one.

    `account` is `{"email": ..., "plan": ...}` and comes from the caller
    already looked up, because where it is looked up is the one thing that does
    differ per tool: claude's is a file plus a keychain, agy's is in its own
    payload. None, or an account with no address, is no segment.

    `ctx_cells` is how wide the context bar is: `CONTEXT_BAR_WIDTH` where it has
    a line to itself, `BAR_WIDTH` where it stands beside the rate limits. It is
    decided HERE, once, because the bar is painted in two places (a percentage
    and an `n/a`) and two widths that could disagree is one width too many.
    """
    mark = glyph(tool)
    head = "%s%s %s%s" % (tool_colour(tool), mark[0] if mark else "", _model_name(payload), RESET)

    level = _effort_level(payload, tool)
    # The reference printed `· ?` when the payload carried no effort. Here the
    # bit is simply left out: `?` is noise in a line read at a glance.
    effort = (" %s· %s%s" % (effort_colour(level), level, RESET)) if level else ""

    folder = pretty_dir(payload.get("cwd"))
    pct = _context_pct(payload)
    if pct is None:
        context = "Ctx %s%s%s n/a" % (DIM, EMPTY_CELL * ctx_cells, RESET)
    else:
        context = "Ctx %s%s%s %d%%" % (context_colour(pct), bar(pct, ctx_cells),
                                       RESET, pct)

    return {"head": head, "effort": effort, "account": account_text(account),
            "dir": folder,
            "dir_leaf": dir_leaf(folder), "branch": git_branch(payload.get("cwd")),
            "context": context, "buckets": _limits(payload, tool), "turns": turns}


def _kept_limits(buckets, drop):
    """The limit segments that survive `drop`, in order.

    Two rungs act here, and only agy's ladder carries them. `mode` takes out
    what the agent is doing (`working` / `idle`, and the sandbox marker beside
    it); each `bucket` takes out one more quota bucket FROM THE END, so
    `gemini-7d` goes before `3p-7d` and what survives is the bucket the line
    listed first. A segment with no `kind` is treated as a quota bucket, which
    is what every one of them was before the rungs existed.
    """
    drops = list(drop)
    kept = [b for b in buckets
            if not ("mode" in drops and b.get("kind") == "mode")]
    for _ in range(drops.count("bucket")):
        for index in range(len(kept) - 1, -1, -1):
            if kept[index].get("kind", "quota") == "quota":
                del kept[index]
                break
        else:
            break   # nothing of that kind left to shed
    return kept


def _identity_segments(parts, drop=()):
    """Who you are: glyph, model, effort, account, folder, branch."""
    segments = [parts["head"] + ("" if "effort" in drop else parts["effort"])]
    # Straight after the model, where the reference put it.
    if parts.get("account") and "account" not in drop:
        segments.append(parts["account"])
    if parts["dir"] and "dir" not in drop:
        folder = parts["dir_leaf"] if "dir_short" in drop else parts["dir"]
        segments.append("%s%s%s" % (DIM, folder, RESET))
    if parts["branch"] and "branch" not in drop:
        segments.append(parts["branch"])
    return segments


def _consumption_segments(parts, drop=()):
    """What you have spent: the context bar, then the limits."""
    segments = [parts["context"]]
    if parts["buckets"]:
        key = "short" if "resets" in drop else "full"
        # The empty case is NOT "limits n/a": that line means the payload
        # carried none, and saying it about limits the ladder has just shed
        # would be telling the user something untrue about their account.
        segments.extend(b[key] for b in _kept_limits(parts["buckets"], drop))
    else:
        segments.append("%s%s%s" % (DIM, LIMITS_NA, RESET))
    return segments


def _identity(parts, drop=()):
    """Line 1 of the three-line layout."""
    return SEP.join(_identity_segments(parts, drop))


def _consumption(parts, drop=()):
    """Line 2 of the three-line layout."""
    return SEP.join(_consumption_segments(parts, drop))


def _assemble(parts, drop=()):
    """The compressed layout's first line: the two of them on one row.

    The same segments in the same order, joined by the same separator, which is
    why the two layouts cannot drift: there is one builder per half and this is
    both of them. What differs is the width of the context bar (see `_parts`)
    and which ladder does the shedding.
    """
    return SEP.join(_identity_segments(parts, drop)
                    + _consumption_segments(parts, drop))


def _token_line(payload, turns, drop=()):
    """The tokens of the message, of the cache and of the session.

    Three numbers that answer three different questions: what this turn cost,
    how much of it was cache (the cheap part) and what the whole conversation
    has spent.
    """
    usage = _get(payload, "context_window", "current_usage", default={})
    usage = usage if isinstance(usage, dict) else {}
    message = _number(usage.get("input_tokens"), 0) + _number(usage.get("output_tokens"), 0)
    cache = (_number(usage.get("cache_creation_input_tokens"), 0)
             + _number(usage.get("cache_read_input_tokens"), 0))
    session = (_number(_get(payload, "context_window", "total_input_tokens"), 0)
               + _number(_get(payload, "context_window", "total_output_tokens"), 0))

    line = "%sTokens: msg %s │ cache %s │ session %s%s" % (
        DIM, format_tokens(message), format_tokens(cache), format_tokens(session), RESET)
    if turns is not None and "turns" not in drop:
        line += " │ turns %s%d%s" % (turns_colour(turns), turns, RESET)
    return line


def render_claude(payload, lines=None, columns=None, turns=None, account=None):
    """The status line for Claude Code, as text with ANSI colours.

    `payload` is the JSON Claude Code sends its status line command on stdin.
    Three lines and a trailing newline, the way the reference printed them:

        ✳ Fable 5.1 · xhigh │ 👤 user@example.com max │ ~/…/claude/flightdeck │ main
        Ctx ████████████░░░ 80% │ 5h ░░░░░░░░░░ 3% ↻07:50 │ 7d █████░░░░░ 54% ↻17/09 20:00
        Tokens: msg 118 │ cache 798.3k │ session 798.4k │ turns 37

    `lines` is how many of them: `3` (the default, and what the config says),
    `2` for the compressed layout the port shipped first -- identity and
    consumption on one row, with a 10-cell context bar -- and `1` for a caller
    with a single row to spare, which gets that same compressed row on its own.
    LEFT OUT (None) it comes from `statusline.lines` in config.json, the way the
    account does, so an edited config takes effect on the next repaint.

    `columns` is the width to fit into: EACH line sheds pieces along its own
    ladder until it does. LEFT OUT (None), the width comes from the `COLUMNS`
    Claude Code exports (`terminal_width`), which is the only place it says how
    wide the pane is -- its payload carries no width the way agy's does, and
    there is no terminal of our own to measure, since the agent pipes this in.
    With neither, the lines are painted whole and the terminal wraps them, which
    is what they did before. A `columns` given as 0 means "do not trim", exactly
    as in `render_agy`: the two renderers have to be swappable for a caller that
    does not know which tool it is holding. `turns` overrides the count read
    from the transcript, which is how the demo shows the segment without writing
    a transcript to disk.

    `account` is the signed-in account the `👤` segment paints,
    `{"email": ..., "plan": ...}`. LEFT OUT (None) it is looked up
    (`claude_account`), which comes to nothing at all if `statusline.account`
    has been switched off in config.json. An account passed in is what the demo
    and the tests use, and it is the only way the segment appears with the
    switch off.

    Every value is read defensively: a payload that has lost a key, or carries a
    string where a number belongs, paints a poorer line, never an exception.
    """
    payload = payload if isinstance(payload, dict) else {}
    # One read of the config for both questions it answers (see
    # `_current_config`), and none at all when the caller has answered them.
    cfg = _current_config() if (lines is None or account is None) else None
    if lines is None:
        lines = configured_lines(cfg)
    if turns is None:
        turns = count_turns(payload.get("transcript_path"))
    if account is None:
        account = claude_account(cfg)

    width = terminal_width() if columns is None else columns
    count = _line_count(lines)
    if count == 3:
        return _three_lines(payload, turns, width, account=account)

    first = _trim(_parts(payload, turns, account=account), width)
    if count == 1:
        return first + "\n"
    return first + "\n" + _token_line(payload, turns) + "\n"


def agy_ladder(parts):
    """agy's ladder: the shared rungs, then one `mode` rung and one `bucket`
    rung per quota bucket that arrived (`TRIM_TAIL_AGY`), and the account under
    all of them (`with_account_last`).

    Built rather than declared because the middle rungs depend on the payload:
    two buckets were measured, a future agy may send three, and a ladder with
    more `bucket` rungs than there are buckets would spend depth on nothing.

    Note the account is NOT where `TRIM_LADDER` has it -- it is after agy's own
    rungs, not before them. That is the point of `with_account_last`: "the last
    thing the line drops" means the same on both lines, so a narrow agy sheds
    what it is doing and its quota buckets while it still says which account is
    signed in.
    """
    quotas = sum(1 for b in parts.get("buckets") or ()
                 if b.get("kind") == "quota")
    return with_account_last(TRIM_SHARED + TRIM_TAIL_AGY[:1]
                             + TRIM_TAIL_AGY[1:] * quotas)


def _fit(build, columns, ladder):
    """One line, shedding pieces along `ladder` until it fits `columns`.

    `build(drop)` paints the line without the pieces `drop` names. The rungs
    accumulate: at depth N the line is built without the first N of them, so
    `drop` is always a PREFIX of the ladder. That is what lets a rung repeat --
    `bucket` appears once per bucket, and `_kept_limits` counts how many of them
    are in `drop` to know how many to shed.

    The one mechanism for every line of every layout, which is what keeps "the
    account is the last thing to go" meaning the same everywhere: the only thing
    that differs between them is which ladder they are handed.
    """
    line = build(())
    width = _number(columns, 0) or 0
    if width <= 0:
        return line
    for depth in range(1, len(ladder) + 1):
        if _visible_len(line) <= width:
            break
        line = build(ladder[:depth])
    return line


def _trim(parts, columns, ladder=TRIM_LADDER):
    """The compressed layout's first line, fitted to `columns`."""
    return _fit(lambda drop: _assemble(parts, drop), columns, ladder)


def _line_count(lines):
    """How many lines were asked for: 1, 2 or 3. Never raises.

    Anything unusable is the default, and anything past 3 is 3: a caller asking
    for more rows than there are has asked for all of them.
    """
    number = _number(lines, LINES_DEFAULT)
    if number is None:
        return LINES_DEFAULT
    return max(1, min(3, int(number)))


def _three_lines(payload, turns, columns, tool="claude", account=None):
    """The reference layout: identity, consumption, tokens -- each fitted alone.

    Three ladders and one `_fit`, which is the whole difference from the
    compressed layout: there the same pieces compete for one row, here every
    line spends only its own width.
    """
    parts = _parts(payload, turns, tool=tool, account=account,
                   ctx_cells=CONTEXT_BAR_WIDTH)
    return "".join(line + "\n" for line in (
        _fit(lambda drop: _identity(parts, drop), columns, TRIM_IDENTITY),
        _fit(lambda drop: _consumption(parts, drop), columns,
             consumption_ladder(parts)),
        _fit(lambda drop: _token_line(payload, turns, drop), columns,
             TRIM_TOKENS)))


def render_agy(payload, lines=None, columns=None, account=None):
    """The status line for agy (Antigravity), as text with ANSI colours.

    The same three lines as `render_claude`, with what agy knows (measured;
    `tests/fixtures/agy_statusline.json` is that captured payload):

        ✦ Gemini 3.8 Flash (High) · high │ 👤 user@example.com Starter │ ~/work/demo │ main
        Ctx ░░░░░░░░░░░░░░░ 2% │ 3p-7d ░░░░░░░░░░ 0% ↻23/09 04:55 │ gemini-7d █░░░░░░░░░ 6% ↻23/09 04:55 │ idle
        Tokens: msg 18.2k │ cache 0 │ session 25.7k

    The context bar needs no conversion: agy sends claude's own
    `context_window.used_percentage`. In the slot where claude paints its rate
    limits go agy's quota buckets, what it is doing, and the cost when there is
    one. The last line is the tokens and NOT the turns -- agy's transcript is a
    diary of steps with no message rows to count, and the path it sends for it
    does not even exist in 1.1.28.

    `lines` is `render_claude`'s: 3 by default, 2 for the compressed layout, 1
    for its first row alone, and None to ask the config.

    `columns` is the width to fit into; LEFT OUT (None), the payload's own
    `terminal_width` is used, and failing that the `COLUMNS` of the environment
    (`agy_width`). agy is the only tool that says how wide its pane is, so its
    own number wins; the fallback is for a payload built by hand, which is what
    `--demo` does -- there the compressed line came out at 178 cells and the
    terminal wrapped it, in the one place whose whole job is to show somebody
    what the line looks like. A `columns` given as 0 means "do not trim",
    exactly as in `render_claude`: the two renderers have to be swappable for a
    caller that does not know which tool it is holding.

    `account` is the `👤` segment's data. LEFT OUT (None) it comes from agy's
    own payload (`email` and `plan_tier`) unless `statusline.account` has been
    switched off -- the same switch as claude's line, because one option that
    answered in one tool and not in the other would read as a bug in whichever
    one stayed quiet.

    What is deliberately NOT painted is the `conversation_title`, for the same
    reason claude's `session_name` is not: the tmux session already names the
    session, and the identity line has an account on it now.
    """
    payload = payload if isinstance(payload, dict) else {}
    cfg = _current_config() if (lines is None or account is None) else None
    if lines is None:
        lines = configured_lines(cfg)
    if account is None:
        account = agy_account(payload, cfg)

    width = agy_width(payload) if columns is None else columns
    count = _line_count(lines)
    if count == 3:
        return _three_lines(payload, None, width, tool="agy", account=account)

    parts = _parts(payload, None, tool="agy", account=account)
    first = _trim(parts, width, ladder=agy_ladder(parts))
    if count == 1:
        return first + "\n"
    return first + "\n" + _token_line(payload, None) + "\n"


def demo_payload_claude():
    """A payload with realistic values, for the installer's demo.

    The numbers are the ones a session in the middle of an afternoon has: enough
    context spent to have colour in the bar, a five-hour limit a third gone and
    resetting in a couple of hours, a weekly one barely started. The reset times
    are relative to NOW, so the demo never shows a limit that reset yesterday.

    The folder is the REAL working directory (and so is the branch read from
    it): the demo is shown while the installer is asking whether to use this
    line, and an invented `~/projects/demo` would answer `no branch` -- which
    reads as a fault in the tool the user is deciding about. Here they see their
    own folder, which is the truest possible preview.
    """
    now = int(time.time())
    try:
        here = os.getcwd()
    except OSError:  # the working directory was deleted underneath us
        here = str(Path.home())
    return {
        "session_id": "demo",
        "cwd": here,
        "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"},
        "effort": {"level": "high"},
        "version": "2.1.270",
        "context_window": {
            "used_percentage": 42,
            "context_window_size": 200000,
            "total_input_tokens": 486300,
            "total_output_tokens": 21450,
            "current_usage": {
                "input_tokens": 1240,
                "output_tokens": 860,
                "cache_creation_input_tokens": 18300,
                "cache_read_input_tokens": 143700,
            },
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 31, "resets_at": now + 2 * 3600},
            "seven_day": {"used_percentage": 12, "resets_at": now + 3 * 86400},
        },
    }


def demo_payload_agy():
    """agy's demo payload, in the shape measured from a real agy.

    Same idea as claude's: the real working directory (so the branch is real
    too), quota resets in the future rather than dated last week, and a context
    far enough along to have colour in the bar.

    No `cost` key, on purpose. Its inner shape was never measured -- nothing
    came through on the plan the capture was made on -- and the demo is what
    someone is shown while deciding whether to use this line: a segment invented
    here would be a promise the real line might not keep.
    """
    try:
        here = os.getcwd()
    except OSError:  # the working directory was deleted underneath us
        here = str(Path.home())
    reset = (datetime.now(timezone.utc)
             + timedelta(days=4, hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "session_id": "demo",
        "conversation_id": "demo",
        "cwd": here,
        "model": {"id": "Gemini 3.8 Flash (High)",
                  "display_name": "Gemini 3.8 Flash (High)",
                  "effort": "high"},
        "workspace": {"current_dir": here, "project_dir": here},
        "version": "1.1.28",
        "product": "antigravity",
        "context_window": {
            "total_input_tokens": 372100,
            "total_output_tokens": 18400,
            "context_window_size": 1048576,
            "used_percentage": 38.2,
            "remaining_percentage": 61.8,
            "current_usage": {
                "input_tokens": 2100,
                "output_tokens": 640,
                # agy reported no cache at all in the measurement; a lively
                # number here would be a nicer demo and a false one.
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
        "quota": {
            "3p-weekly": {"remaining_fraction": 0.88, "reset_time": reset},
            "gemini-weekly": {"remaining_fraction": 0.46, "reset_time": reset},
        },
        "agent_state": "working",
        "vcs": {"type": "git"},
        "sandbox": {"enabled": False},
    }


DEMO_CAPTION = "Flightdeck status line — Claude Code:"
DEMO_CAPTION_AGY = "Flightdeck status line — Antigravity (agy):"

# The account the demo shows. An address that belongs to nobody (example.com is
# the documentation domain, RFC 2606) and a plan word in place of the real one,
# so the two lines are the same length they were and nothing anybody has to
# read is a real account. The SAME one on both lines on purpose: it is one
# switch, and a picture where agy's line was missing the segment would advertise
# a difference that is not there.
DEMO_ACCOUNT = {"email": "user@example.com", "plan": "max"}
DEMO_ACCOUNT_NOTE = ('%s👤 is on: put "statusline": {"account": false} in '
                     'config.json to leave it out%s' % (DIM, RESET))

USAGE = ("usage: python3 -m flightdeck.statusline claude|agy   (the payload on stdin)\n"
         "       python3 -m flightdeck.statusline --demo\n"
         "       python3 -m flightdeck.statusline --mode own|wrap|stack [--tool claude|agy]\n"
         "       python3 -m flightdeck.statusline --restore [--tool claude|agy]\n"
         "       python3 -m flightdeck.statusline --status [--tool claude|agy]\n")

# The tools this command can paint a line for. codex is not one of them: it has
# no external status line command at all, only its own built-in items, which is
# why its installer writes a list of those instead.
TOOLS = ("claude", "agy")


def _read_stdin():
    """Everything on stdin, as text. Works with a real stdin and with a fake one."""
    try:
        data = sys.stdin.buffer.read()
    except (AttributeError, ValueError):
        data = sys.stdin.read()
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def _demo(tools=TOOLS):
    """One line per tool, each under its own caption.

    `--demo` shows both and not just claude's: the installer asks about the two
    tools, and the agy line is the one nobody expects to exist (its context %
    had no source until this status line hook). `flightdeck install` narrows it
    to what the machine has, since a line for a tool nobody has installed is a
    picture of nothing they can use.

    Both the LAYOUT and the account are pinned here rather than read from the
    config: this is the line Flightdeck ships, shown to somebody who is being
    asked whether they want it, and a preview that quietly matched a config they
    have not agreed to yet would be a preview of the wrong thing. The account is
    `DEMO_ACCOUNT`, from an address that belongs to nobody, and the line
    underneath says how to take the segment off a shared screen.
    """
    if "claude" in tools:
        sys.stdout.write(DEMO_CAPTION + "\n")
        sys.stdout.write(render_claude(demo_payload_claude(), lines=3,
                                       turns=DEMO_TURNS, account=DEMO_ACCOUNT))
    if "agy" in tools:
        sys.stdout.write(DEMO_CAPTION_AGY + "\n")
        sys.stdout.write(render_agy(demo_payload_agy(), lines=3,
                                    account=DEMO_ACCOUNT))
    sys.stdout.write(DEMO_ACCOUNT_NOTE + "\n")
    return 0


# The flags that manage the line instead of painting it. They live here because
# this is the command the user already knows (`flightdeck statusline ...`), but
# they touch the installer's files, not the bar.
MANAGING = ("--mode", "--restore", "--status")


def _installer(tool):
    """That tool's installer, imported only when a flag needs it.

    Not at the top of the module: this file runs on every repaint of every
    session and has no business importing the code that writes settings files.
    """
    if tool == "agy":
        from flightdeck.install import agy
        return agy
    from flightdeck.install import claude
    return claude


def _tool_argument(argv):
    """Pull `--tool <name>` out of the words. -> (the rest, the tool or None).

    None means they asked for a tool there is no line for, and the caller
    complains. Absent, it is claude: that is the tool whose flags these were
    before agy had a status line of its own, and the one most people mean.
    """
    rest, tool = [], "claude"
    skip = False
    for i, word in enumerate(argv):
        if skip:
            skip = False
            continue
        if word.startswith("--tool="):
            tool = word.split("=", 1)[1]
        elif word == "--tool":
            tool = argv[i + 1] if i + 1 < len(argv) else ""
            skip = True
        else:
            rest.append(word)
    return rest, (tool if tool in TOOLS else None)


def _mode_argument(argv):
    """The mode asked for on the command line. -> the word, or None to complain."""
    if argv[0].startswith("--mode="):
        return argv[0].split("=", 1)[1]
    return argv[1] if len(argv) > 1 else None


def _manage(argv):
    """`--mode`, `--restore` and `--status`, for claude or for `--tool agy`.

    -> the exit code. Exit codes follow `flightdeck pin`: 2 when the words do
    not parse (no mode, a mode or a tool that does not exist), 1 when they
    parsed and it could not be done (a config.json or a settings file that
    cannot be read, a disk that will not take the write). One line, never a
    traceback: this is a terminal command.

    Both tools go through the same three flags because their modes mean the same
    three things. What differs is underneath: for agy, `set_mode` also writes
    `stack_with_default` into agy's own settings, since there half the mode
    lives in a file that is not ours.
    """
    argv, tool = _tool_argument(argv)
    if tool is None or not argv:
        sys.stderr.write(USAGE)
        return 2
    installer = _installer(tool)
    command = argv[0]
    try:
        if command == "--restore":
            installer.restore()
        elif command == "--status":
            # Everything is read BEFORE anything is printed: asking whether ours
            # is registered reads the tool's settings and can refuse, and half a
            # report followed by an error line is worse than the error line on
            # its own.
            mode = installer.configured_mode()
            saved = installer.saved_delegate()
            registered = (installer.tee_registered() if tool == "claude"
                          else installer.status_line_registered())
            what = "tee" if tool == "claude" else "status line"
            sys.stdout.write("mode: %s — %s\n" % (mode, installer.MODE_HELP[mode]))
            sys.stdout.write("your own status line, saved: %s\n" % (saved or "—"))
            sys.stdout.write("Flightdeck's %s registered: %s\n"
                             % (what, "yes" if registered else "no"))
        else:
            mode = _mode_argument(argv)
            if mode is None:
                sys.stderr.write(USAGE)
                return 2
            installer.set_mode(mode)
            sys.stdout.write("status line mode: %s — %s\n"
                             % (mode, installer.MODE_HELP[mode]))
    except ValueError as exc:      # a mode that does not exist
        sys.stderr.write("%s\n" % exc)
        return 2
    except config.ConfigError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    except OSError as exc:
        sys.stderr.write("cannot write the status line setting: %s\n" % exc)
        return 1
    return 0


def main(argv=None):
    """`python3 -m flightdeck.statusline claude|agy|--demo|--mode|--restore|--status`.

    A TOOL (`claude`, `agy`) reads that tool's payload from stdin and prints its
    line. It exits 0 and prints an empty line whatever arrives -- rubbish on
    stdin, a payload with no keys, a bug of ours: the agent paints this where the
    status bar goes, so the worst allowed outcome is a blank bar for one repaint.

    The flags are the opposite kind of command: they are read by a person, so
    they say what went wrong and exit non-zero. They manage claude unless
    `--tool agy` says otherwise.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""

    if command == "--demo":
        return _demo()

    if command in MANAGING or command.startswith("--mode="):
        return _manage(argv)

    if command not in TOOLS:
        sys.stderr.write(USAGE)
        return 2

    try:
        payload = json.loads(_read_stdin())
        # Valid JSON that is not an object (`[1,2,3]`, `null`) is not a payload:
        # it is whatever else got piped in, and the honest answer is a blank line.
        if not isinstance(payload, dict):
            text = ""
        elif command == "agy":
            text = render_agy(payload)
        else:
            text = render_claude(payload)
    except Exception:
        text = ""
    sys.stdout.write(text if text else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
