#!/usr/bin/env python3
"""codex's `notify`, chained: Flightdeck first, then whatever was there.

codex calls its `notify` with ONE JSON argument at the end of every turn
(`{"type":"agent-turn-complete","thread-id":...,"cwd":...}`, read from the 0.145
binary). codex has NO `Stop` hook event: this tee is what turns the end of turn
into Flightdeck's `Stop` (it invokes `session_hook.py Stop --tool codex` with a
payload shaped like claude's) and afterwards FORWARDS the JSON UNTOUCHED to the
notify the user already had, saved by the installer as the `codex-notify`
delegate -- the same pattern as the status line tee. Whatever happens: exit 0 and
forward; breaking someone's notify because of a failure of ours is unacceptable.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Two levels up from this file is the directory that holds the `flightdeck`
# package: codex launches this by absolute path and sets no PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from flightdeck import config  # noqa: E402  (after the sys.path line, on purpose)
except Exception:  # pragma: no cover - covered by a subprocess test
    # The package itself can be broken: a half-written file, an update caught
    # halfway. From the moment this tee is installed the user's own `notify` is
    # only ever called through us, so a failure of ours must not swallow their
    # command -- the forward below is computed without the package and still
    # happens. What is skipped is everything that is ours (the session card),
    # since the code that owns it is the missing part.
    config = None

HOOK = Path(__file__).resolve().parent / "session_hook.py"


def _delegates_dir():
    """Where the saved commands live, with the package and without it.

    The fallback spells `config.state_dir()`'s rule again on purpose: this is
    the path taken when `flightdeck` cannot be imported at all, and the one
    thing that must still work then is finding the user's own notify.
    """
    if config is not None:
        return config.delegates_dir()
    override = os.environ.get("FLIGHTDECK_STATE_DIR")
    if override:
        return Path(override) / "delegates"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "flightdeck" / "delegates"
    return Path.home() / ".local" / "state" / "flightdeck" / "delegates"


def _delegate_file():
    """The notify command there was before the tee (saved by the installer)."""
    return _delegates_dir() / "codex-notify"


def _parent_is_exec():
    """Is the codex calling us a `codex exec` (a script, not a session)?

    Same criterion as `session_hook.is_non_interactive`: the tee is a CHILD of
    codex, so its argv is in the parent. (`FLIGHTDECK_PARENT_ARGV_TEST` is the
    tests' seam.)"""
    probe = os.environ.get("FLIGHTDECK_PARENT_ARGV_TEST")
    if probe is not None:
        argv = probe.split()
    else:
        try:
            r = subprocess.run(["ps", "-o", "args=", "-p", str(os.getppid())],
                               capture_output=True, text=True, timeout=2)
            argv = (r.stdout or "").split()
        except Exception:
            return False
    if not argv or "codex" not in os.path.basename(argv[0]):
        return False
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        return token in ("exec", "e", "review")
    return False


def _tell_flightdeck(event):
    """The end of turn, to the usual hook (card + notice + menu refresh)."""
    sid = event.get("thread-id") or event.get("session-id")
    if not sid:
        return
    # The pid Flightdeck needs is CODEX's (our parent): the hook, invoked by the
    # tee, would see the tee itself with getppid, and the tee dies on exit -- and
    # a card with a dead pid is not listed (measured in the experiment).
    payload = {"session_id": sid, "cwd": event.get("cwd"), "_pid": os.getppid()}
    subprocess.run([sys.executable, str(HOOK), "Stop", "--tool", "codex"],
                   input=json.dumps(payload), text=True,
                   capture_output=True, timeout=10)


def _forward(json_arg):
    """The user's original notify, called exactly as codex would (argv + JSON)."""
    try:
        delegate = json.loads(_delegate_file().read_text())
    except Exception:
        return
    if not isinstance(delegate, list) or not delegate:
        return
    try:
        subprocess.Popen([str(a) for a in delegate] + [json_arg],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def main():
    json_arg = sys.argv[-1] if len(sys.argv) > 1 else "{}"
    try:
        event = json.loads(json_arg)
    except Exception:
        event = {}
    try:
        if (config is not None
                and isinstance(event, dict)
                and event.get("type") == "agent-turn-complete"
                and not _parent_is_exec()):
            _tell_flightdeck(event)
    except Exception:
        pass
    _forward(json_arg)
    sys.exit(0)


if __name__ == "__main__":
    main()
