#!/usr/bin/env python3
"""The session state hook: what tells Flightdeck how every agent is doing.

It registers on several events (SessionStart/Stop/Notification/
UserPromptSubmit/SessionEnd, plus codex's and agy's own) and writes the state of
EVERY session to `<state>/sessions/<id>.json`, so the menu and the status bar
know which one is working, which one is waiting for you and which one is asking
for something.

Defensive by design: whatever happens it exits 0 and prints NOTHING to stdout
(the stdout of UserPromptSubmit/SessionStart would be injected into the agent's
session as context). The ONLY exception is `--tool agy`: agy READS a JSON object
back from its hooks and waits for it, so there the answer is `{}` ("no
decision"), whatever happens.

It is run by absolute path by a tool that knows nothing about Flightdeck
(`python3 <code>/flightdeck/hooks/session_hook.py Stop [--tool codex|agy]`), so
it puts its own root on `sys.path` and imports the package from there. The
extras it borrows from the package (the transcript reader, the badge, the
per-tool payload) are imported LAZILY and inside a `try`: none of them is worth
taking down the session that invoked us.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Two levels up from this file is the directory that holds the `flightdeck`
# package. Nobody sets PYTHONPATH for us: the hook is launched by the agent, from
# whatever working directory it happens to be in.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from flightdeck import config  # noqa: E402  (after the sys.path line, on purpose)
    # The notice's argv (which depends on the tmux installed) is built in ONE
    # place for every emitter; measured, it costs 0.4 ms to import here.
    from flightdeck.common import notice_args, tmux_version  # noqa: E402
except Exception:  # pragma: no cover - covered by a subprocess test
    # This script is registered for 7 Claude Code events, 6 of codex's and 3 of
    # agy's: it is the most-fired thing in the product. A package that cannot be
    # imported -- a half-written file, an `update` caught halfway -- would
    # otherwise break the two contracts at the top of this file on every one of
    # them (rc 1 with a traceback, and for agy no `{}` at all, which leaves it
    # sitting there waiting for an answer). `main()` degrades instead: nothing
    # of ours is written, no notice is sent, and agy still gets its `{}`.
    config = None
    notice_args = tmux_version = None

STATE_BY_EVENT = {
    "SessionStart": "working",
    "UserPromptSubmit": "working",
    # agy's start of turn ("before calling the model"): it has no
    # UserPromptSubmit, so this is what puts its card to work.
    "PreInvocation": "working",
    # Every tool executed = working: it is what brings the state back to normal
    # after granting a permission (there is no "permission granted" event;
    # without this the "needs attention" went stale). And it is also what
    # "un-asks" a card: answering a form IS using that tool.
    "PostToolUse": "working",
    # This only arrives for the tools in the matcher (see the Claude Code
    # installer), but the default is "working" on purpose: if the matcher were
    # installed wrong, a `PreToolUse` of `Bash` must not paint "asking you". See
    # `state_for`.
    "PreToolUse": "working",
    "Stop": "awaiting_input",
    "Notification": "needs_attention",
    # codex's "asking for approval" (its hooks mirror claude's payload, but the
    # permission is an event of its own, not a Notification with text).
    "PermissionRequest": "needs_attention",
    "SessionEnd": "ended",
}

# The two tools Claude ASKS with and then stands still waiting: the options menu
# and the "does the plan look right?". Exact names, read from the 2.1.233 binary
# (`AskUserQuestion` carries the description "Asks the user multiple choice
# questions..."). Careful: there is also `EnterPlanMode`, which does NOT ask.
TOOLS_THAT_ASK = ("AskUserQuestion", "ExitPlanMode")

# How long the floating notice lasts in the tmux bar (`display-message -d`).
# Without `-d` tmux uses its display-time (750 ms out of the box): a flash too
# short to read. Any key clears it earlier (and the key still reaches the pane,
# measured in 3.6a). This is the FALLBACK; the value in force comes from the
# config (`notice_ms`), which is also what the status line tee and the handover
# read -- so the 🧠 notice cannot end up lasting a different time from the
# "waiting for you" one. There is a cross-check test on the three.
# (With no package at all this stays None and is never read: the degraded mode
# writes no card and sends no notice, and the notice is an extra owned by the
# very code that is missing.)
NOTICE_MS = config.DEFAULTS["notice_ms"] if config else None


def _notice_ms():
    """The notice duration in force, read from the config when it is shown.

    It falls back to the default instead of raising: a `notice_ms` the user typed
    as a word must not swallow the notice -- being told nothing is the one
    unacceptable outcome. `doctor` is the place that complains about the value.
    """
    try:
        return int(config.load()["notice_ms"])
    except (KeyError, TypeError, ValueError):
        return NOTICE_MS


def state_for(event, payload, previous_state=None, loop=False):
    """The fine-grained state: three events are no good as they come.

    `PreToolUse` only interests us for the tools that ask (`TOOLS_THAT_ASK`) --
    there Claude has opened a form and does not carry on until you pick, which is
    as good a "waiting for you" as the `Stop`. From any other tool, "working":
    the matcher should stop them arriving, and if they do arrive it means it is
    installed wrong, not that Claude is asking.

    `Notification`s come from two different places: "Claude needs your
    permission..." = real attention (blocked on a permission); "Claude is waiting
    for your input" = the reminder that the turn is over, i.e. waiting
    (awaiting_input), not an alarm -- without this the badge jumped by itself
    from "WAITING FOR YOU" to "needs attention" a while after finishing.

    And with the card already `asking` (`previous_state`), a `Notification` does
    NOT move it: that is the reminder Claude Code sends 6 s after opening ANY
    dialog (constant `Xwn=6000` in the 2.1.233 binary), and for the options form
    it arrives with the GENERIC text "Claude needs your permission" (its dialog
    table uses that same text for nearly all of them; the plan's says "needs your
    approval for the plan"). Without this, that reminder overwrote `asking` with
    `needs_attention` and the menu said "needs attention" in front of a form.
    Getting out of `asking` is still a matter of answering (`PostToolUse`) or
    typing.

    And the `Stop` of a `/loop` ROUND is not "waiting for you": the agent is
    waiting for its timer, not for you. `loop=True` (decided by
    `flightdeck.turn` reading the end of the transcript, see `_loop_after_stop`)
    leaves it `looping`; Claude Code's idle reminder (`Notification`) does not
    move it either, and the next round -- or a prompt of yours -- puts it back to
    `working` as always.
    """
    st = STATE_BY_EVENT.get(event)
    if event == "PreToolUse":
        return "asking" if payload.get("tool_name") in TOOLS_THAT_ASK else st
    if event == "Stop" and loop:
        return "looping"
    if event == "Notification":
        if previous_state in ("asking", "looping"):
            return previous_state
        # Matched on Claude Code's own English text: the notification's wording
        # is not a documented contract, so the two words that tell the reminder
        # from a permission are looked for, and anything else stays "attention".
        # A `message` that is not text counts as no text: the payload is not
        # ours, and `.lower()` on a number used to take the hook to rc 1.
        msg = payload.get("message")
        msg = msg.lower() if isinstance(msg, str) else ""
        if "permission" not in msg and "waiting" in msg:
            return "awaiting_input"
    return st


def notice_text(event, state, who):
    """The floating notice for the tmux bar, or None when there is nothing to say.

    We announce when the turn is yours: when it finishes (`Stop`), when the agent
    asks for a permission (`Notification`) and when it ASKS you something. Normal
    work is not announced: a message per tool executed would be a constant
    flicker.
    """
    if state == "asking":
        return "❓ %s is asking you" % who
    if state == "looping":
        return None   # waiting for its timer, not for you: nothing to sing
    if event in ("Stop", "Notification", "PermissionRequest"):
        return "⏳ %s is waiting for you" % who
    return None


def _loop_after_stop(payload):
    """(turn analysis, is a loop) from the end of the transcript, or (None,
    False) when it cannot be worked out. It reuses `flightdeck.turn` (a lazy
    import, like the rest of the hook's extras): if the module is not there or
    fails, "waiting for you", which is what it did before."""
    try:
        from flightdeck.turn import analyze_turn, is_loop
        turn = analyze_turn(payload.get("transcript_path"))
        return turn, is_loop(turn)
    except Exception:
        return None, False


def is_non_interactive(argv):
    """Is that argv a tool in NON-interactive mode (a script, not a session)?

    - claude: `-p`/`--print` (a wrapper that runs `claude --print` on every Bash
      command launches one from HOME constantly; with no filter, 4,047 cards and
      false notices on one machine).
    - codex: the `exec`/`e`/`review` subcommands. CAREFUL: only if the executable
      IS codex and "exec" is the FIRST token that is not a flag -- in a typed
      prompt ("fix the exec") or in a claude, that word is text, not a
      subcommand.
    - agy: `-p`/`--print`/`--prompt`, its one-shot mode.
    """
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if "codex" in base:
        # In codex `-p` is --profile (not --print): ONLY the subcommands count.
        for token in argv[1:]:
            if token.startswith("-"):
                continue
            return token in ("exec", "e", "review")
        return False
    if base.startswith("agy"):
        # agy is a Go binary: the same flag works with one dash or two and the
        # value can come glued on with `=`, so looking for "-p" in the list is
        # not enough as it is in claude.
        for token in argv[1:]:
            if token.startswith("-") and \
                    token.partition("=")[0].lstrip("-") in ("p", "print", "prompt"):
                return True
        return False
    return "--print" in argv or "-p" in argv


def parent_argv():
    """The argv of the process that fires this hook (the agent, via getppid), in
    tokens. `ps` returns the line flattened. Empty when it cannot be known --
    and then it is treated as interactive, which is the case we cannot afford to
    lose. (`FLIGHTDECK_PARENT_ARGV_TEST` is the tests' seam.)"""
    probe = os.environ.get("FLIGHTDECK_PARENT_ARGV_TEST")
    if probe is not None:
        return probe.split()
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(os.getppid())],
                           capture_output=True, text=True, timeout=2)
        return (r.stdout or "").split()
    except Exception:
        return []


# `ended` cards older than this are swept on every SessionEnd (nothing deleted
# them: 4,131 piled up on one machine). An hour of slack in case something reads
# one just after it finishes (the menu shows "shell", the handover discards it).
SWEEP_AGE_S = 3600


def died_without_goodbye(card):
    """A codex/agy card whose process no longer exists: those tools send no
    SessionEnd (measured: codex's /exit fires nothing), so without this the card
    would stay for ever. Only outside claude: in claude a dead pid with a live
    pane may be an agent still starting up (see `common.load_sessions`)."""
    if card.get("tool") in (None, "claude") or not isinstance(card.get("pid"), int):
        return False
    try:
        os.kill(card["pid"], 0)
        return False
    except ProcessLookupError:
        return True
    except Exception:
        return False


def sweep_old_cards(sess_dir, age_s=SWEEP_AGE_S):
    """Delete the `ended` cards with an `updated_at` older than `age_s`, and
    their `<id>.ctx.json`. Returns how many files it deleted. Everything else is
    left: live cards, undated ones, broken ones. No failure escapes from here."""
    deleted = 0
    try:
        limit = (datetime.datetime.now().astimezone()
                 - datetime.timedelta(seconds=age_s))
        for f in Path(sess_dir).glob("*.json"):
            if f.name.endswith(".ctx.json"):
                continue
            try:
                d = json.loads(f.read_text())
                if not isinstance(d, dict):
                    continue
                if d.get("state") != "ended" and not died_without_goodbye(d):
                    continue
                upd = datetime.datetime.fromisoformat(d["updated_at"])
                if upd.tzinfo is None:
                    upd = upd.astimezone()
                if upd > limit:
                    continue
                f.unlink()
                deleted += 1
                ctx = f.with_name(f.name[:-5] + ".ctx.json")
                if ctx.exists():
                    ctx.unlink()
                    deleted += 1
            except Exception:
                continue
    except Exception:
        pass
    return deleted


# How long the Stop's deferred notice waits before re-reading the card and the
# registry. Claude Code's registry (`~/.claude/sessions/<pid>.json`, status
# busy/idle/waiting/shell) changes a few ms AFTER the Stop hooks run (measured:
# 8 ms), and a wake-up by a background task arrives within seconds: 1.5 s is
# enough to see both, and the notice still arrives "right away".
NOTICE_WAIT_S = 1.5


def registry_dir():
    """Where Claude Code records its own live sessions.

    `FLIGHTDECK_REGISTRY_TEST` is the tests' seam: without it the suite would
    read the registry of whoever runs it.
    """
    override = os.environ.get("FLIGHTDECK_REGISTRY_TEST")
    return Path(override) if override else None


def registry_status(sid, pid=None):
    """The `status` Claude Code records for that session (busy/idle/waiting/
    shell), or None when it is not found. It is looked up by sessionId (and by
    pid as a fallback) in Claude Code's registry. Any failure -> None.

    `flightdeck.registry` is imported lazily, like the hook's other extras: this
    only runs in the deferred notice's own process, and a registry that cannot be
    read has to come out as "I do not know", never as an exception.
    """
    try:
        from flightdeck.registry import read_registry
        for e in read_registry(registry_dir()):
            if e.get("session_id") == sid or (pid is not None and e.get("pid") == pid):
                return e.get("status")
    except Exception:
        pass
    return None


def tmux_bin():
    sock = config.tmux_socket()
    return ["tmux"] + (["-L", sock] if sock else [])


def notify_tmux(text):
    """The floating notice on EVERY tmux client. Silent on any failure."""
    try:
        duration = _notice_ms()
        r = subprocess.run(tmux_bin() + ["list-clients", "-F", "#{client_name}"],
                           capture_output=True, text=True, timeout=2)
        clients = (r.stdout or "").split()
        if not clients:
            return
        # Asked for once there is somebody to tell: this hook fires on every tool
        # use of every session, so a `tmux -V` nobody reads is a subprocess too
        # many. `common.notice_args` is what turns it into the argv: the text
        # travels literally where tmux can do it (`-l`, from 3.4) and escaped
        # where it cannot, so a project called "#(something)" is never run as a
        # format. -C = the pane KEEPS painting while the notice is on screen:
        # without it tmux freezes that client until the message goes (measured in
        # 3.6a) -- at 750 ms you did not notice, at 5 s it would freeze the
        # agent's output on every screen.
        version = tmux_version()
        for client in clients:
            subprocess.run(tmux_bin() + notice_args(text, version, duration,
                                                    client),
                           capture_output=True, timeout=2)
    except Exception:
        pass


def deferred_notice(sid, text):
    """The Stop's "waiting for you" notice, a moment later and only if it is
    still true: the card still says awaiting_input (no UserPromptSubmit has put
    it back to work: a background task has not woken it up again) and Claude
    Code's registry does not say `busy` (agents or delegated tasks running).
    This is what got rid of the "N waiting for you" that cleared itself a second
    later."""
    try:
        wait = os.environ.get("FLIGHTDECK_NOTICE_WAIT_TEST")
        time.sleep(float(wait) if wait else NOTICE_WAIT_S)
        try:
            card = json.loads(
                (config.sessions_dir() / ("%s.json" % sid)).read_text())
        except Exception:
            card = {}
        if card.get("state") != "awaiting_input":
            return
        if registry_status(sid, card.get("pid")) == "busy":
            return
        notify_tmux(text)
    except Exception:
        pass


def launch_deferred_notice(sid, text):
    """`deferred_notice` in a separate process, without waiting and without
    inheriting stdout/stderr (Claude Code waits for those to close before
    considering the hook finished)."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                          "deferred-notice", str(sid), text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _badge(card):
    """What the menu paints of this card (the badge), or the bare state if the
    menu cannot be reached (every extra of this hook is optional)."""
    try:
        from flightdeck.picker import state_badge
        return state_badge(card.get("state"), card.get("last_event"),
                           card.get("source"))
    except Exception:
        return card.get("state")


def row_changes(before, after):
    """Does what the menu PAINTS of this session change between the two cards?

    It is the signal to reload the list in the open menus. It is decided with the
    menu's own `state_badge`, and not with the bare `state`, because the badge
    also looks at `last_event`/`source`: "○ open" (working + SessionStart)
    becomes "● working" with the first prompt without `state` moving. And the
    other way round, Claude Code's reminders (the "still waiting" `Notification`,
    or the 6 s one with a form open) do not move the badge and reload nothing.
    A new card (an empty `before`) always counts as a change.
    """
    return _badge(before) != _badge(after)


# The bash command that knows how to reach the open menus, inside the code
# directory. It lives in `bin/` and NOT next to the package: a file cannot
# share the name of the `flightdeck/` package directory, so a command called
# `flightdeck` at the root would resolve to that directory, the launch below
# would fail, and the open menus would never reload on their own. This is the
# one place its path is spelled.
COMMAND_RELPATH = ("bin", "flightdeck")


def command_path():
    return config.code_dir().joinpath(*COMMAND_RELPATH)


def refresh_menus():
    """Reload the list of EVERY live menu (`flightdeck refresh-menus`).

    In the background and without waiting: it is up to a second of HTTP per menu
    and the hook has no business paying for it. Without inheriting stdout/stderr,
    which Claude Code waits to see closed before considering the hook finished.
    If the command is not there or fails, nothing happens: the list refreshes
    anyway when you enter the menu.
    """
    try:
        subprocess.Popen([str(command_path()), "refresh-menus"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _human_title(session_id):
    """The last `/rename` (or the handover's `-n`) of that conversation, or None.

    It reuses `flightdeck.history.title_for_session` (HUMAN titles only, never
    the ones the AI makes up). It is an extra: if the module is not there or
    fails, None and that is that.
    """
    try:
        from flightdeck.history import title_for_session
        return title_for_session(session_id)
    except Exception:
        return None


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def tmux_info(pane):
    if not pane:
        return {}
    try:
        r = subprocess.run(
            tmux_bin()
            + ["display-message", "-p", "-t", pane,
               "#{session_name}\t#{window_index}\t#{window_name}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            parts = (r.stdout.strip().split("\t") + ["", "", ""])[:3]
            return {
                "tmux_session": parts[0],
                "tmux_window": parts[1],
                "tmux_window_name": parts[2],
            }
    except Exception:
        pass
    return {}


def _normalized_payload(tool, payload):
    """The payload with the keys this hook expects (claude's): agy's speak
    camelCase (`conversationId`, `workspacePaths`, `transcriptPath`). The
    translation lives in `flightdeck.tools` -- the only module that knows about
    tools -- and importing it is lazy, like the rest of the extras: if it is not
    there, the payload as it came (an "unknown" card and carry on, never a hook
    that blows up)."""
    try:
        from flightdeck.tools import payload_for
        return payload_for(tool, payload)
    except Exception:
        return payload


def _safe_sid(sid):
    """The session id, checked BEFORE it becomes a file name.

    The id arrives in a payload that is not ours, and it is used directly as
    `<state>/sessions/<sid>.json`. Measured: `{"session_id": "../escaped"}`
    wrote the card one level ABOVE `sessions/`, which is where `sweep_old_cards`,
    `deferred_notice` and the menu's reader all assume the cards live -- so that
    card is never read and never swept. The status line tee already validates
    its id before touching the disk (`context_tee.parse_ctx`); this did not.

    Anything that is not a plain id becomes "unknown", which is what an event
    with no id gets anyway.

    The regex is the package's (`common.SAFE_ID`), with a copy as a fallback for
    the same reason the tee keeps one: not being able to import `common` must
    not send every session to the same card.
    """
    try:
        from flightdeck.common import SAFE_ID
    except Exception:  # pragma: no cover - safety net
        SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
    return sid if SAFE_ID.match(str(sid)) else "unknown"


def _tool_from_argv(argv):
    """`--tool codex|agy`: which tool these hooks are talking to (codex 0.145's
    mirror claude's payload and call right here). With no flag, claude."""
    if "--tool" in argv:
        try:
            return argv[argv.index("--tool") + 1]
        except Exception:
            pass
    return "claude"


def main():
    tool = _tool_from_argv(sys.argv)
    try:
        if config is None:
            # The package is broken (see the guarded import at the top). There is
            # nothing of ours worth doing without it -- no card, no notice, no
            # menu refresh -- and half of any of them would be worse than none:
            # a card nobody can vouch for is what the menu and the status bar
            # read. What still has to happen is exactly what the `finally`
            # below does, and exit 0.
            sys.exit(0)
        _main(tool, sys.argv[1] if len(sys.argv) > 1 else "")
    finally:
        if tool == "agy":
            # agy READS a JSON object from its hooks' stdout: `{}` is "no
            # decision" -- it lets it stop and injects nothing into it. It is in
            # the `finally` because it has to go out by EVERY path (including the
            # `sys.exit(0)` of the non-interactive filter, and including
            # something blowing up): with no answer, agy sits there waiting for
            # it. claude and codex still receive NOTHING there (claude would be
            # given it as session context).
            try:
                sys.stdout.write("{}\n")
                sys.stdout.flush()
            except Exception:
                pass


def _main(tool, event):
    """The hook proper. `main` only wraps agy's answer around it."""
    if event == "deferred-notice":
        # The second half of the Stop's notice (launched by
        # `launch_deferred_notice`).
        try:
            deferred_notice(sys.argv[2], sys.argv[3])
        except Exception:
            pass
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        # Valid JSON that is not an object (an array, a bare string): everything
        # below reads the payload as a mapping, and a `.get` on a list took the
        # hook to rc 1 -- the one thing the defensive design promises never
        # happens. An empty payload behaves like a broken one: the event is
        # still recorded, under the "unknown" id.
        payload = {}
    if tool == "agy":
        payload = _normalized_payload(tool, payload)

    # A `claude --print` (a script, not a session of yours): no card, no tmux, no
    # notice. It goes BEFORE touching anything. The cost is one `ps` per event.
    try:
        if is_non_interactive(parent_argv()):
            sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        pass

    sid = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "unknown"
    sid = _safe_sid(sid)
    pane = os.environ.get("TMUX_PANE")

    # Reading the card cannot take the hook down: a full disk or broken
    # permissions would leave the agent with a hook that exits != 0 on every
    # event. `ensure_dir` is what CREATES the directory (asking for the path no
    # longer does), and it swallows its own mkdir and hands back the path either
    # way -- the write below has its own guard.
    sess_dir = config.ensure_dir(config.sessions_dir())
    f = sess_dir / ("%s.json" % sid)
    try:
        data = json.loads(f.read_text())
    except Exception:
        data = {}
    before = dict(data)  # to know later whether the menu has to repaint the row

    data["session_id"] = sid
    if tool != "claude":
        data["tool"] = tool
    # Proof of life for Flightdeck: this hook is a child of the agent process
    # that fires it, so its parent IS that agent's pid. If tomorrow the card
    # still says "working" but that pid no longer exists, the agent died without
    # saying goodbye (a crash / kill -9 / closing the app, with no SessionEnd)
    # and it stops being listed as a live session.
    # `_pid` comes from codex's tee (the hook's parent would be the tee, which is
    # ephemeral).
    data["pid"] = payload.get("_pid") if isinstance(payload.get("_pid"), int) \
        else os.getppid()
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    # The cwd has to be TEXT. The payload is not ours, and a `cwd` that arrived
    # as a number or an object made `Path(cwd)` raise right here, outside any
    # try, taking the whole hook to rc 1 -- see the contract. Anything that is
    # not a string is ignored rather than written down: a card carrying a
    # non-string `cwd` would only move the failure to whoever reads it next.
    # (`.name` on a string cannot raise: it is pure text handling.)
    if isinstance(cwd, str) and cwd:
        data["cwd"] = cwd
        data["project"] = Path(cwd).name
    if tool == "agy" and payload.get("transcript_path"):
        # For agy the transcript is only known from here: its file name does not
        # carry the conversation id (claude's does, and codex has its glob).
        data["transcript_path"] = payload["transcript_path"]
    if pane:
        data["tmux_pane"] = pane
        # The tmux session's name is ALWAYS the human's business: the hook
        # READS it for the card, it never changes it.
        data.update(tmux_info(pane))
        # The conversation's TITLE (`/rename`, or the handover's -n) goes to the
        # tmux session's `@title` option: that is what the terminal paints in its
        # tab (the bash command's set-titles-string: `@title`, and failing that
        # the session name). When the agent finishes it is cleared and the name
        # comes back. A card with no human title -> cleared as well (so an old
        # one does not stay stuck).
        #
        # claude only: `@title` is an option of the tmux SESSION, and codex/agy
        # (which have no human title to set) would clear the one belonging to the
        # claude sharing that session in another window -- the tab flickered
        # between the title and the session name on every event of theirs.
        if tool == "claude":
            try:
                title = None if event == "SessionEnd" else _human_title(sid)
                if title:
                    subprocess.run(
                        tmux_bin() + ["set-option", "-t", pane, "@title", title],
                        capture_output=True, timeout=2)
                else:
                    subprocess.run(
                        tmux_bin() + ["set-option", "-t", pane, "-u", "@title"],
                        capture_output=True, timeout=2)
            except Exception:
                pass

    loop = False
    if event == "Stop" and tool == "claude":
        # Is a `/loop` round finishing (or has another been scheduled)? The
        # analysis is recorded on the card so the badge can be explained when
        # needed.
        turn, loop = _loop_after_stop(payload)
        data["turn"] = turn
    st = state_for(event, payload, data.get("state"), loop=loop)
    if st:
        data["state"] = st
    if event == "SessionStart":
        data.setdefault("started_at", now_iso())
        # WHY it started: "startup", "resume", "clear", "compact" or "fork" (the
        # enum of the 2.1.233 payload). The handover needs it: a SessionStart of
        # a COMPACTION can land in the middle of a turn, and without this it is
        # indistinguishable from a session just opened and standing still.
        data["source"] = payload.get("source")
    data["updated_at"] = now_iso()
    data["last_event"] = event

    try:
        f.write_text(json.dumps(data))
    except Exception:
        pass

    # When a session says goodbye, the old `ended` cards are swept along the way:
    # it is the only moment the directory can grow, and they are files nobody
    # reads any more. Never the card just written (it has an hour).
    if event == "SessionEnd":
        try:
            sweep_old_cards(sess_dir)
        except Exception:
            pass

    # If what the menu paints of this session has changed, let the open menus
    # know (without this, a menu left in a window showed the picture from when
    # you entered). After the card, which is what they are going to read.
    changed = True
    try:
        changed = row_changes(before, data)
        if changed:
            refresh_menus()
    except Exception:
        pass

    # When the turn comes back to you -- finishing, asking for a permission or
    # ASKING something -- a notice goes to the status bar of EVERY tmux client
    # (also if you are looking at another session). It goes AFTER writing the
    # card on purpose: it is up to 2 s of `subprocess` per client, and the state
    # Flightdeck reads cannot wait for a sluggish tmux.
    # The try wraps the ASSEMBLY of the text too, not just the tmux calls:
    # deciding the notice touches the payload (`sid[:8]`, to have a short name to
    # announce) and a malformed payload can make that raise -- with a
    # `session_id` that is not text, that slice is a TypeError. Outside the try it
    # took the whole hook down (rc 1), breaking the "whatever happens, exit 0".
    try:
        # The notice says the name of the TMUX SESSION, which is what the status
        # bar and the menu call that row ("prototypes is waiting for you" says
        # nothing: it is the basename of the cwd, and there may well be two
        # claudes in that folder). Same order of fallbacks as the tee
        # (`name_for_notice`).
        who = (data.get("tmux_session") or data.get("project") or sid[:8])
        notice = notice_text(event, data.get("state"), who)
        if notice and not changed and event in ("Stop", "Notification",
                                                "PermissionRequest"):
            # The SAME state twice in a row is not sung twice: that is codex's
            # double Stop (the file hook plus the notify tee) or a repeated
            # reminder -- one notice per real change, like the menu refresh.
            notice = None
        if notice and event == "Stop" and tool == "claude":
            # The Stop's "waiting for you" is checked again a moment later (see
            # `deferred_notice`): the Stop fires at the end of EVERY turn, also
            # with background agents running that wake the model up again a
            # second later. In codex the Stop comes from the notify (a real end
            # of turn, with no agents to wake it): straight through.
            launch_deferred_notice(sid, notice)
        elif notice:
            # asking / attention: an exact signal, right away.
            notify_tmux(notice)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
