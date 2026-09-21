#!/usr/bin/env python3
"""agy's status line: the same tee, in front of Antigravity's own line.

This is what `/statusline <command>` runs on every repaint. It notes the context
percentage down in `<state>/sessions/<conversation id>.ctx.json`, paints the
line, and only then sends tmux the notice of the first time that session crosses
the warning threshold -- exactly what `context_tee.py` does for Claude Code, and
mostly by calling it: agy's payload turned out to be shaped almost exactly like
claude's, `context_window.used_percentage` included (measured;
`tests/fixtures/agy_statusline.json` is that captured payload).

That note is the whole reason this file exists. agy has no other source for a
context percentage: its transcript carries no tokens (measured), it
prints no percentage in its own built-in line, and the trick that works for
codex -- reading its rollout -- has no equivalent here. Without this hook agy's
rows in the menu and the status bar have no 🧠 at all.

Three things differ from the claude tee, and only three:

- the mode it obeys is the config's `statusline.agy`,
- the command it delegates to is saved in `delegates/agy-statusline`,
- in `stack` it paints OURS ALONE. agy stacks its own default line underneath by
  itself, because the installer wrote `stack_with_default: true` next to the
  command; running the delegate as well would paint a third line.

Deliberately paranoid, for the same reason as the claude tee: this runs on every
repaint of every agy, so any failure ends in silence and exit 0. The worst that
may happen is one cycle painting nothing.

    echo '<json>' | python3 agy_statusline.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Two levels up from this file is the directory that holds the `flightdeck`
# package: agy launches this by absolute path and sets no PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from flightdeck import config  # noqa: E402  (after the sys.path line, on purpose)
    from flightdeck.hooks import context_tee as tee  # noqa: E402
except Exception:  # pragma: no cover - covered by a subprocess test
    # From the moment this hook is installed, agy's status line IS this script:
    # a package that cannot be imported -- a half-written file, an `update`
    # caught halfway -- would otherwise leave every open agy with a blank line
    # and a traceback under it, INCLUDING the user's own delegated line.
    # `main()` degrades instead (see `_paint_degraded`). Both names are guarded
    # together because `context_tee` lives inside the same package: when
    # `flightdeck` raises, so does it.
    config = tee = None

# The three ways the user can have this line (config key `statusline.agy`),
# spelled the same as claude's. The note of the % and the 🧠 notice happen in
# all three: they are what the menu and the tmux bar read.
MODES = ("own", "wrap", "stack")
# With no package at all this stays None and is never read: the degraded path
# has no mode to obey, it just paints the delegate.
DEFAULT_MODE = config.DEFAULTS["statusline"]["agy"] if config else None

# The file name under `delegates/`. Its own, not claude's: a user can perfectly
# well have a custom status line in both tools, and one file would mean each
# installer overwriting the other's only copy.
DELEGATE_NAME = "agy-statusline"

# Seconds the delegate gets in degraded mode (the claude tee's own number; that
# module is exactly what cannot be imported here).
DELEGATE_TIMEOUT = 10


def _state_dir():
    """The state directory, with the package and without it.

    The fallback spells `config.state_dir()`'s rule again on purpose: it is the
    path taken when `flightdeck` cannot be imported at all, and finding the
    user's own status line is the one thing that must still work then. Same
    shape, and for the same reason, as `context_tee._state_dir`.
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


def delegate_file():
    """The `/statusline` command there was before us (saved by the installer)."""
    if config is not None:
        return config.delegates_dir() / DELEGATE_NAME
    return _state_dir() / "delegates" / DELEGATE_NAME


def statusline_mode():
    """The configured mode, degrading to the default.

    A mode spelled wrong (or a `statusline` key that is not an object) must not
    leave the bar mute: it falls back to the default, and `doctor` is the place
    that complains about the typo.
    """
    try:
        mode = config.load()["statusline"]["agy"]
    except (KeyError, TypeError, IndexError):
        return DEFAULT_MODE
    return mode if mode in MODES else DEFAULT_MODE


def parse_ctx(stdin_json):
    """{"session_id", "pct"} out of agy's status line JSON; None when there is none.

    The claude tee's reader answers this verbatim -- same `session_id`, same
    `context_window.used_percentage`, same rounding -- which is the measurement's
    punchline and the reason there is no second copy of that validation here.

    The fallback is insurance and not a measurement: agy also sends
    `remaining_percentage`, so if a version ever stops sending the used half the
    bar and the 🧠 survive on the other one.
    """
    info = tee.parse_ctx(stdin_json)
    if info is not None:
        return info
    try:
        data = json.loads(stdin_json)
        window = data["context_window"]
        if "used_percentage" in window:
            return None   # it was there and unusable: not ours to repair
        window["used_percentage"] = 100 - float(window["remaining_percentage"])
        return tee.parse_ctx(json.dumps(data))
    except Exception:
        return None


def paint_own(raw):
    """Flightdeck's own line for this payload. Silent if anything goes wrong.

    Imported here and not at the top of the file: this runs on every repaint of
    every agy, and in `wrap` mode the rendering code is never needed.
    """
    try:
        from flightdeck.statusline import render_agy
        payload = json.loads(raw.decode("utf-8", "replace"))
        # Valid JSON that is not an object is not a payload: it is whatever else
        # got piped in, and the honest answer is to paint nothing (the same rule
        # `flightdeck statusline <tool>` follows). An EMPTY object is different:
        # that is a payload with no fields, and it paints what little it can.
        if not isinstance(payload, dict):
            return
        text = render_agy(payload)
    except Exception:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def paint_delegate(raw, pct):
    """The sacred part: the command that was there, same stdin, stdout byte for byte.

    With no delegate saved it paints the minimal `Ctx NN%`, as the claude tee
    does and for the same reason: `delegates/` lives in the disposable state
    directory, and losing that file would otherwise leave the pane with a blank
    line and nothing to blame it on.
    """
    tee.paint_delegate(raw, pct, minimal=True,
                       command=tee.read_delegate(delegate_file()))


def paint_statusline(raw, pct):
    """Paint whichever line the mode asks for.

    `stack` paints only ours: agy adds its own default line underneath (the
    installer set `stack_with_default`), which is where claude's tee runs the
    delegate instead.
    """
    mode = statusline_mode()
    if mode in ("own", "stack"):
        paint_own(raw)
    elif mode == "wrap":
        paint_delegate(raw, pct)


def _read_delegate_degraded():
    """The user's own line, read straight off the delegate file.

    `context_tee.read_delegate` says the same thing in the same words; it is
    copied here rather than called because the whole point of this path is that
    `context_tee` is one of the modules that could not be imported.
    """
    try:
        lines = [l.strip() for l in delegate_file().read_text().splitlines()
                 if l.strip()]
    except Exception:
        return ""
    return lines[0] if lines else ""


def _pct_degraded(raw_text):
    """The context % straight off the payload, with no package to help.

    Nothing is written here, so unlike `parse_ctx` it does not insist on a
    usable session id: a payload whose id is odd still gets its number painted.
    """
    try:
        window = json.loads(raw_text)["context_window"]
        if "used_percentage" in window:
            return int(float(window["used_percentage"]) + 0.5)
        return int(100 - float(window["remaining_percentage"]) + 0.5)
    except Exception:
        return None


def _paint_degraded(raw):
    """The broken-package path: the user's line, or the minimal `Ctx NN%`.

    The sacred part is whatever agy was painting before us, and it is still
    painted: their own command, saved in `delegates/agy-statusline`, with its
    stdout handed back byte for byte. With no command of theirs, the minimal
    line off the payload, which beats a blank bar. The note and the 🧠 notice
    are skipped rather than half-done -- the code that owns them is the missing
    part, and a note nobody can vouch for would have the menu showing a
    percentage out of nowhere.
    """
    delegate = _read_delegate_degraded()
    if not delegate:
        pct = _pct_degraded(raw.decode("utf-8", "replace"))
        try:
            sys.stdout.write("Ctx n/a\n" if pct is None else "Ctx %d%%\n" % pct)
            sys.stdout.flush()
        except Exception:
            pass
        return
    try:
        r = subprocess.run(delegate, shell=True, input=raw,
                           capture_output=True, timeout=DELEGATE_TIMEOUT)
    except Exception:
        # A delegate that hangs or cannot be launched: this cycle paints
        # nothing. The next repaint tries again.
        return
    try:
        sys.stdout.buffer.write(r.stdout)
        sys.stdout.buffer.flush()
        if r.stderr:
            sys.stderr.buffer.write(r.stderr)
            sys.stderr.buffer.flush()
    except Exception:
        pass


def main():
    raw = sys.stdin.buffer.read()

    if config is None or tee is None:
        _paint_degraded(raw)
        return

    # Everything that is OURS (looking at the %, noting it down and deciding the
    # notice) goes in its own try: whatever happens here, the line still gets
    # painted. Same order as the claude tee -- paint first, tmux afterwards.
    info = notice = None
    try:
        raw_text = raw.decode("utf-8", "replace")
        info = parse_ctx(raw_text)
        if info:
            ctx = tee.record_ctx(info["session_id"], info["pct"])
            try:
                if ctx is not None:
                    # `ctx` is what was left on disk and record_ctx does not
                    # touch "warned": that one is therefore the PREVIOUS state.
                    notice = tee.prepare_notice(info["session_id"], info["pct"],
                                                ctx, raw_text)
            except Exception:
                notice = None
    except Exception:
        info = None

    paint_statusline(raw, info["pct"] if info else None)

    if notice:
        try:
            tee.notify_tmux(notice)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
