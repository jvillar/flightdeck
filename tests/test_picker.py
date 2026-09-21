"""`flightdeck.picker`: the fzf menu, its rows and what each key does.

The pure logic (building the list, formatting it, resolving the selection into a
tmux action) is tested here; the effectful shell around it is exercised with
`subprocess.run` patched, so no test ever launches a real fzf or reaches a tmux
server.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flightdeck import config
from flightdeck import picker as cp
from flightdeck import statusbar as sb

NOW = 10_000.0

# The colour escapes fzf (started with --ansi) eats while painting: the
# selection it gives back comes stripped of them. The tests simulate that.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Live tmux sessions. `flightdeck` (the menu itself) and `accounts` (a pin's
# session) are reserved: they are not work, so they stay out of the green group.
TS = [{"name": "recomm", "id": "$3", "activity": 200.0, "attached": True},
      {"name": "idle", "id": "$5", "activity": 100.0, "attached": False},
      {"name": "flightdeck", "id": "$0", "activity": 999.0, "attached": True},
      {"name": "accounts", "id": "$1", "activity": 50.0, "attached": False}]
# Hook cards of live sessions INSIDE tmux: they name their tmux session.
LIVE = [{"session_id": "aaa", "project": "recommender", "state": "awaiting_input",
         "tmux_session": "recomm", "tmux_pane": "%7", "cwd": "/x/recommender",
         "_age": 30}]
# A live session OUTSIDE tmux (the desktop app): informative only.
OUT = [{"session_id": "bbb", "project": "retail", "state": "working", "_age": 60}]
# History: closed sessions that can be brought back.
REC = [{"session_id": "ccc", "project": "old", "title": "t", "cwd": "/x/old",
        "last_activity": 10.0}]
# The user's pinned rows, as they live in `config.json`.
PINS = [{"name": "accounts", "label": "⚙ accounts", "session": "accounts",
         "command": "cswap tui"}]


def entries():
    return cp.build_entries(TS, LIVE, OUT, REC, now=1000.0, pins=PINS)


@contextlib.contextmanager
def temp_config(**values):
    """A throwaway `config.json` holding exactly `values`."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
            yield path


class TestBuildEntries(unittest.TestCase):
    def test_outside_carries_the_name_from_the_registry(self):
        out = [dict(OUT[0], _name_cc="CLI to manage Claude sessions in tmux")]
        e = cp.build_entries(TS, LIVE, out, REC, now=1000.0, pins=PINS)[3]
        self.assertEqual(e["kind"], "outside")
        self.assertEqual(e["name"], "CLI to manage Claude sessions in tmux")

    def test_order_and_groups(self):
        kinds = [e["kind"] for e in entries()]
        self.assertEqual(kinds, ["tmux", "tmux", "pin", "outside", "recent"])

    def test_the_menu_and_the_pins_are_not_listed_as_work(self):
        names = [e.get("name") for e in entries() if e["kind"] == "tmux"]
        self.assertNotIn("flightdeck", names)
        self.assertNotIn("accounts", names)

    def test_a_tmux_row_is_annotated_with_the_session_inside_it(self):
        e = entries()[0]
        self.assertEqual(e["name"], "recomm")
        self.assertEqual(e["state"], "awaiting_input")
        self.assertEqual(e["session_id"], "aaa")
        self.assertEqual(e["target"], "$3")

    def test_a_tmux_row_with_nothing_inside_is_a_plain_shell(self):
        e = entries()[1]
        self.assertEqual(e["name"], "idle")
        self.assertIsNone(e["state"])
        self.assertIsNone(e["session_id"])

    def test_green_rows_by_activity_desc(self):
        self.assertEqual([e["name"] for e in entries() if e["kind"] == "tmux"],
                         ["recomm", "idle"])

    def test_recent_ones_newest_first(self):
        rec = [{"session_id": "old", "project": "beta", "title": None,
                "cwd": "/x/beta", "last_activity": NOW - 5000},
               {"session_id": "new", "project": "beta", "title": None,
                "cwd": "/x/beta", "last_activity": NOW - 100}]
        grey = [e for e in cp.build_entries([], [], [], rec, now=NOW)
                if e["kind"] == "recent"]
        self.assertEqual([e["session_id"] for e in grey], ["new", "old"])
        self.assertEqual(grey[0]["age"], "1m")  # age worked out against `now`


class TestTheAnnotationOfATmuxSession(unittest.TestCase):
    # One tmux session can hold SEVERAL sessions inside (several panes) and the
    # green row only annotates one: the one WAITING wins.
    TWO = [{"session_id": "t1", "project": "recommender", "state": "working",
            "tmux_session": "recomm", "tmux_pane": "%1", "_age": 3},
           {"session_id": "t2", "project": "recommender", "state": "awaiting_input",
            "tmux_session": "recomm", "tmux_pane": "%2", "_age": 40}]

    def _row(self, live):
        return cp.build_entries(TS, live, [], [], now=1000.0)[0]

    def test_the_one_waiting_wins_not_the_last_in_the_list(self):
        # Without this (a dict built by assignment, last one wins) the session
        # asking for its turn was covered by a "● working" and you did not see
        # it in the menu.
        for live in (self.TWO, list(reversed(self.TWO))):
            e = self._row(live)
            self.assertEqual(e["state"], "awaiting_input")
            self.assertEqual(e["session_id"], "t2")
            self.assertIn("WAITING FOR YOU", cp.visible_columns(e))

    def test_a_notification_also_beats_working(self):
        asks = dict(self.TWO[1], state="needs_attention")
        self.assertEqual(self._row([self.TWO[0], asks])["state"], "needs_attention")
        self.assertEqual(self._row([asks, self.TWO[0]])["state"], "needs_attention")

    def test_if_none_is_waiting_any_valid_one_is_annotated(self):
        both_working = [self.TWO[0], dict(self.TWO[1], state="working")]
        self.assertEqual(self._row(both_working)["state"], "working")


class TestASessionInALoop(unittest.TestCase):
    """"looping" = the session finished a `/loop` round and is waiting for its
    timer, not for you. Its own badge, NEUTRAL: it does not count as a wait (nor
    does it win the row's annotation when several sessions share a tmux one)."""

    LOOPING = [{"session_id": "t1", "project": "watch", "state": "looping",
                "last_event": "Stop", "tmux_session": "recomm",
                "tmux_pane": "%1", "_age": 5}]

    def test_it_is_painted_as_looping(self):
        self.assertEqual(cp.state_badge("looping"), "🔁 looping")
        row = cp.build_entries(TS, self.LOOPING, [], [], now=1000.0)[0]
        v = cp.visible_columns(row)
        self.assertIn("🔁 looping", v)
        self.assertNotIn("WAITING", v)

    def test_it_does_not_count_as_a_wait(self):
        self.assertNotIn("looping", cp._WAITING_STATES)


class TestResumingPerTool(unittest.TestCase):
    GREY_CODEX = {"kind": "recent", "session_id": "019ffd5b-abc", "project": "retail",
                  "title": None, "cwd": "/x/retail", "age": "3h", "tool": "codex"}

    def _typed_command(self, seq):
        return next(a[-1] for a in seq if a[0] == "send-keys" and "-l" in a)

    def test_enter_on_a_grey_codex_row_types_codex_resume(self):
        seq = cp.action_argvs(self.GREY_CODEX, set())
        self.assertEqual(self._typed_command(seq), "codex resume 019ffd5b-abc")

    def test_ctrl_f_on_a_grey_codex_row_does_nothing(self):
        # codex has no --fork-session: the copy belongs to claude (flightdeck.tools).
        self.assertEqual(cp.action_argvs(self.GREY_CODEX, set(), fork=True), [])

    def test_enter_on_a_grey_agy_row_types_agy_conversation_and_ctrl_f_does_not(self):
        # agy conversations are part of the history; agy has no copy
        # (--fork-session belongs to claude).
        grey = dict(self.GREY_CODEX, tool="agy", session_id="38777a1e-8cb4")
        self.assertEqual(self._typed_command(cp.action_argvs(grey, set())),
                         "agy --conversation=38777a1e-8cb4")
        self.assertEqual(cp.action_argvs(grey, set(), fork=True), [])

    def test_ctrl_l_on_a_grey_codex_row_DOES_launch_and_on_an_unknown_one_does_not(self):
        # codex has a flag catalogue of its own; a tool without one still does
        # not launch.
        seq = cp.launch_argvs(self.GREY_CODEX, set(), "codex resume 019ffd5b-abc --search")
        self.assertTrue(seq)
        self.assertEqual(cp.launch_argvs(dict(self.GREY_CODEX, tool="gemini"), set(), "x"), [])

    def test_command_with_flags_per_tool(self):
        self.assertEqual(cp.command_with_flags("019ffd5b-abc", ["--search"], tool="codex"),
                         "codex resume 019ffd5b-abc --search")
        self.assertEqual(cp.command_with_flags("abc-1", ["--model opus"]),
                         "claude --resume abc-1 --model opus")
        self.assertEqual(cp.command_with_flags("abc-1", [], tool="gemini"), "")

    def test_catalogue_per_tool(self):
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cp.flag_catalogue("codex"))
        self.assertIn("--dangerously-skip-permissions", cp.flag_catalogue())
        self.assertIn("--mode plan", cp.flag_catalogue("agy"))
        self.assertIsNone(cp.flag_catalogue("gemini"))

    def test_the_grey_claude_rows_stay_the_same(self):
        grey = dict(self.GREY_CODEX, tool=None, session_id="abc-1")
        self.assertEqual(self._typed_command(cp.action_argvs(grey, set())),
                         "claude --resume abc-1")
        self.assertEqual(self._typed_command(cp.action_argvs(grey, set(), fork=True)),
                         "claude --resume abc-1 --fork-session")


class TestTheToolMark(unittest.TestCase):
    """A codex/agy session is seen with its mark in the annotation; claude ones
    stay as they always were, without noise."""

    GREEN = {"kind": "tmux", "name": "retail", "session_id": "t1", "project": "retail",
             "state": "awaiting_input", "last_event": "Stop", "cwd": "/x",
             "age": "2m", "tool": "codex"}
    GREY = {"kind": "recent", "session_id": "r1", "project": "retail",
            "title": "mrp fix", "cwd": "/x", "age": "3h", "tool": "codex"}
    OUTSIDE = {"kind": "outside", "session_id": "o1", "project": "retail",
               "state": "working", "cwd": "/x", "age": "1m", "name": None, "tool": "agy"}

    def test_the_brand_glyph_with_its_colour(self):
        # ✳ claude clay / ⬡ codex green / ✦ agy blue; a shell gets a blank.
        v = cp.visible_columns(dict(self.GREEN, tool=None))        # claude
        self.assertIn("\x1b[38;5;173m✳", v)
        v = cp.visible_columns(self.GREEN)                         # codex
        self.assertIn("\x1b[38;5;36m⬡", v)
        v = cp.visible_columns(dict(self.GREEN, session_id=None, tool=None))
        self.assertNotIn("✳", v)                                   # shell: a blank
        self.assertIn("  retail", v)
        self.assertIn("⬡", cp.visible_columns(self.GREY))
        self.assertIn("\x1b[38;5;69m✦", cp.visible_columns(self.OUTSIDE))
        # and the grey claude rows carry theirs too
        self.assertIn("✳", cp.visible_columns(dict(self.GREY, tool=None)))

    def test_green_grey_and_outside_all_mark_the_tool(self):
        self.assertIn("· codex ·", cp.visible_columns(self.GREEN))
        self.assertIn("· codex", cp.visible_columns(self.GREY))
        self.assertIn("agy ·", cp.visible_columns(self.OUTSIDE))

    def test_claude_carries_no_mark(self):
        for e in (dict(self.GREEN, tool=None), dict(self.GREEN, tool="claude")):
            v = cp.visible_columns(e)
            self.assertNotIn("claude", v)
            self.assertNotIn("· codex", v)

    def test_build_entries_propagates_the_tool(self):
        live = [{"session_id": "t1", "project": "demo", "state": "working",
                 "last_event": "PostToolUse", "tmux_session": "recomm",
                 "tmux_pane": "%1", "_age": 5, "tool": "codex"}]
        rec = [{"session_id": "r1", "project": "p", "title": None, "cwd": "/x",
                "last_activity": 900.0, "tool": "codex"}]
        out = [{"session_id": "o1", "project": "p", "state": "working", "cwd": "/x",
                "_age": 5, "tool": "agy"}]
        es = cp.build_entries(TS, live, out, rec, now=1000.0)
        by_kind = {e["kind"]: e for e in es if e["kind"] in ("outside", "recent")}
        green = next(e for e in es if e["kind"] == "tmux" and e.get("session_id") == "t1")
        self.assertEqual(green.get("tool"), "codex")
        self.assertEqual(by_kind["outside"].get("tool"), "agy")
        self.assertEqual(by_kind["recent"].get("tool"), "codex")


class TestABackgroundedSession(unittest.TestCase):
    """The pane's conversation went to a daemon job (`/background` or the ←
    arrow): `load_sessions` already brings the job's state and `_parked` in the
    pane's card; the row says so, so it is understood why a pane that looks
    still is "working" (and why the handover does something else there)."""

    PARKED = [{"session_id": "t1", "project": "retail_specs", "state": "looping",
               "last_event": "Stop", "tmux_session": "recomm", "tmux_pane": "%1",
               "_age": 30, "_parked": "e8053ca3-2805-x"}]

    def test_the_row_marks_it(self):
        row = cp.build_entries(TS, self.PARKED, [], [], now=1000.0)[0]
        self.assertEqual(row["parked"], "e8053ca3-2805-x")
        v = cp.visible_columns(row)
        self.assertIn("🔁 looping", v)
        self.assertIn("background", v)

    def test_without_parking_there_is_no_mark(self):
        row = cp.build_entries(TS, [dict(self.PARKED[0], _parked=None)], [], [],
                               now=1000.0)[0]
        self.assertIsNone(row["parked"])
        self.assertNotIn("background", cp.visible_columns(row))

    def test_a_pane_that_only_WATCHES_one_is_marked_the_same(self):
        """A pane running `claude attach <id>` is a window onto a job.

        `common._attach_rows` gives that row the same `_parked` key, so the mark
        needs nothing new here: from where the user sits the two are one thing,
        a conversation that lives in the background.
        """
        watcher = dict(self.PARKED[0], state="awaiting_input", last_event=None,
                       _parked="23a53419", _attach="23a53419")
        v = cp.visible_columns(cp.build_entries(TS, [watcher], [], [],
                                                now=1000.0)[0])
        self.assertIn("background", v)


class TestASessionOPENandSTILL(unittest.TestCase):
    """"working" + the last event still being the one that started it = open,
    not working.

    Starting or resuming leaves the card in "working" (only the `Stop` brings it
    down), so the menu painted "● working" over a session that was doing
    nothing. It is the same signal the handover uses to decide whether it can
    type, and the lie cost a handover that refused itself, seen live.
    """

    OPEN = [{"session_id": "t1", "project": "retail", "state": "working",
             "last_event": "SessionStart", "tmux_session": "recomm",
             "tmux_pane": "%1", "_age": 5}]

    def _row(self, live):
        return cp.build_entries(TS, live, [], [], now=1000.0)[0]

    def test_build_entries_passes_the_last_event(self):
        self.assertEqual(self._row(self.OPEN)["last_event"], "SessionStart")

    def test_it_is_painted_open_and_NOT_working(self):
        v = cp.visible_columns(self._row(self.OPEN))
        self.assertIn("open", v)
        self.assertNotIn("working", v)

    def test_a_real_turn_still_says_working(self):
        for event in ("UserPromptSubmit", "PostToolUse"):
            live = [dict(self.OPEN[0], last_event=event)]
            self.assertIn("working", cp.visible_columns(self._row(live)), event)

    def test_a_COMPACTION_is_not_painted_as_open(self):
        """The menu and the handover read the same signal and have to agree.

        Claude compacts when its context fills up, that is, in the middle of a
        turn: that `SessionStart` leaves the card looking like a start-up, but
        there it IS working (and the handover refuses, see
        `flightdeck.handover.reason_not_to_close`).
        """
        live = [dict(self.OPEN[0], source="compact")]
        v = cp.visible_columns(self._row(live))
        self.assertIn("working", v)
        self.assertNotIn("open", v)

    def test_every_other_start_is_painted_open(self):
        for source in ("startup", "resume", "clear", "fork", None):
            live = [dict(self.OPEN[0], source=source)]
            self.assertIn("open", cp.visible_columns(self._row(live)), source)

    def test_a_card_without_a_last_event_is_painted_as_always(self):
        """Cards written by an older hook: nothing changes for them."""
        old = {k: v for k, v in self.OPEN[0].items() if k != "last_event"}
        self.assertIn("working", cp.visible_columns(self._row([old])))

    def test_the_bar_does_not_notice(self):
        """`○ open` is neutral: it was not a wait before and it is not now.

        The bar counts the `state` (`awaiting_input`/`needs_attention`), not the
        badge, so a repainted "working" cannot sneak in as "waiting for you". It
        is checked with the real bar, not with its list of states.
        """
        row = self._row(self.OPEN)
        self.assertEqual(row["state"], "working")
        self.assertEqual(sb._waiting_section([row]), "")


class TestTheAskingBadge(unittest.TestCase):
    """"asking" = Claude opened a form and is waiting for you to choose."""

    ASKING = {"session_id": "t9", "project": "pricing", "state": "asking",
              "last_event": "PreToolUse", "tmux_session": "recomm",
              "tmux_pane": "%1", "_age": 4}

    def _row(self, live):
        return cp.build_entries(TS, live, [], [], now=1000.0)[0]

    def test_you_can_see_that_it_is_asking_you(self):
        v = cp.visible_columns(self._row([self.ASKING]))
        self.assertIn("❓", v)
        self.assertIn("asking you", v)

    def test_it_wins_the_annotation_over_a_session_that_works(self):
        """Two sessions in the same tmux one: the row annotates only one, and
        the one that needs you cannot end up covered -- that is what the menu is
        for."""
        works = {"session_id": "t8", "project": "pricing", "state": "working",
                 "last_event": "PostToolUse", "tmux_session": "recomm",
                 "tmux_pane": "%2", "_age": 1}
        for live in ([works, self.ASKING], [self.ASKING, works]):
            self.assertEqual(self._row(live)["state"], "asking")

    def test_for_the_bar_it_is_a_wait(self):
        """If it is asking you, it is waiting for you: it counts in `⏳ N waiting`."""
        row = self._row([self.ASKING])
        self.assertIn("waiting", sb._waiting_section([row]))


class TestVisibleColumns(unittest.TestCase):
    def test_visible_ansi(self):
        vs = [cp.visible_columns(e) for e in entries()]
        self.assertTrue(vs[0].startswith("\033[32m"))   # tmux: green
        self.assertIn("\033[2m", vs[3])                 # outside tmux: dimmed
        self.assertTrue(vs[4].startswith("\033[90m"))   # history: grey
        self.assertIn("⚙", vs[2])                       # the pinned row

    def test_a_tmux_row_annotates_project_and_state(self):
        vs = [cp.visible_columns(e) for e in entries()]
        self.assertIn("recomm", vs[0])                  # the tmux session's name
        self.assertIn("recommender", vs[0])             # the project inside it
        self.assertIn("WAITING FOR YOU", vs[0])
        self.assertIn("shell", vs[1])                   # a session with nothing inside

    def test_outside_says_the_name_the_tool_registers(self):
        """"◇ claude · outside tmux" said nothing: it was the basename of the
        cwd, and a fork (⑂) was unrecognisable in the menu, seen live. The
        label is the NAME from Claude Code's registry (the same one its agents
        view shows), trimmed to 40 with an ellipsis of our own so the state
        stays visible on a phone too (~80 col); the folder is
        the fallback."""
        long = ("MySensors home automation Arduino ⑂ investigate the mechanisms "
                "of the new series")
        e = {"kind": "outside", "session_id": "x", "project": "claude",
             "state": "awaiting_input", "cwd": "/x", "age": "19m",
             "name": long}
        v = cp.visible_columns(e)
        self.assertIn("MySensors home automation Arduino ⑂ inv…", v)
        self.assertNotIn("mechanisms", v)          # really trimmed
        self.assertIn("outside tmux (⏳ WAITING FOR YOU)", v)
        self.assertNotIn(" claude ", v)            # the folder is no longer the label
        # Without a name in the registry: the folder, as always, with no ellipsis.
        v = cp.visible_columns(dict(e, name=None))
        self.assertIn("claude", v)
        self.assertNotIn("…", v)

    def test_truncate_with_an_ellipsis(self):
        self.assertEqual(cp.truncate("abc", 5), "abc")
        self.assertEqual(cp.truncate("abcdef", 5), "abcd…")
        self.assertEqual(cp.truncate("", 5), "")
        self.assertEqual(cp.truncate(None, 5), "")

    def test_outside_and_recent_say_their_own_thing(self):
        vs = [cp.visible_columns(e) for e in entries()]
        self.assertIn("retail", vs[3])
        self.assertIn("outside tmux", vs[3])
        self.assertIn("old", vs[4])                     # the history project
        # and the transcript's title, which is what you search for in fzf
        grey = dict(entries()[4], title="fix the import")
        self.assertIn("fix the import", cp.visible_columns(grey))


class TestTheFlagCatalogue(unittest.TestCase):
    """Ctrl-L's flag menu: what is shown and what comes back."""

    def test_the_catalogue_carries_the_everyday_flags(self):
        flags = [f for f, _ in cp.COMMON_FLAGS]
        for expected in ("--dangerously-skip-permissions", "--model fable",
                         "--model opus", "--model sonnet",
                         "--permission-mode plan"):
            self.assertIn(expected, flags)

    def test_the_copy_is_NOT_in_the_catalogue(self):
        """`--fork-session` belongs to Ctrl-F, and not as a division of labour:
        Ctrl-F christens the session `<project>-fork`. From Ctrl-L the copy
        would be born with the original's name and in the green list you could
        no longer tell which is the good conversation -- what decision 5 of the
        source's CLAUDE.md protects.
        """
        self.assertNotIn("--fork-session", [f for f, _ in cp.COMMON_FLAGS])

    def test_the_note_is_seen_but_does_NOT_travel_into_the_command(self):
        """The row explains itself in plain words; that explanation is not a flag.

        If the note slipped into the line, claude would get `(start` as a prompt.
        """
        line = cp.flag_line("--permission-mode plan", "start in plan mode")
        self.assertIn("start in plan mode", line)
        self.assertEqual(cp.line_flag(line), "--permission-mode plan")

    def test_a_row_without_a_note_is_the_bare_flag(self):
        self.assertEqual(cp.flag_line("--model opus", ""), "--model opus")
        self.assertEqual(cp.line_flag("--model opus"), "--model opus")

    def test_the_flag_survives_spare_whitespace(self):
        for line in ("  --model opus  ", "--model opus   (the usual one)"):
            self.assertEqual(cp.line_flag(line), "--model opus", line)

    def test_the_whole_catalogue_goes_and_comes_back(self):
        """Every line handed to fzf has to come back as its flag."""
        lines = cp.flag_catalogue().split("\n")
        self.assertEqual(len(lines), len(cp.COMMON_FLAGS))
        self.assertEqual([cp.line_flag(l) for l in lines],
                         [f for f, _ in cp.COMMON_FLAGS])


class TestCommandWithFlags(unittest.TestCase):
    """The PRE-FILLED line the user edits before launching."""

    ID = "9f3c1a2b-0000-4444-8888-aaaabbbbcccc"

    def test_without_flags_it_is_the_usual_resume(self):
        self.assertEqual(cp.command_with_flags(self.ID, []),
                         "claude --resume %s" % self.ID)

    def test_the_flags_go_behind_the_resume(self):
        self.assertEqual(
            cp.command_with_flags(self.ID, ["--model opus",
                                            "--dangerously-skip-permissions"]),
            "claude --resume %s --model opus --dangerously-skip-permissions" % self.ID)

    def test_an_id_that_is_not_an_id_builds_nothing(self):
        """That line ends up in a `send-keys`: a strange id would be typing commands."""
        for bad in ("; rm -rf ~", "$(id)", "9f3c 1a2b", "", None):
            self.assertEqual(cp.command_with_flags(bad, ["--model opus"]), "", bad)


class TestLaunchArgvs(unittest.TestCase):
    """Ctrl-L's sequence: the SAME one as resuming, with another command inside."""

    GREY = {"kind": "recent", "session_id": "9f3c1a2b-0000-4444-8888-aaaabbbbcccc",
            "project": "example.com", "cwd": "/x/example.com"}

    def test_it_is_create_type_and_jump_in_that_order(self):
        seq = cp.launch_argvs(self.GREY, set(), "claude --resume abc --model opus")
        self.assertEqual([a[0] for a in seq],
                         ["new-session", "send-keys", "send-keys", "switch-client"])
        # Name sanitised as tmux would, and the project's cwd.
        self.assertEqual(seq[0], ["new-session", "-d", "-s", "example_com",
                                  "-c", "/x/example.com"])
        # The command goes LITERAL (-l) and after the end of flags (--); Enter apart.
        self.assertEqual(seq[1], ["send-keys", "-t", "example_com", "-l", "--",
                                  "claude --resume abc --model opus"])
        self.assertEqual(seq[2], ["send-keys", "-t", "example_com", "Enter"])
        self.assertEqual(seq[3], ["switch-client", "-t", "example_com"])

    def test_the_name_is_deduped_against_the_existing_ones(self):
        seq = cp.launch_argvs(self.GREY, {"example_com"}, "claude --resume abc")
        self.assertEqual(seq[0][3], "example_com#2")
        self.assertEqual(seq[-1], ["switch-client", "-t", "example_com#2"])

    def test_only_on_a_grey_row(self):
        """On a green one there is nothing to relaunch: the session is open."""
        for kind in ("tmux", "pin", "outside"):
            self.assertEqual(
                cp.launch_argvs(dict(self.GREY, kind=kind), set(), "claude"),
                [], kind)

    def test_without_a_command_nothing_is_typed(self):
        """An empty line = cancelled: better not to open the session either."""
        self.assertEqual(cp.launch_argvs(self.GREY, set(), "   "), [])

    def test_the_users_line_is_typed_AS_IS(self):
        """It is THEIR line, as if typed into their shell: nothing is re-escaped."""
        theirs = "claude --resume abc --append-system-prompt 'be brief' # note"
        seq = cp.launch_argvs(self.GREY, set(), theirs)
        self.assertEqual(seq[1], ["send-keys", "-t", "example_com", "-l", "--", theirs])

    def test_an_odd_line_travels_as_TEXT_and_not_as_flags_or_keys(self):
        """The three that broke a one-piece send-keys (measured on tmux 3.6a):

        starting with a dash came out as `invalid flag` (rc 1) LEAVING the tmux
        session created -- an orphan; `Enter` was read as the key and typed
        nothing; and `C-c` sent a real Ctrl-C, which killed the process in the
        test pane. Hence `-l` (literal) and `--` (end of flags), with Enter in a
        send-keys of its own.
        """
        for theirs in ("--model opus and tell me something", "Enter", "C-c"):
            with self.subTest(theirs=theirs):
                seq = cp.launch_argvs(self.GREY, set(), theirs)
                self.assertEqual(seq[1],
                                 ["send-keys", "-t", "example_com", "-l", "--", theirs])
                self.assertEqual(seq[2], ["send-keys", "-t", "example_com", "Enter"])


class TestFzfFormat(unittest.TestCase):
    def test_a_line_carries_index_hidden_key_text_and_the_two_detail_lines(self):
        # Five tab-separated fields: index, stable key (the tmux session's id),
        # the visible text, and the two lines of the strip fzf previews. The key
        # goes in a field of its OWN so fzf can follow it across reloads
        # (--id-nth 2, see _fzf_args).
        line = cp.format_fzf_line(0, entries()[0])
        index, key, visible, first, second = line.split("\t")
        self.assertEqual((index, key), ("0", "$3"))
        self.assertIn("recomm", visible)
        self.assertEqual((first, second), cp.detail_lines(entries()[0]))

    def test_the_visible_field_is_byte_for_byte_what_it_always_was(self):
        # --with-nth went from `3..` to `3` when the two detail fields joined:
        # with five fields `3..` would paint them into the row as well. This
        # pins that the row itself did not change.
        for entry in entries():
            with self.subTest(kind=entry["kind"]):
                self.assertEqual(cp.format_fzf_line(0, entry).split("\t")[2],
                                 cp._one_line(cp.visible_columns(entry)))

    def test_a_stable_key_per_row_type(self):
        # tmux -> the session's id; pin -> its tmux session; outside/history ->
        # the session's id. None of this changes while the menu is open.
        self.assertEqual([cp.entry_key(e) for e in entries()],
                         ["$3", "$5", "accounts", "bbb", "ccc"])

    def test_the_key_does_not_depend_on_the_age(self):
        e = entries()[0]
        self.assertEqual(cp.entry_key(dict(e, age="99h 59m")), cp.entry_key(e))

    def test_a_row_without_an_id_does_not_break_the_format(self):
        # A card with no session_id: a filler key, but NEVER empty nor with
        # spaces (it would split the hidden field in two).
        e = {"kind": "recent", "session_id": None, "project": "p", "title": None,
             "cwd": "/x", "age": "1h", "state": None}
        self.assertEqual(cp.entry_key(e), "?")
        self.assertEqual(cp.format_fzf_line(4, e).split("\t", 2)[:2], ["4", "?"])

    def test_the_input_has_one_line_per_entry(self):
        es = entries()
        self.assertEqual(len(cp.fzf_input(es).splitlines()), len(es))

    def test_parse_selection_recovers_index_and_key(self):
        self.assertEqual(cp.parse_selection("3\t$7\tsomething visible"), (3, "$7"))
        # And out of a whole five-field line, which is what fzf gives back now.
        self.assertEqual(cp.parse_selection("3\t$7\tvisible\t📁 /x\t✳ claude"),
                         (3, "$7"))
        # And it survives fzf giving back something format_fzf_line never made.
        for odd in ("", "x\ty\tz", "3", "3\t"):
            with self.subTest(odd=odd), self.assertRaises(ValueError):
                cp.parse_selection(odd)

    def test_format_fzf_line_collapses_newlines(self):
        entry = {"kind": "recent", "session_id": "R9", "project": "p",
                 "title": "line1\nline2\tcol", "cwd": "/x/p", "age": "1h",
                 "state": None}
        line = cp.format_fzf_line(0, entry)
        self.assertEqual(line.count("\n"), 0)
        # index / key / visible / detail 1 / detail 2, and nothing else: a tab
        # inside a title would shift every field after it.
        self.assertEqual(line.count("\t"), 4)
        self.assertIn("line1 line2 col", line.split("\t")[4])

    def test_format_fzf_line_collapses_a_cr_in_a_tmux_row(self):
        entry = {"kind": "tmux", "name": "p\ttab", "target": "$9",
                 "session_id": "L9", "project": "pro\r\nject", "state": "working",
                 "cwd": "/x", "age": "5s"}
        line = cp.format_fzf_line(0, entry)
        self.assertEqual(line.count("\n"), 0)
        self.assertEqual(line.count("\t"), 4)


class TestTheDetailStrip(unittest.TestCase):
    """The two lines under the list that follow the cursor.

    `detail_lines` builds them; they travel in two hidden fields of the line and
    fzf paints them as the preview of the current row (see `_fzf_args`).
    """

    def test_a_green_row_shows_its_folder_its_branch_and_who_is_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ssr_cache"
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            first, second = cp.detail_lines(
                {"kind": "tmux", "name": "ssr", "target": "$3",
                 "session_id": "ef1864c2-99ab-4e6d", "project": "ssr_cache",
                 "title": "ssr cache 2", "state": "working",
                 "last_event": "SessionStart", "source": "startup",
                 "cwd": str(repo), "tool": "claude", "age": "42h"})
        self.assertTrue(first.startswith("📁 "), first)
        self.assertTrue(first.endswith("ssr_cache · main"), first)
        self.assertEqual(second, "✳ claude · ssr cache 2 · open · 42h · id ef1864c2")

    def test_a_worktree_answers_with_its_own_branch(self):
        # `.git` as a FILE saying `gitdir:` is a worktree, and being on another
        # branch is the whole point of one.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").mkdir()
            (root / "real" / "HEAD").write_text("ref: refs/heads/feature/x\n")
            work = root / "wt"
            work.mkdir()
            (work / ".git").write_text("gitdir: %s\n" % (root / "real"))
            first, _ = cp.detail_lines(
                {"kind": "tmux", "session_id": "s1", "project": "wt",
                 "state": "working", "cwd": str(work), "age": "1m"})
        self.assertTrue(first.endswith(" · feature/x"), first)

    def test_a_detached_head_shows_the_short_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".git" / "HEAD").write_text("17bdb2a9c3f4e5d6a7b8c9\n")
            first, _ = cp.detail_lines(
                {"kind": "tmux", "session_id": "s1", "project": "p",
                 "state": "working", "cwd": tmp, "age": "1m"})
        self.assertTrue(first.endswith(" · 17bdb2a"), first)

    def test_outside_a_repo_the_line_is_only_the_folder(self):
        # `git_branch` answers `no branch` there, and a line saying so would be
        # noise on every folder that is not a checkout.
        first, _ = cp.detail_lines(
            {"kind": "tmux", "session_id": "s1", "project": "proj",
             "state": "working", "cwd": "/x/proj", "age": "1m"})
        self.assertEqual(first, "📁 /x/proj")

    def test_a_card_with_no_folder_says_so_instead_of_guessing(self):
        first, _ = cp.detail_lines(
            {"kind": "recent", "session_id": "s1", "project": "p", "title": "t",
             "state": None, "cwd": None, "age": "3h"})
        self.assertEqual(first, "📁 no folder")

    def test_a_grey_row_shows_the_title_whole_and_claims_no_state(self):
        # The list truncates a long title at the width of the terminal; this is
        # where you read the rest of it. And a closed conversation is in no
        # state at all: saying `shell` there (the badge's fallback) would be a lie.
        title = "Rework the margin calculation so the weekly report stops lying"
        first, second = cp.detail_lines(
            {"kind": "recent", "session_id": "ccccdddd-1111-2222", "project": "old",
             "title": title, "cwd": "/x/old", "age": "18h 40m", "state": None,
             "tool": "claude"})
        self.assertEqual(first, "📁 /x/old")
        self.assertEqual(second, "✳ claude · %s · 18h 40m · id ccccdddd" % title)

    def test_every_tool_carries_its_own_glyph_and_its_name(self):
        base = {"kind": "recent", "session_id": "abcdefgh-9", "project": "api",
                "title": "t", "cwd": "/x/api", "age": "2m", "state": None}
        self.assertTrue(cp.detail_lines(dict(base, tool="codex"))[1]
                        .startswith("⬡ codex · "))
        self.assertTrue(cp.detail_lines(dict(base, tool="agy"))[1]
                        .startswith("✦ agy · "))
        # No tool on the card is claude, as it is everywhere else.
        self.assertTrue(cp.detail_lines(dict(base, tool=None))[1]
                        .startswith("✳ claude · "))

    def test_an_unknown_tool_is_named_without_borrowing_a_glyph(self):
        second = cp.detail_lines({"kind": "recent", "session_id": "z1", "title": "t",
                                  "project": "p", "cwd": "/x", "age": "1m",
                                  "state": None, "tool": "newthing"})[1]
        self.assertTrue(second.startswith("newthing · "), second)

    def test_a_shell_row_says_shell_and_falls_back_to_the_projects_folder(self):
        # A green row with no agent inside carries no folder: tmux's session
        # list does not hand out the pane's path. The projects directory is
        # where Ctrl-N opened it, so it is the honest answer.
        with mock.patch.dict(os.environ, {"FLIGHTDECK_PROJECTS_DIR": "/h/code"}):
            self.assertEqual(
                cp.detail_lines({"kind": "tmux", "name": "sandbox", "target": "$9",
                                 "session_id": None, "project": None, "state": None,
                                 "cwd": None, "age": ""}),
                ("📁 /h/code", "shell"))

    def test_a_pin_shows_the_command_it_runs_and_the_session_it_lives_in(self):
        self.assertEqual(cp.detail_lines(cp.build_entries([], [], [], [], pins=PINS)[0]),
                         ("pin: cswap tui", "pinned · session accounts"))

    def test_a_pin_with_no_command_says_it_opens_a_plain_shell(self):
        entry = cp.build_entries([], [], [], [],
                                 pins=[{"name": "notes", "session": "notes"}])[0]
        self.assertEqual(cp.detail_lines(entry),
                         ("pin: (the default shell)", "pinned · session notes"))

    def test_an_outside_row_names_itself_and_its_state(self):
        self.assertEqual(
            cp.detail_lines({"kind": "outside", "name": "review the PR",
                             "target": None, "session_id": "bbbbcccc-22",
                             "project": "retail", "state": "working",
                             "cwd": "/x/retail", "tool": "claude", "age": "8m"}),
            ("📁 /x/retail", "outside tmux · review the PR · working"))

    def test_the_state_is_the_badge_said_in_plain_words(self):
        # Derived from the badge and not from a second table, so the strip can
        # never tell a different story from the row above it.
        base = {"kind": "tmux", "session_id": "s1", "project": "p", "title": "t",
                "cwd": "/x", "age": "1m", "tool": "claude"}
        said = {s: cp.detail_lines(dict(base, state=s))[1].split(" · ")[2]
                for s in ("awaiting_input", "needs_attention", "asking",
                          "working", "looping")}
        self.assertEqual(said, {"awaiting_input": "waiting for you",
                                "needs_attention": "needs attention",
                                "asking": "asking you", "working": "working",
                                "looping": "looping"})

    def test_a_backgrounded_row_says_so_where_the_list_says_it(self):
        _, second = cp.detail_lines(
            {"kind": "tmux", "session_id": "s1", "project": "p", "title": "t",
             "cwd": "/x", "age": "1m", "state": "awaiting_input", "parked": "ab12"})
        self.assertEqual(second,
                         "✳ claude · t · waiting for you · background · 1m · id s1")

    def test_a_tab_or_a_newline_in_a_title_cannot_shift_the_fields(self):
        line = cp.format_fzf_line(0, {"kind": "recent", "session_id": "R9",
                                      "project": "p", "title": "one\ttwo\nthree",
                                      "cwd": "/x/p", "age": "1h", "state": None})
        self.assertEqual(line.count("\t"), 4)
        self.assertEqual(line.count("\n"), 0)
        self.assertIn("one two three", line.split("\t")[4])


class TestActions(unittest.TestCase):
    # `action_argvs` is pure: it returns the sequence of tmux argvs (without the
    # 'tmux' in front) to run in order. [] = there is nothing to do.
    def test_action_tmux_is_a_switch_by_id(self):
        e = {"kind": "tmux", "target": "$3"}
        self.assertEqual(cp.action_argvs(e, set()), [["switch-client", "-t", "$3"]])

    def test_action_recent_resumes_shell_first(self):
        # They come back in a NEW tmux session (shell first, then the
        # `claude --resume` is typed into it): if the tool dies, the pane is
        # still a usable shell.
        e = {"kind": "recent", "session_id": "abc-1", "project": "foo", "cwd": "/x/foo"}
        argvs = cp.action_argvs(e, {"foo"})
        self.assertEqual(argvs, [
            ["new-session", "-d", "-s", "foo#2", "-c", "/x/foo"],
            ["send-keys", "-t", "foo#2", "-l", "--", "claude --resume abc-1"],
            ["send-keys", "-t", "foo#2", "Enter"],
            ["switch-client", "-t", "foo#2"],
        ])

    def test_action_recent_with_an_unsafe_id_does_nothing(self):
        # The id goes inside a `send-keys`: if it is not a clean id, we type nothing.
        e = {"kind": "recent", "session_id": "a;rm -rf", "project": "x", "cwd": "/tmp"}
        self.assertEqual(cp.action_argvs(e, set()), [])

    def test_action_pin_creates_the_session_if_it_is_not_there(self):
        e = cp.build_entries([], [], [], [], pins=PINS)[0]
        self.assertEqual(cp.action_argvs(e, {"flightdeck"}), [
            ["new-session", "-d", "-s", "accounts", "cswap tui"],
            ["switch-client", "-t", "accounts"]])
        self.assertEqual(cp.action_argvs(e, {"accounts"}),
                         [["switch-client", "-t", "accounts"]])

    def test_action_outside_is_empty(self):
        self.assertEqual(cp.action_argvs({"kind": "outside"}, set()), [])

    def test_create_argvs(self):
        self.assertEqual(cp.create_argvs("new", {"new"}, home="/h/code"), [
            ["new-session", "-d", "-s", "new#2", "-c", "/h/code"],
            ["switch-client", "-t", "new#2"]])

    def test_a_name_with_a_dot_is_sanitised_as_tmux_would(self):
        # tmux turns "." and ":" into "_" SILENTLY when creating the session. If
        # we do not sanitise first, (a) the dedup does not see the real clash and
        # (b) the switch-client points at "example.com", which does not exist: the
        # session is created and you do not jump to it.
        self.assertEqual(cp.create_argvs("example.com", set(), home="/h"), [
            ["new-session", "-d", "-s", "example_com", "-c", "/h"],
            ["switch-client", "-t", "example_com"]])
        self.assertEqual(cp.create_argvs("example.com", {"example_com"}, home="/h"), [
            ["new-session", "-d", "-s", "example_com#2", "-c", "/h"],
            ["switch-client", "-t", "example_com#2"]])
        # And the same when resuming from the history, which uses the project.
        e = {"kind": "recent", "session_id": "abc-1", "project": "example.com",
             "cwd": "/x/example.com"}
        self.assertEqual(cp.action_argvs(e, {"example_com"}), [
            ["new-session", "-d", "-s", "example_com#2", "-c", "/x/example.com"],
            ["send-keys", "-t", "example_com#2", "-l", "--", "claude --resume abc-1"],
            ["send-keys", "-t", "example_com#2", "Enter"],
            ["switch-client", "-t", "example_com#2"]])


class TestPolish(unittest.TestCase):
    def test_parse_fzf_output(self):
        # --print-query (line0=query) + --expect (line1=key) + selection (line2+)
        self.assertEqual(cp.parse_fzf_output("test\nctrl-n\n"), ("test", "ctrl-n", []))
        q, k, sel = cp.parse_fzf_output("cars\n\n0\tvisible\n")
        self.assertEqual((q, k), ("cars", ""))
        self.assertEqual(sel, ["0\tvisible"])


class TestFeedAndFzfArgs(unittest.TestCase):
    # What `python3 -m flightdeck.picker feed` writes: one line per entry, with
    # the hidden index in front (fzf reloads with this through --listen).
    def test_fzf_input_still_one_line_per_entry(self):
        lines = cp.fzf_input([{"kind": "pin", "label": "⚙ accounts",
                               "target": "accounts"}]).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("0\taccounts\t"))

    def test_the_fallback_port_is_the_documented_default(self):
        # The contract with the bash command, which reads `menu_port` from the
        # config (`python3 -m flightdeck.config menu_port`).
        self.assertEqual(cp.LISTEN_PORT, config.DEFAULTS["menu_port"])
        self.assertEqual(cp.LISTEN_PORT, 42707)

    def test_args_paint_ansi_and_only_listen_if_the_version_can(self):
        with_l, without = cp._fzf_args(True), cp._fzf_args(False)
        self.assertIn("--ansi", without)      # without this the menu paints escapes
        self.assertIn("--listen", with_l)
        self.assertIn(str(cp.LISTEN_PORT), with_l)
        self.assertNotIn("--listen", without)

    def test_args_hide_index_and_key_and_follow_the_key_across_reloads(self):
        """The visible field starts at 3 (index and key, hidden), and with an
        fzf that understands it (>= 0.71) the row's identity is its key
        (--id-nth 2): that way a `track-current+reload` leaves the cursor on the
        SAME session even if the list is reordered (measured on 0.72: without
        --id-nth the cursor stays in the same position, i.e. on another row)."""
        args = cp._fzf_args(True, supports_id_nth=True)
        self.assertEqual(args[args.index("--with-nth") + 1], "3")
        self.assertEqual(args[args.index("--id-nth") + 1], "2")
        self.assertNotIn("--track", args)   # per-reload tracking, not global
        without = cp._fzf_args(True, supports_id_nth=False)
        self.assertNotIn("--id-nth", without)  # an old fzf would reject it and not start
        self.assertEqual(without[without.index("--with-nth") + 1], "3")

    def test_the_detail_strip_is_fzfs_preview_of_the_two_hidden_fields(self):
        """Fields 4 and 5 painted by a `printf`, two rows under the list.

        `{4}`/`{5}` index the ORIGINAL line, which `--with-nth 3` does not
        renumber, and fzf quotes them for the shell (both measured on 0.72).
        So moving the cursor costs one `printf` and never a python re-scan.

        Three rows for two lines, also measured: the size counts the border, so
        `down,2` shows line 1 and scrolls line 2 out of sight.
        """
        args = cp._fzf_args(False)
        self.assertEqual(args[args.index("--preview") + 1],
                         'printf "%s\\n" {4} {5}')
        self.assertEqual(args[args.index("--preview-window") + 1],
                         "down,3,wrap,border-top")
        # `?` hides it again (the header says so, pinned with the other keys in
        # `test_fzf_listens_for_ctrl_l_and_announces_it`).
        self.assertIn("?:toggle-preview", args)

    def test_the_fzf_version_decides_whether_there_is_listen(self):
        self.assertTrue(cp._version_supports_listen("0.72.0 (Homebrew)\n"))
        self.assertTrue(cp._version_supports_listen("0.36.0"))
        self.assertFalse(cp._version_supports_listen("0.35.1 (brew)"))
        self.assertFalse(cp._version_supports_listen(""))   # odd output: no --listen

    def test_the_fzf_version_decides_whether_there_is_id_nth(self):
        self.assertTrue(cp._version_supports_id_nth("0.72.0 (Homebrew)\n"))
        self.assertTrue(cp._version_supports_id_nth("0.71.0"))
        self.assertFalse(cp._version_supports_id_nth("0.70.1 (brew)"))
        self.assertFalse(cp._version_supports_id_nth("0.36.0"))
        self.assertFalse(cp._version_supports_id_nth(""))


class TestSelectionAgainstAFreshList(unittest.TestCase):
    # Two things change between fzf painting the list and the human pressing
    # Enter: (1) the list may have been RELOADED (--listen + the tmux hook), so
    # the index numbers another list; (2) the visible text carries the AGE
    # ("12s"), which moves by itself every second. Hence the row is re-resolved
    # by the stable KEY of the hidden field, never by the text.
    def _line(self, i, es):
        """The line EXACTLY as fzf prints it: without the colour codes.

        Verified against fzf 0.72.0: with --ansi it eats the escapes while
        painting and gives the selection back stripped ('0\\t$3\\tnice green',
        not '0\\t$3\\t\\x1b[32mnice green\\x1b[0m').
        """
        return _ANSI.sub("", cp.format_fzf_line(i, es[i]))

    def test_the_simulated_line_carries_no_colour(self):
        # Pins the property that makes the tests below falsifiable.
        line = self._line(0, entries())
        self.assertNotIn("\x1b", line)
        self.assertIn("recomm", line)

    def test_an_index_that_still_matches_gives_back_that_entry(self):
        es = entries()
        self.assertIs(cp.resolve_selection(self._line(1, es), es), es[1])

    def test_the_age_changes_between_painting_and_pressing_and_it_still_resolves(self):
        # The failure that forced the stable key in: fzf paints the row with
        # "12s", the human takes 3 s to press Enter and the fresh list already
        # says "15s". Comparing text matched neither way and Enter did nothing,
        # precisely on freshly opened sessions.
        painted = cp.build_entries(TS, [dict(LIVE[0], _age=12)], [], [], now=1000.0)
        fresh = cp.build_entries(TS, [dict(LIVE[0], _age=15)], [], [], now=1000.0)
        self.assertIn("12s", cp.visible_columns(painted[0]))
        self.assertIn("15s", cp.visible_columns(fresh[0]))   # the text DOES change
        self.assertIs(cp.resolve_selection(self._line(0, painted), fresh), fresh[0])

    def test_a_shifted_list_finds_the_row_by_its_key(self):
        # Somebody opened a session while the menu was open: everything +1.
        es = entries()
        line = self._line(1, es)                                    # 'idle'
        shifted = [dict(es[0], name="just-born", target="$99")] + es
        chosen = cp.resolve_selection(line, shifted)
        self.assertEqual(chosen["name"], "idle")
        self.assertIs(chosen, shifted[2])

    def test_a_row_that_is_gone_gives_back_nothing(self):
        es = entries()
        line = self._line(1, es)
        without_idle = [e for e in es if e is not es[1]]
        self.assertIsNone(cp.resolve_selection(line, without_idle))

    def test_a_broken_line_does_not_blow_up(self):
        self.assertIsNone(cp.resolve_selection("no-index-no-tab", entries()))

    def test_enter_without_a_selection_stays_in_the_menu(self):
        # A filter matching nothing: Enter picks no row (fzf gives back 0 and an
        # empty selection). Dropping to the shell for mistyping a search was
        # losing sight of the menu; now it repaints.
        fzf = subprocess.CompletedProcess([], 0, "whatever\n\n", "")
        with mock.patch.object(cp, "gather_entries", return_value=[]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf):
            self.assertTrue(cp._chooser_once(False))

    def test_no_matches_rc1_stays_in_the_menu(self):
        # The REAL code for "the filter matches nothing" is 1, not 0 (measured
        # with fzf 0.72.0: rc=1 and stdout 'zzzznomatch\n\n').
        fzf = subprocess.CompletedProcess([], 1, "zzzznomatch\n\n", "")
        with mock.patch.object(cp, "gather_entries", return_value=[]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf):
            self.assertTrue(cp._chooser_once(False))

    def test_esc_still_falls_to_the_shell(self):
        # The other side: Esc/Ctrl-C (rc 130) does leave the pane as a usable shell.
        fzf = subprocess.CompletedProcess([], 130, "", "")
        with mock.patch.object(cp, "gather_entries", return_value=[]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf):
            self.assertFalse(cp._chooser_once(False))

    def test_an_fzf_that_does_not_even_start_falls_to_the_shell_instead_of_spinning(self):
        # An fzf that dies on start-up exits with 2 and an empty stdout
        # (measured: a bad flag or an invalid --listen port). Staying in the loop
        # would mean reopening it at several Hz for nothing, burning CPU in
        # silence: it is explained and we fall to the shell, which is at least a
        # usable pane.
        fzf = subprocess.CompletedProcess([], 2, "", "invalid listen port: 999999")
        err = io.StringIO()
        with mock.patch.object(cp, "gather_entries", return_value=[]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            self.assertFalse(cp._chooser_once(False))
        self.assertIn("2", err.getvalue())
        self.assertIn("invalid listen port", err.getvalue())

    def test_a_selection_that_is_gone_warns_instead_of_going_quiet(self):
        # The session died between painting and pressing: without the warning,
        # Enter looked broken.
        es = entries()
        fzf = subprocess.CompletedProcess([], 0, "\n\n%s\n" % self._line(1, es), "")
        err = io.StringIO()
        with mock.patch.object(cp, "gather_entries", side_effect=[es, []]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            self.assertTrue(cp._chooser_once(False))
        self.assertIn("gone", err.getvalue())

    def test_the_loop_acts_on_the_fresh_list_not_on_the_old_one(self):
        # A guard on the wiring. fzf opened with `old`, reloaded with `new` (one
        # row more in front) and the human pressed Enter on 'idle', which in the
        # RELOADED list is index 2. Indexing the old list with that 2 would give
        # the pinned row: another session.
        old = entries()
        new = [dict(old[0], name="just-born", target="$99")] + old
        self.assertEqual(new[2]["name"], "idle")
        self.assertEqual(old[2]["kind"], "pin")          # what the bug would give
        line = self._line(2, new)
        acted = []
        fzf = subprocess.CompletedProcess([], 0, "\n\n%s\n" % line, "")
        with mock.patch.object(cp, "gather_entries", side_effect=[old, new]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf), \
                mock.patch.object(cp, "_act", lambda e, **kw: acted.append(e)):
            self.assertTrue(cp._chooser_once(False))
        self.assertEqual([e["name"] for e in acted], ["idle"])


class TestTheTmuxRunner(unittest.TestCase):
    SEQ = [["new-session", "-d", "-s", "foo", "-c", "/x/foo"],
           ["send-keys", "-t", "foo", "-l", "--", "claude --resume abc-1"],
           ["send-keys", "-t", "foo", "Enter"],
           ["switch-client", "-t", "foo"]]

    def _run(self, codes):
        """Run the sequence with a fake tmux; returns (ok, argvs, stderr)."""
        calls, left = [], list(codes)

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, left.pop(0), "", "no such session")

        err = io.StringIO()
        with mock.patch.object(cp.subprocess, "run", fake_run), \
                mock.patch.object(sys, "stderr", err):
            ok = cp._run_tmux_seq(self.SEQ)
        return ok, calls, err.getvalue()

    def test_it_stops_at_the_first_failure(self):
        # The point: if the new-session fails (name taken), carrying on typing
        # would write the `claude --resume` into SOMEBODY ELSE'S session that
        # already has that name.
        ok, calls, err = self._run([1, 0, 0, 0])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("new-session", err)

    def test_the_whole_sequence_when_all_goes_well(self):
        ok, calls, err = self._run([0, 0, 0, 0])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 4)
        self.assertEqual(err, "")


class TestTheTitleOnGreenRows(unittest.TestCase):
    """The conversation's title (/rename or the AI one) is seen on the green
    row, with the project as the fallback when there is no title."""

    TS = [{"name": "payments", "id": "$7", "activity": 100.0, "attached": True}]

    def _live(self, title):
        return [{"session_id": "aaa", "project": "example_com",
                 "state": "awaiting_input", "tmux_session": "payments",
                 "tmux_pane": "%1", "cwd": "/x/example.com", "_age": 5,
                 "_title": title}]

    def test_build_entries_passes_the_title(self):
        e = cp.build_entries(self.TS, self._live("review redsys"), [], [])[0]
        self.assertEqual(e["title"], "review redsys")

    def test_visible_prefers_the_title_over_the_project(self):
        e = cp.build_entries(self.TS, self._live("review redsys"), [], [])[0]
        v = cp.visible_columns(e)
        self.assertIn("review redsys", v)
        self.assertNotIn("example_com", v)

    def test_visible_without_a_title_falls_back_to_the_project(self):
        e = cp.build_entries(self.TS, self._live(None), [], [])[0]
        self.assertIn("example_com", cp.visible_columns(e))


class TestTheBrainOnTheGreenRow(unittest.TestCase):
    """The % of context ("how much brain is left") is seen on the green row from
    CTX_SHOW on, so you know which session needs a /compact soon.

    Below that threshold it is not shown: a session just opened at 12% has
    nothing to say, and filling the list with numbers would make it unreadable.
    """

    TS = [{"name": "payments", "id": "$7", "activity": 100.0, "attached": True}]

    def _live(self, pct):
        return [{"session_id": "aaa", "project": "example_com",
                 "state": "working", "tmux_session": "payments",
                 "tmux_pane": "%1", "cwd": "/x", "_age": 5, "_title": "billing",
                 "_ctx_pct": pct}]

    def _row(self, pct):
        return cp.build_entries(self.TS, self._live(pct), [], [])[0]

    def test_build_entries_passes_the_percentage(self):
        self.assertEqual(self._row(82)["ctx_pct"], 82)

    def test_without_a_note_the_field_exists_but_is_empty(self):
        self.assertIsNone(self._row(None)["ctx_pct"])

    def test_visible_shows_a_full_brain(self):
        v = cp.visible_columns(self._row(82))
        self.assertIn("🧠82%", v)
        self.assertIn("billing", v)         # and it does not eat what was there
        self.assertIn("working", v)

    def test_visible_stays_quiet_below_the_threshold(self):
        for pct in (None, 0, 49, cp.CTX_SHOW - 1):
            with self.subTest(pct=pct):
                self.assertNotIn("🧠", cp.visible_columns(self._row(pct)))

    def test_right_at_the_threshold_it_shows(self):
        self.assertIn("🧠%d%%" % cp.CTX_SHOW,
                      cp.visible_columns(self._row(cp.CTX_SHOW)))

    def test_a_tmux_session_with_nothing_inside_carries_no_brain(self):
        row = cp.build_entries(self.TS, [], [], [])[0]
        self.assertIsNone(row["ctx_pct"])
        self.assertNotIn("🧠", cp.visible_columns(row))

    def test_the_row_is_still_a_single_line(self):
        self.assertNotIn("\n", cp.format_fzf_line(0, self._row(95)))


class TestForkingAFrozenOne(unittest.TestCase):
    """Ctrl-F on a grey row opens a COPY of that conversation.

    The case: going back to a frozen point to look at it (or to pull from there
    down another path) WITHOUT writing a single turn into the original. Enter
    still means "I carry on with that same conversation"; Ctrl-F is
    `--fork-session`, which resumes into a NEW id and leaves the old transcript
    untouched.
    """

    E = {"kind": "recent", "session_id": "abc-1", "project": "foo", "cwd": "/x/foo"}

    def test_the_copy_types_fork_session_and_is_named_differently(self):
        # The different name is not decoration: in the green list it has to be
        # obvious at a glance that this is the copy and not the good conversation.
        self.assertEqual(cp.action_argvs(self.E, set(), fork=True), [
            ["new-session", "-d", "-s", "foo-fork", "-c", "/x/foo"],
            ["send-keys", "-t", "foo-fork", "-l", "--",
             "claude --resume abc-1 --fork-session"],
            ["send-keys", "-t", "foo-fork", "Enter"],
            ["switch-client", "-t", "foo-fork"]])

    def test_without_asking_for_a_copy_the_sequence_is_the_usual_one(self):
        # Enter does not change behaviour because Ctrl-F exists.
        self.assertEqual(cp.action_argvs(self.E, set()),
                         cp.action_argvs(self.E, set(), fork=False))
        self.assertIn("claude --resume abc-1",
                      cp.action_argvs(self.E, set())[1])

    def test_two_copies_of_the_same_work_are_numbered(self):
        argvs = cp.action_argvs(self.E, {"foo", "foo-fork"}, fork=True)
        self.assertEqual(argvs[0], ["new-session", "-d", "-s", "foo-fork#2",
                                    "-c", "/x/foo"])
        self.assertEqual(argvs[-1], ["switch-client", "-t", "foo-fork#2"])

    def test_a_project_with_a_dot_is_sanitised_as_when_resuming(self):
        # tmux turns "." into "_" silently: without sanitising, the
        # switch-client points at a name that does not exist (see the test above
        # without fork).
        e = dict(self.E, project="example.com", cwd="/x/example.com")
        self.assertEqual(cp.action_argvs(e, set(), fork=True)[0],
                         ["new-session", "-d", "-s", "example_com-fork",
                          "-c", "/x/example.com"])

    def test_a_dirty_id_is_not_typed_in_the_copy_either(self):
        # The send-keys guard holds for both ways, not just for Enter.
        e = dict(self.E, session_id="a;rm -rf")
        self.assertEqual(cp.action_argvs(e, set(), fork=True), [])

    def test_copying_only_makes_sense_in_the_history(self):
        for entry in ({"kind": "tmux", "target": "$3"},
                      {"kind": "pin", "target": "accounts"},
                      {"kind": "outside", "project": "retail"}):
            with self.subTest(kind=entry["kind"]):
                self.assertEqual(cp.action_argvs(entry, set(), fork=True), [])

    def test_fzf_listens_for_ctrl_f_and_announces_it(self):
        args = cp._fzf_args(False)
        self.assertIn("ctrl-f", args[args.index("--expect") + 1])
        self.assertIn("ctrl-n", args[args.index("--expect") + 1])  # still there
        self.assertIn("Ctrl-F", args[args.index("--header") + 1])

    def test_parse_fzf_output_brings_the_ctrl_f_key(self):
        self.assertEqual(cp.parse_fzf_output("foo\nctrl-f\n2\tccc\tvisible\n"),
                         ("foo", "ctrl-f", ["2\tccc\tvisible"]))

    def _press_on_the_grey_row(self, key):
        """One turn of the loop with `key` on the grey row. -> [(kind, fork)]"""
        es = entries()
        self.assertEqual(es[4]["kind"], "recent")
        line = _ANSI.sub("", cp.format_fzf_line(4, es[4]))
        fzf = subprocess.CompletedProcess([], 0, "\n%s\n%s\n" % (key, line), "")
        seen = []
        with mock.patch.object(cp, "gather_entries", side_effect=[es, es]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf), \
                mock.patch.object(cp, "_act",
                                  lambda e, fork=False: seen.append((e["kind"], fork))):
            self.assertTrue(cp._chooser_once(False))
        return seen

    def test_ctrl_f_resolves_like_enter_and_asks_for_the_copy(self):
        # Wiring: the row is re-resolved against the FRESH list (just like
        # Enter) and the only difference is that the copy is asked for.
        self.assertEqual(self._press_on_the_grey_row("ctrl-f"), [("recent", True)])

    def test_enter_on_that_same_row_still_does_not_copy(self):
        # Enter arrives with an empty key (--expect only marks its own).
        self.assertEqual(self._press_on_the_grey_row(""), [("recent", False)])

    def test_ctrl_f_on_a_green_row_warns_and_does_not_touch_tmux(self):
        # Without this warning, Ctrl-F on a live session went quiet and the menu
        # looked stuck.
        runs, err = [], io.StringIO()
        with mock.patch.object(cp, "_run_tmux_seq", runs.append), \
                mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            cp._act({"kind": "tmux", "target": "$3"}, fork=True)
        self.assertEqual(runs, [])
        self.assertIn("history", err.getvalue())

    def test_ctrl_f_on_a_grey_codex_or_agy_row_says_why(self):
        """The refusal has to name the TOOL, not blame the row.

        `action_argvs(fork=True)` comes back empty for a tool with no fork, and
        the picker could not tell that from a bad id: the message read "that row
        cannot be opened: it has no usable session id", which is false about a
        perfectly good codex row -- and two public documents ended up
        apologising for it instead of it being fixed.
        """
        for tool in ("codex", "agy"):
            with self.subTest(tool=tool):
                runs, err = [], io.StringIO()
                with mock.patch.object(cp, "_run_tmux_seq", runs.append), \
                        mock.patch.object(cp, "_existing_names", set), \
                        mock.patch.object(cp, "_pause_to_read", lambda: None), \
                        mock.patch.object(sys, "stderr", err):
                    cp._act({"kind": "recent", "session_id": "abc-1",
                             "project": "api", "tool": tool}, fork=True)
                self.assertEqual(runs, [])
                text = err.getvalue()
                self.assertIn("Claude Code conversations only", text)
                self.assertNotIn("no usable session id", text)

    def test_ctrl_f_on_a_grey_claude_row_still_copies(self):
        # The guard above must not have switched the copy off.
        runs = []
        with mock.patch.object(cp, "_run_tmux_seq",
                               lambda seq: runs.append(seq) or True), \
                mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "configured_pins", list):
            cp._act({"kind": "recent", "session_id": "abc-1", "project": "api",
                     "cwd": "/x/api"}, fork=True)
        self.assertIn("--fork-session", " ".join(runs[0][1]))


class TestCtrlLWiring(unittest.TestCase):
    """Ctrl-L end to end: the key, the two steps and their cancellations."""

    GREY = {"kind": "recent", "session_id": "abc-1", "project": "foo", "cwd": "/x/foo"}

    def _fzf(self, rc, stdout):
        """A fake fzf: `_fzf_simple` only looks at returncode and stdout."""
        return lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, "")

    # --- the key ----------------------------------------------------------
    def test_fzf_listens_for_ctrl_l_and_announces_it(self):
        args = cp._fzf_args(False)
        expect, header = args[args.index("--expect") + 1], args[args.index("--header") + 1]
        self.assertIn("ctrl-l", expect)
        for still_there in ("ctrl-n", "ctrl-f"):
            self.assertIn(still_there, expect)
        for key in ("Ctrl-L", "Ctrl-F", "Ctrl-N", "?", "F12", "Esc"):
            self.assertIn(key, header)
        # The header fits a phone's 80 columns: fzf TRIMS it on the right,
        # which is exactly where the keys for getting out live. 77 is what fzf
        # paints there -- the 78th character brings the `··` ellipsis and eats
        # the end of the line (measured under a pty on 0.72).
        self.assertLessEqual(len(header), 77)
        self.assertTrue(header.endswith("Esc: shell"), header)

    def test_ctrl_l_resolves_the_row_just_like_enter(self):
        es = entries()
        line = _ANSI.sub("", cp.format_fzf_line(4, es[4]))
        fzf = subprocess.CompletedProcess([], 0, "\nctrl-l\n%s\n" % line, "")
        seen = []
        with mock.patch.object(cp, "gather_entries", side_effect=[es, es]), \
                mock.patch.object(cp.subprocess, "run", lambda *a, **k: fzf), \
                mock.patch.object(cp, "_launch_with_flags", seen.append), \
                mock.patch.object(cp, "_act", lambda *a, **k: self.fail("this is not Enter")):
            self.assertTrue(cp._chooser_once(False))
        self.assertEqual([e["kind"] for e in seen], ["recent"])

    # --- step 1: the flag menu -------------------------------------------
    def test_the_marked_ones_come_back_as_flags_without_their_note(self):
        out = "--dangerously-skip-permissions\n--permission-mode plan   (start in plan mode)\n"
        with mock.patch.object(cp.subprocess, "run", self._fzf(0, out)):
            self.assertEqual(cp._pick_flags(),
                             ["--dangerously-skip-permissions", "--permission-mode plan"])

    def test_the_none_row_contributes_no_flag(self):
        """It exists because fzf -m with nothing marked gives back the row under
        the cursor: without it, entering and pressing Enter would sneak in the
        first flag of the list."""
        self.assertEqual(cp.line_flag(cp._NO_FLAGS_ROW), "")
        with mock.patch.object(cp.subprocess, "run", self._fzf(0, cp._NO_FLAGS_ROW + "\n")):
            self.assertEqual(cp._pick_flags(), [])

    def test_the_none_row_goes_FIRST_of_all(self):
        """The ORDER is what makes it useful: fzf -m with nothing marked gives
        back the row under the cursor, and the cursor starts at the top. At the
        end of the list it would protect nothing -- Ctrl-L + Enter would sneak
        in the first flag, which is `--dangerously-skip-permissions`.
        """
        seen = []

        def spy(*a, **k):
            seen.append(k.get("input", ""))
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(cp.subprocess, "run", spy):
            cp._pick_flags()
        self.assertEqual(seen[0].split("\n")[0], cp._NO_FLAGS_ROW)

    def test_esc_in_the_flags_differs_from_marking_nothing(self):
        # None (cancelled) vs [] (no flags): the first stops, the second carries on.
        with mock.patch.object(cp.subprocess, "run", self._fzf(130, "")):
            self.assertIsNone(cp._pick_flags())

    def test_a_BROKEN_fzf_is_not_accepted_and_says_so(self):
        """rc 2 = an fzf that does not even start. It is told apart from 130
        (Esc, cancelling in silence): without the warning, Ctrl-L looked like
        "this key does nothing"."""
        err = io.StringIO()
        with mock.patch.object(cp.subprocess, "run", self._fzf(2, "whatever")), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            self.assertIsNone(cp._pick_flags())
            self.assertIsNone(cp._edit_command("claude --resume abc-1"))
        self.assertIn("fzf exited with 2", err.getvalue())

    def test_the_Esc_does_not_dirty_the_screen_with_warnings(self):
        err = io.StringIO()
        with mock.patch.object(cp.subprocess, "run", self._fzf(130, "")), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            cp._pick_flags()
        self.assertEqual(err.getvalue(), "")

    # --- step 2: the editable command ------------------------------------
    def test_the_edited_command_is_the_first_line_fzf_prints(self):
        # With an empty list, Enter exits with 1 ("nothing matches") having
        # printed the query: measured on fzf 0.72.0, and that is why
        # `_fzf_simple` accepts the 1.
        with mock.patch.object(cp.subprocess, "run",
                               self._fzf(1, "claude --resume abc-1 --model opus\n")):
            self.assertEqual(cp._edit_command("claude --resume abc-1"),
                             "claude --resume abc-1 --model opus")

    def test_esc_while_editing_the_command_cancels(self):
        with mock.patch.object(cp.subprocess, "run", self._fzf(130, "")):
            self.assertIsNone(cp._edit_command("claude --resume abc-1"))

    # --- the whole thing --------------------------------------------------
    def _launch(self, entry, flags, edited):
        """The whole of Ctrl-L with both fzf simulated. -> tmux sequences run."""
        runs, err = [], io.StringIO()
        with mock.patch.object(cp, "_pick_flags", lambda tool=None: flags), \
                mock.patch.object(cp, "_edit_command", lambda c: edited), \
                mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "_run_tmux_seq", lambda s: runs.append(s) or True), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            cp._launch_with_flags(entry)
        return runs, err.getvalue()

    def test_what_was_edited_is_what_gets_typed(self):
        command = "claude --resume abc-1 --model opus --add-dir /other"
        runs, _ = self._launch(self.GREY, ["--model opus"], command)
        self.assertEqual(runs, [[
            ["new-session", "-d", "-s", "foo", "-c", "/x/foo"],
            ["send-keys", "-t", "foo", "-l", "--", command],
            ["send-keys", "-t", "foo", "Enter"],
            ["switch-client", "-t", "foo"]]])

    def test_esc_in_either_of_the_two_steps_opens_nothing(self):
        for step, flags, edited in (("flags", None, "claude --resume abc-1"),
                                    ("command", ["--model opus"], None)):
            with self.subTest(step=step):
                self.assertEqual(self._launch(self.GREY, flags, edited)[0], [])

    def test_an_empty_command_does_not_even_open_the_session(self):
        self.assertEqual(self._launch(self.GREY, [], "   ")[0], [])

    def test_ctrl_l_on_a_green_row_warns_and_does_not_touch_tmux(self):
        runs, err = self._launch({"kind": "tmux", "target": "$3"}, [], "claude")
        self.assertEqual(runs, [])
        self.assertIn("history", err)

    def test_a_dirty_id_does_not_even_reach_the_form(self):
        """The send-keys guard comes BEFORE asking: there is no point asking for
        flags for a row that cannot be typed afterwards."""
        asked = []
        with mock.patch.object(cp, "_pick_flags", lambda: asked.append(1)), \
                mock.patch.object(cp, "_run_tmux_seq", lambda s: self.fail("nothing is typed")), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            cp._launch_with_flags(dict(self.GREY, session_id="a; rm -rf ~"))
        self.assertEqual(asked, [])


class TestReservedSessions(unittest.TestCase):
    """`flightdeck`, the per-window menus (`flightdeck-N`) and every pin's
    session are infrastructure: they are not the user's work and they do not
    show up in the green group."""

    def test_the_menu_itself(self):
        self.assertTrue(cp.is_reserved("flightdeck"))

    def test_the_per_window_menus_too(self):
        for n in ("flightdeck-2", "flightdeck-3", "flightdeck-17"):
            with self.subTest(n=n):
                self.assertTrue(cp.is_reserved(n))

    def test_what_looks_like_it_but_is_ordinary_work(self):
        # "flightdeck-2" is reserved; "flightdeck2", "flightdeck-x" or
        # "flightdecking" are names one could choose for a work session: listed.
        for n in ("flightdeck2", "flightdeck-x", "flightdecking", "my-flightdeck-2",
                  "recommender", "", None):
            with self.subTest(n=n):
                self.assertFalse(cp.is_reserved(n))

    def test_build_entries_does_not_list_a_per_window_menu(self):
        ts = [{"name": "flightdeck-2", "id": "$9", "activity": 500.0, "attached": True},
              {"name": "work", "id": "$3", "activity": 100.0, "attached": False}]
        names = [e["name"] for e in cp.build_entries(ts, [], [], []) if e["kind"] == "tmux"]
        self.assertEqual(names, ["work"])


class TestThePortPerMenu(unittest.TestCase):
    """Each menu (flightdeck, flightdeck-2, flightdeck-3...) listens on its own
    port: two fzf cannot share one, and since the menu became per-window they
    coexist. The bash command, which sends the `reload`, reads the same base
    from the config."""

    def test_the_main_one_uses_the_base_port(self):
        self.assertEqual(cp.port_for("flightdeck"), cp.LISTEN_PORT)

    def test_the_per_window_menus_add_their_number(self):
        self.assertEqual(cp.port_for("flightdeck-2"), cp.LISTEN_PORT + 2)
        self.assertEqual(cp.port_for("flightdeck-17"), cp.LISTEN_PORT + 17)

    def test_anything_that_is_not_a_menu_falls_back_to_the_base(self):
        # Outside tmux, or in an odd session: better the base than blowing up.
        for odd in ("", None, "work", "flightdeck-x", "flightdecking"):
            with self.subTest(odd=odd):
                self.assertEqual(cp.port_for(odd), cp.LISTEN_PORT)

    def test_the_bash_command_reads_the_same_base_port(self):
        """Pin of the cross-file contract. The bash command duplicates no
        formula -- it asks `python3 -m flightdeck.config menu_port`, so what has
        to agree is that value and the base `port_for` uses.
        """
        with temp_config(menu_port=43000):
            out = io.StringIO()
            with redirect_stdout(out):
                code = config.main(["menu_port"])
            self.assertEqual(code, 0)
            self.assertEqual(int(out.getvalue().strip()), cp.port_for("flightdeck"))
            self.assertEqual(cp.port_for("flightdeck-2"), 43002)

    def test_fzf_args_carries_the_port_it_is_given(self):
        args = cp._fzf_args(True, 42709)
        self.assertIn("42709", args)
        self.assertNotIn(str(cp.LISTEN_PORT), args)


class TestCtrlNAsksForTheName(unittest.TestCase):
    """Ctrl-N with nothing typed asks for the name with fzf's line editor (the
    same one as Ctrl-L), not with `sys.stdin.readline()`: after an fzf the tty
    is left in line mode, where the arrows are not interpreted and a left arrow
    ended up INSIDE the name as `^[[D` (measured with a pty)."""

    def _fzf(self, rc, stdout):
        return lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, "")

    def test_the_name_is_the_first_line_stripped(self):
        with mock.patch.object(cp.subprocess, "run", self._fzf(1, "  landing  \n")):
            self.assertEqual(cp._ask_name(), "landing")

    def test_esc_while_asking_for_the_name_cancels(self):
        with mock.patch.object(cp.subprocess, "run", self._fzf(130, "")):
            self.assertIsNone(cp._ask_name())

    def test_enter_without_typing_anything_is_not_a_name(self):
        with mock.patch.object(cp.subprocess, "run", self._fzf(1, "\n")):
            self.assertEqual(cp._ask_name(), "")

    def test_it_is_asked_with_an_empty_editable_fzf_and_never_with_readline(self):
        seen = []

        def fzf(argv, **k):
            seen.append(argv)
            return subprocess.CompletedProcess([], 1, "landing\n", "")

        stdin = mock.Mock()
        stdin.readline.side_effect = AssertionError("bare readline: arrows type ^[[D")
        with mock.patch.object(cp.subprocess, "run", fzf), \
                mock.patch.object(cp.sys, "stdin", stdin):
            self.assertEqual(cp._ask_name(), "landing")
        argv = seen[0]
        self.assertEqual(argv[0], "fzf")
        self.assertIn("--print-query", argv)
        self.assertEqual(argv[argv.index("--query") + 1], "")
        self.assertIn("name", argv[argv.index("--prompt") + 1])

    def test_ctrl_n_without_text_creates_the_session_with_the_name_asked_for(self):
        answers = [subprocess.CompletedProcess([], 0, "\nctrl-n\n", ""),     # the menu
                   subprocess.CompletedProcess([], 1, "landing\n", "")]      # the name
        created = []
        with mock.patch.object(cp.subprocess, "run", lambda *a, **k: answers.pop(0)), \
                mock.patch.object(cp, "gather_entries", lambda: []), \
                mock.patch.object(cp, "_taken_names", lambda: set()), \
                mock.patch.object(cp, "_run_tmux_seq", lambda seq: created.append(seq) or True):
            self.assertTrue(cp._chooser_once(False))
        self.assertEqual(len(created), 1)
        self.assertIn("landing", " ".join(" ".join(a) for a in created[0]))

    def test_ctrl_n_and_esc_on_the_name_creates_nothing(self):
        answers = [subprocess.CompletedProcess([], 0, "\nctrl-n\n", ""),
                   subprocess.CompletedProcess([], 130, "", "")]
        with mock.patch.object(cp.subprocess, "run", lambda *a, **k: answers.pop(0)), \
                mock.patch.object(cp, "gather_entries", lambda: []), \
                mock.patch.object(cp, "_run_tmux_seq", lambda seq: self.fail("must not create")):
            self.assertTrue(cp._chooser_once(False))


# --- the fixed rows -------------------------------------------------------

class TestPinnedRows(unittest.TestCase):
    """There is no hard-wired account-switcher row. Its place is taken by one
    row per pin from `config.json`, so the fixed entry is a published feature
    and not one user's own tool."""

    def test_one_row_per_pin_with_its_label(self):
        pins = [PINS[0],
                {"name": "top", "label": "📈 top", "session": "top", "command": "htop"}]
        es = cp.build_entries([], [], [], [], pins=pins)
        self.assertEqual([e["kind"] for e in es], ["pin", "pin"])
        self.assertEqual([cp.visible_columns(e) for e in es], ["⚙ accounts", "📈 top"])

    def test_without_pins_there_are_no_fixed_rows(self):
        """Nothing is pinned by default: a fresh install shows only real
        sessions. And with nothing pinned, a tmux session called `accounts` is
        ordinary work again -- it was only reserved because a pin claimed it."""
        es = cp.build_entries(TS, LIVE, OUT, REC, now=1000.0)
        self.assertNotIn("pin", [e["kind"] for e in es])
        self.assertEqual([e["name"] for e in es if e["kind"] == "tmux"],
                         ["recomm", "idle", "accounts"])

    def test_there_is_no_hard_wired_accounts_row_left(self):
        for e in cp.build_entries(TS, LIVE, OUT, REC, now=1000.0, pins=[]):
            self.assertNotIn("cuentas", cp.visible_columns(e))

    def test_enter_creates_the_pin_session_with_its_command_then_jumps(self):
        e = cp.build_entries([], [], [], [], pins=PINS)[0]
        self.assertEqual(cp.action_argvs(e, set()), [
            ["new-session", "-d", "-s", "accounts", "cswap tui"],
            ["switch-client", "-t", "accounts"]])

    def test_a_pin_without_a_command_opens_a_plain_shell_session(self):
        """tmux with no command starts the default shell: a pin whose command
        the user removed still opens something usable instead of nothing."""
        pins = [{"name": "notes", "label": "notes", "session": "notes"}]
        e = cp.build_entries([], [], [], [], pins=pins)[0]
        self.assertEqual(cp.action_argvs(e, set()), [
            ["new-session", "-d", "-s", "notes"],
            ["switch-client", "-t", "notes"]])

    def test_a_pin_session_is_reserved_and_never_shows_up_in_green(self):
        self.assertTrue(cp.is_reserved("accounts", PINS))
        self.assertFalse(cp.is_reserved("accounts"))   # nothing is reserved by itself
        names = [e["name"] for e in entries() if e["kind"] == "tmux"]
        self.assertNotIn("accounts", names)

    def test_unusable_pins_are_skipped_instead_of_breaking_the_menu(self):
        """`config.json` is edited by hand: a pin with no session has nothing to
        switch to, and the menu must not die (nor offer a row that does
        nothing) because of a typo."""
        pins = ["not-a-dict", {"label": "no session"}, {"session": "   "}, PINS[0]]
        es = cp.build_entries([], [], [], [], pins=pins)
        self.assertEqual([e["target"] for e in es], ["accounts"])

    def test_a_pin_session_is_spelled_the_way_tmux_would_spell_it(self):
        """`.` and `:` become `_` here as well, not only in `flightdeck pin add`.

        tmux replaces them silently, so a hand-written `"session": "my.notes"`
        had the menu asking for `my.notes` while tmux had made `my_notes`:
        `new-session` came back saying the session was there and the
        `switch-client` behind it went to the wrong place. `pin add` already
        sanitised on the way in; the two sides read the file with one helper now
        (`common.pin_session`), so they cannot answer differently.
        """
        pins = [{"name": "notes", "session": "my.notes:1", "command": "vim"}]
        e = cp.build_entries([], [], [], [], pins=pins)[0]
        self.assertEqual(e["target"], "my_notes_1")
        self.assertTrue(cp.is_reserved("my_notes_1", pins))
        self.assertEqual(cp.action_argvs(e, set()), [
            ["new-session", "-d", "-s", "my_notes_1", "vim"],
            ["switch-client", "-t", "my_notes_1"]])

    def test_two_pins_whose_sessions_differ_only_in_spacing_are_different_rows(self):
        """A tmux session name may carry spaces, and the row's hidden key used
        to squeeze every space out: "my notes" and "mynotes" came out as the
        same key, so Enter on one opened whichever of the two fzf listed first.
        Only tabs and newlines are taken out now (they are the line's real
        separators)."""
        pins = [{"name": "a", "session": "my notes", "command": "vim"},
                {"name": "b", "session": "mynotes", "command": "vim"}]
        es = cp.build_entries([], [], [], [], pins=pins)
        self.assertEqual([cp.entry_key(e) for e in es], ["my notes", "mynotes"])
        # And each one still resolves to its own row after a reload.
        lines = [_ANSI.sub("", cp.format_fzf_line(i, e)) for i, e in enumerate(es)]
        self.assertIs(cp.resolve_selection(lines[1], list(reversed(es))), es[1])

    def test_a_pin_without_a_label_falls_back_to_its_name_and_session(self):
        pins = [{"name": "lazygit", "session": "git", "command": "lazygit"},
                {"session": "k9s", "command": "k9s"}]
        es = cp.build_entries([], [], [], [], pins=pins)
        self.assertEqual([cp.visible_columns(e) for e in es], ["lazygit", "k9s"])

    def test_taken_names_holds_flightdeck_and_the_pins_even_if_they_do_not_exist(self):
        """A session created by hand and called `accounts` would eclipse the
        pin's (and `flightdeck` the menu itself), and tmux would resolve the
        repeated name to the wrong one. They are deduped to `accounts#2`."""
        with temp_config(pins=PINS):
            with mock.patch.object(cp, "_existing_names", lambda: {"work"}):
                taken = cp._taken_names()
        self.assertEqual(taken, {"work", "flightdeck", "accounts"})
        self.assertEqual(cp.create_argvs("accounts", taken, home="/h")[0][3], "accounts#2")


class TestValuesThatComeFromTheConfig(unittest.TestCase):
    """These values are configuration rather than hard-wired constants, read
    when they are used so an edited `config.json` takes effect without
    restarting the tmux server."""

    def test_ctrl_n_opens_the_shell_in_the_configured_projects_dir(self):
        with temp_config(projects_dir="/tmp/code"):
            seq = cp.create_argvs("new", set())
        self.assertEqual(seq[0], ["new-session", "-d", "-s", "new", "-c", "/tmp/code"])

    def test_the_projects_dir_env_override_wins(self):
        with temp_config(projects_dir="/tmp/code"):
            with mock.patch.dict(os.environ, {"FLIGHTDECK_PROJECTS_DIR": "/tmp/other"}):
                seq = cp.create_argvs("new", set())
        self.assertEqual(seq[0][-1], "/tmp/other")

    def test_the_menu_port_comes_from_the_config(self):
        with temp_config(menu_port=43000):
            self.assertEqual(cp.port_for("flightdeck"), 43000)
            self.assertEqual(cp.port_for("flightdeck-3"), 43003)

    def test_an_unusable_menu_port_falls_back_to_the_default(self):
        """The menu must open even with a `menu_port` typed as a word: `doctor`
        is the place that complains about the value's type."""
        with temp_config(menu_port="forty-two"):
            self.assertEqual(cp.port_for("flightdeck"), cp.LISTEN_PORT)

    def test_the_history_limit_comes_from_the_config(self):
        asked = {}

        def fake_recent(**kw):
            asked.update(kw)
            return []

        with temp_config(history_limit=7):
            with mock.patch.object(cp, "load_sessions", lambda: []), \
                    mock.patch.object(cp, "load_outside_sessions", lambda: []), \
                    mock.patch.object(cp, "list_tmux_sessions", lambda: []), \
                    mock.patch.object(cp, "list_recent_sessions", fake_recent):
                cp.gather_entries()
        self.assertEqual(asked["limit"], 7)

    def test_an_unusable_history_limit_falls_back_to_the_default(self):
        with temp_config(history_limit="lots"):
            self.assertEqual(cp._history_limit(), config.DEFAULTS["history_limit"])

    def test_gather_entries_brings_the_pins_from_the_config(self):
        with temp_config(pins=PINS):
            with mock.patch.object(cp, "load_sessions", lambda: []), \
                    mock.patch.object(cp, "load_outside_sessions", lambda: []), \
                    mock.patch.object(cp, "list_tmux_sessions", lambda: []), \
                    mock.patch.object(cp, "list_recent_sessions", lambda **kw: []):
                es = cp.gather_entries()
        self.assertEqual([e["kind"] for e in es], ["pin"])
        self.assertEqual(cp.visible_columns(es[0]), "⚙ accounts")

    def test_the_brain_threshold_comes_from_the_config(self):
        """`context_show_pct`: from which % the 🧠 appears on a green row."""
        row = {"kind": "tmux", "name": "payments", "target": "$1", "session_id": "a",
               "project": "p", "state": "working", "age": "5s", "ctx_pct": 30}
        with temp_config(context_show_pct=20):
            self.assertIn("🧠30%", cp.visible_columns(row))
        with temp_config(context_show_pct="half"):   # unusable: the default (50)
            self.assertNotIn("🧠", cp.visible_columns(row))


class TestTheEnglishInterface(unittest.TestCase):
    """Every visible string is English. These pin the ones a user reads most
    often, so a half-translated menu fails here."""

    def test_the_badges(self):
        self.assertEqual(cp.state_badge("awaiting_input"), "⏳ WAITING FOR YOU")
        self.assertEqual(cp.state_badge("needs_attention"), "⚠ needs attention")
        self.assertEqual(cp.state_badge("asking"), "❓ asking you")
        self.assertEqual(cp.state_badge("working"), "● working")
        self.assertEqual(cp.state_badge("looping"), "🔁 looping")
        self.assertEqual(cp.state_badge("working", "SessionStart"), "○ open")
        self.assertEqual(cp.state_badge(None), "shell")

    def test_the_prompts_and_headers_of_every_step(self):
        args = cp._fzf_args(False)
        self.assertEqual(args[args.index("--prompt") + 1], "session> ")
        self.assertEqual(args[args.index("--header") + 1],
                         "Ctrl-L: flags · Ctrl-F: copy · Ctrl-N: new · ?: details"
                         " · F12 · Esc: shell")
        seen = []

        def spy(argv, **k):
            seen.append(argv)
            return subprocess.CompletedProcess([], 130, "", "")

        with mock.patch.object(cp.subprocess, "run", spy):
            cp._pick_flags()
            cp._edit_command("claude --resume abc-1")
            cp._ask_name()
        prompts = [a[a.index("--prompt") + 1] for a in seen]
        headers = [a[a.index("--header") + 1] for a in seen]
        self.assertEqual(prompts, ["flags> ", "command> ", "new session name> "])
        self.assertEqual(headers, ["Tab: pick several · Enter: continue · Esc: cancel",
                                   "Edit the command and press Enter · Esc: cancel",
                                   "Type the name and press Enter · Esc: cancel"])

    def test_no_catalogue_ships_a_pinned_model_version(self):
        """The rows are generic flags, and a model VERSION is nobody's default.

        claude's `--model fable|opus|sonnet` stay: those are the tool's own
        aliases and they do not go stale. codex has no such alias, so its only
        `--model` row was a pinned version -- one particular model id, which
        would have shipped to everybody and been wrong on the first release
        after this one. Ctrl-L's second step is an editable
        line, which is where a particular model belongs.
        """
        generic = {"--model fable", "--model opus", "--model sonnet"}
        every = (cp.COMMON_FLAGS + cp.COMMON_FLAGS_CODEX + cp.COMMON_FLAGS_AGY)
        models = [flag for flag, _ in every if flag.startswith("--model")]
        self.assertEqual(sorted(set(models) - generic), [])

    def test_the_row_of_no_flags_and_the_catalogue_notes(self):
        self.assertEqual(cp._NO_FLAGS_ROW, "(none — just edit the command)")
        notes = [n for _, n in cp.COMMON_FLAGS + cp.COMMON_FLAGS_CODEX
                 + cp.COMMON_FLAGS_AGY if n]
        self.assertEqual(sorted(set(notes)), sorted(set([
            "no permission prompts", "start in plan mode", "no approvals, no sandbox",
            "never asks; failures go back to the model",
            "sandbox with write access to the project", "web search"])))

    def test_the_warning_when_fzf_is_missing(self):
        def missing(*a, **k):
            raise FileNotFoundError("fzf")

        err = io.StringIO()
        with mock.patch.object(cp, "gather_entries", return_value=[]), \
                mock.patch.object(cp.subprocess, "run", missing), \
                mock.patch.object(sys, "stderr", err):
            self.assertFalse(cp._chooser_once(False))
        self.assertEqual(err.getvalue(),
                         "'fzf' is missing (install it: brew install fzf, "
                         "or sudo apt install fzf).\n")

    def test_the_warnings_of_the_rows_that_cannot_be_opened(self):
        err = io.StringIO()
        with mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "_pause_to_read", lambda: None), \
                mock.patch.object(sys, "stderr", err):
            cp._act({"kind": "outside", "project": "retail"})
            cp._act({"kind": "recent", "session_id": "a;rm -rf", "project": "p"})
        text = err.getvalue()
        self.assertIn("that session lives outside tmux — project retail;"
                      " it cannot be adopted", text)
        self.assertIn("that row cannot be opened: it has no usable session id", text)

    def test_the_usage_line(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = cp.main(["nonsense"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(),
                         "usage: python3 -m flightdeck.picker [loop|feed]\n")


class TestRevivingNamesTheNewSessionSafely(unittest.TestCase):
    def test_reviving_never_steals_a_pins_session_name(self):
        """The name of a REVIVED session is deduped against the taken set.

        `_existing_names()` is what is alive, and the pin branch needs exactly
        that (it is how it decides whether to start the pin's command or just
        jump in). Reviving is the other question -- what a NEW session may be
        called -- and it was asking the live set too. So a grey row whose
        project folder is called like a pin that is not running yet (`htop`,
        `k9s`, a notes file) created a tmux session with that exact name;
        `is_reserved` then hid it from the green group and Enter on the pin's
        row jumped INTO that session instead of starting the pin's command. The
        revived session was only reachable through F12.
        """
        pins = [{"name": "htop", "label": "☸ htop", "session": "htop",
                 "command": "htop"}]
        seqs = []
        grey = {"kind": "recent", "session_id": "abc-1", "project": "htop",
                "cwd": "/x/htop"}
        with mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "configured_pins", lambda: pins), \
                mock.patch.object(cp, "_run_tmux_seq",
                                  lambda seq: seqs.append(seq) or True):
            cp._act(grey)
        self.assertEqual(seqs[0][0],
                         ["new-session", "-d", "-s", "htop#2", "-c", "/x/htop"])
        # And the pin's own session is untouched, so its row still starts it.
        self.assertEqual(cp.action_argvs(
            cp.build_entries([], [], [], [], pins=pins)[0], set()),
            [["new-session", "-d", "-s", "htop", "htop"],
             ["switch-client", "-t", "htop"]])

    def test_ctrl_l_names_the_new_session_the_same_way(self):
        # Ctrl-L shares the mould (`_session_with_command`), so it shares the
        # bug and the fix.
        pins = [{"name": "htop", "label": "☸ htop", "session": "htop",
                 "command": "htop"}]
        seqs = []
        grey = {"kind": "recent", "session_id": "abc-1", "project": "htop",
                "cwd": "/x/htop", "tool": "claude"}
        with mock.patch.object(cp, "_existing_names", set), \
                mock.patch.object(cp, "configured_pins", lambda: pins), \
                mock.patch.object(cp, "_pick_flags", lambda tool: []), \
                mock.patch.object(cp, "_edit_command",
                                  lambda line: "claude --resume abc-1"), \
                mock.patch.object(cp, "_run_tmux_seq",
                                  lambda seq: seqs.append(seq) or True):
            cp._launch_with_flags(grey)
        self.assertEqual(seqs[0][0],
                         ["new-session", "-d", "-s", "htop#2", "-c", "/x/htop"])


class TestTheMenuReportsABrokenConfig(unittest.TestCase):
    """An invalid `config.json` is reported in `doctor` AND on entering the
    menu.

    Every reader degrades to the defaults, which is right for a menu that has to
    open -- but done silently it costs the user their pins and their `menu_port`
    with nothing on screen to explain it. A stray comma is enough.
    """

    def _enter_the_menu(self, config_text):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(config_text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}), \
                    mock.patch.object(cp, "_fzf_version_output", lambda: "0.72.0"), \
                    mock.patch.object(cp, "_my_tmux_session", lambda: "flightdeck"), \
                    mock.patch.object(cp, "_pause_to_read", lambda: None), \
                    mock.patch.object(cp, "_chooser_once",
                                      lambda *a, **k: False), \
                    mock.patch.object(cp, "_shell_exec", lambda: None), \
                    mock.patch.object(sys, "stderr", err):
                cp.chooser_loop()
        return err.getvalue()

    def test_a_config_that_cannot_be_read_is_announced_once(self):
        text = self._enter_the_menu('{"pins": [],}')
        self.assertEqual(text.count("config.json ignored"), 1)
        self.assertIn("invalid JSON in", text)

    def test_a_config_that_is_fine_says_nothing(self):
        self.assertEqual(self._enter_the_menu('{"menu_port": 43000}'), "")


class TestTheRowOfAClaudeThatPredatesTheHooks(unittest.TestCase):
    """A claude that was already open when Flightdeck was installed has no card
    until it has a turn, so `common.load_sessions` synthesises one from Claude
    Code's registry. From here on it is a card like any other: these pin what the
    user ends up seeing instead of the bare "shell" row of the first day.
    """

    TS = [{"name": "ssr cache", "id": "$3", "activity": 200.0, "attached": False}]
    # A card as `common._registry_cards` builds it, with the `_title`
    # `gather_entries` puts on every live card.
    CARD = {"session_id": "ef1864c2-x", "pid": 4242, "tmux_pane": "%9",
            "tmux_session": "ssr cache", "tool": "claude", "project": "ssr_cache",
            "cwd": "/w/ssr_cache", "title": "ssr cache 2", "_title": "ssr cache 2",
            "state": "working", "last_event": "SessionStart", "source": "startup",
            "_age": 30, "_from_registry": True}

    def test_the_row_is_open_and_carries_the_name_claude_code_registered(self):
        e = cp.build_entries(self.TS, [dict(self.CARD)], [], [], now=NOW)[0]
        self.assertEqual((e["kind"], e["name"]), ("tmux", "ssr cache"))
        self.assertEqual(e["title"], "ssr cache 2")
        line = _ANSI.sub("", cp.visible_columns(e))
        self.assertIn("ssr cache 2 · ○ open", line)
        self.assertIn("✳", line)   # claude's glyph: this is not a bare shell
        self.assertIn("30s", line)

    def test_an_idle_one_waits_for_you_like_any_other(self):
        card = dict(self.CARD, state="awaiting_input")
        card.pop("last_event")
        e = cp.build_entries(self.TS, [card], [], [], now=NOW)[0]
        self.assertIn("⏳ WAITING FOR YOU", _ANSI.sub("", cp.visible_columns(e)))
        # And the status bar counts it, because it counts `state`.
        self.assertIn("⏳ 1 waiting: ssr cache", sb.render_status_line([e]))

    def test_the_registered_name_is_the_fallback_when_the_transcript_has_none(self):
        # `gather_entries` asks the TRANSCRIPT for the human title; a claude that
        # predates the hooks may have none there (it was never /renamed), and
        # then the name Claude Code registered is what keeps the row
        # recognisable instead of falling back to the folder.
        card = dict(self.CARD)
        card.pop("_title")
        with temp_config():
            with mock.patch.object(cp, "load_sessions", lambda: [card]), \
                    mock.patch.object(cp, "load_outside_sessions", lambda: []), \
                    mock.patch.object(cp, "list_tmux_sessions", lambda: self.TS), \
                    mock.patch.object(cp, "list_recent_sessions", lambda **kw: []), \
                    mock.patch.object(cp, "title_for_session", lambda sid: None):
                es = cp.gather_entries()
        self.assertEqual(es[0]["title"], "ssr cache 2")

    def test_a_human_title_in_the_transcript_still_wins(self):
        card = dict(self.CARD)
        card.pop("_title")
        with temp_config():
            with mock.patch.object(cp, "load_sessions", lambda: [card]), \
                    mock.patch.object(cp, "load_outside_sessions", lambda: []), \
                    mock.patch.object(cp, "list_tmux_sessions", lambda: self.TS), \
                    mock.patch.object(cp, "list_recent_sessions", lambda **kw: []), \
                    mock.patch.object(cp, "title_for_session", lambda sid: "renamed"):
                es = cp.gather_entries()
        self.assertEqual(es[0]["title"], "renamed")


if __name__ == "__main__":
    unittest.main()
