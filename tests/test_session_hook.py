"""The state hook, tested the way the agent calls it: argv + payload on stdin.

`session_hook.py` is run BY ABSOLUTE PATH (`python3
<code>/flightdeck/hooks/session_hook.py Stop`) by a tool that knows nothing
about Flightdeck, so that is how most of these tests run it too: a subprocess,
with `FLIGHTDECK_STATE_DIR` pointing at a temporary directory, so every case
gets a fresh one and nothing touches the real session cards.

Two contracts are checked for free on every run: the hook exits 0 and prints
NOTHING to stdout (whatever it printed there would be injected into the agent's
session as context). The one exception is `--tool agy`, which answers exactly
`{}` -- agy reads a JSON object back from its hooks.

And the hook DOES talk to tmux on `Stop`/`Notification` (that is where it sends
the "waiting for you" notice to every client), so it is given a
`FLIGHTDECK_TMUX_SOCKET` pointing at a socket that does NOT exist: tmux finds no
server, fails, and the hook swallows it. Without that line the notice would land
in the real tmux status bar of whoever runs the suite -- a false "waiting for
you" from a session that does not exist.

A handful of cases cannot be reached from a subprocess: they are about a module
next to the hook being missing, and here every module lives in the same
package. Those run the hook IN PROCESS (`sh.main()` with argv, stdin and the
environment patched) and hide the module with `sys.modules[...] = None`, which
is what makes an `import` of it raise.
"""
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import SUBPROCESS_HOME, broken_package_copy
from flightdeck import config
from flightdeck import handover as hv
from flightdeck.hooks import context_tee as tee
from flightdeck.hooks import session_hook as sh

HOOK = config.code_dir() / "flightdeck" / "hooks" / "session_hook.py"

# The hook reads the environment (TMUX_PANE, CLAUDE_SESSION_ID...); it is run
# clean so the result does not depend on the suite being launched inside tmux,
# or on the state directory of whoever runs it.
_OUT = ("TMUX_PANE", "TMUX", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR",
        "FLIGHTDECK_STATE_DIR", "FLIGHTDECK_CONFIG", "FLIGHTDECK_TMUX_SOCKET",
        "FLIGHTDECK_PARENT_ARGV_TEST", "FLIGHTDECK_NOTICE_WAIT_TEST",
        "FLIGHTDECK_REGISTRY_TEST", "HOME")

# A made-up tmux socket: any `tmux -L <this> ...` fails with "no server". It is
# what keeps the real tmux out of the suite (see the docstring).
_DEAD_SOCKET = "flightdeck-hooktest-nonexistent"

_INTERACTIVE = "claude --dangerously-skip-permissions"


def _clean_env(state_dir, **extra):
    """The environment a hook run gets: no leaks in, everything pointed at `tmp`."""
    env = {k: v for k, v in os.environ.items() if k not in _OUT}
    env["FLIGHTDECK_TMUX_SOCKET"] = _DEAD_SOCKET
    env["FLIGHTDECK_STATE_DIR"] = str(state_dir)
    # A home of the suite's own: the hook looks the conversation's title up in
    # `~/.claude/projects` (read only, one glob) and the deferred notice reads
    # Claude Code's registry in `~/.claude/sessions`. With this, neither reaches
    # the real home of whoever runs the suite.
    env["HOME"] = SUBPROCESS_HOME
    # A config file that does not exist: the defaults, and never the config of
    # whoever is running the suite.
    env["FLIGHTDECK_CONFIG"] = str(Path(state_dir) / "config.json")
    # The hook looks at its parent's argv to skip the `claude --print`s; here the
    # parent would be the test runner (`python3 -m unittest ... -p pattern` even
    # carries a `-p` that is not claude's): it is given an interactive argv.
    env["FLIGHTDECK_PARENT_ARGV_TEST"] = _INTERACTIVE
    env.update(extra)
    return env


class _RunsTheHook(unittest.TestCase):
    """Shared harness: the hook as a subprocess, called the way the agent calls it."""

    def _run(self, folder, event, payload, env_extra=None):
        """One hook event with `folder` as the state directory. Returns the card."""
        env = _clean_env(folder, **(env_extra or {}))
        r = subprocess.run([sys.executable, str(HOOK), event],
                           input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=20)
        # Two of the hook's contracts, free on every case: it exits 0 and prints
        # NOTHING to stdout (what it printed there would be injected into the
        # agent as context).
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        card = Path(folder) / "sessions" / ("%s.json" % payload["session_id"])
        return json.loads(card.read_text())

    def _card(self, folder, session_id):
        f = Path(folder) / "sessions" / ("%s.json" % session_id)
        return json.loads(f.read_text()) if f.exists() else None

    def _run_in_process(self, folder, event, payload, tool=None, env_extra=None,
                        real_refresh=False):
        """The hook called inside this process. -> its stdout (`""`, or `"{}\\n"`).

        Only used where the case is a module next to the hook being MISSING.
        Here the package is always complete, so the module is hidden with
        `sys.modules[name] = None` instead -- and that only reaches a hook
        running in this process.

        The hook's two side effects that leave a PROCESS behind -- the Stop's
        deferred notice and the menu refresh -- are stubbed by default. In
        process they were reaching the real `Popen`, so every in-process `Stop`
        launched a notice that waits a second and a half and outlived the test
        (the suite's `ResourceWarning: subprocess N is still running`).
        `real_refresh=True` is for the three cases that are ABOUT the refresh:
        they replace `Popen` themselves and look at the argv.
        """
        argv = ["session_hook.py", event] + (["--tool", tool] if tool else [])
        env = _clean_env(folder, **(env_extra or {}))
        # `mock.patch.dict` does not clear: the ones that must NOT be there are
        # emptied, and an empty variable is an unset one for everything that
        # reads them.
        for name in _OUT:
            env.setdefault(name, "")
        out = io.StringIO()
        stubs = [mock.patch.object(sh, "launch_deferred_notice",
                                   lambda *a, **k: None)]
        if not real_refresh:
            stubs.append(mock.patch.object(sh, "refresh_menus",
                                           lambda *a, **k: None))
        with contextlib.ExitStack() as stack:
            for stub in stubs:
                stack.enter_context(stub)
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.object(
                sys, "stdin", io.StringIO(json.dumps(payload))))
            stack.enter_context(mock.patch.object(sys, "stdout", out))
            with self.assertRaises(SystemExit) as caught:
                sh.main()
        self.assertEqual(caught.exception.code, 0)
        return out.getvalue()


class TestTheHookRecordsWhyItStarted(_RunsTheHook):
    """`source` on the card: the fact that tells a start from a compaction."""

    def test_it_records_the_source_of_a_SessionStart(self):
        for source in ("startup", "resume", "clear", "compact", "fork"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(source=source):
                card = self._run(tmp, "SessionStart",
                                 {"session_id": "sid-1", "source": source})
                self.assertEqual(card["source"], source)
                # And the usual, which is the other half of the signal.
                self.assertEqual(card["last_event"], "SessionStart")
                self.assertEqual(card["state"], "working")

    def test_a_SessionStart_without_a_source_does_not_blow_up(self):
        """The `.get` never raises: the card is left with `source` at None."""
        with tempfile.TemporaryDirectory() as tmp:
            card = self._run(tmp, "SessionStart", {"session_id": "sid-2"})
            self.assertIsNone(card["source"])

    def test_a_compaction_in_the_middle_of_a_turn_shows_on_the_card(self):
        """The real sequence: prompt, tool, and claude compacts by itself.

        This is the case the handover has to be able to tell apart: at the end
        the card says `working` + `SessionStart` exactly like a start, and the
        only thing that gives it away is the `source`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, "UserPromptSubmit", {"session_id": "sid-3"})
            self._run(tmp, "PostToolUse", {"session_id": "sid-3"})
            card = self._run(tmp, "SessionStart",
                             {"session_id": "sid-3", "source": "compact"})
            self.assertEqual((card["state"], card["last_event"], card["source"]),
                             ("working", "SessionStart", "compact"))

    def test_the_other_events_do_not_invent_a_source(self):
        """Only the `SessionStart` writes it; the rest neither set nor clear it."""
        with tempfile.TemporaryDirectory() as tmp:
            only_stop = self._run(tmp, "Stop", {"session_id": "sid-4"})
            self.assertNotIn("source", only_stop)
            self._run(tmp, "SessionStart",
                      {"session_id": "sid-4", "source": "resume"})
            after_prompt = self._run(tmp, "UserPromptSubmit",
                                     {"session_id": "sid-4"})
            # Still there (the handover only looks at it when `last_event` is
            # SessionStart).
            self.assertEqual(after_prompt["source"], "resume")
            self.assertEqual(after_prompt["last_event"], "UserPromptSubmit")


class TestTheAskingState(unittest.TestCase):
    """The newer state: Claude has opened a form and is waiting for you to pick.

    An EXACT signal, not a guess: the hook registers on `PreToolUse` with a
    matcher by tool name, and the two that ask are `AskUserQuestion` (the
    multiple-choice menu) and `ExitPlanMode` (the "does the plan look right?").
    """

    def test_the_two_tools_that_ask(self):
        for tool in ("AskUserQuestion", "ExitPlanMode"):
            self.assertEqual(
                sh.state_for("PreToolUse", {"tool_name": tool}), "asking", tool)

    def test_any_other_tool_is_working(self):
        """A defence in case the matcher is installed wrong: a `Bash` is not a form.

        Without this, a matcher that was too wide would paint "asking you" on
        every tool Claude uses, which is the opposite of a signal.
        """
        for tool in ("Bash", "Read", "Edit", "EnterPlanMode", "", None):
            self.assertEqual(
                sh.state_for("PreToolUse", {"tool_name": tool}), "working", tool)

    def test_a_PreToolUse_without_a_tool_name_does_not_blow_up(self):
        self.assertEqual(sh.state_for("PreToolUse", {}), "working")

    def test_the_usual_states_do_not_move(self):
        self.assertEqual(sh.state_for("SessionStart", {}), "working")
        self.assertEqual(sh.state_for("Stop", {}), "awaiting_input")
        self.assertEqual(sh.state_for("SessionEnd", {}), "ended")
        self.assertEqual(sh.state_for("Notification",
                                      {"message": "needs your permission"}),
                         "needs_attention")


class TestTheLoopingState(unittest.TestCase):
    """An agent that finishes a `/loop` ROUND is not waiting for you: it is
    waiting for its timer. `state_for` is handed the decision already made
    (`loop=`, worked out by `flightdeck.turn` reading the transcript) and on the
    `Stop` paints "looping" instead of "awaiting_input"."""

    def test_a_Stop_in_a_loop_leaves_looping(self):
        self.assertEqual(sh.state_for("Stop", {}, loop=True), "looping")
        self.assertEqual(sh.state_for("Stop", {}, loop=False), "awaiting_input")
        self.assertEqual(sh.state_for("Stop", {}), "awaiting_input")

    def test_the_loop_only_matters_on_the_Stop(self):
        for event, expected in (("UserPromptSubmit", "working"),
                                ("PostToolUse", "working"),
                                ("SessionStart", "working"),
                                ("SessionEnd", "ended")):
            self.assertEqual(sh.state_for(event, {}, loop=True), expected, event)

    def test_the_idle_reminder_does_not_take_it_out_of_the_loop(self):
        # After 60 s idle Claude Code sends "Claude is waiting for your input":
        # with the card looping, it stays looping (same rule as `asking`).
        self.assertEqual(
            sh.state_for("Notification",
                         {"message": "Claude is waiting for your input"},
                         previous_state="looping"), "looping")

    def test_the_next_round_or_a_prompt_of_yours_put_it_to_work(self):
        self.assertEqual(
            sh.state_for("UserPromptSubmit", {}, previous_state="looping"),
            "working")


class TestTheNoticeText(unittest.TestCase):
    def test_in_a_loop_nothing_is_announced(self):
        """The Stop of a loop round does not say "waiting for you": it is not."""
        self.assertIsNone(sh.notice_text("Stop", "looping", "nightwatch"))
        self.assertIsNone(sh.notice_text("Notification", "looping", "nightwatch"))

    """What gets said to the tmux status bar, and when it keeps quiet."""

    def test_asking_has_its_own_notice(self):
        self.assertEqual(sh.notice_text("PreToolUse", "asking", "pricing"),
                         "❓ pricing is asking you")

    def test_the_6s_reminder_repeats_the_asking_notice(self):
        """The `Notification` Claude Code sends with the form open leaves the
        card `asking`, and the notice says the same (a second nudge, not a
        "waiting for you" that contradicts the menu)."""
        self.assertEqual(sh.notice_text("Notification", "asking", "pricing"),
                         "❓ pricing is asking you")

    def test_finishing_the_turn_and_asking_permission_still_announce(self):
        self.assertEqual(sh.notice_text("Stop", "awaiting_input", "pricing"),
                         "⏳ pricing is waiting for you")
        self.assertEqual(sh.notice_text("Notification", "needs_attention", "x"),
                         "⏳ x is waiting for you")

    def test_ordinary_work_says_nothing(self):
        """A notice per tool executed would be a constant flicker."""
        for event, st in (("PreToolUse", "working"), ("PostToolUse", "working"),
                          ("UserPromptSubmit", "working"),
                          ("SessionStart", "working"), ("SessionEnd", "ended")):
            self.assertIsNone(sh.notice_text(event, st, "pricing"), event)


class TestTheFormReminderDoesNotOverrideAsking(unittest.TestCase):
    """With a form open, Claude Code sends its OWN reminder after 6 s
    (`Notification`, and with the generic text "Claude needs your permission"
    for the options menu; "Claude Code needs your approval for the plan" for the
    plan -- read from the 2.1.233 binary). Without this, that reminder
    overwrote `asking` with `needs_attention` and the menu said "needs
    attention" in front of a form, seen live.
    """

    def test_a_notification_with_the_card_asking_keeps_asking(self):
        for msg in ("Claude needs your permission",
                    "Claude Code needs your approval for the plan",
                    "Claude is waiting for your input", ""):
            self.assertEqual(
                sh.state_for("Notification", {"message": msg},
                             previous_state="asking"),
                "asking", msg)

    def test_with_another_previous_state_the_notification_wins(self):
        for previous in ("working", "awaiting_input", "needs_attention", None):
            self.assertEqual(
                sh.state_for("Notification",
                             {"message": "Claude needs your permission"},
                             previous_state=previous),
                "needs_attention", previous)
            self.assertEqual(
                sh.state_for("Notification",
                             {"message": "Claude is waiting for your input"},
                             previous_state=previous),
                "awaiting_input", previous)

    def test_the_previous_state_only_matters_on_a_notification(self):
        """Answering (PostToolUse) or writing (UserPromptSubmit) DO take it out
        of `asking`: the previous state is not a latch, it only covers the
        reminder."""
        self.assertEqual(
            sh.state_for("PostToolUse", {"tool_name": "AskUserQuestion"},
                         previous_state="asking"), "working")
        self.assertEqual(sh.state_for("UserPromptSubmit", {},
                                      previous_state="asking"), "working")
        self.assertEqual(sh.state_for("Stop", {}, previous_state="asking"),
                         "awaiting_input")


class TestTheHookWritesTheAskingState(_RunsTheHook):
    """End to end: the event comes in and the card is left "asking"."""

    def test_an_AskUserQuestion_leaves_the_card_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = self._run(tmp, "PreToolUse",
                             {"session_id": "sid-5",
                              "tool_name": "AskUserQuestion"})
            self.assertEqual(card["state"], "asking")
            self.assertEqual(card["last_event"], "PreToolUse")

    def test_the_6s_reminder_does_not_take_it_out_of_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, "PreToolUse", {"session_id": "sid-7",
                                          "tool_name": "AskUserQuestion"})
            card = self._run(tmp, "Notification",
                             {"session_id": "sid-7",
                              "message": "Claude needs your permission",
                              "notification_type": "permission_prompt"})
            self.assertEqual(card["state"], "asking")
            self.assertEqual(card["last_event"], "Notification")

    def test_answering_puts_it_back_to_working_via_the_PostToolUse(self):
        """Nothing new is needed to "un-ask": answering USES that tool, and the
        `PostToolUse` of that same tool already takes the card back to working."""
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, "PreToolUse", {"session_id": "sid-6",
                                          "tool_name": "AskUserQuestion"})
            card = self._run(tmp, "PostToolUse",
                             {"session_id": "sid-6",
                              "tool_name": "AskUserQuestion"})
            self.assertEqual(card["state"], "working")


class _WithFakeTmux(_RunsTheHook):
    """Harness with a fake `tmux` on the PATH that records every call.

    The usual harness points at a dead socket (no tmux answers); here we need to
    SEE the argv, so a fake `tmux` is put in front of the PATH: it appends every
    call to a file and answers two clients to `list-clients`. It still does not
    touch the real tmux: the fake one wins on the PATH.
    """

    def _run_with_fake_tmux(self, tmp, event, payload, env_extra=None, args=(),
                            expected_stdout="", version="tmux 3.6a"):
        binary = Path(tmp) / "bin"
        binary.mkdir(exist_ok=True)
        log = Path(tmp) / "tmux.log"
        # It answers `-V` too: the notice's argv depends on the version (`-l` is
        # tmux >= 3.4), and a test must not be told what the developer's own tmux
        # happens to be. `version` is what this fake claims to be.
        (binary / "tmux").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> 'LOG'\n"
            "case \"$*\" in\n"
            "  -V) printf 'VERSION\\n';;\n"
            "  *list-clients*) printf '/dev/ttys001\\n/dev/ttys002\\n';;\n"
            "esac\n"
            "exit 0\n".replace("LOG", str(log)).replace("VERSION", version))
        (binary / "tmux").chmod(0o755)
        env = _clean_env(tmp, **(env_extra or {}))
        env.pop("FLIGHTDECK_TMUX_SOCKET", None)
        env["PATH"] = "%s:%s" % (binary, env.get("PATH", ""))
        r = subprocess.run([sys.executable, str(HOOK), event, *args],
                           input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        # agy's stdout is `{}` (its answer); every other tool prints nothing.
        self.assertEqual(r.stdout, expected_stdout)
        return log.read_text().splitlines() if log.exists() else []


class TestTheNoticeTheHookSends(_WithFakeTmux):
    """The hook's `display-message`, seen from the fake `tmux`."""

    def test_one_display_message_per_client_with_a_duration_and_no_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            displays = self._run_deferred_stop(tmp, "idle")
        # `-d`: without it, the factory display-time (750 ms) makes it a flash.
        # `-C`: without it the pane freezes in that client while it lasts
        # (measured in tmux 3.6a). `-l`: literal, a project called "#(something)"
        # is not executed.
        self.assertEqual(displays, [
            "display-message -c /dev/ttys001 -d %d -C -l ⏳ pricing is waiting for you"
            % sh.NOTICE_MS,
            "display-message -c /dev/ttys002 -d %d -C -l ⏳ pricing is waiting for you"
            % sh.NOTICE_MS,
        ])

    def _fake_registry(self, tmp, status, pid=None):
        """A fake `~/.claude/sessions/<pid>.json` (FLIGHTDECK_REGISTRY_TEST)."""
        reg = Path(tmp) / "registry"
        reg.mkdir(exist_ok=True)
        (reg / ("%d.json" % (pid or 4242))).write_text(json.dumps(
            {"pid": pid or 4242, "sessionId": "sid-8", "kind": "interactive",
             "status": status}))
        return reg

    @staticmethod
    def _displays(log):
        """The `display-message` lines the fake tmux has written so far."""
        if not log.exists():
            return []
        return [l for l in log.read_text().splitlines()
                if l.startswith("display-message")]

    def _run_deferred_stop(self, tmp, status, wait_s=5.0, payload=None,
                           env_extra=None, expect_new=2):
        """A Stop with the registry saying `status`; returns every
        `display-message` line the fake tmux has, once the deferred notice has
        had its chance.

        The Stop's notice is sent by a SEPARATE process (`launch_deferred_notice`)
        which runs one `tmux display-message` per client, so the lines arrive one
        at a time and the test has to wait for them. It waits for the NUMBER it
        expects rather than for a fixed time: the first version waited for the
        first line and then slept a flat 0.3 s "to give the other clients time",
        and on a loaded machine the second line missed that window -- two
        failures in eight runs here, and CI runs on slower hardware than this.

        `expect_new` counts lines that are NEW since before the hook ran, which
        is what lets the same helper be called twice in one temporary directory
        (`TestOneNoticePerChange`): the second call is asking whether anything
        MORE arrives, and the first call's two lines are still in the log.

        `expect_new=0` means "nothing should come", and there the full `wait_s`
        is spent on purpose -- returning early would prove nothing at all. Those
        callers pass a short one.
        """
        import time
        log = Path(tmp) / "tmux.log"
        before = len(self._displays(log))
        extra = {"FLIGHTDECK_NOTICE_WAIT_TEST": "0",
                 "FLIGHTDECK_REGISTRY_TEST": str(self._fake_registry(tmp, status))}
        extra.update(env_extra or {})
        self._run_with_fake_tmux(
            tmp, "Stop", payload or {"session_id": "sid-8", "cwd": "/x/pricing"},
            env_extra=extra)
        end = time.time() + wait_s
        while time.time() < end:
            if expect_new and len(self._displays(log)) - before >= expect_new:
                break
            time.sleep(0.05)
        return self._displays(log)

    def test_the_Stop_notice_is_deferred_and_goes_out_if_the_registry_is_not_busy(self):
        """Claude Code's registry changes a few ms AFTER the hook (measured:
        8 ms), so the Stop's notice is sent by a separate process that waits
        NOTICE_WAIT_S, re-reads, and only speaks if the session is still idle
        and the registry does not say busy."""
        for status in ("idle", "shell", "waiting", None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                displays = self._run_deferred_stop(tmp, status)
                self.assertEqual(len(displays), 2, displays)
                self.assertIn("⏳ pricing is waiting for you", displays[0])

    def test_with_the_registry_busy_the_Stop_does_not_announce(self):
        # Agents or delegated tasks running (or already woken up again): it is
        # not waiting for you.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._run_deferred_stop(tmp, "busy", wait_s=1.0, expect_new=0),
                [])

    def test_if_the_session_is_no_longer_idle_on_the_re_read_it_does_not_announce(self):
        # Between the Stop and the re-read a UserPromptSubmit arrived (woken up
        # again by a background task): the card no longer says awaiting_input.
        with tempfile.TemporaryDirectory() as tmp:
            reg = self._fake_registry(tmp, "idle")
            (Path(tmp) / "sessions").mkdir(parents=True)
            (Path(tmp) / "sessions" / "sid-8.json").write_text(json.dumps(
                {"session_id": "sid-8", "state": "working",
                 "last_event": "UserPromptSubmit"}))
            lines = self._run_with_fake_tmux(
                tmp, "deferred-notice", {},
                args=["sid-8", "⏳ pricing is waiting for you"],
                env_extra={"FLIGHTDECK_NOTICE_WAIT_TEST": "0",
                           "FLIGHTDECK_REGISTRY_TEST": str(reg)})
            self.assertEqual(
                [l for l in lines if l.startswith("display-message")], [])

    def test_the_asking_and_attention_notices_are_still_immediate(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._run_with_fake_tmux(
                tmp, "PreToolUse", {"session_id": "sid-8", "cwd": "/x/pricing",
                                    "tool_name": "AskUserQuestion"})
            self.assertTrue(any("❓ pricing is asking you" in l for l in lines),
                            lines)

    def test_the_notice_says_the_tmux_session_name_not_the_folder(self):
        """"prototypes is waiting for you" says nothing: it is the basename of
        the cwd, and there may well be TWO claudes in that folder, in the tmux
        sessions `recommender` and `recommender#3`. The notice has to say
        the SAME name as the status bar and the green rows (the tmux session's),
        as the tee already does (`name_for_notice`); the folder is the fallback
        when there is no tmux."""
        with tempfile.TemporaryDirectory() as tmp:
            # The previous card carries tmux_session (an event with a pane wrote
            # it); the Stop arrives with no TMUX_PANE (the harness takes it out
            # of the environment) and even so the notice must use the card's
            # session name.
            (Path(tmp) / "sessions").mkdir(parents=True)
            (Path(tmp) / "sessions" / "sid-8.json").write_text(json.dumps(
                {"session_id": "sid-8", "tmux_session": "recommender#3",
                 "project": "prototypes", "state": "working",
                 "last_event": "PostToolUse"}))
            displays = self._run_deferred_stop(
                tmp, "idle", payload={"session_id": "sid-8",
                                      "cwd": "/x/forecasting/prototypes"})
            self.assertEqual(len(displays), 2, displays)
            self.assertIn("⏳ recommender#3 is waiting for you", displays[0])
            self.assertNotIn("prototypes", displays[0])

    def test_the_three_notice_emitters_last_the_same(self):
        """The hook, the tee and the handover each announce on their own (three
        entry points, no shared notice code); the duration has to be the SAME in
        all three or the 🧠 notice would last a different time from the "waiting
        for you" one. Three separate emitters could each carry their own `5000`
        and drift; they share one source of truth instead -- the config's
        `notice_ms` -- so the cross-check is that the three read that key."""
        self.assertEqual(sh.NOTICE_MS, config.DEFAULTS["notice_ms"])
        self.assertEqual(tee.NOTICE_MS, config.DEFAULTS["notice_ms"])
        self.assertEqual(hv.NOTICE_MS, config.DEFAULTS["notice_ms"])


class TestTheHookNoticeDuration(_WithFakeTmux):
    """The duration in force comes from the config.

    The other two thirds of the cross-check live in `test_handover`
    (`TestNoticeDuration`) and `test_context_tee`.
    """

    def test_a_configured_duration_reaches_display_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"notice_ms": 8000}))
            lines = self._run_with_fake_tmux(
                tmp, "PreToolUse", {"session_id": "sid-ms", "cwd": "/x/pricing",
                                    "tool_name": "AskUserQuestion"},
                env_extra={"FLIGHTDECK_CONFIG": str(cfg)})
        display = [l for l in lines if l.startswith("display-message")][0]
        self.assertIn(" -d 8000 -C -l ", display)

    def test_an_unusable_duration_falls_back_instead_of_swallowing_the_notice(self):
        """A `notice_ms` typed as a word must not swallow the notice: `doctor`
        is the place that complains about a value of the wrong type."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"notice_ms": "five seconds"}))
            lines = self._run_with_fake_tmux(
                tmp, "PreToolUse", {"session_id": "sid-ms2", "cwd": "/x/pricing",
                                    "tool_name": "AskUserQuestion"},
                env_extra={"FLIGHTDECK_CONFIG": str(cfg)})
        display = [l for l in lines if l.startswith("display-message")][0]
        self.assertIn(" -d %d -C -l " % sh.NOTICE_MS, display)


class TestTheNoticeOnAnOldTmux(_WithFakeTmux):
    """`display-message -l` is tmux >= 3.4, and Ubuntu 22.04 (3.2a) and Debian 12
    (3.3a) are inside the supported range. Below 3.4 the message is read as a
    FORMAT, so the text is escaped (`#` -> `##`) instead: a project called
    `#(rm -rf /)` must never be run just because it got announced."""

    def _asks(self, tmp, version):
        lines = self._run_with_fake_tmux(
            tmp, "PreToolUse",
            {"session_id": "sid-tmux", "cwd": "/x/pri#ing",
             "tool_name": "AskUserQuestion"},
            version=version)
        return [l for l in lines if l.startswith("display-message -c")]

    def test_on_3_2_the_hashes_are_doubled_and_l_is_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            displays = self._asks(tmp, "tmux 3.2a")
        self.assertEqual(displays, [
            "display-message -c /dev/ttys001 -d %d -C ❓ pri##ing is asking you"
            % sh.NOTICE_MS,
            "display-message -c /dev/ttys002 -d %d -C ❓ pri##ing is asking you"
            % sh.NOTICE_MS,
        ])

    def test_on_3_6_it_travels_literally_as_it_always_did(self):
        with tempfile.TemporaryDirectory() as tmp:
            displays = self._asks(tmp, "tmux 3.6a")
        self.assertEqual(displays[0],
                         "display-message -c /dev/ttys001 -d %d -C -l ❓ pri#ing "
                         "is asking you" % sh.NOTICE_MS)

    def test_a_tmux_that_says_something_odd_gets_the_safe_form(self):
        # An unreadable `tmux -V` means "I do not know", and not knowing is read
        # as the oldest tmux.
        with tempfile.TemporaryDirectory() as tmp:
            displays = self._asks(tmp, "tmux master")
        self.assertIn("❓ pri##ing is asking you", displays[0])
        self.assertNotIn(" -l ", displays[0])


class TestWhenTheMenuReloads(unittest.TestCase):
    """The menu only reloads when what it PAINTS of the session changes (the
    badge: working / waiting for you / needs attention / asking you / open), not
    on every tool executed: that would be a reload every couple of seconds for
    every live agent, and each reload re-reads the history (~1 s)."""

    def _c(self, state, last_event=None, source=None):
        return {"state": state, "last_event": last_event, "source": source}

    def test_it_changes_when_the_state_changes(self):
        self.assertTrue(sh.row_changes(self._c("working", "PostToolUse"),
                                       self._c("awaiting_input", "Stop")))
        self.assertTrue(sh.row_changes(self._c("awaiting_input", "Stop"),
                                       self._c("working", "UserPromptSubmit")))
        self.assertTrue(sh.row_changes(self._c("working", "PostToolUse"),
                                       self._c("asking", "PreToolUse")))
        self.assertTrue(sh.row_changes(self._c("working", "PostToolUse"),
                                       self._c("ended", "SessionEnd")))

    def test_a_new_card_counts_as_a_change(self):
        self.assertTrue(sh.row_changes({}, self._c("working", "SessionStart",
                                                   "startup")))

    def test_it_does_not_change_within_the_same_state(self):
        self.assertFalse(sh.row_changes(self._c("working", "PostToolUse"),
                                        self._c("working", "PostToolUse")))
        # The "still waiting" (idle) reminder does not move the badge.
        self.assertFalse(sh.row_changes(self._c("awaiting_input", "Stop"),
                                        self._c("awaiting_input", "Notification")))
        # Nor the 6 s one with the form open.
        self.assertFalse(sh.row_changes(self._c("asking", "PreToolUse"),
                                        self._c("asking", "Notification")))

    def test_open_to_working_is_a_change_even_though_the_state_does_not_move(self):
        """`working` + `SessionStart` paints "○ open"; the first prompt makes it
        "● working" without changing `state`. It is the same signal as
        `flightdeck.picker.state_badge`, and that is why it is decided with it."""
        self.assertTrue(sh.row_changes(
            self._c("working", "SessionStart", "startup"),
            self._c("working", "UserPromptSubmit", "startup")))
        # A compaction does not paint "open": working -> working, no change.
        self.assertFalse(sh.row_changes(
            self._c("working", "PostToolUse", "startup"),
            self._c("working", "SessionStart", "compact")))


class TestTheHookRefreshesTheMenus(_RunsTheHook):
    """End to end: the hook launches `flightdeck refresh-menus` (in the
    background, without waiting) when the badge changes, and NOT when it does
    not.

    The command is at a fixed path inside the code directory, so what is
    replaced is the `Popen` and what is checked is the argv it would have been
    given.
    """

    def _with_fake_popen(self):
        calls = []
        return calls, mock.patch.object(
            sh.subprocess, "Popen",
            lambda argv, **kw: calls.append(list(argv)))

    def test_the_argv_is_the_bash_command_and_refresh_menus(self):
        """The command lives in `bin/` of the code directory, not next to the
        package: a file cannot share the name of the `flightdeck/` package
        directory. This is the pin that makes the two drifting apart a loud
        failure."""
        calls, patched = self._with_fake_popen()
        with patched:
            sh.refresh_menus()
        expected = config.code_dir() / "bin" / "flightdeck"
        self.assertEqual(sh.command_path(), expected)
        self.assertEqual(calls, [[str(expected), "refresh-menus"]])

    def test_when_the_state_changes_it_launches_refresh_menus(self):
        calls, patched = self._with_fake_popen()
        with tempfile.TemporaryDirectory() as tmp, patched:
            self._run_in_process(tmp, "UserPromptSubmit", {"session_id": "sid-9"},
                                 real_refresh=True)
        self.assertEqual([c[1] for c in calls], ["refresh-menus"])

    def test_within_the_same_state_it_does_not_launch_it(self):
        calls, patched = self._with_fake_popen()
        with tempfile.TemporaryDirectory() as tmp, patched:
            self._run_in_process(tmp, "UserPromptSubmit", {"session_id": "sid-10"},
                                 real_refresh=True)
            del calls[:]
            self._run_in_process(tmp, "PostToolUse", {"session_id": "sid-10",
                                                      "tool_name": "Bash"},
                                 real_refresh=True)
            self._run_in_process(tmp, "PostToolUse", {"session_id": "sid-10",
                                                      "tool_name": "Read"},
                                 real_refresh=True)
        self.assertEqual(calls, [])

    def test_without_the_command_in_place_the_hook_still_exits_0(self):
        """With nothing runnable at the command's path the launch fails, is
        swallowed, and the hook still exits 0 having written the card.

        A test has no business running the real command, so the CODE DIRECTORY
        is moved instead -- which only works in process, because `code_dir()`
        is resolved from the package's own `__file__` and no environment
        variable moves it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            nowhere = Path(tmp) / "no-code-here"  # no bin/flightdeck inside
            # The Stop's deferred notice is a real process of its own, and here
            # it would outlive the test (it waits a second and a half before
            # re-reading the card). It is not what this case is about: what has
            # to reach the real `Popen` is the menu refresh, so that its failure
            # is the one being swallowed.
            with mock.patch.object(sh.config, "code_dir", lambda: nowhere), \
                    mock.patch.object(sh, "launch_deferred_notice",
                                      lambda *a, **k: None):
                self.assertFalse(sh.command_path().exists())
                out = self._run_in_process(tmp, "Stop", {"session_id": "sid-11"},
                                           real_refresh=True)
            # The two contracts the subprocess harness checks for free: exit 0
            # (asserted inside `_run_in_process`) and nothing on stdout.
            self.assertEqual(out, "")
            self.assertEqual(self._card(tmp, "sid-11")["state"], "awaiting_input")


class TestTheHookReadsTheTranscriptOnTheStop(_RunsTheHook):
    """End to end: the hook receives `transcript_path` in the payload and on the
    `Stop` reads the end of the transcript (with `flightdeck.turn`)."""

    _FIRE = (
        '{"type":"system","subtype":"scheduled_task_fire","uuid":"s1","content":"Running scheduled task"}\n'
        '{"type":"user","uuid":"u1","parentUuid":"s1","isMeta":true,"promptSource":"system",'
        '"message":{"role":"user","content":"Nightwatch check"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":[{"type":"text","text":"All quiet"}]}}\n')
    _TYPED = (
        '{"type":"user","uuid":"u1","promptSource":"typed","message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n')

    def _transcript(self, tmp, text):
        t = Path(tmp) / "t.jsonl"
        t.write_text(text)
        return str(t)

    def test_a_loop_round_leaves_the_card_looping(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._transcript(tmp, self._FIRE)
            self._run(tmp, "UserPromptSubmit",
                      {"session_id": "sid-12", "transcript_path": t})
            card = self._run(tmp, "Stop",
                             {"session_id": "sid-12", "transcript_path": t})
            self.assertEqual(card["state"], "looping")
            self.assertEqual(card["turn"],
                             {"start": "fire", "schedule": None, "complete": True})
            # ...and the idle reminder does not move it.
            card = self._run(tmp, "Notification",
                             {"session_id": "sid-12", "transcript_path": t,
                              "message": "Claude is waiting for your input"})
            self.assertEqual(card["state"], "looping")

    def test_a_typed_turn_leaves_waiting_for_you(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._transcript(tmp, self._TYPED)
            card = self._run(tmp, "Stop",
                             {"session_id": "sid-13", "transcript_path": t})
            self.assertEqual(card["state"], "awaiting_input")
            self.assertEqual(card["turn"]["start"], "prompt")

    def test_without_a_transcript_or_without_the_module_it_is_waiting_for_you(self):
        # Without `flightdeck.turn` (hidden with sys.modules, the way a missing
        # module next to the hook would look) and with a loop-round transcript:
        # it degrades to "waiting for you", never blows up.
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "t.jsonl"
            t.write_text(self._FIRE)
            with mock.patch.dict(sys.modules, {"flightdeck.turn": None}):
                self.assertEqual(
                    sh._loop_after_stop({"transcript_path": str(t)}),
                    (None, False))
            card = self._run(tmp, "Stop",
                             {"session_id": "sid-14", "transcript_path": str(t)})
            self.assertEqual(card["state"], "looping")   # with the module, it reads it
        with tempfile.TemporaryDirectory() as tmp:
            card = self._run(tmp, "Stop", {"session_id": "sid-15",
                                           "transcript_path": "/does/not/exist"})
            self.assertEqual(card["state"], "awaiting_input")
            card = self._run(tmp, "Stop", {"session_id": "sid-15"})
            self.assertEqual(card["state"], "awaiting_input")


class TestNonInteractiveSessionsDoNotCount(_RunsTheHook):
    """A `claude --print` (non-interactive mode: a wrapper may launch one from
    HOME on EVERY Bash command of EVERY session) fires the same global hooks:
    SessionStart, UserPromptSubmit, Stop, SessionEnd.
    Without this, Flightdeck made it a card (4,047 of them piled up) and sang
    "⏳ home is waiting for you" on every Stop -- with the notice at 5 s,
    a constant flicker."""

    def test_is_non_interactive_looks_at_the_parents_argv(self):
        cases = {
            "claude --print --model claude-haiku-4-5 --allowedTools Read,Glob": True,
            "/Users/x/.local/bin/claude -p hi": True,
            "claude -n retail mrp wolf 5 --dangerously-skip-permissions": False,
            "claude --resume abc --dangerously-skip-permissions": False,
            "claude": False,
            "": False,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertIs(sh.is_non_interactive(argv.split()), expected)

    def test_a_claude_print_leaves_no_card_and_does_not_announce(self):
        # `parent_argv` is replaced by a --print's argv: the hook writes NOTHING
        # (no card, no tmux) and exits 0 with an empty stdout.
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(
                tmp,
                FLIGHTDECK_PARENT_ARGV_TEST="claude --print --model claude-haiku-4-5")
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
                r = subprocess.run(
                    [sys.executable, str(HOOK), event],
                    input=json.dumps({"session_id": "print-1", "cwd": "/Users/x"}),
                    text=True, capture_output=True, env=env, timeout=20)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")
            self.assertFalse((Path(tmp) / "sessions" / "print-1.json").exists())

    def test_an_interactive_claude_still_leaves_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(
                tmp,
                FLIGHTDECK_PARENT_ARGV_TEST="claude -n pricing --dangerously-skip-permissions")
            subprocess.run([sys.executable, str(HOOK), "Stop"],
                           input=json.dumps({"session_id": "inter-1"}), text=True,
                           capture_output=True, env=env, timeout=20)
            self.assertTrue((Path(tmp) / "sessions" / "inter-1.json").exists())


class TestSweepingOldCards(unittest.TestCase):
    """Nothing deleted the `ended` cards: 4,131 piled up on one machine (almost
    all of them from `claude --print`s). On every SessionEnd the hook
    sweeps the `ended` ones older than `SWEEP_AGE_S` and their
    `<id>.ctx.json`; everything else is left alone."""

    def _card(self, folder, sid, state, ago_s, with_ctx=False):
        upd = (datetime.datetime.now().astimezone()
               - datetime.timedelta(seconds=ago_s)).isoformat(timespec="seconds")
        (folder / ("%s.json" % sid)).write_text(json.dumps(
            {"session_id": sid, "state": state, "updated_at": upd}))
        if with_ctx:
            (folder / ("%s.ctx.json" % sid)).write_text('{"pct": 10}')

    def test_it_deletes_the_old_ended_ones_with_their_ctx_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            self._card(c, "old", "ended", 7200, with_ctx=True)
            self._card(c, "recent", "ended", 60)
            self._card(c, "alive", "working", 7200, with_ctx=True)
            self._card(c, "undated", "ended", 0)
            (c / "undated.json").write_text(json.dumps(
                {"session_id": "undated", "state": "ended"}))
            (c / "broken.json").write_text("{not json")
            deleted = sh.sweep_old_cards(c)
            left = sorted(f.name for f in c.iterdir())
        self.assertEqual(deleted, 2)   # old.json + old.ctx.json
        self.assertEqual(left, ["alive.ctx.json", "alive.json", "broken.json",
                                "recent.json", "undated.json"])

    def test_a_directory_that_does_not_exist_does_not_blow_up(self):
        self.assertEqual(sh.sweep_old_cards(Path("/does/not/exist")), 0)


class TestTheMultiToolHook(unittest.TestCase):
    """The hook learns `--tool codex` (codex 0.145's hooks mirror claude's
    payload): the card carries the tool, PermissionRequest is attention, and the
    `codex exec` (non-interactive) is filtered by the parent's argv like the
    `claude --print`."""

    def test_permission_request_is_attention(self):
        self.assertEqual(sh.state_for("PermissionRequest", {}), "needs_attention")

    def test_the_permission_request_notice_says_waiting_for_you(self):
        self.assertEqual(
            sh.notice_text("PermissionRequest", "needs_attention", "retail"),
            "⏳ retail is waiting for you")

    def test_is_non_interactive_knows_codex_exec(self):
        cases = {
            "codex exec fix the lint": True,
            "/opt/x/bin/codex e quick": True,
            "codex review": True,
            "codex": False,
            "codex resume 019ffd5b-abc": False,
            # a prompt, not a subcommand
            "codex --model gpt-5.6 fix the exec of the turns": False,
            # in codex -p is --profile, not --print
            "codex -p profile fix something": False,
            "claude explain what exec does": False,  # claude's "exec" is a prompt
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertIs(sh.is_non_interactive(argv.split()), expected, argv)

    def test_is_non_interactive_knows_agys_print(self):
        """agy is a Go binary: its flags work with one dash or two and with `=`,
        and `-p`/`--print`/`--prompt` is its one-shot mode."""
        cases = {
            "agy -p hi": True,
            "agy --prompt=hi": True,
            "agy -print x": True,
            "/Users/j/.local/bin/agy --print sum this up": True,
            "agy --mode plan": False,
            "agy -i hi": False,
            "agy": False,
            "agy --conversation 38777a1e": False,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertIs(sh.is_non_interactive(argv.split()), expected, argv)


class TestOneNoticePerChange(TestTheNoticeTheHookSends):
    def test_two_Stops_in_a_row_announce_once(self):
        """codex can produce TWO Stops per turn (the file hook and the notify
        tee): the second must not sing the notice again. General rule: the
        turn-finished notice only if the badge CHANGED (the same signal as the
        menu refresh)."""
        with tempfile.TemporaryDirectory() as tmp:
            displays = self._run_deferred_stop(tmp, "idle")
            self.assertEqual(len(displays), 2)   # two clients, first Stop
            # `expect_new=0`: the question is whether anything MORE arrives, and
            # the whole second is spent waiting to find out. The first version
            # returned almost at once here -- the first call's lines were
            # already in the log and any line the second Stop sent late would
            # have been missed.
            displays = self._run_deferred_stop(tmp, "idle", wait_s=1.0,
                                               expect_new=0)
            self.assertEqual(len(displays), 2)   # second Stop: NOTHING new


class TestTheHookWithToolCodex(_RunsTheHook):
    def _run_codex(self, tmp, event, payload):
        env = _clean_env(
            tmp, FLIGHTDECK_PARENT_ARGV_TEST="/opt/homebrew/bin/codex --model gpt-5.6")
        r = subprocess.run([sys.executable, str(HOOK), event, "--tool", "codex"],
                           input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        f = Path(tmp) / "sessions" / ("%s.json" % payload["session_id"])
        return json.loads(f.read_text()) if f.exists() else None

    def test_the_card_carries_tool_codex_and_the_usual_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = self._run_codex(tmp, "SessionStart",
                                   {"session_id": "cx-1", "cwd": "/x/retail",
                                    "source": "startup"})
            self.assertEqual(card["tool"], "codex")
            self.assertEqual(card["state"], "working")
            card = self._run_codex(tmp, "PermissionRequest",
                                   {"session_id": "cx-1"})
            self.assertEqual(card["state"], "needs_attention")

    def test_codexs_stop_neither_reads_a_transcript_nor_sets_looping(self):
        with tempfile.TemporaryDirectory() as tmp:
            # transcript_path points at a file that does not exist: if the hook
            # tried to analyse it, "complete" would be False but `turn` not None.
            card = self._run_codex(tmp, "Stop",
                                   {"session_id": "cx-2", "cwd": "/x",
                                    "transcript_path": "/does/not/exist.jsonl"})
            self.assertEqual(card["state"], "awaiting_input")
            self.assertIsNone(card.get("turn"))

    def test_a_codex_exec_leaves_no_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(tmp, FLIGHTDECK_PARENT_ARGV_TEST="codex exec fix")
            r = subprocess.run(
                [sys.executable, str(HOOK), "SessionStart", "--tool", "codex"],
                input=json.dumps({"session_id": "cx-3"}), text=True,
                capture_output=True, env=env, timeout=20)
            self.assertEqual(r.returncode, 0)
            self.assertFalse((Path(tmp) / "sessions" / "cx-3.json").exists())


class TestTheHookWithToolAgy(_RunsTheHook):
    """`--tool agy`: the payload comes in camelCase (`conversationId`,
    `workspacePaths`, `transcriptPath`), the "before calling the model" is
    `PreInvocation` (agy has no UserPromptSubmit) and agy READS a JSON object
    from stdout -- `{}` is "no decision": it lets it stop and injects nothing.
    It is the ONLY tool this hook writes to stdout for.
    """

    PAYLOAD = {"conversationId": "38777a1e-8cb4-45a0-973e-5d0ccb89c4df",
               "workspacePaths": ["/Users/j/proj"],
               "transcriptPath": "/Users/j/.gemini/antigravity-cli/transcript.jsonl",
               "modelName": "auto", "invocationNum": 1, "initialNumSteps": 0}

    def _run_agy(self, tmp, event, payload,
                 parent="/Users/j/.local/bin/agy --mode plan"):
        """One agy event. Returns (rc, stdout, card-or-None): here the stdout
        DOES matter (it is the answer agy reads), so it is not checked by the
        harness, it is returned."""
        env = _clean_env(tmp, FLIGHTDECK_PARENT_ARGV_TEST=parent)
        r = subprocess.run([sys.executable, str(HOOK), event, "--tool", "agy"],
                           input=json.dumps(payload), text=True,
                           capture_output=True, env=env, timeout=20)
        f = Path(tmp) / "sessions" / ("%s.json" % payload.get("conversationId"))
        return (r.returncode, r.stdout,
                json.loads(f.read_text()) if f.exists() else None)

    def test_preinvocation_creates_a_working_card_with_tool_agy(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, card = self._run_agy(tmp, "PreInvocation", self.PAYLOAD)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}\n")
        self.assertEqual(card["tool"], "agy")
        self.assertEqual(card["session_id"], self.PAYLOAD["conversationId"])
        self.assertEqual(card["state"], "working")
        self.assertEqual(card["cwd"], "/Users/j/proj")
        self.assertEqual(card["project"], "proj")
        self.assertEqual(card["transcript_path"], self.PAYLOAD["transcriptPath"])
        self.assertEqual(card["last_event"], "PreInvocation")

    def test_the_stop_leaves_awaiting_and_still_prints_the_braces(self):
        p = dict(self.PAYLOAD, executionNum=1, terminationReason="model_stop",
                 fullyIdle=True)
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, card = self._run_agy(tmp, "Stop", p)
        self.assertEqual((rc, out), (0, "{}\n"))
        self.assertEqual(card["state"], "awaiting_input")
        # agy's transcript is not one `flightdeck.turn` knows how to read (it is
        # claude's): it is neither analysed nor painted "looping".
        self.assertIsNone(card.get("turn"))

    def test_a_tool_takes_the_card_back_to_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_agy(tmp, "Stop", self.PAYLOAD)
            rc, out, card = self._run_agy(tmp, "PostToolUse", self.PAYLOAD)
        self.assertEqual((rc, out), (0, "{}\n"))
        self.assertEqual(card["state"], "working")

    def test_agys_print_makes_no_card_but_still_prints_the_braces(self):
        """agy's non-interactive shortcut (`-p`, `--print`, `--prompt`) is a
        script, not a session: no card, no notice. But the stdout answer is sent
        all the same, because agy waits for it even when the hook does nothing."""
        for parent in ("agy -p say hi", "agy --prompt=hi", "agy -print x"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(parent=parent):
                rc, out, card = self._run_agy(tmp, "Stop", self.PAYLOAD,
                                              parent=parent)
                self.assertEqual((rc, out), (0, "{}\n"))
                self.assertIsNone(card)

    def test_without_flightdeck_tools_it_degrades_but_still_answers(self):
        """The import is lazy and swallows the failure (like the hook's other
        extras): with no translation there is no card under that id, but the
        exit 0 and the `{}` stay.

        `flightdeck.tools` is hidden with `sys.modules`, the way a missing
        module next to the hook would look; that needs the hook to run in this
        process.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(sys.modules, {"flightdeck.tools": None}):
                out = self._run_in_process(tmp, "Stop", self.PAYLOAD, tool="agy")
            self.assertEqual(out, "{}\n")
            self.assertIsNone(self._card(tmp, self.PAYLOAD["conversationId"]))
            # The card is there, under the "unknown" id: nothing translated the
            # camelCase `conversationId`.
            self.assertEqual(self._card(tmp, "unknown")["state"],
                             "awaiting_input")

    def test_claude_and_codex_still_print_nothing(self):
        """The usual contract: whatever the hook prints in claude is injected
        into the session as context. `{}` is agy's ONLY."""
        with tempfile.TemporaryDirectory() as tmp:
            for args, parent in (((), _INTERACTIVE),
                                 (("--tool", "codex"), "/opt/homebrew/bin/codex")):
                with self.subTest(args=args):
                    env = _clean_env(tmp, FLIGHTDECK_PARENT_ARGV_TEST=parent)
                    r = subprocess.run(
                        [sys.executable, str(HOOK), "Stop", *args],
                        input=json.dumps({"session_id": "no-stdout"}),
                        text=True, capture_output=True, env=env, timeout=20)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stdout, "")


class TestOnlyClaudeTouchesTheTabTitle(_WithFakeTmux):
    """`@title` is an option of the tmux SESSION, not of the pane.

    A codex or an agy in another window of the same tmux session has no human
    title to set, so clearing it on every event of theirs made the terminal's tab
    flicker between the neighbouring claude's title and the session name. Only
    claude touches that option; the others send no set-option at all.
    """

    AGY = "/Users/j/.local/bin/agy --mode plan"
    CODEX = "/opt/homebrew/bin/codex --model gpt-5.6"
    CLAUDE = _INTERACTIVE

    def _title_set_options(self, tmp, event, payload, parent, args=(),
                           expected_stdout=""):
        """The tmux calls about `@title` of an event with a pane (%7)."""
        lines = self._run_with_fake_tmux(
            tmp, event, payload, args=args, expected_stdout=expected_stdout,
            env_extra={"TMUX_PANE": "%7",
                       "FLIGHTDECK_PARENT_ARGV_TEST": parent})
        return [l for l in lines if "@title" in l]

    def test_neither_agy_nor_codex_send_a_title_set_option(self):
        cases = (("agy", "PreInvocation", TestTheHookWithToolAgy.PAYLOAD,
                  self.AGY, "{}\n"),
                 ("agy", "Stop", TestTheHookWithToolAgy.PAYLOAD, self.AGY, "{}\n"),
                 ("codex", "Stop", {"session_id": "cx-9", "cwd": "/x/retail"},
                  self.CODEX, ""))
        for tool, event, payload, parent, out in cases:
            with self.subTest(tool=tool, event=event), \
                    tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(
                    self._title_set_options(tmp, event, payload, parent,
                                            args=("--tool", tool),
                                            expected_stdout=out), [])

    def test_an_event_of_claudes_does_touch_the_title(self):
        # The control: with no human title to set it CLEARS it (so an old one
        # does not stay stuck), but it sends its set-option.
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._title_set_options(
                tmp, "Stop", {"session_id": "sid-title", "cwd": "/x/pricing"},
                self.CLAUDE)
        self.assertEqual(lines, ["set-option -t %7 -u @title"])


class TestThePidFromThePayload(_RunsTheHook):
    def test_the_hook_respects_the_pid_the_tee_sends(self):
        """codex's tee is ephemeral: the useful pid (codex's) travels in the
        payload as `_pid` -- without it the card was born with the dead tee's
        pid and the menu did not list it (measured on a real codex)."""
        with tempfile.TemporaryDirectory() as tmp:
            card = self._run(tmp, "Stop", {"session_id": "sid-pid", "_pid": 4242})
            self.assertEqual(card["pid"], 4242)
            card = self._run(tmp, "Stop",
                             {"session_id": "sid-pid2", "_pid": "not-an-int"})
            self.assertNotEqual(card["pid"], "not-an-int")


def _dead_pid():
    pr = subprocess.Popen(["true"])
    pr.wait()
    return pr.pid


class TestSweepingTheOnesThatDiedWithoutSayingGoodbye(unittest.TestCase):
    def test_an_old_codex_card_with_a_dead_pid_is_swept(self):
        """codex sends no SessionEnd (its `/exit` fires nothing, measured): its
        card with a dead pid is the "ended" that never arrived."""
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.datetime.now().astimezone()
                   - datetime.timedelta(seconds=7200)).isoformat(timespec="seconds")
            (Path(tmp) / "cx.json").write_text(json.dumps(
                {"session_id": "cx", "tool": "codex", "pid": _dead_pid(),
                 "state": "awaiting_input", "updated_at": old}))
            (Path(tmp) / "cl.json").write_text(json.dumps(
                {"session_id": "cl", "pid": _dead_pid(),
                 "state": "awaiting_input", "updated_at": old}))
            deleted = sh.sweep_old_cards(Path(tmp))
        # claude's, with a dead pid, is NOT swept (its pane may still be alive).
        self.assertEqual(deleted, 1)


class TestTheExit0IsUnconditional(_RunsTheHook):
    """The contract: whatever happens, rc 0 and nothing on stdout.

    "Whatever happens" includes a MALFORMED payload. The case that broke it: a
    `session_id` that is not text (a number). The notice trims it (`sid[:8]`) to
    have a short name to announce, and on an integer that is a TypeError; as the
    call was OUTSIDE the try, it took the whole hook down -- rc 1 on an event of
    the agent's, which is exactly what the defensive design promises never
    happens.
    """

    def test_a_session_id_that_is_not_text_does_not_take_the_hook_down(self):
        # The harness already checks rc 0 and an empty stdout on every run; what
        # matters here is that it gets to check them with this payload.
        for event in ("Stop", "Notification", "SessionStart", "PostToolUse"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(event=event):
                card = self._run(tmp, event, {"session_id": 12345})
                self.assertEqual(card["session_id"], 12345)
                self.assertEqual(card["last_event"], event)

    def test_a_cwd_that_is_not_text_does_not_take_the_hook_down(self):
        """A `cwd` that is not text used to exit 1, because `Path(cwd)` raises
        outside any try. "Whatever happens" has to mean whatever happens.

        The value is ignored rather than written down: a card carrying a
        non-string `cwd` would only move the failure to whoever reads it.
        """
        for cwd in (7, {"a": 1}, ["/x"], True):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(cwd=cwd):
                card = self._run(tmp, "Stop", {"session_id": "odd-cwd", "cwd": cwd})
                self.assertEqual(card["state"], "awaiting_input")
                self.assertNotIn("cwd", card)
                self.assertNotIn("project", card)

    def test_a_payload_that_is_not_an_object_does_not_take_the_hook_down(self):
        """Same rule: a JSON array or a bare string on stdin used to
        reach `payload.get(...)` and exit 1. It is treated like a broken
        payload -- the event is still recorded, under the "unknown" id."""
        for raw in ("[1, 2, 3]", '"just a string"', "17", "null"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(raw=raw):
                env = _clean_env(tmp)
                r = subprocess.run([sys.executable, str(HOOK), "Stop"],
                                   input=raw, text=True, capture_output=True,
                                   env=env, timeout=20)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(self._card(tmp, "unknown")["state"],
                                 "awaiting_input")

    def test_a_notification_message_that_is_not_text_does_not_take_the_hook_down(self):
        """Same rule: `message` reaches `.lower()`, and on a number or
        an object that exited 1. A message that is not text counts as no text,
        so the notification stays "needs attention"."""
        for message in ({"a": 1}, 42, ["hi"]):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(message=message):
                card = self._run(tmp, "Notification",
                                 {"session_id": "odd-msg", "message": message})
                self.assertEqual(card["state"], "needs_attention")

    def test_a_session_id_that_would_escape_the_sessions_directory_is_refused(self):
        """The id becomes a FILE NAME, so it goes through `SAFE_ID` first.

        Measured before the fix: `{"session_id": "../escaped"}` wrote the card
        one level ABOVE `sessions/`, which is where `sweep_old_cards`, the
        deferred notice and the menu's reader all look -- so that card was never
        read and never swept. The status line tee already checked its id before
        touching the disk; this did not. Anything that is not a plain id becomes
        "unknown", which is what an event with no id gets anyway.
        """
        for sid in ("../escaped", "a/b", "with space", "../../far", ""):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(sid=sid):
                env = _clean_env(tmp)
                r = subprocess.run([sys.executable, str(HOOK), "Stop"],
                                   input=json.dumps({"session_id": sid}),
                                   text=True, capture_output=True, env=env,
                                   timeout=20)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")
                # Everything written is inside `sessions/`, and it is the
                # "unknown" card. (`escaped.json` sat right here before the fix.)
                sessions = Path(tmp) / "sessions"
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                                 ["sessions"])
                self.assertEqual([p.name for p in sessions.iterdir()],
                                 ["unknown.json"])
                self.assertEqual(json.loads((sessions / "unknown.json").read_text())
                                 ["state"], "awaiting_input")


class TestABrokenPackageStillExits0(unittest.TestCase):
    """The two contracts hold even when WE are the broken part.

    This script is registered for 7 Claude Code events, 6 of codex's and 3 of
    agy's: it is the most-fired thing in the product, and `flightdeck update`
    unpacks a tarball over the same directory -- a half-written `config.py` is
    exactly the window in which every open session fires it. Measured before
    the fix: `Stop` exited 1 with a traceback, and `Stop --tool agy` exited 1
    with NO `{}`, which leaves agy sitting there waiting for an answer.

    Run as a subprocess against a copy of the script whose package really does
    raise on import (`tests.broken_package_copy`); a flag the code could
    recognise would not prove anything.
    """

    def _run(self, tmp, *args):
        script = broken_package_copy(Path(tmp) / "broken", HOOK)
        env = _clean_env(Path(tmp) / "state")
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, str(script)] + list(args),
                              input=json.dumps({"session_id": "sid-broken"}),
                              text=True, capture_output=True, env=env, timeout=20)

    def test_claude_exits_0_and_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, "Stop")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr, "")

    def test_agy_still_gets_its_empty_object(self):
        # Without it agy waits for an answer that never comes: the `{}` is the
        # one thing that has to survive a broken package.
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, "Stop", "--tool", "agy")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "{}\n")
            self.assertEqual(r.stderr, "")

    def test_nothing_of_ours_is_written(self):
        # No card, no notice, no menu refresh: half a card is worse than none,
        # since the menu and the status bar are what read it.
        with tempfile.TemporaryDirectory() as tmp:
            for args in (("Stop",), ("Stop", "--tool", "agy"),
                         ("SessionStart",), ("SessionEnd",)):
                with self.subTest(args=args):
                    self.assertEqual(self._run(tmp, *args).returncode, 0)
            self.assertFalse((Path(tmp) / "state" / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
