#!/usr/bin/env python3
"""The status line tee: it watches the context % go past.

The agent runs its status line command on EVERY repaint and sends it a JSON on
stdin carrying, among other things, the session_id and
`context_window.used_percentage`. This script sits in the middle: it notes the
percentage down in `<state>/sessions/<id>.ctx.json` (so Flightdeck knows how much
brain each session has left), paints the line, and ONLY THEN sends the tmux bar
the notice of the first time that session crosses 80%, so the handover does not
catch anyone by surprise.

WHICH line gets painted is the user's choice, kept in the config's
`statusline.claude` (see MODES below): Flightdeck's own (`own`, the default),
the one they already had (`wrap` -- run with the SAME stdin, its stdout given
back byte for byte), or both (`stack`). The note and the notice are identical in
the three: they are what the menu and the tmux bar read.

That order is the rule of the house: paint first, side effects afterwards.
Talking to tmux can take a while and the repaint waits for nobody.

Deliberately paranoid: this runs on every render of EVERY session, so any
failure (the disk, an odd JSON, a delegate that blows up) ends in silence and
exit 0. The worst that can happen is that one cycle paints nothing. Not even the
`flightdeck` package is a hard dependency: it is put on the path by locating the
script's own root, and if it cannot be imported at all the tee drops to a
degraded mode (see `main()`) rather than leaving every open session with a blank
bar.

    echo '<json>' | python3 context_tee.py
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Two levels up from this file is the directory that holds the `flightdeck`
# package: the agent launches this by absolute path and sets no PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from flightdeck import config  # noqa: E402  (after the sys.path line, on purpose)
except Exception:  # pragma: no cover - covered by a subprocess test
    # From the moment this tee is installed, Claude Code's status line IS this
    # script. A package that cannot be imported -- a half-written file, an
    # update caught halfway -- would otherwise leave every open session with a
    # blank bar and a traceback under it. `main()` degrades instead.
    config = None

DELEGATE_TIMEOUT = 10


def _state_dir():
    """The state directory, with the package and without it.

    The fallback spells `config.state_dir()`'s rule again on purpose: it is the
    path taken when `flightdeck` cannot be imported at all, and finding the
    user's own status line is the one thing that must still work then.
    """
    if config is not None:
        return config.state_dir()
    override = os.environ.get("FLIGHTDECK_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "flightdeck"
    return Path.home() / ".local" / "state" / "flightdeck"


def _sessions_dir():
    """Where the notes (`<id>.ctx.json`) go: next to the hook's session cards."""
    return _state_dir() / "sessions"


def _delegate_file():
    """The status line command there was before the tee (saved by the installer).

    Missing or empty = there was none.
    """
    return _state_dir() / "delegates" / "statusline"


# The handover notice's thresholds, with hysteresis: it is sung on reaching
# `context_warn_pct` and not sung again until it has come down below
# `context_rearm_pct`. The dead band (75-80 by default) exists because the
# context dances around the edge -- a /compact that frees little, a turn that
# fills it up again -- and without it the same notice would arrive on every
# repaint of the bar.
#
# These two constants are the FALLBACK; the values in force are read from the
# config every time the decision is made, so an edited config.json takes effect
# without restarting anything. Same treatment as the status bar: `doctor` is the
# place that complains about a threshold spelled as a word, and a tee that blew
# up on it would leave the user's status line mute.
#
# With no package at all these stay None and are never read: the degraded mode
# only paints, and the notice is an extra owned by the very code that is
# missing.
CTX_WARN_TEE = config.DEFAULTS["context_warn_pct"] if config else None
CTX_REARM = config.DEFAULTS["context_rearm_pct"] if config else None

# Seconds we give each tmux before giving up on it. The same number as the state
# hook: this runs in the middle of a repaint of the bar.
TMUX_TIMEOUT = 2
# How long the floating notice lasts (`display-message -d`); without `-d` tmux
# uses its display-time (750 ms out of the box: a flash). The FALLBACK; the value
# in force comes from the config's `notice_ms`, the same key the state hook and
# the handover read -- there is a cross-check test on the three.
NOTICE_MS = config.DEFAULTS["notice_ms"] if config else None


def _from_config(key, fallback):
    """One numeric config value, degrading to the fallback.

    Everything this file does is an extra on top of painting the user's status
    line, so a value of the wrong type must never raise here.
    """
    try:
        return int(config.load()[key])
    except (KeyError, TypeError, ValueError):
        return fallback


try:
    from flightdeck.common import SAFE_ID, notice_args, tmux_version
except Exception:  # pragma: no cover - safety net
    # If `flightdeck.common` cannot be imported (a file half-edited, say) the tee
    # STILL has to paint the user's status line: without this copy of the
    # pattern, a broken import would leave the bar mute in every open session.
    SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
    # The notice, on the other hand, is an extra: there is ONE place that knows
    # how to build it for this tmux (`common.notice_args`) and a second copy here
    # is exactly what the port set out to get rid of. Without it the tee paints
    # and stays quiet.
    notice_args = tmux_version = None


def parse_ctx(stdin_json):
    """{"session_id", "pct"} out of the status line's JSON; None when there is none.

    None when the JSON is broken, carries no context_window, or the session_id is
    not usable as a file name (checked BEFORE touching the disk).
    """
    try:
        data = json.loads(stdin_json)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    sid = data.get("session_id")
    if not isinstance(sid, str) or not SAFE_ID.match(sid):
        return None

    window = data.get("context_window")
    if not isinstance(window, dict):
        return None
    try:
        pct = float(window.get("used_percentage"))
        # The rounding goes INSIDE the try on purpose: json.loads accepts NaN and
        # Infinity (invalid JSON, but Python parses them) and with those int()
        # blows up (ValueError / OverflowError). Outside the try, an odd payload
        # would leave the bar blank.
        # The same rounding the agent's own status line uses (floor(x+0.5)) so
        # the number on the note and the one in the bar never disagree.
        whole_pct = int(pct + 0.5)
    except (TypeError, ValueError, OverflowError):
        return None

    return {"session_id": sid, "pct": whole_pct}


def record_ctx(session_id, pct, sess_dir=None, now=None, extra=None):
    """Note pct+at down in `<id>.ctx.json` WITHOUT overwriting what was there.

    Read-merge-write on purpose: the state of the 80% notice ("warned") lives in
    this very file and cannot be lost.

    `extra` (a dict) are additional keys merged into the same write; that is how
    that "warned" gets saved without another function having to know the file's
    format. Leaving it out keeps the usual behaviour.

    Returns the dict EXACTLY as it was left on disk (with the other keys already
    merged in, including the "warned" that was there BEFORE when `extra` is not
    passed), or None when the write failed -- in which case the caller must not
    consider itself saved.
    """
    base = Path(sess_dir) if sess_dir is not None else _sessions_dir()
    f = base / ("%s.ctx.json" % session_id)
    try:
        data = json.loads(f.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    data["pct"] = pct
    data["at"] = time.time() if now is None else now
    if extra:
        data.update(extra)
    try:
        base.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
    except Exception:
        return None  # it did not persist: the caller must not believe it did
    return data


def decide_notice(pct, previous_state):
    """Is it time for the handover notice? Returns (announce, new state).

    Pure apart from reading the configured thresholds: it touches neither the
    disk nor tmux, it only decides. The state is {"warned": bool} and lives
    inside the session's own .ctx.json.

    - It announces ONLY on crossing upwards: pct >= the warn threshold while not
      warned. Staying above (82 -> 85) is no longer news.
    - It rearms on dropping below the rearm threshold (a /compact or a real
      handover), and dropping does NOT announce: the notice is for when the brain
      fills up.
    - With no usable pct (an odd payload) there is no decision: the state comes
      out EXACTLY as it went in, so nothing is rearmed or marked by accident.

    "warned" counts as warned only if it is exactly True: anything else (missing,
    null, a file half-written) is treated as "not warned". The bias is towards
    one notice too many -- which corrects itself, because the good state is saved
    right afterwards -- and against eternal silence.
    """
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return False, previous_state

    previous = previous_state if isinstance(previous_state, dict) else {}
    warned = previous.get("warned") is True

    if pct >= _from_config("context_warn_pct", CTX_WARN_TEE) and not warned:
        return True, {"warned": True}
    if pct < _from_config("context_rearm_pct", CTX_REARM) and warned:
        return False, {"warned": False}
    return False, {"warned": warned}


def name_for_notice(session_id, cwd=None, sess_dir=None):
    """How to name that session in the notice.

    In order: the TMUX SESSION NAME the hook wrote on its card (it is the one
    read in the status bar and in the menu's row, so it is the one recognised
    without thinking), the "project" of that same card, the working directory
    from the payload, and last the start of the id.

    Defensive reading from end to end: the card may not exist yet or may be
    half-written, the id is checked before the path is built (it arrives clean
    from parse_ctx, but this function cannot trust its caller either) and the
    last resort copes with a session_id that is not text.
    """
    base = Path(sess_dir) if sess_dir is not None else _sessions_dir()
    if session_id and SAFE_ID.match(str(session_id)):
        try:
            d = json.loads((base / ("%s.json" % session_id)).read_text())
            for key in ("tmux_session", "project"):
                value = d.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:
            pass
    if isinstance(cwd, str) and cwd.strip():
        name = Path(cwd.strip()).name
        if name:
            return name
    return str(session_id)[:8] if session_id else "session"


def payload_cwd(stdin_json):
    """The cwd carried by the status line's JSON, or None.

    It is separate from parse_ctx on purpose: parse_ctx runs on EVERY repaint and
    its result is what the % note uses; this is only needed on the (rare) day the
    session has to be named in a notice.
    """
    try:
        d = json.loads(stdin_json)
        cwd = d.get("cwd")
    except Exception:
        return None
    return cwd if isinstance(cwd, str) else None


def tmux_bin():
    sock = config.tmux_socket() if config else os.environ.get("FLIGHTDECK_TMUX_SOCKET")
    return ["tmux"] + (["-L", sock] if sock else [])


def notify_tmux(text):
    """Put `text` in the status bar of EVERY tmux client. Silent.

    Same pattern as the state hook: with no tmux server, `list-clients` fails and
    nothing at all happens here. The argv comes from `common.notice_args`, which
    is what knows whether this tmux can print the text literally (`-l`, from 3.4)
    or whether the `#` has to be escaped instead -- without one or the other, a
    project called `#(something)` would be a command that gets run.
    """
    duration = _from_config("notice_ms", NOTICE_MS)
    try:
        r = subprocess.run(tmux_bin() + ["list-clients", "-F", "#{client_name}"],
                           capture_output=True, text=True, timeout=TMUX_TIMEOUT)
    except Exception:
        return
    if r.returncode != 0:
        return
    clients = (r.stdout or "").split()
    if not clients:
        return
    # Asked for once there is somebody to tell: this runs on every repaint of
    # every session, and a `tmux -V` nobody reads is a subprocess too many.
    version = tmux_version()
    for client in clients:
        try:
            # -C: the pane keeps painting while the notice lasts (without it tmux
            # freezes that client until the message goes -- measured in 3.6a).
            subprocess.run(tmux_bin() + notice_args(text, version, duration,
                                                    client),
                           capture_output=True, timeout=TMUX_TIMEOUT)
        except Exception:
            # A client that has just left cannot silence the notice for the rest.
            pass


def prepare_notice(session_id, pct, previous_ctx, stdin_json):
    """Decide, save the state and return the notice's text (or None).

    It does NOT talk to tmux: it only leaves the message ready. Sending it is the
    last thing the tee does, once the user's status line is already painted.

    `previous_ctx` is the .ctx.json exactly as it has just been left on disk: its
    "warned" is the one from BEFORE this render, because record_ctx does not
    touch it.

    The order in here is deliberate: the new state is saved first and only if
    that worked is the notice considered good. The other way round, a disk
    failure would turn the "once per crossing" notice into one per repaint --
    shrapnel.
    """
    announce, new = decide_notice(pct, previous_ctx)
    before = isinstance(previous_ctx, dict) and previous_ctx.get("warned") is True
    now = isinstance(new, dict) and new.get("warned") is True
    if now != before:
        if record_ctx(session_id, pct, extra={"warned": now}) is None:
            return None
    if not announce:
        return None
    who = name_for_notice(session_id, cwd=payload_cwd(stdin_json))
    return "🧠 %s at %d%% — start thinking about a handover" % (who, pct)


def read_delegate(path=None):
    """The status line that was there before (the file's first non-empty line)."""
    p = Path(path) if path is not None else _delegate_file()
    try:
        lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
    except Exception:
        return ""
    return lines[0] if lines else ""


# The three ways the user can have this line (config key `statusline.claude`):
#   own   -- Flightdeck's line, and only it
#   wrap  -- the user's own line, which the tee only passes through (and which
#            is what the tee did before there was a choice)
#   stack -- Flightdeck's line, and the user's underneath it
# The note of the % and the 🧠 notice happen in all three: they are what the
# menu and the tmux bar read, and they do not depend on who paints.
MODES = ("own", "wrap", "stack")
DEFAULT_MODE = config.DEFAULTS["statusline"]["claude"] if config else None


def statusline_mode():
    """The configured mode, degrading to the default.

    A mode spelled wrong (or a `statusline` key that is not an object) must not
    leave the bar mute: it falls back to the default and `doctor` is the place
    that complains about the typo.
    """
    try:
        mode = config.load()["statusline"]["claude"]
    except (KeyError, TypeError, IndexError):
        return DEFAULT_MODE
    return mode if mode in MODES else DEFAULT_MODE


def paint_own(raw):
    """Flightdeck's own line for this payload. Silent if anything goes wrong.

    Imported here and not at the top of the file: this hook runs on every
    repaint of every session, and in `wrap` mode the rendering code is never
    needed. It also keeps the tee's one hard dependency (the `config` module)
    where it was -- a half-edited `statusline.py` leaves the bar blank for a
    cycle instead of taking the tee down.
    """
    try:
        from flightdeck.statusline import render_claude
        payload = json.loads(raw.decode("utf-8", "replace"))
        # Valid JSON that is not an object is not a payload: it is whatever else
        # got piped in, and the honest answer is to paint nothing (the same rule
        # `flightdeck statusline <tool>` follows). An EMPTY object is different:
        # that is a payload with no fields, and it paints what little it can.
        if not isinstance(payload, dict):
            return
        text = render_claude(payload)
    except Exception:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def paint_delegate(raw, pct, minimal=True, command=None):
    """The sacred part: the usual status line, with the SAME stdin and its stdout
    byte for byte.

    `minimal` is the fallback when no delegate is configured: a bare `Ctx NN%`,
    which is better than a blank bar. It is off in `stack` mode -- Flightdeck's
    line has just been painted above and already carries the percentage, so the
    fallback there would be a second, poorer copy of it.

    `command` is WHICH command to run; left out, the one saved for Claude Code.
    agy's status line hook passes its own (a user can have a custom line in both
    tools), and this stays the one place that runs somebody else's command.
    """
    delegate = read_delegate() if command is None else command
    if not delegate:
        if minimal:
            sys.stdout.write("Ctx n/a\n" if pct is None else "Ctx %d%%\n" % pct)
        return

    try:
        r = subprocess.run(delegate, shell=True, input=raw,
                           capture_output=True, timeout=DELEGATE_TIMEOUT)
    except Exception:
        # A delegate that hangs or cannot be launched: this cycle paints nothing.
        # The next render (there is one per interaction) tries again.
        return

    try:
        sys.stdout.buffer.write(r.stdout)
        sys.stdout.buffer.flush()
        if r.stderr:
            sys.stderr.buffer.write(r.stderr)
            sys.stderr.buffer.flush()
    except Exception:
        pass


def paint_statusline(raw, pct):
    """Paint whichever line the mode asks for, in the order it asks for it."""
    mode = statusline_mode()
    if mode in ("own", "stack"):
        paint_own(raw)
    if mode in ("wrap", "stack"):
        paint_delegate(raw, pct, minimal=(mode == "wrap"))


def pct_only(raw_text):
    """The context % straight off the payload, for the degraded path.

    `parse_ctx` also insists on a usable session_id, because it is about to
    build a file name out of it. Nothing is written here, so a payload whose id
    is missing or odd still gets its number onto the bar.
    """
    try:
        window = json.loads(raw_text)["context_window"]
        return int(float(window["used_percentage"]) + 0.5)
    except Exception:
        return None


def main():
    raw = sys.stdin.buffer.read()

    if config is None:
        # The package is broken. The user's status line is the sacred part and
        # it still gets painted: their own command, saved in the delegate file,
        # or the minimal `Ctx NN%` when they never had one. The note and the 🧠
        # notice are skipped rather than half-done -- the code that owns them is
        # the missing part, and a note nobody can vouch for would have the menu
        # showing a percentage out of nowhere.
        paint_delegate(raw, pct_only(raw.decode("utf-8", "replace")))
        return

    # Everything that is OURS (looking at the %, noting it down and deciding the
    # notice) goes in its own try: whatever happens here, the delegate runs all
    # the same. The user's status line is the sacred part; our note is the extra.
    info = notice = None
    try:
        raw_text = raw.decode("utf-8", "replace")
        info = parse_ctx(raw_text)
        if info:
            ctx = record_ctx(info["session_id"], info["pct"])
            # Deciding the notice goes in ITS OWN try, inside the one above: it
            # cannot take down the note that has already been written.
            try:
                if ctx is not None:
                    # `ctx` is what was left on disk and record_ctx does not touch
                    # "warned": that one is therefore the PREVIOUS state.
                    notice = prepare_notice(info["session_id"], info["pct"], ctx,
                                            raw_text)
            except Exception:
                notice = None
    except Exception:
        info = None

    paint_statusline(raw, info["pct"] if info else None)

    # The notice is SENT last, with the bar already painted: `display-message`
    # talks to every tmux client (up to 2 s each if the server is stuck) and that
    # wait cannot go between the render and what the user sees.
    if notice:
        try:
            notify_tmux(notice)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
