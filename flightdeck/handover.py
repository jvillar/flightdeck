#!/usr/bin/env python3
"""The handover: close the agent in a pane and open the next one, already numbered.

You finish your wrap-up by hand, press the handover key (prefix+n) and this
happens IN FRONT OF YOU, in your own pane: the agent there is closed (`/exit`),
the pane is waited on until it is a shell again -- Flightdeck's sessions are
shell-first -- and `claude -n '<title> <N+1>'` is typed in the same directory.
The old conversation is not lost: it stays frozen and resumable in the menu's
grey history.

The number comes from the conversation's TITLE (the one `/rename` sets, or the
one the previous handover's `-n` left), never from the tmux session name: session
names belong entirely to the user. With no title, the fallback is the tmux
session name.

Verified with claude 2.1.233: `claude -n "<title>"` leaves a `custom-title` line
in the transcript, which is exactly what `title_for_session` reads -- so the next
handover can count without anyone having to run `/rename`.

Everything with an effect goes through tmux with a short timeout, and any
stumble is reported on screen (stderr + floating notice): the one unacceptable
outcome is leaving the pane half-done in silence.
"""
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from flightdeck import config
from flightdeck.common import (load_sessions, notice_args, parse_iso, pid_alive,
                               tmux, tmux_version)
from flightdeck.history import title_for_session
from flightdeck.picker import configured_pins, is_reserved
from flightdeck.registry import read_registry

# How long the agent is given to close before we give up (and touch nothing
# else). Measured for real: `/exit` leaves the pane in the shell in under 1 s;
# the 20 s are for the agent that is saving something or has a dialog open.
TIMEOUT_SHELL = 20.0
WAIT_STEP = 0.5

# How tmux sees a shell in `pane_current_command`. The list is explicit on
# purpose: "ssh" also ends in "sh" and is not a shell of ours. The leading dash
# is what login shells carry ("-zsh").
SHELLS = frozenset(("sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh"))

# A counter is a number at the END and SEPARATED by a space: "pricing 2" counts,
# "v2" does not (there the 2 is part of the name, not a turn).
_COUNTER = re.compile(r"^(.*\S)\s+(\d+)$")

# Flags that are NOT inherited, for three different reasons:
#  - they pick WHICH conversation to open -- or WHERE inside it -- and the
#    handover opens a NEW one: inheriting them would be doing exactly the
#    opposite (`--resume` would reopen the one we just closed, `--session-id`
#    would clash with its id, `-n` is ours and numbered, and
#    `--resume-session-at`/`--resume-drops-turn` point at a MESSAGE of the
#    conversation we are closing -- Claude Code sets them when resuming at a
#    particular point, `/background` among others);
#  - they CREATE the place the session lives in (`--worktree`, and `--tmux`,
#    which only works with it): inheriting them would be a new worktree -- and
#    one more tmux session in the menu -- ON EVERY HANDOVER;
#  - `--print` is not interactive, and a handover is by definition -- and with it
#    goes its whole family, the one claude REJECTS when there is no `--print`:
#    inheriting one of those from a `claude -p` would leave the new agent
#    WITHOUT STARTING. Measured in the 2.1.233 messages: "--input-format=
#    stream-json requires --print", "--prompt-suggestions requires --print and
#    --output-format=stream-json", same for `--include-partial-messages` and
#    `--forward-subagent-text`, and "--no-session-persistence /
#    --plan-mode-instructions can only be used with --print mode".
#    `--output-format` does not error but without `-p` it is inert ("only works
#    with --print"), so it goes with its own.
NON_INHERITABLE = frozenset((
    "-n", "--name", "-r", "--resume", "-c", "--continue", "--fork-session",
    "--session-id", "--from-pr", "--teleport", "--cloud",
    "--resume-session-at", "--resume-drops-turn",
    "-w", "--worktree", "--tmux", "-p", "--print",
    "--input-format", "--output-format", "--prompt-suggestions",
    "--include-partial-messages", "--forward-subagent-text",
    "--no-session-persistence", "--plan-mode-instructions"))

# What claude's binary is called in `argv[0]`: "claude", a path ending in
# "claude", or the binary christened with the version ("2.1.233", which is also
# what tmux shows in `pane_current_command`).
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# Flags that swallow the next token as their value, and we have to know which
# because of how the argv arrives: `ps` gives the line FLATTENED, so without the
# table there is no telling where a value ends, nor which token is a value and
# which a loose prompt word.
#
# The list is the BINARY'S OWN (2.1.233), not the `--help`'s: inside its argument
# tables there is a set of "flags that take the next token" for claude's command
# line, and this comes from there entry by entry (each one corroborated, on top
# of that, with its `<value>` declaration; the only ones with no visible
# declaration are `--exec` and `--routine`, which are hidden but in that set, and
# none of the new ones appears in the boolean set). It used to come from the
# `--help`, and 21 were missing: a flag with a value absent from here lost its
# value on being inherited. If a future version adds flags, the hole comes back
# -- the safety net is the ORDER of `launch_command`, which turns it into a loud
# failure.
FLAGS_WITH_VALUE = frozenset((
    "--add-dir", "--advisor", "--agent", "--agents", "--allowed-tools",
    "--allowedTools", "--append-subagent-system-prompt",
    "--append-system-prompt", "--append-system-prompt-file", "--autocompact",
    "--betas", "--channels", "--cloud", "-d", "--debug", "--debug-file",
    "--disallowed-tools", "--disallowedTools", "--effort", "--environment",
    "--exec", "--fallback-model", "--file", "--from-pr", "--input-format",
    "--json-schema", "-m", "--max-budget-usd", "--max-thinking-tokens",
    "--max-turns", "--mcp-config", "--model", "-n", "--name",
    "--output-format", "--permission-mode", "--permission-prompt-tool",
    "--plan-mode-instructions", "--plugin-dir", "--plugin-dir-no-mcp",
    "--plugin-url", "--prompt-suggestions", "-r", "--remote-control",
    "--remote-control-session-name-prefix", "--resume", "--resume-drops-turn",
    "--resume-session-at", "--rewind-files", "--routine", "--session-id",
    "--setting-sources", "--settings", "--system-prompt",
    "--system-prompt-file", "--task-budget", "--teleport", "--thinking",
    "--thinking-display", "--tools", "-w", "--watch-artifact",
    "--watch-artifact-no-autoreact", "--worktree"))


# --- pure logic ------------------------------------------------------------

def _one_line(text):
    """The text on one line and with no spare whitespace.

    Not cosmetic: this title ends up being TYPED into a shell, and a newline
    from a copy-pasted `/rename` would split the command in two.
    """
    return " ".join(str(text).split()) if text else ""


def next_title(current_title, fallback):
    """The handover's title: "pricing" -> "pricing 2" -> "pricing 3"...

    With no title (a conversation nobody christened) the fallback wins, which is
    the tmux session name. And the fallback is numbered by the same rule: if it
    already ends in a loose number, it carries on counting instead of doubling it.
    """
    base = _one_line(current_title) or _one_line(fallback) or "session"
    m = _COUNTER.match(base)
    if m:
        return "%s %d" % (m.group(1), int(m.group(2)) + 1)
    return "%s 2" % base


def new_title(session_id, fallback, look_up=title_for_session):
    """The title for the new agent, asking the current one via its transcript.

    `look_up` only returns titles SET BY THE HUMAN (`/rename` or the previous
    handover's `-n`); the ones the AI makes up do not count, so an unchristened
    conversation falls back.
    """
    current = look_up(session_id) if session_id else None
    return next_title(current, fallback)


def is_shell(cmd):
    """Has the pane gone back to being a shell (i.e. is there no agent inside)?"""
    return (cmd or "").lstrip("-") in SHELLS


def _freshness(card):
    """Sort key: a card with no date loses against any dated one."""
    upd = parse_iso(card.get("updated_at"))
    return (1, upd.timestamp()) if upd else (0, 0.0)


def card_for_pane(cards, pane_id, alive=pid_alive):
    """The session card of the agent running in that pane RIGHT NOW, or None.

    A pane gets recycled (you close one agent, open another in the same place),
    so there can be several cards pointing there: the freshest wins. Out go the
    ones that already said goodbye (`ended`) and the ghosts (a "working" whose
    process no longer exists: an agent killed the hard way, with no SessionEnd).

    `cards` is what `common.load_sessions()` returns, which is the SAME list the
    menu and the status bar paint their rows from -- and that is the point. It
    carries the cards the hooks wrote, the registry's correction of a false wait
    and the rows synthesised for a claude that predates the hooks
    (`_from_registry`), where a real card always wins. Reading the cards
    directory instead, as this used to, meant `prefix + n` answering "there is no
    live agent to hand over in this pane" on a pane whose row the same product
    had just painted `⏳ WAITING FOR YOU`; a synthesised card carries the `state`,
    the `last_event`/`source`, the `pid` and the `session_id` that everything
    below reads, so there is nothing for the handover to be careful about that it
    is not already careful about.
    """
    candidates = [c for c in cards
                  if c.get("tmux_pane") == pane_id
                  and c.get("state") != "ended" and alive(c.get("pid"))]
    return max(candidates, key=_freshness) if candidates else None


def reason_not_to_close(card, status_cc=None):
    """Why `/exit` must NOT be typed at that agent (or None if go ahead).

    The three cases are the same worry: what we type goes in through the agent's
    input box, and there the Enter does not always mean "close yourself".
    - Asking for a permission: the Enter would say YES to whatever it was asking.
    - Asking a question (`asking`): with a multiple-choice form open, the Enter
      PICKS one -- and the `/exit` would be left written inside the answer.
    - Working: the `/exit` would be queued up for it as if it were a message.
    In all three the handover stops and touches nothing; you attend to it and
    press again. And a fourth, different one: in `looping` (waiting for its
    `/loop`) the `/exit` WOULD go in cleanly, but the loop lives in the memory of
    that agent and the new one would not inherit it -- the handover would run it
    over in silence. It refuses and says so; if that is what you want, stop the
    loop and press again.

    With one exception, which is a clean signal and not a heuristic: a card in
    "working" whose LAST event is still the starting one is not working. Starting
    -- and resuming -- sets "working" because the only event that takes it down
    to "awaiting_input" is the `Stop` at the end of the turn; and if since the
    start there has been neither a prompt (`UserPromptSubmit`) nor a tool
    (`PostToolUse`), there is no turn in flight and the `/exit` lands in a still
    input box. Without this the handover refused precisely on the session just
    resumed (seen live). A card with no `last_event` (an old hook) still blocks:
    when in doubt, nothing is typed.

    With one trim to the exception: the `SessionStart` of a COMPACTION (`source
    == "compact"`) is not a start, and it can land IN THE MIDDLE of a turn --
    claude only compacts when its context fills up -- so there IS work in flight
    and it keeps blocking. The hook records the `source`
    (`startup`/`resume`/`clear`/`compact`/`fork`, the enum of the 2.1.233
    payload); if it is not there (a card from before that note) it goes through,
    which is the common case -- a start or a resume.
    """
    card = card or {}
    # And one that is not about a state at all: the pane is only WATCHING a
    # conversation that lives in a background job (`claude attach <id>`, see
    # `common._attach_rows`). What is on screen belongs to the job, not to this
    # pane, and how that viewer closes has never been measured -- its nearest
    # relative says how wrong the guess can go: in a PARKED pane `/exit` does
    # not exit, it opens the agents view, and any text typed there becomes a NEW
    # background task. So nothing is typed, whatever the conversation's state.
    if card.get("_attach"):
        return ("🔄 this pane is watching a background conversation "
                "(`claude attach`) — open it with ← in any claude, or close "
                "the viewer yourself")
    state = card.get("state")
    # And Claude Code's own registry (`status_cc`, see flightdeck.registry):
    # `busy` with the card in awaiting_input = the Stop fired but there are
    # agents or delegated tasks running (or it has already been woken up again):
    # it is still working.
    if state == "awaiting_input" and status_cc == "busy":
        return ("🔄 that agent is still working (background tasks running) — "
                "wait for it to finish and press again")
    if state == "needs_attention":
        return ("🔄 that agent is waiting for you to answer (a permission) — "
                "answer it and press the handover again")
    if state == "asking":
        return ("🔄 that agent is asking you something (a form) — "
                "answer it and press the handover again")
    if state == "looping":
        # The /loop lives in the memory of the agent being closed: the new one
        # would not inherit it and the handover would run it over without a word.
        return ("🔄 that agent is in a /loop — stop the loop first "
                "(the handover would run it over)")
    just_started = (card.get("last_event") == "SessionStart"
                    and card.get("source") != "compact")
    if state == "working" and not just_started:
        return ("🔄 that agent is still working — wait for it to finish "
                "(or close it yourself) and press the handover again")
    return None


def is_claude_argv(argv):
    """Is that argv claude's? (i.e.: can its flags be copied?)

    It is needed because the card's pid may be RECYCLED: `pid_alive` only checks
    that a process with that number exists, not that it is still the same program
    (an agent killed with `kill -9` leaves its card alive, and the system hands
    that number to the next one). Measured: with a pid that was already the
    `zsh -c` Claude Code runs commands with, four inherited flags came out,
    `['--', '-f', '--', '-P']`, and that leading `--` turns everything behind it
    into a PROMPT.
    """
    try:
        name = Path(str(argv[0])).name
    except (IndexError, TypeError):
        return False
    return name.startswith("claude") or bool(_VERSION.match(name))


def inheritable_flags(argv):
    """The flags the closing agent ran with and the new one inherits.

    Left out: `argv[0]` (the binary), the flags that are not inherited
    (`NON_INHERITABLE`) along with their value, the bare `--` (which would turn
    everything behind it into a PROMPT) and **loose tokens**. If the argv is not
    claude's (a recycled pid), nothing is inherited.

    The loose-token thing is not fussiness: `ps` returns the line FLATTENED, so
    the `claude -n 'pricing 3'` the previous handover typed comes back as four
    tokens and the "3" is orphaned as soon as we drop the `-n`. Sneaking it
    through would be worse than losing it -- on the new line a loose token is the
    initial PROMPT of the agent being born. That is why a token not starting with
    "-" only survives if it is the value of a flag we know takes a value.
    """
    if not is_claude_argv(argv or []):
        return []
    tokens = [str(t) for t in (argv or [])][1:]
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        i += 1
        if not t.startswith("-"):
            continue  # a loose prompt word, or the rest of a flattened value
        if t == "--":
            continue  # "everything that follows is text": just what we do not want
        name, glued, _ = t.partition("=")
        # A flag with a value takes the next token, unless it already carries it
        # glued on (`--model=opus`) or what follows is another flag.
        with_value = (not glued and name in FLAGS_WITH_VALUE
                      and i < len(tokens) and not tokens[i].startswith("-"))
        value = tokens[i] if with_value else None
        if with_value:
            i += 1
        if name in NON_INHERITABLE:
            continue  # that one, and its value, stay here
        out.append(t)
        if value is not None:
            out.append(value)
    return out


def launch_command(title, flags=()):
    """The line typed into the shell to open the handover's agent.

    The `-n` with the title goes FIRST and the inherited flags BEHIND, and that
    order is a defence, not a preference: `FLAGS_WITH_VALUE` can fall short
    (claude adds a new flag that demands a value), and then the flag arrives here
    without its value and claude's parser swallows the next token BLINDLY. With
    the flags in front, what it would swallow is the `-n`, and the title would
    become the FIRST PROMPT of the agent being born: it fails silently and spends
    tokens. Behind the title, the gap takes at most another inherited flag, and
    normally claude complains out loud about the missing value.

    The title is written by a human (`/rename`), so it can carry quotes, `$` or
    semicolons: `shlex.quote` (on every flag too) leaves it as ONE single
    argument, without the shell expanding or executing anything inside it.
    """
    parts = ["claude", "-n", shlex.quote(title)]
    return " ".join(parts + [shlex.quote(str(f)) for f in flags])


def wait_for_shell(read_cmd, timeout=TIMEOUT_SHELL, step=WAIT_STEP):
    """Wait for the pane to be a shell again. False = the time ran out.

    `read_cmd` returns the pane's `pane_current_command`, or None when tmux does
    not answer -- which here counts as "not yet", not as an error.
    """
    limit = time.monotonic() + timeout
    while True:
        if is_shell(read_cmd()):
            return True
        if time.monotonic() >= limit:
            return False
        time.sleep(step)


# --- what talks to the disk and to tmux ------------------------------------

def argv_of_pid(pid):
    """The argv that process started with, split into tokens. [] if it cannot be.

    Mind what this can and cannot do: `ps` gives the line already FLATTENED (the
    spaces inside an argument are indistinguishable from the ones separating
    arguments), so this is a reconstruction, not the real argv.
    `inheritable_flags` is written knowing that.

    Any failure (an odd pid, a process already dead, a `ps` that does not answer)
    returns [] without a fuss: inheriting the flags is a bonus, and if it does not
    work out the handover carries on with a bare `claude`, as it always did.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    if pid <= 0:
        return []
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    line = (r.stdout or "").strip()
    try:
        return shlex.split(line)
    except ValueError:
        # An unclosed quote (a title with an apostrophe flattened by `ps`): we
        # split on spaces, which for reading flags makes no difference.
        return line.split()


def pane_info(pane_id):
    """(tmux session, running command) of that pane. None if it no longer exists.

    Mind the pane that is gone: tmux 3.6a does NOT fail there (verified), it
    exits 0 with an EMPTY line. Without this detail, an old card pointing at a
    dead pane got as far as the `/exit` and ended in a "did not close in 20 s"
    that explained nothing; an empty session name = that pane does not exist.
    """
    r = tmux("display-message", "-p", "-t", pane_id,
             "#{session_name}\t#{pane_current_command}")
    if not r or r.returncode != 0:
        return None
    parts = (r.stdout.strip("\n").split("\t") + ["", ""])[:2]
    return (parts[0], parts[1]) if parts[0] else None


def status_cc_of(card, registry=None):
    """The `status` from Claude Code's registry (busy/idle/waiting/shell) for
    that card's pid, or None."""
    if not card or not isinstance(card.get("pid"), int):
        return None
    if registry is None:
        try:
            registry = read_registry()
        except Exception:
            return None
    for e in registry or []:
        if e.get("pid") == card["pid"]:
            return e.get("status") or None
    return None


def parked_job(card, registry=None):
    """The SHORT id of the job the conversation of that card's agent is parked in
    (`/background` or the ← arrow), or None when it is not parked.

    It is looked up in Claude Code's own registry (`flightdeck.registry`), by the
    card's PID: there the pane's interactive claude carries `parkedJobId`.
    """
    if not card or not isinstance(card.get("pid"), int):
        return None
    if registry is None:
        try:
            registry = read_registry()
        except Exception:
            return None
    for e in registry or []:
        if e.get("pid") == card["pid"]:
            return e.get("parked_job") or None
    return None


def press(pane_id, key):
    """Send ONE key (a tmux key name: `C-c`, `Escape`...) to the pane.

    This is the opposite of `type_keys`: here we WANT tmux to interpret the name.
    False if tmux could not (a pane that no longer exists: rc 1, without touching
    anywhere else).
    """
    r = tmux("send-keys", "-t", pane_id, key)
    return bool(r) and r.returncode == 0


# The pause after each ctrl+c used to close a PARKED agent, before looking for a
# shell: it gives the agent time to react, and keeps the presses inside the
# window of its "press again to quit" (a matter of seconds).
CTRL_C_PAUSE = 0.4
# The press cap. It took FOUR live: the 1st eats the half-written text, the 2nd
# goes to the agents view, the 3rd and the 4th are that view's "ctrl+c twice
# quits". Some slack is left; the rest lands in the shell (a ^C on an empty
# prompt does nothing).
MAX_CTRL_C = 6


def close_parked(pane_id, read_cmd, pause=CTRL_C_PAUSE, sleep=None,
                 cap=MAX_CTRL_C):
    """Close a pane's PARKED agent: ctrl+c until there is a shell, typing no text.

    `sleep` defaults to None rather than to `time.sleep` itself, and is resolved
    below: a default bound in the signature is bound when this module is
    IMPORTED, so `handover()`'s call kept the real `time.sleep` even with the
    module's patched -- which is how the suite came to spend nine seconds
    genuinely waiting for panes that do not exist.

    Measured with 2.1.234: in a parked claude `/exit` does NOT exit --
    it opens the agents view ("Your conversation moved to the background… ctrl+c
    twice quits") -- and there any TEXT typed becomes a NEW background TASK
    ("describe a task for a new session"), so nothing is written here: keys only.
    And from the conversation view a ctrl+c also goes to that view first (with
    three fixed presses it got stuck there). That is why it is not a number:
    press, wait `pause`, and look at `read_cmd()`; if it is a shell already,
    done. With the cap reached it returns True anyway and `wait_for_shell`
    decides (it keeps looking until TIMEOUT_SHELL). The background job survives
    all of this. False only if the pane went away on the way.
    """
    sleep = time.sleep if sleep is None else sleep
    for _ in range(cap):
        if not press(pane_id, "C-c"):
            return False
        sleep(pause)
        if is_shell(read_cmd()):
            return True
    return True


def type_keys(pane_id, text):
    """Type a line + Enter into the pane. False if tmux could not.

    Looking at the result is not a luxury: `send-keys` to a pane that no longer
    exists fails (rc 1, without typing anywhere else -- verified), and without
    checking it we would announce a handover that never happened.

    The INVARIANT this single `send-keys` depends on (as against the picker's
    `-l --`, see `flightdeck.picker._session_with_command`): the FIRST TOKEN of
    the text is always a literal written by hand -- `/exit` or the `claude` of
    `launch_command`. That is why it never starts with a dash (which tmux takes
    for a flag: `invalid flag`, rc 1) nor is a key name (`Enter`, `C-c`, which
    tmux INTERPRETS -- a `C-c` like that kills the pane's process, measured in
    3.6a); and what comes behind goes through `shlex.quote`, which leaves it
    closed in quotes. The day the text comes from the user -- a "handover with
    your own line" -- this invariant breaks and we have to move to `-l --` with
    the Enter separately.
    """
    r = tmux("send-keys", "-t", pane_id, text, "Enter")
    return bool(r) and r.returncode == 0


def current_cmd(pane_id):
    """The pane's `pane_current_command`, or None if tmux does not answer."""
    r = tmux("display-message", "-p", "-t", pane_id, "#{pane_current_command}")
    return r.stdout.strip() if r and r.returncode == 0 else None


# How long the floating notice lasts (`display-message -d`); without `-d` tmux
# uses its display-time (750 ms out of the box: a flash). This is the FALLBACK;
# the value in force comes from the config (`notice_ms`). The state hook and the
# status line tee read the same key, so the 🧠 notice cannot end up lasting a
# different time from the "waiting for you" one. A cross-check test pins that
# the three agree.
NOTICE_MS = config.DEFAULTS["notice_ms"]


def _notice_ms():
    """The notice duration in force, read from the config when it is shown.

    It falls back to the default instead of raising, for the same reason the
    status bar does: a `notice_ms` the user typed as a word must not swallow the
    notice -- and being told nothing after pressing the key is the one
    unacceptable outcome here. `doctor` is the place that complains about a value
    of the wrong type.
    """
    try:
        return int(config.load()["notice_ms"])
    except (KeyError, TypeError, ValueError):
        return NOTICE_MS


def notify(text):
    """Report something on screen: stderr + a floating message on every tmux.

    Same pattern as the state hook: we send it to ALL connected clients (you may
    be watching from your phone), and `common.notice_args` builds the argv for
    the tmux that is actually installed -- text treated as text (`-l` from 3.4,
    the `#` escaped below that), `-d <notice_ms>` so there is time to read it and
    `-C` so the pane keeps painting meanwhile (without it, tmux freezes that
    client until the message goes -- measured in 3.6a).
    """
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    duration = _notice_ms()
    r = tmux("list-clients", "-F", "#{client_name}")
    clients = (r.stdout or "").split() if r and r.returncode == 0 else []
    if not clients:
        return
    # Asked for once there is somebody to tell: with no tmux at all this saves
    # the `tmux -V` that nobody would read.
    version = tmux_version()
    for client in clients:
        tmux(*notice_args(text, version, duration, client))


def handover(pane_id):
    """The whole handover on one pane. Returns the new title, or None.

    None = there was no handover, and always with its notice: silence is the one
    unacceptable result here (you press a key and you have to see something,
    either the new agent or the reason why not).
    """
    info = pane_info(pane_id)
    if info is None:
        notify("🔄 the handover cannot find that pane (%s)" % pane_id)
        return None
    session, running = info
    if is_reserved(session, configured_pins()):
        notify("🔄 the handover does not run in «%s» — that session belongs to "
               "Flightdeck itself" % session)
        return None
    card = card_for_pane(load_sessions(), pane_id)
    if card is None:
        notify("🔄 there is no live agent to hand over in this pane")
        return None
    # A pane that is only WATCHING a conversation in a background job
    # (`claude attach`, see `common._attach_rows`) is refused HERE, before
    # anything else is worked out and whatever the pane is running. It cannot
    # wait for the guards further down, because those live inside "the pane is
    # not a shell": a viewer SUSPENDED with ctrl+z leaves
    # `pane_current_command` reading `zsh`, the pane would pass for an empty one
    # and the launch line would be typed into the shell the viewer is sleeping
    # in -- on top of the job it is watching.
    if card.get("_attach"):
        notify(reason_not_to_close(card))
        return None
    tool = card.get("tool") or "claude"
    # The title is read BEFORE closing anything: if something goes wrong from
    # here on, we will not have touched the agent for a piece of data we did not
    # have.
    new = new_title(card.get("session_id"), session)
    # And the flags, also before: we ask the old process (the card's `pid`),
    # which will be gone the moment we send it the `/exit`.
    old_argv = argv_of_pid(card.get("pid"))
    # And with them, the whole line of the one being BORN: if the card's tool is
    # not one we know how to relaunch (`handover_command` = None), we have not
    # touched anything yet -- closing it first would leave the pane in a mute
    # shell.
    if tool != "claude":
        from flightdeck.tools import handover_command, inheritable_flags_for
        # The recycled-pid guard already lives inside `inheritable_flags_for`.
        line = handover_command(tool, new, inheritable_flags_for(tool, old_argv))
        if line is None:
            notify("🔄 I don't know how to relaunch «%s»" % tool)
            return None
    else:
        line = launch_command(new, inheritable_flags(old_argv))
    # Is the conversation PARKED (/background or the ← arrow)? Then the pane's
    # agent is a watcher: its card is an echo (notifications from the job, or the
    # turn it was parked in) and the state guards are no good; it is closed with
    # keys (see `close_parked`) and the background job stays alive -- which we
    # say.
    # (A viewer never reaches this: it was refused above. Which is what has to
    # happen, because the card's pid is the JOB's -- asking the registry about it
    # would answer for the job, which is not parked, and the ctrl+c branch would
    # fire at the wrong pane.)
    parked = parked_job(card)
    # A pane that is already a shell (a live card but a fallen agent) skips this
    # whole stretch: there is nobody to type `/exit` at, only the new one to open.
    if not is_shell(running):
        if parked:
            if not close_parked(pane_id, lambda: current_cmd(pane_id)):
                notify("🔄 the handover could not type into that pane")
                return None
        else:
            reason = reason_not_to_close(card, status_cc=status_cc_of(card))
            if reason:
                notify(reason)
                return None
            if not type_keys(pane_id, "/exit"):
                notify("🔄 the handover could not type into that pane")
                return None
        # The times go in explicitly, not by default: the notice below reads them
        # while it is hot, and with the default frozen in the signature they
        # could end up quoting a different number from the one actually waited.
        if not wait_for_shell(lambda: current_cmd(pane_id),
                              TIMEOUT_SHELL, WAIT_STEP):
            notify("🔄 the agent did not close in %d s — I did not touch "
                   "anything else; close it yourself and press again"
                   % int(TIMEOUT_SHELL))
            return None
    if not type_keys(pane_id, line):
        notify("🔄 the pane went away just as the handover started — open it "
               "yourself")
        return None
    if parked:
        notify("🔄 handover → «%s» · the backgrounded conversation stays in the "
               "background (job %s; ← in any claude to see it)" % (new, parked))
    else:
        notify("🔄 handover → «%s»" % new)
    return new


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != "handover":
        sys.stderr.write(
            "usage: python3 -m flightdeck.handover handover <pane_id>\n")
        return 1
    try:
        handover(argv[1])
    except Exception as e:
        # A handover that fails is reported; what cannot happen is tmux spitting
        # a traceback over the pane and the user not knowing how it was left.
        try:
            notify("🔄 the handover failed (%.80s)" % e)
        except Exception:
            pass
    # Always 0: a non-zero rc gets announced by tmux with a "returned N" that
    # explains nothing, and explaining is what we have already done.
    return 0


if __name__ == "__main__":
    sys.exit(main())
