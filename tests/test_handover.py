"""`flightdeck.handover`: prefix+n, the key that passes the baton.

The old agent in the pane is closed (`/exit`), the pane is waited on until it is
a shell again -- Flightdeck's sessions are shell-first -- and the next one is
typed in, already numbered and carrying the flags the old one ran with.

Everything that has an effect (tmux, `ps`, the state directory) lives as a
module global, so the orchestration tests can replace it and look at EXACTLY
what would be typed into the pane without standing up a tmux server.
"""
import io
import json
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import common, config
from flightdeck import handover as hv


class _Response:
    """What `subprocess.run` returns, as `flightdeck.common.tmux` sees it."""

    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class TestNextTitle(unittest.TestCase):
    """The handover's little number: "pricing" -> "pricing 2" -> "pricing 3"."""

    def test_a_title_without_a_number_starts_at_2(self):
        self.assertEqual(hv.next_title("pricing", "session"), "pricing 2")

    def test_a_title_with_a_number_counts_on(self):
        self.assertEqual(hv.next_title("pricing 2", "session"), "pricing 3")
        self.assertEqual(hv.next_title("pricing 9", "session"), "pricing 10")

    def test_without_a_title_the_fallback_wins(self):
        for empty in (None, "", "   "):
            self.assertEqual(hv.next_title(empty, "pricing"), "pricing 2")

    def test_odd_whitespace_does_not_throw_it_off(self):
        self.assertEqual(hv.next_title("  pricing   2  ", "x"), "pricing 3")
        self.assertEqual(hv.next_title("pricing\t2", "x"), "pricing 3")
        self.assertEqual(hv.next_title("  pricing  ", "x"), "pricing 2")

    def test_a_GLUED_number_is_not_a_counter(self):
        """"v2" is a name, not a counter: it becomes "v2 2", never "v3"."""
        self.assertEqual(hv.next_title("v2", "x"), "v2 2")
        self.assertEqual(hv.next_title("pricing2", "x"), "pricing2 2")
        self.assertEqual(hv.next_title("2", "x"), "2 2")

    def test_the_fallback_is_numbered_by_the_same_rule(self):
        """A fallback already ending in a number counts on, it is not doubled."""
        self.assertEqual(hv.next_title(None, "pricing 2"), "pricing 3")

    def test_without_title_or_fallback_there_is_a_generic_name(self):
        self.assertEqual(hv.next_title(None, ""), "session 2")
        self.assertEqual(hv.next_title(None, None), "session 2")


class TestNewTitle(unittest.TestCase):
    """Who is asked for the current title (the transcript) and who is the
    fallback."""

    def test_it_uses_the_HUMAN_title_of_the_conversation(self):
        self.assertEqual(
            hv.new_title("abc", "pricing", look_up=lambda sid: "margins 2"),
            "margins 3")

    def test_without_a_human_title_it_falls_to_the_tmux_session_name(self):
        self.assertEqual(
            hv.new_title("abc", "pricing", look_up=lambda sid: None),
            "pricing 2")

    def test_without_a_session_id_it_does_not_even_ask(self):
        def blows_up(sid):
            raise AssertionError("it should not ask without an id")
        self.assertEqual(hv.new_title(None, "pricing", look_up=blows_up),
                         "pricing 2")


class TestCardForPane(unittest.TestCase):
    """From a tmux pane to the session card of the agent running inside it."""

    def _card(self, sid, pane, updated, state="working", pid=1):
        return {"session_id": sid, "tmux_pane": pane, "state": state,
                "pid": pid, "updated_at": updated}

    def test_the_one_of_the_pane_asked_for_wins(self):
        cards = [self._card("other", "%9", "2026-08-17T10:00:00+02:00"),
                 self._card("this", "%3", "2026-08-17T09:00:00+02:00")]
        c = hv.card_for_pane(cards, "%3", alive=lambda pid: True)
        self.assertEqual(c["session_id"], "this")

    def test_with_several_from_the_same_pane_the_freshest_wins(self):
        """A recycled pane (one agent closed, another opened) leaves old cards."""
        cards = [self._card("old", "%3", "2026-08-17T09:00:00+02:00"),
                 self._card("new", "%3", "2026-08-17T11:30:00+02:00"),
                 self._card("middle", "%3", "2026-08-17T10:00:00+02:00")]
        c = hv.card_for_pane(cards, "%3", alive=lambda pid: True)
        self.assertEqual(c["session_id"], "new")

    def test_a_card_without_a_date_loses_against_a_dated_one(self):
        cards = [{"session_id": "undated", "tmux_pane": "%3", "state": "working"},
                 self._card("dated", "%3", "2026-08-17T09:00:00+02:00")]
        c = hv.card_for_pane(cards, "%3", alive=lambda pid: True)
        self.assertEqual(c["session_id"], "dated")

    def test_it_drops_the_ones_that_ended(self):
        cards = [self._card("dead", "%3", "2026-08-17T11:00:00+02:00",
                            state="ended"),
                 self._card("alive", "%3", "2026-08-17T09:00:00+02:00")]
        c = hv.card_for_pane(cards, "%3", alive=lambda pid: True)
        self.assertEqual(c["session_id"], "alive")

    def test_it_drops_the_ghosts_whose_pid_is_dead(self):
        """An agent killed the hard way: card says "working", process is gone."""
        cards = [self._card("ghost", "%3", "2026-08-17T11:00:00+02:00", pid=999),
                 self._card("alive", "%3", "2026-08-17T09:00:00+02:00", pid=1)]
        c = hv.card_for_pane(cards, "%3", alive=lambda pid: pid != 999)
        self.assertEqual(c["session_id"], "alive")

    def test_without_candidates_it_returns_none(self):
        cards = [self._card("other", "%9", "2026-08-17T10:00:00+02:00")]
        self.assertIsNone(hv.card_for_pane(cards, "%3", alive=lambda pid: True))
        self.assertIsNone(hv.card_for_pane([], "%3", alive=lambda pid: True))


class TestReasonNotToClose(unittest.TestCase):
    """When `/exit` must NOT be typed at the agent inside."""

    def test_asking_for_a_permission_is_not_touched(self):
        """The Enter of the `/exit` would say YES to the permission it is asking."""
        reason = hv.reason_not_to_close({"state": "needs_attention"})
        self.assertIn("permission", reason)

    def test_working_is_not_touched(self):
        """To a busy agent, the `/exit` is queued up as if it were text."""
        reason = hv.reason_not_to_close({"state": "working"})
        self.assertIn("still working", reason)

    def test_asking_you_something_is_not_touched(self):
        """Same danger as a permission: the Enter of the `/exit` would PICK one.

        With a form open (`AskUserQuestion`) or a plan waiting for the "ok"
        (`ExitPlanMode`), whatever the handover types goes into that form.
        """
        reason = hv.reason_not_to_close({"state": "asking"})
        self.assertIn("asking", reason or "")

    def test_looping_refuses_and_says_so(self):
        """The new agent would not inherit the `/loop` (it lives in the memory of
        the one being closed): the handover would run it over in silence. It
        refuses, and the user stops the loop first if that is what they want."""
        reason = hv.reason_not_to_close({"state": "looping"})
        self.assertIn("loop", reason or "")

    def test_a_stop_with_a_busy_registry_is_still_working(self):
        """The Stop fires at the end of every turn, background agents/tasks
        running included (Claude Code's registry: status busy): the /exit would be
        queued. With idle/shell/waiting or no registry, the normal case."""
        reason = hv.reason_not_to_close({"state": "awaiting_input"}, status_cc="busy")
        self.assertIn("still working", reason or "")
        for st in ("idle", "shell", None):
            self.assertIsNone(hv.reason_not_to_close({"state": "awaiting_input"},
                                                     status_cc=st), st)

    def test_waiting_for_you_is_the_handovers_normal_case(self):
        self.assertIsNone(hv.reason_not_to_close({"state": "awaiting_input"}))

    def test_just_opened_or_just_RESUMED_can_be_handed_over(self):
        """The false positive, seen live.

        Starting -- or resuming -- leaves the card in "working", because the only
        event that takes it down to "awaiting_input" is the `Stop` at the end of
        the turn. A resumed session sitting still read as "working" and the
        handover refused. The signal that tells them apart is not a heuristic: if
        the LAST event is still the one that started it, since then there has been
        neither a prompt (`UserPromptSubmit`) nor a tool (`PostToolUse`), so there
        is no turn in flight and the `/exit` lands in an idle input box.
        """
        self.assertIsNone(hv.reason_not_to_close(
            {"state": "working", "last_event": "SessionStart"}))

    def test_a_REAL_turn_in_flight_is_still_shielded(self):
        for event in ("UserPromptSubmit", "PostToolUse"):
            reason = hv.reason_not_to_close({"state": "working",
                                             "last_event": event})
            self.assertIn("still working", reason or "", event)

    def test_the_SessionStart_of_a_COMPACTION_is_not_a_start(self):
        """Claude compacts when its context fills up: in the middle of a turn.

        That `SessionStart` leaves the card looking like a start (working +
        SessionStart) but there IS work in flight, so here it still blocks. The
        `source` the hook records is what tells them apart.
        """
        reason = hv.reason_not_to_close(
            {"state": "working", "last_event": "SessionStart",
             "source": "compact"})
        self.assertIn("still working", reason or "")

    def test_the_other_starts_do_go_through(self):
        """`startup`/`resume`/`clear`/`fork` are the 2.1.233 enum minus
        `compact`; and without a `source` (a card older than that note) it goes
        through too."""
        for source in ("startup", "resume", "clear", "fork", None):
            card = {"state": "working", "last_event": "SessionStart"}
            if source is not None:
                card["source"] = source
            self.assertIsNone(hv.reason_not_to_close(card), source)

    def test_a_state_we_do_not_know_does_not_block(self):
        self.assertIsNone(hv.reason_not_to_close({}))
        self.assertIsNone(hv.reason_not_to_close({"state": "who knows"}))
        self.assertIsNone(hv.reason_not_to_close(None))

    def test_a_pane_that_only_WATCHES_a_background_conversation_is_not_touched(self):
        """`claude attach <id>`: the pane is a window onto a job, not a session.

        Nothing is typed there because nothing has been MEASURED there: how that
        viewer closes is unknown, and its nearest relative says how wrong the
        guess can go -- in a parked pane `/exit` does not exit but opens the
        agents view, where any text typed becomes a NEW background task. The
        refusal holds whatever the state of the conversation is: the state
        belongs to the job, not to the pane.
        """
        for state in ("awaiting_input", "working", "looping", None):
            with self.subTest(state=state):
                reason = hv.reason_not_to_close({"_attach": "23a53419",
                                                 "state": state})
                self.assertIn("attach", reason or "")
                self.assertIn("background", reason or "")


class TestIsShell(unittest.TestCase):
    def test_it_recognises_the_shells(self):
        for cmd in ("sh", "bash", "zsh", "-zsh", "fish", "dash", "ksh"):
            self.assertTrue(hv.is_shell(cmd), cmd)

    def test_what_is_not_a_shell(self):
        # "2.1.233" is how tmux sees claude's binary (see the README).
        for cmd in ("2.1.233", "node", "python3", "claude", "ssh", "vim", "", None):
            self.assertFalse(hv.is_shell(cmd), cmd)


class TestWaitForShell(unittest.TestCase):
    def test_if_it_is_already_a_shell_it_does_not_wait(self):
        self.assertTrue(hv.wait_for_shell(lambda: "zsh", timeout=5, step=5))

    def test_it_waits_for_the_shell_to_come_back(self):
        readings = iter(["2.1.233", "2.1.233", "zsh"])
        self.assertTrue(hv.wait_for_shell(lambda: next(readings),
                                          timeout=5, step=0))

    def test_it_gives_up_when_the_time_runs_out(self):
        self.assertFalse(hv.wait_for_shell(lambda: "2.1.233",
                                           timeout=0.05, step=0.01))

    def test_a_failed_reading_does_not_break_the_wait(self):
        """tmux can answer None (a timeout): that is "not yet", not an error."""
        readings = iter([None, "zsh"])
        self.assertTrue(hv.wait_for_shell(lambda: next(readings),
                                          timeout=5, step=0))


class TestInheritableFlags(unittest.TestCase):
    """What the new agent takes with it from the one being closed."""

    def test_it_inherits_the_skip_permissions_flag(self):
        # A real argv, measured with `ps -o args= -p <pid>` on a live claude.
        self.assertEqual(
            hv.inheritable_flags(["claude", "--dangerously-skip-permissions"]),
            ["--dangerously-skip-permissions"])

    def test_it_inherits_a_flag_with_a_value_without_splitting_it(self):
        self.assertEqual(
            hv.inheritable_flags(["claude", "--model", "claude-fable-5"]),
            ["--model", "claude-fable-5"])

    def test_the_flags_with_a_value_the_table_used_to_miss(self):
        """The table was filled in from the binary's own (2.1.233).

        Before, a flag with a value that was not in `FLAGS_WITH_VALUE` lost its
        value: it was inherited bare and claude complained. One per family of the
        missing ones, with `-m` leading because it is the one that opened the can.
        """
        for flag, value in (("-m", "opus"),
                            ("--max-turns", "5"),
                            ("--thinking", "adaptive"),
                            ("--max-thinking-tokens", "31999"),
                            ("--permission-prompt-tool", "mcp__ask"),
                            ("--system-prompt-file", "/x/prompt.md"),
                            ("--task-budget", "120000"),
                            ("--routine", "nightly")):
            self.assertEqual(hv.inheritable_flags(["claude", flag, value]),
                             [flag, value], flag)

    def test_it_does_not_inherit_WHERE_in_the_conversation_being_closed(self):
        """`--resume-session-at` and `--resume-drops-turn` are `--resume`'s family.

        Claude Code sets them when resuming at a particular point (`/background`
        uses them to carry on a turn in flight), so they point at a message of the
        conversation we are CLOSING: in the new one they mean nothing. They are
        here because, once in the flags-with-value table, they would start being
        inherited **with their value**, which is worse than losing it.
        """
        for argv in (["claude", "--resume-session-at", "msg-abc123"],
                     ["claude", "--resume-drops-turn", "msg-abc123"]):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)

    def test_it_keeps_the_order_and_what_it_does_not_know(self):
        argv = ["claude", "--verbose", "--model", "opus", "--ide"]
        self.assertEqual(hv.inheritable_flags(argv),
                         ["--verbose", "--model", "opus", "--ide"])

    def test_it_does_not_inherit_the_title(self):
        for argv in (["claude", "-n", "pricing 2"],
                     ["claude", "--name", "pricing 2"],
                     ["claude", "--name=pricing 2"]):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)

    def test_it_does_not_inherit_which_conversation_it_came_from(self):
        """Inheriting a `--resume` would be the exact opposite of handing over."""
        for argv in (["claude", "--resume", "9f3c"], ["claude", "-r", "9f3c"],
                     ["claude", "--resume=9f3c"], ["claude", "--continue"],
                     ["claude", "-c"], ["claude", "--fork-session"],
                     ["claude", "--session-id", "0a-uuid"]):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)

    def test_a_chain_of_handovers_does_not_pile_up_junk(self):
        """The case that will happen MOST, and the one that hurts most if it fails.

        `ps` gives the line back FLATTENED, so the `claude -n 'pricing 3'` the
        previous handover typed comes back as four tokens and the "3" is orphaned
        once we drop the `-n`. Sneaking it through would be worse than losing it:
        on the new line a loose token is the initial PROMPT of the agent being born.
        """
        flattened = ["claude", "--dangerously-skip-permissions",
                     "-n", "pricing", "3"]
        self.assertEqual(hv.inheritable_flags(flattened),
                         ["--dangerously-skip-permissions"])

    def test_a_flag_does_not_swallow_the_next_flag(self):
        """Measured on a live claude: `claude --resume --dangerously-…`.

        `--resume` takes a value but does not carry one there. If it swallowed it,
        the handover would lose exactly the flag we came to inherit.
        """
        argv = ["claude", "--resume", "--dangerously-skip-permissions"]
        self.assertEqual(hv.inheritable_flags(argv),
                         ["--dangerously-skip-permissions"])

    def test_it_does_not_inherit_what_CREATES_the_place_the_session_lives_in(self):
        """Inheriting them would be one worktree -- and one tmux session -- MORE
        PER HANDOVER.

        `-w/--worktree` opens the agent in a separate copy of the repo, and
        `--tmux` (which only works with it) sets up its own tmux session on top.
        The handover hands over WHERE IT IS, it does not move house.
        """
        for argv in (["claude", "-w"], ["claude", "-w", "branch-x"],
                     ["claude", "--worktree"],
                     ["claude", "--worktree", "branch-x"],
                     ["claude", "--worktree=branch-x"],
                     ["claude", "--tmux"], ["claude", "--tmux", "pricing"]):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)

    def test_it_does_not_inherit_the_mode_that_is_not_interactive(self):
        """`-p/--print` answers and exits; a handover is an open conversation."""
        for argv in (["claude", "-p"], ["claude", "--print"],
                     ["claude", "-p", "how", "many", "tests"]):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)
        # And only the `-p` falls: whatever came with it is still inherited.
        self.assertEqual(
            hv.inheritable_flags(["claude", "-p",
                                  "--dangerously-skip-permissions"]),
            ["--dangerously-skip-permissions"])

    def test_it_does_not_inherit_the_family_CLAUDE_REJECTS_without_print(self):
        """Inheriting one of these would leave the new agent WITHOUT STARTING.

        They are the other half of "the `-p` is not inherited": the binary REJECTS
        them in interactive mode (2.1.233 messages, measured: "--input-format=
        stream-json requires --print", "--include-partial-messages requires
        --print and --output-format=stream-json", "--no-session-persistence can
        only be used with --print mode"...). A `-p` claude handed over was born
        with them, claude complained and the pane stayed in the shell.
        """
        for flag in ("--input-format", "--output-format",
                     "--prompt-suggestions", "--include-partial-messages",
                     "--forward-subagent-text", "--no-session-persistence",
                     "--plan-mode-instructions"):
            self.assertEqual(hv.inheritable_flags(["claude", flag]), [], flag)

    def test_that_family_takes_its_value_with_it_too(self):
        """And it does not matter whether we know the value from the table or not.

        `--output-format` IS in `FLAGS_WITH_VALUE`, so its value falls by table.
        `--no-session-persistence` is NOT (it is a boolean), and whatever comes
        behind falls anyway by the loose-token rule -- which is the one that saves
        the case `ps` flattened, with the value split across several tokens.
        """
        self.assertEqual(
            hv.inheritable_flags(["claude", "--output-format", "stream-json",
                                  "--dangerously-skip-permissions"]),
            ["--dangerously-skip-permissions"])
        self.assertEqual(
            hv.inheritable_flags(["claude", "--no-session-persistence",
                                  "without", "saving", "anything"]),
            [])
        self.assertEqual(
            hv.inheritable_flags(["claude", "--input-format=stream-json"]), [])

    def test_the_bare_double_dash_is_not_inherited(self):
        """`--` means "from here on, TEXT": the opposite of a flag.

        `claude --model opus -- fix the bug` is a legitimate way of passing the
        prompt, and that `--` sneaked into the new line would turn whatever came
        behind it into a prompt.
        """
        argv = ["claude", "--model", "opus", "--", "fix", "the", "bug"]
        self.assertEqual(hv.inheritable_flags(argv), ["--model", "opus"])

    def test_the_initial_prompt_is_not_inherited(self):
        argv = ["claude", "--model", "opus", "fix", "the", "bug"]
        self.assertEqual(hv.inheritable_flags(argv), ["--model", "opus"])

    def test_an_argv_that_says_nothing(self):
        for argv in ([], ["claude"], None):
            self.assertEqual(hv.inheritable_flags(argv), [], argv)


class TestAnArgvThatIsNotClaude(unittest.TestCase):
    """A RECYCLED pid: `pid_alive` says "it exists", but it is ANOTHER program.

    The card stores a process number; the system reuses those numbers. If the
    agent died leaving no trace (`kill -9`) and the number went to something
    else, `argv_of_pid` returns the argv of that something else. Nothing is
    inherited from there: its dashes are not claude's flags.
    """

    # A REAL line from a live machine (`ps -o args=`, username replaced):
    # it is the one Claude Code itself uses to run a shell command, which is
    # exactly what can sit behind a recycled pid. Its 40 tokens produced four
    # inherited flags -- ['--', '-f', '--', '-P'] -- before the guard, and that
    # leading `--` turns everything behind it into a PROMPT.
    ZSH = shlex.split(
        "/bin/zsh -c source /Users/j/.claude/shell-snapshots/snap.sh "
        "2>/dev/null || true && setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL "
        "2>/dev/null || true && { \\builtin unalias -- 'unsetenv'; "
        "\\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 || true && "
        "eval 'node ~/.config/tools/socket-watch.mjs' < /dev/null && "
        "pwd -P >| /tmp/claude-33fb-cwd")

    def test_from_a_zsh_not_one_flag_is_inherited(self):
        self.assertEqual(hv.inheritable_flags(self.ZSH), [])

    def test_claude_is_recognised_by_its_argv0(self):
        for argv0 in ("claude", "/opt/homebrew/bin/claude",
                      "claude-code", "2.1.233",
                      "/Users/j/.local/bin/2.1.233"):
            self.assertTrue(hv.is_claude_argv([argv0]), argv0)

    def test_what_is_not_claude(self):
        for argv in ([], None, ["/bin/zsh"], ["-zsh"], ["python3", "x.py"],
                     ["/usr/bin/ssh"], ["2.1"], ["v2.1.233"], [None]):
            self.assertFalse(hv.is_claude_argv(argv), argv)


class TestArgvOfPid(unittest.TestCase):
    def test_it_reads_the_argv_of_a_live_process(self):
        argv = hv.argv_of_pid(os.getpid())
        self.assertTrue(argv, "it should read this very process's argv")
        self.assertIn("python", argv[0].lower())

    def test_a_pid_that_is_no_good_is_not_an_error(self):
        for pid in (None, 0, -1, "hello", 999999):
            self.assertEqual(hv.argv_of_pid(pid), [], pid)


class TestPaneInfo(unittest.TestCase):
    def test_a_live_pane(self):
        with mock.patch.object(hv, "tmux",
                               lambda *a, **k: _Response(0, "pricing\t2.1.233\n")):
            self.assertEqual(hv.pane_info("%3"), ("pricing", "2.1.233"))

    def test_a_pane_that_is_gone_does_not_get_through(self):
        """tmux 3.6a does not fail on a dead pane: it exits 0 with an empty line."""
        with mock.patch.object(hv, "tmux", lambda *a, **k: _Response(0, "\t\n")):
            self.assertIsNone(hv.pane_info("%999"))

    def test_a_tmux_that_does_not_answer(self):
        with mock.patch.object(hv, "tmux", lambda *a, **k: None):
            self.assertIsNone(hv.pane_info("%3"))
        with mock.patch.object(hv, "tmux", lambda *a, **k: _Response(1, "")):
            self.assertIsNone(hv.pane_info("%3"))


class TestTypeKeys(unittest.TestCase):
    def test_what_it_sends_to_tmux(self):
        calls = []

        def fake(*args, **kw):
            calls.append(args)
            return _Response(0, "")
        with mock.patch.object(hv, "tmux", fake):
            self.assertTrue(hv.type_keys("%3", "claude -n 'x 2'"))
        self.assertEqual(calls,
                         [("send-keys", "-t", "%3", "claude -n 'x 2'", "Enter")])

    def test_a_pane_that_went_away_is_not_a_success(self):
        with mock.patch.object(hv, "tmux", lambda *a, **k: _Response(1, "")):
            self.assertFalse(hv.type_keys("%999", "hello"))
        with mock.patch.object(hv, "tmux", lambda *a, **k: None):
            self.assertFalse(hv.type_keys("%3", "hello"))


class TestLaunchCommand(unittest.TestCase):
    """What is TYPED into the shell: the title goes in single quotes."""

    def test_a_normal_title(self):
        self.assertEqual(hv.launch_command("pricing 3"), "claude -n 'pricing 3'")

    def test_the_inherited_flags_go_BEHIND_the_title(self):
        """The order protects the `-n`, and it is not cosmetic.

        If `FLAGS_WITH_VALUE` falls short (claude adds a new flag that demands a
        value), its value is lost on inheriting and on the new line that flag
        swallows the next token BLINDLY. With the flags in front, what it swallows
        is the `-n`, and then the title becomes the FIRST PROMPT of the agent
        being born: it fails silently and spends tokens. Behind the title the `-n`
        is safe: at worst another inherited flag is lost, and normally claude
        complains out loud about the missing value.
        """
        self.assertEqual(
            hv.launch_command("pricing 3",
                              ["--dangerously-skip-permissions", "--model", "opus"]),
            "claude -n 'pricing 3' --dangerously-skip-permissions --model opus")

    def test_a_flag_with_spaces_is_quoted_too(self):
        cmd = hv.launch_command("x 2", ["--append-system-prompt", "be brief"])
        self.assertEqual(shlex.split(cmd),
                         ["claude", "-n", "x 2",
                          "--append-system-prompt", "be brief"])

    def test_a_title_with_a_quote_does_not_break_the_line(self):
        cmd = hv.launch_command("it's")
        # The real proof: the shell splits it into the expected arguments.
        self.assertEqual(shlex.split(cmd), ["claude", "-n", "it's"])

    def test_a_title_with_metacharacters_executes_nothing(self):
        cmd = hv.launch_command("$(rm -rf ~) `id` ; echo")
        self.assertEqual(shlex.split(cmd),
                         ["claude", "-n", "$(rm -rf ~) `id` ; echo"])


class TestHandoverOrchestration(unittest.TestCase):
    """The glue of `handover()`: what is typed, in what order, and when NOT.

    The pieces that talk to tmux and to the disk are replaced (they all live as
    module globals) so we can look at EXACTLY what would be typed into the pane
    without standing up a tmux.
    """

    # `awaiting_input` always comes from the `Stop`: the card is faithful on
    # purpose, since the last event is precisely what the guard now looks at.
    CARD = {"session_id": "sid", "tmux_pane": "%3", "state": "awaiting_input",
            "last_event": "Stop", "pid": 1,
            "updated_at": "2026-08-17T10:00:00+02:00"}
    _DEFAULT = object()  # so "card=None" can mean "there is no card"

    def _harness(self, session="pricing", running="2.1.233", card=_DEFAULT,
                 shell_arrives=True,
                 argv=("claude", "--dangerously-skip-permissions"),
                 parked=None, closes_after=3, status_cc=None):
        """Mute the module and return (typed, notices, waits, titles) to look at.

        `session=None` = that pane is gone; `card=None` = there is no live agent
        there; `running` is the `pane_current_command`; `argv` is what `ps` would
        answer about the agent being closed.
        """
        if card is self._DEFAULT:
            card = self.CARD
        typed, notices, waits, titles = [], [], [], []

        def patch(name, value):
            p = mock.patch.object(hv, name, value)
            p.start()
            self.addCleanup(p.stop)

        patch("pane_info", lambda pane: (session, running) if session else None)
        patch("load_sessions", lambda: [card] if card else [])
        patch("argv_of_pid", lambda pid: list(argv))
        patch("new_title",
              lambda sid, fallback: titles.append((sid, fallback)) or "pricing 3")
        patch("type_keys", lambda pane, txt: typed.append((pane, txt)) or True)
        patch("wait_for_shell",
              lambda read, *a: waits.append(a) or shell_arrives)
        patch("notify", notices.append)
        patch("parked_job", lambda card, registry=None: parked)
        patch("status_cc_of", lambda card, registry=None: status_cc)
        patch("press", lambda pane, key: typed.append((pane, "<%s>" % key)) or True)
        # The parked pane "closes" (turns into a shell) after `closes_after`
        # ctrl+c presses -- or never, if shell_arrives is False.
        patch("current_cmd",
              lambda pane: "zsh" if (shell_arrives and sum(1 for t in typed if t[1] == "<C-c>") >= closes_after)
              else running)
        # No sleeping in the suite. This reaches `close_parked` too, now that its
        # `sleep` default is resolved when it is CALLED and not when the module
        # is imported -- before that, the parked tests really waited out six
        # ctrl+c pauses each and this file took nine seconds.
        p = mock.patch.object(hv.time, "sleep", lambda s: None)
        p.start(); self.addCleanup(p.stop)
        return typed, notices, waits, titles

    def test_the_happy_path_closes_and_starts_in_that_order(self):
        typed, notices, _, titles = self._harness()
        self.assertEqual(hv.handover("%3"), "pricing 3")
        # The new agent is born with the flags of the one being closed, and
        # behind the title (see `test_the_inherited_flags_go_BEHIND_the_title`).
        self.assertEqual(
            typed,
            [("%3", "/exit"),
             ("%3", "claude -n 'pricing 3' --dangerously-skip-permissions")])
        # The title's fallback is the tmux session name, and the id comes from
        # the card: that is the wiring the design asks for.
        self.assertEqual(titles, [("sid", "pricing")])
        self.assertEqual(len(notices), 1)

    def test_if_the_shell_does_not_arrive_nothing_is_started(self):
        typed, notices, waits, _ = self._harness(shell_arrives=False)
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [("%3", "/exit")])  # and nothing else
        self.assertIn("did not close", notices[0])
        # The time waited and the time the notice quotes are the same figure.
        self.assertEqual(waits, [(hv.TIMEOUT_SHELL, hv.WAIT_STEP)])
        self.assertIn("%d s" % int(hv.TIMEOUT_SHELL), notices[0])

    def test_a_working_agent_does_not_get_a_single_key(self):
        typed, notices, _, _ = self._harness(
            card=dict(self.CARD, state="working", last_event="PostToolUse"))
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])

    def test_a_just_resumed_agent_IS_handed_over(self):
        """Resumed, sitting still, and the card said "working".

        Here the handover has to go all the way -- `/exit` and start -- which is
        what it did not do before it looked at the last event.
        """
        typed, notices, _, _ = self._harness(
            card=dict(self.CARD, state="working", last_event="SessionStart"))
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(
            typed,
            [("%3", "/exit"),
             ("%3", "claude -n 'pricing 3' --dangerously-skip-permissions")])

    def test_an_agent_asking_for_a_permission_does_not_get_a_single_key(self):
        typed, _, _, _ = self._harness(
            card=dict(self.CARD, state="needs_attention"))
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])

    def test_an_agent_with_an_open_form_does_not_either(self):
        typed, notices, _, _ = self._harness(
            card=dict(self.CARD, state="asking", last_event="PreToolUse"))
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("asking", notices[0])

    def test_in_a_pane_with_no_agent_nothing_is_typed(self):
        """The pane is the user's shell, not an agent to hand over.

        Typing here would be the worst of all: `/exit` and `claude -n '...'` would
        be EXECUTED as commands in their shell, on top of whatever they were
        doing. That is why we check that not a single key comes out, not just
        that `handover` returns None.
        """
        typed, notices, _, _ = self._harness(card=None)
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("no live agent", notices[0])

    def test_in_a_reserved_session_nothing_is_typed(self):
        typed, _, _, _ = self._harness(session="flightdeck")
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])

    def test_a_tool_we_cannot_relaunch_closes_nothing(self):
        """With no line to type behind it, the `/exit` would leave the pane mute.

        That is why the line for the one being BORN is built BEFORE closing the
        one leaving: a tool Flightdeck does not know how to relaunch is left
        exactly as it was.
        """
        typed, notices, _, _ = self._harness(
            card=dict(self.CARD, tool="gemini"), running="gemini")
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("relaunch", notices[0])

    def test_a_pane_that_is_already_a_shell_only_starts(self):
        """There is nobody to say `/exit` to, and typing it would execute it."""
        typed, _, _, _ = self._harness(running="zsh")
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(
            typed,
            [("%3", "claude -n 'pricing 3' --dangerously-skip-permissions")])

    def test_whatever_happens_it_gives_exactly_one_notice(self):
        """Pressing the key and seeing NOTHING is the one unacceptable ending."""
        cases = [{}, {"session": None}, {"card": None}, {"session": "flightdeck"},
                 {"card": dict(self.CARD, state="working")},
                 {"card": dict(self.CARD, tool="gemini"), "running": "gemini"},
                 {"shell_arrives": False}]
        for kw in cases:
            with self.subTest(**kw):
                _, notices, _, _ = self._harness(**kw)
                hv.handover("%3")
                self.assertEqual(len(notices), 1, notices)


class TestHandoverWithABusyRegistry(TestHandoverOrchestration):
    def test_with_a_busy_registry_it_refuses_without_typing(self):
        typed, notices, _, _ = self._harness(status_cc="busy")
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("still working", notices[0])

    def test_with_an_idle_registry_the_handover_is_normal(self):
        typed, _, _, _ = self._harness(status_cc="idle")
        self.assertEqual(hv.handover("%3"), "pricing 3")


class TestHandoverOnARowThatCameFromTheRegistry(unittest.TestCase):
    """`prefix + n` on a claude that was already open when the hooks went in.

    Those panes have no card of their own -- cards are written by the hooks --
    and their green row is synthesised from Claude Code's own registry
    (`common._registry_cards`). The handover used to read the cards directory
    itself, so it answered "there is no live agent to hand over in this pane" on
    a pane whose row the same menu had just painted `⏳ WAITING FOR YOU`. It asks
    `load_sessions` now, which is the very list the menu and the bar paint from,
    so these rows are handed over like any other -- and refused, like any other,
    while the registry says that session is busy.

    Nothing is faked between the registry and the decision: the real
    `load_sessions` runs over a faked registry and faked panes with an empty
    cards directory, which is exactly the shape of a machine on the day of an
    install.
    """

    PANES = {"%9": {"session": "ssr cache", "window": "0", "cmd": "2.1.273",
                    "window_name": "claude"}}

    def _entry(self, status="idle", **kw):
        """A registry entry as `read_registry` normalises it (a real shape, read
        off a live machine with the personal values cut down). The timestamps
        are moved to now so the ages come out sane."""
        now_ms = int(time.time() * 1000)
        d = {"pid": os.getpid(), "session_id": "ef1864c2-x", "cwd": "/w/ssr_cache",
             "kind": "interactive", "tmux": "ssr cache:@9.%9", "parked_job": None,
             "job_id": None, "name": "ssr cache 2", "status": status,
             "waiting_for": None, "started_at": now_ms - 3600_000,
             "status_updated_at": now_ms, "updated_at": now_ms}
        d.update(kw)
        return d

    def _run(self, entry):
        """Hand over on `%9`. -> (returned title, typed, notices, titles)."""
        typed, notices, titles = [], [], []
        with tempfile.TemporaryDirectory() as tmp:
            patches = [
                # The state directory is empty: no hook has ever written a card
                # for this pane, which is the whole point.
                mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}),
                mock.patch.object(common, "live_panes", return_value=self.PANES),
                mock.patch.object(common, "read_registry", return_value=[entry]),
                # `status_cc_of` and `parked_job` read the registry themselves,
                # through handover's own import of it.
                mock.patch.object(hv, "read_registry", return_value=[entry]),
                mock.patch.object(hv, "configured_pins", list),
                mock.patch.object(hv, "pane_info",
                                  lambda pane: ("ssr cache", "2.1.273")),
                mock.patch.object(hv, "argv_of_pid",
                                  lambda pid: ["claude",
                                               "--dangerously-skip-permissions"]),
                mock.patch.object(
                    hv, "new_title",
                    lambda sid, fallback: titles.append((sid, fallback))
                    or "ssr cache 3"),
                mock.patch.object(hv, "type_keys",
                                  lambda pane, txt: typed.append((pane, txt)) or True),
                mock.patch.object(hv, "wait_for_shell", lambda *a: True),
                mock.patch.object(hv, "notify", notices.append),
            ]
            for p in patches:
                p.start()
            try:
                return hv.handover("%9"), typed, notices, titles
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_a_busy_session_is_refused_without_a_single_key(self):
        # `busy` is the registry's own word for "a turn (or a delegated task) is
        # running", and it is the same word `_fix_with_registry` already trusts
        # to refuse a handover on a card the hooks wrote.
        title, typed, notices, _ = self._run(self._entry(status="busy"))
        self.assertIsNone(title)
        self.assertEqual(typed, [])
        self.assertIn("still working", notices[0])

    def test_after_a_turn_it_is_handed_over_like_any_other_row(self):
        title, typed, notices, titles = self._run(self._entry(status="idle"))
        self.assertEqual(title, "ssr cache 3")
        self.assertEqual(
            typed,
            [("%9", "/exit"),
             ("%9", "claude -n 'ssr cache 3' --dangerously-skip-permissions")])
        # The id the title was counted from is the registry's, and the fallback
        # the tmux session name: the synthesised card carries both.
        self.assertEqual(titles, [("ef1864c2-x", "ssr cache")])
        self.assertEqual(len(notices), 1)

    def test_a_claude_that_has_never_had_a_turn_is_handed_over_too(self):
        # The `○ open` case: Claude Code writes `idle` the moment it registers
        # itself, so the synthesised card comes out `working` +
        # `last_event: SessionStart`, and that is precisely the exception
        # `reason_not_to_close` makes for a session just opened or just resumed.
        now_ms = int(time.time() * 1000)
        title, typed, _, _ = self._run(
            self._entry(status="idle", started_at=now_ms - 46,
                        status_updated_at=now_ms))
        self.assertEqual(title, "ssr cache 3")
        self.assertEqual(typed[0], ("%9", "/exit"))


class TestHandoverOnAParkedPane(TestHandoverOrchestration):
    """The pane's agent is PARKED (`/background` or the ← arrow): its
    conversation lives in a daemon job, and the one in the pane is a watcher
    with a stale card. Measured with 2.1.234: there `/exit` does NOT
    exit (it opens the agents view: "ctrl+c twice quits"), and ctrl+c ×2 does
    exit leaving the job ALIVE. And in the agents view, typing text would create
    a new task -- which is why nothing is typed there, only keys."""

    # pid 1 as in CARD: `card_for_pane` checks that the pid is really ALIVE
    # (with the pid of a real parked claude, the test died the moment that
    # claude was closed).
    PARKED = {"session_id": "afbe6f19-x", "tmux_pane": "%3",
              "state": "needs_attention", "last_event": "Notification", "pid": 1,
              "updated_at": "2026-08-18T09:38:54+02:00"}

    def test_it_closes_with_ctrl_c_until_the_shell_arrives_not_with_exit_and_says_the_job_lives(self):
        # It took FOUR live: the 1st eats the half-written text, the
        # 2nd goes to the agents view (like /exit), the 3rd and 4th are that
        # view's "ctrl+c twice quits". That is why it is not a fixed number: it
        # presses and looks for a shell, up to a cap.
        typed, notices, _, _ = self._harness(card=self.PARKED, parked="e8053ca3",
                                             closes_after=4)
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(typed[:4], [("%3", "<C-c>")] * 4)
        self.assertEqual(typed[4], ("%3", "claude -n 'pricing 3' --dangerously-skip-permissions"))
        self.assertNotIn(("%3", "/exit"), typed)
        self.assertEqual(len(notices), 1)
        self.assertIn("pricing 3", notices[0])
        self.assertIn("background", notices[0])
        self.assertIn("e8053ca3", notices[0])

    def test_the_state_guards_do_not_apply_the_panes_card_is_an_echo(self):
        # needs_attention / working / looping on the parked pane's card are echoes
        # (notifications from the job, or the turn it was parked in): they do not
        # block. What protects us is that if the agent does not close in 20 s
        # (e.g. we cut a real turn short), nothing is started.
        for state in ("needs_attention", "working", "looping", "asking"):
            with self.subTest(state=state):
                typed, notices, _, _ = self._harness(
                    card=dict(self.PARKED, state=state, last_event="PostToolUse"),
                    parked="e8053ca3")
                self.assertEqual(hv.handover("%3"), "pricing 3")

    def test_if_the_shell_does_not_arrive_nothing_is_started(self):
        typed, notices, _, _ = self._harness(card=self.PARKED, parked="e8053ca3",
                                             shell_arrives=False)
        self.assertIsNone(hv.handover("%3"))
        # Up to the press cap, and nothing else.
        self.assertEqual(typed, [("%3", "<C-c>")] * hv.MAX_CTRL_C)
        self.assertIn("did not close", notices[0])

    def test_without_parking_everything_stays_the_same(self):
        typed, _, _, _ = self._harness(parked=None)
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(typed[0], ("%3", "/exit"))


class TestCloseParked(unittest.TestCase):
    def _close(self, after, cap=hv.MAX_CTRL_C, press_fails=False):
        keys, pauses = [], []
        read = lambda: "zsh" if len(keys) >= after else "2.1.233"
        with mock.patch.object(hv, "press",
                               lambda pane, k: keys.append((pane, k)) or not press_fails):
            ok = hv.close_parked("%3", read, pause=0.25, sleep=pauses.append,
                                 cap=cap)
        return ok, keys, pauses

    def test_it_presses_ctrl_c_and_looks_until_there_is_a_shell(self):
        for after in (2, 4):
            with self.subTest(after=after):
                ok, keys, pauses = self._close(after)
                self.assertTrue(ok)
                self.assertEqual(keys, [("%3", "C-c")] * after)
                # one pause after EVERY press, before looking
                self.assertEqual(pauses, [0.25] * after)

    def test_with_the_cap_reached_it_returns_True_and_lets_wait_for_shell_decide(self):
        ok, keys, _ = self._close(after=99, cap=6)
        self.assertTrue(ok)
        self.assertEqual(len(keys), 6)

    def test_if_the_pane_goes_away_it_returns_False_and_stops(self):
        ok, keys, _ = self._close(after=99, press_fails=True)
        self.assertFalse(ok)
        self.assertEqual(keys, [("%3", "C-c")])


class TestStatusCcOf(unittest.TestCase):
    REG = [{"pid": 57250, "session_id": "afbe6f19-x", "kind": "interactive",
            "status": "busy"},
           {"pid": 8832, "session_id": "65cfe20d-x", "kind": "interactive",
            "status": "shell"}]

    def test_by_the_cards_pid(self):
        self.assertEqual(hv.status_cc_of({"pid": 57250}, self.REG), "busy")
        self.assertEqual(hv.status_cc_of({"pid": 8832}, self.REG), "shell")
        self.assertIsNone(hv.status_cc_of({"pid": 1}, self.REG))
        self.assertIsNone(hv.status_cc_of(None, self.REG))


class TestParkedJob(unittest.TestCase):
    REG = [{"pid": 57250, "session_id": "afbe6f19-x", "kind": "interactive",
            "tmux": "retail_specs:@12.%15", "parked_job": "e8053ca3",
            "job_id": None},
           {"pid": 8832, "session_id": "65cfe20d-x", "kind": "interactive",
            "tmux": None, "parked_job": None, "job_id": None}]

    def test_it_returns_the_jobs_short_id_by_the_cards_pid(self):
        self.assertEqual(hv.parked_job({"pid": 57250}, self.REG), "e8053ca3")
        self.assertIsNone(hv.parked_job({"pid": 8832}, self.REG))
        self.assertIsNone(hv.parked_job({"pid": 1}, self.REG))
        self.assertIsNone(hv.parked_job({}, self.REG))
        self.assertIsNone(hv.parked_job({"pid": 57250}, []))
        self.assertIsNone(hv.parked_job(None, self.REG))


class TestHandoverOnCodex(TestHandoverOrchestration):
    CODEX_CARD = {"session_id": "cx-1", "tmux_pane": "%3",
                  "state": "awaiting_input", "last_event": "Stop", "pid": 1,
                  "tool": "codex", "updated_at": "2026-09-07T10:00:00+02:00"}

    def test_it_closes_with_exit_and_relaunches_codex_with_its_flags(self):
        # /exit closes codex cleanly (measured on a real codex).
        typed, notices, _, _ = self._harness(
            card=self.CODEX_CARD, running="codex",
            argv=("codex", "--model", "frontier-x", "-p", "profile"))
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(typed, [("%3", "/exit"),
                                 ("%3", "codex --model frontier-x -p profile")])

    def test_a_recycled_pid_inherits_nothing(self):
        typed, _, _, _ = self._harness(card=self.CODEX_CARD, running="codex",
                                       argv=("zsh", "-c", "x"))
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(typed[1], ("%3", "codex"))


class TestHandoverOnAgy(TestHandoverOrchestration):
    AGY_CARD = {"session_id": "ag-1", "tmux_pane": "%3",
                "state": "awaiting_input", "last_event": "Stop", "pid": 1,
                "tool": "agy", "updated_at": "2026-09-08T10:00:00+02:00"}

    def test_it_closes_agy_with_exit_and_relaunches_with_go_flags(self):
        # The close is the same `/exit` for all three (agy's help: "Exit: Ctrl+D
        # Ctrl+D (or /exit or /quit)"). The flags come back in Go's canonical
        # form -- a typed `-mode plan` comes out `--mode plan` -- and
        # `--conversation` is left out: it picks WHICH conversation to open, i.e.
        # the opposite of handing over.
        typed, _, _, _ = self._harness(
            card=self.AGY_CARD, running="agy",
            argv=("/Users/j/.local/bin/agy", "-mode", "plan",
                  "--conversation", "abc", "--dangerously-skip-permissions"))
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(
            typed,
            [("%3", "/exit"),
             ("%3", "agy --mode plan --dangerously-skip-permissions")])

    def test_a_working_agy_is_refused(self):
        # `PreInvocation` is agy's turn-opening event (it has no
        # UserPromptSubmit): there is work in flight and nothing is typed.
        typed, notices, _, _ = self._harness(
            card=dict(self.AGY_CARD, state="working", last_event="PreInvocation"),
            running="agy")
        self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("still working", notices[0])

    def test_an_agy_with_a_recycled_pid_relaunches_bare(self):
        typed, _, _, _ = self._harness(card=self.AGY_CARD, running="agy",
                                       argv=("zsh", "-c", "x"))
        self.assertEqual(hv.handover("%3"), "pricing 3")
        self.assertEqual(typed[1], ("%3", "agy"))


class TestNotify(unittest.TestCase):
    """The handover's floating notice: the same argv as the hook and the tee."""

    def _notify(self, text, version=(3, 6)):
        """`notify` against a made-up tmux of the version asked for.

        The version is pinned instead of read from the machine so the argv is
        the same here, on Ubuntu 22.04's 3.2a and in CI.
        """
        calls = []

        class _R:
            returncode, stdout, stderr = 0, "/dev/ttys001\n/dev/ttys002\n", ""

        def fake_tmux(*args, **kw):
            calls.append(list(args))
            return _R()

        with mock.patch.object(hv, "tmux", fake_tmux), \
                mock.patch.object(hv, "tmux_version", lambda: version), \
                mock.patch.object(hv.sys, "stderr", io.StringIO()):
            hv.notify(text)
        return [c for c in calls if c[0] == "display-message"]

    def test_one_display_message_per_client_with_a_duration_and_no_freezing(self):
        self.assertEqual(self._notify("🔄 pricing 3"), [
            ["display-message", "-c", "/dev/ttys001", "-d", str(hv.NOTICE_MS),
             "-C", "-l", "🔄 pricing 3"],
            ["display-message", "-c", "/dev/ttys002", "-d", str(hv.NOTICE_MS),
             "-C", "-l", "🔄 pricing 3"],
        ])

    def test_on_tmux_3_2_it_escapes_the_text_instead_of_passing_l(self):
        # `display-message -l` is tmux >= 3.4. On an older one the flag does not
        # exist and the message is read as a format, so a session called
        # `#(something)` would be RUN: the `#` is doubled instead.
        self.assertEqual(self._notify("🔄 #(rm -rf) 3", (3, 2)), [
            ["display-message", "-c", "/dev/ttys001", "-d", str(hv.NOTICE_MS),
             "-C", "🔄 ##(rm -rf) 3"],
            ["display-message", "-c", "/dev/ttys002", "-d", str(hv.NOTICE_MS),
             "-C", "🔄 ##(rm -rf) 3"],
        ])

    def test_a_tmux_it_could_not_read_gets_the_safe_form(self):
        self.assertEqual(self._notify("🔄 a#b 3", None)[0][-2:],
                         ["-C", "🔄 a##b 3"])

    def test_without_tmux_it_does_not_blow_up(self):
        with mock.patch.object(hv, "tmux", lambda *a, **k: None), \
                mock.patch.object(hv.sys, "stderr", io.StringIO()):
            hv.notify("hello")  # it does not raise


class TestAPinnedSessionIsAlsoReserved(unittest.TestCase):
    """A pin's tmux session is only known from the config, so `is_reserved`
    takes the pins as an argument and the handover has to pass them. Without
    this the key would type `/exit` and a `claude -n` into a pinned `top` or
    `cswap tui`, which is exactly what the reserved names exist to prevent.
    """

    def test_the_handover_does_not_run_in_a_pins_session(self):
        typed, notices = [], []
        with mock.patch.object(hv, "pane_info", lambda p: ("accounts", "cswap")), \
                mock.patch.object(hv, "configured_pins",
                                  lambda: [{"name": "accounts", "label": "⚙ accounts",
                                            "session": "accounts",
                                            "command": "cswap tui"}]), \
                mock.patch.object(hv, "type_keys",
                                  lambda pane, txt: typed.append(txt) or True), \
                mock.patch.object(hv, "notify", notices.append):
            self.assertIsNone(hv.handover("%3"))
        self.assertEqual(typed, [])
        self.assertIn("Flightdeck itself", notices[0])


class TestNoticeDuration(unittest.TestCase):
    """The duration of the floating notice, and the cross-check between the
    three emitters.

    Three separate emitters could each carry their own `5000` and drift, and
    then the 🧠 notice would last a different time from the "waiting for you"
    one. They share one source of truth instead, the config's `notice_ms`: this
    half pins that the handover reads it, and the state hook's and the status
    line tee's halves are pinned in their own files.
    """

    def test_the_fallback_is_the_configs_default(self):
        self.assertEqual(hv.NOTICE_MS, config.DEFAULTS["notice_ms"])

    def test_the_notice_lasts_long_enough_to_read_it(self):
        # Without `-d` tmux uses its factory display-time, 750 ms: a flash too
        # short to read.
        self.assertGreaterEqual(hv.NOTICE_MS, 3000)

    def test_a_configured_duration_reaches_tmux(self):
        calls = []

        class _R:
            returncode, stdout, stderr = 0, "/dev/ttys001\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"notice_ms": 8000}))
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}), \
                    mock.patch.object(hv, "tmux",
                                      lambda *a, **k: calls.append(list(a)) or _R()), \
                    mock.patch.object(hv, "tmux_version", lambda: (3, 6)), \
                    mock.patch.object(hv.sys, "stderr", io.StringIO()):
                hv.notify("hello")
        display = [c for c in calls if c[0] == "display-message"][0]
        self.assertEqual(display[display.index("-d") + 1], "8000")

    def test_an_unusable_duration_falls_back_instead_of_breaking_the_notice(self):
        """A `notice_ms` typed as a word must not swallow the notice: `doctor` is
        the place that complains about a value of the wrong type."""
        calls = []

        class _R:
            returncode, stdout, stderr = 0, "/dev/ttys001\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"notice_ms": "five seconds"}))
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}), \
                    mock.patch.object(hv, "tmux",
                                      lambda *a, **k: calls.append(list(a)) or _R()), \
                    mock.patch.object(hv, "tmux_version", lambda: (3, 6)), \
                    mock.patch.object(hv.sys, "stderr", io.StringIO()):
                hv.notify("hello")
        display = [c for c in calls if c[0] == "display-message"][0]
        self.assertEqual(display[display.index("-d") + 1], str(hv.NOTICE_MS))


class TestHandoverOnAPaneWatchingABackgroundConversation(unittest.TestCase):
    """`prefix + n` on a pane that is running `claude attach <id>`.

    That pane is a WINDOW onto a conversation living in a background job, and
    since the row now says so (`common._attach_rows`) the handover finds a live
    card there where it used to find none. It must not act on it: what is on
    screen belongs to the job, and how the viewer closes has never been measured.
    So it says what it sees and types nothing at all.

    Nothing is faked between the process table and the decision: the real
    `load_sessions` runs over a faked registry, faked panes and a faked `ps`,
    with an empty cards directory -- the shape measured on a real machine.
    """

    PANES = {"%2": {"session": "homeassistant", "window": "0", "cmd": "2.1.269",
                    "window_name": "claude", "pid": 5116}}
    PS = [(5116, 1, "-zsh"), (5168, 5116, "claude attach 23a53419")]

    def _job(self):
        now_ms = int(time.time() * 1000)
        return {"pid": os.getpid(),
                "session_id": "23a53419-68a0-491b-8621-6446cb284ab0",
                "cwd": "/w/homeassistant", "kind": "bg", "tmux": None,
                "parked_job": None, "job_id": "23a53419",
                "name": "homeassistant 2", "status": "idle", "waiting_for": None,
                "started_at": now_ms - 3600_000, "status_updated_at": now_ms,
                "updated_at": now_ms}

    def _run(self, running="2.1.269"):
        """Hand over on `%2`. -> (returned title, typed, keys, notices)."""
        typed, keys, notices = [], [], []
        entry = self._job()
        with tempfile.TemporaryDirectory() as tmp:
            patches = [
                mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}),
                mock.patch.object(common, "live_panes", return_value=self.PANES),
                mock.patch.object(common, "read_registry", return_value=[entry]),
                mock.patch.object(common, "process_table", return_value=self.PS),
                mock.patch.object(hv, "read_registry", return_value=[entry]),
                mock.patch.object(hv, "configured_pins", list),
                mock.patch.object(hv, "pane_info",
                                  lambda pane: ("homeassistant", running)),
                mock.patch.object(hv, "argv_of_pid", lambda pid: ["claude"]),
                mock.patch.object(hv, "new_title",
                                  lambda sid, fallback: "homeassistant 3"),
                mock.patch.object(hv, "type_keys",
                                  lambda pane, txt: typed.append((pane, txt)) or True),
                mock.patch.object(hv, "press",
                                  lambda pane, key: keys.append((pane, key)) or True),
                mock.patch.object(hv, "wait_for_shell", lambda *a: True),
                mock.patch.object(hv, "notify", notices.append),
            ]
            for p in patches:
                p.start()
            try:
                return hv.handover("%2"), typed, keys, notices
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_not_a_single_key_reaches_the_viewer_and_it_says_why(self):
        title, typed, keys, notices = self._run()
        self.assertIsNone(title)
        self.assertEqual(typed, [])
        self.assertEqual(keys, [])   # nor the ctrl+c of a parked pane
        self.assertEqual(len(notices), 1)
        self.assertIn("attach", notices[0])
        self.assertIn("background", notices[0])

    def test_a_viewer_SUSPENDED_with_ctrl_z_is_refused_just_the_same(self):
        """ctrl+z on the viewer leaves `pane_current_command` reading `zsh`.

        The pane then passes for one with no agent in it, which is the path that
        skips every guard and only starts the new one -- so the launch line
        would be typed into the shell the viewer is sleeping in, on top of the
        job it is watching. The refusal cannot live inside that branch.
        """
        title, typed, keys, notices = self._run(running="zsh")
        self.assertIsNone(title)
        self.assertEqual(typed, [])
        self.assertEqual(keys, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("attach", notices[0])


if __name__ == "__main__":
    unittest.main()
