import contextlib
import datetime
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import common, config
from flightdeck.common import (parse_tmux_sessions, dedup_name, human_age,
                               pid_alive, tmux_safe_name)


@contextlib.contextmanager
def temp_sessions_dir():
    """A throwaway state directory; yields its `sessions/` path.

    There is no module-level SESSIONS_DIR to patch here: `common` asks
    `config.sessions_dir()` at call time, so a test moves the whole state
    directory with `FLIGHTDECK_STATE_DIR` instead.

    The directory is created here because the tests WRITE cards into it: asking
    for the path does not create it any more (that is `config.ensure_dir`, which
    only the writers call), and `common` is a reader from end to end.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}):
            yield config.ensure_dir(config.sessions_dir())


class TestCommon(unittest.TestCase):

    def test_parse_tmux_sessions(self):
        out = "recomm\t$3\t1755300000\t1\nflightdeck\t$0\t1755200000\t0\n"
        ses = parse_tmux_sessions(out)
        self.assertEqual(ses[0], {"name": "recomm", "id": "$3",
                                  "activity": 1755300000.0, "attached": True})
        self.assertEqual(ses[1]["name"], "flightdeck")
        self.assertIs(ses[1]["attached"], False)

    def test_parse_tmux_sessions_broken_lines(self):
        self.assertEqual(parse_tmux_sessions("just-one-field\n\n"), [])

    def test_dedup_name_without_clash(self):
        self.assertEqual(dedup_name("foo", {"bar", "flightdeck"}), "foo")

    def test_dedup_name_with_clashes(self):
        self.assertEqual(dedup_name("foo", {"foo"}), "foo#2")
        self.assertEqual(dedup_name("foo", {"foo", "foo#2"}), "foo#3")

    def test_human_age_unchanged(self):
        self.assertEqual(human_age(59), "59s")
        self.assertEqual(human_age(3661), "1h 01m")


# tmux does not accept "." or ":" in a session name: it swaps them for "_"
# without saying so (session_check_name). Create/revive sanitise BEFORE, so the
# dedup and the switch-client point at the name tmux is really going to leave.
class TestTmuxSafeName(unittest.TestCase):

    def test_sanitises_dot_and_colon(self):
        self.assertEqual(tmux_safe_name("example.com"), "example_com")
        self.assertEqual(tmux_safe_name("a:b"), "a_b")

    def test_clean_or_empty_name(self):
        self.assertEqual(tmux_safe_name("recommender"), "recommender")
        self.assertEqual(tmux_safe_name(None), "")


def _dead_pid():
    """A pid that surely does NOT exist: spawn a child, let it die, reap it.

    The `wait()` is essential: without it the child stays a zombie and its pid
    STILL exists for `os.kill(pid, 0)` (which would answer "alive").
    """
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class TestPidAlive(unittest.TestCase):
    """When in doubt, a pid counts as ALIVE: one row too many is much better
    than hiding a session from the user that is actually working."""

    def test_own_process_is_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_reaped_process_is_dead(self):
        self.assertFalse(pid_alive(_dead_pid()))

    def test_no_pid_or_junk_gets_benefit_of_the_doubt(self):
        for odd in (None, "", "abc", [], 0, -1):
            with self.subTest(pid=odd):
                self.assertTrue(pid_alive(odd))


class TestContextNote(unittest.TestCase):
    """The status line tee leaves the context % in `<id>.ctx.json`, in the SAME
    directory as the hook session cards (`<id>.json`).

    Two things to prove: that those notes are not mistaken for cards (the
    `*.json` glob caught them, and with prune `load_sessions` DELETED them), and
    that reading the % is defensive: the tee does not write atomically, so
    catching the file half-written is NORMAL and has to come back None, never
    an exception.
    """

    PANES = {"%1": {"session": "recomm", "window": "0", "cmd": "node",
                    "window_name": "claude"}}

    def _card(self, folder, sid, now=None):
        upd = (now or datetime.datetime.now().astimezone()).isoformat(timespec="seconds")
        (folder / ("%s.json" % sid)).write_text(json.dumps(
            {"session_id": sid, "state": "working", "project": "demo",
             "tmux_session": "recomm", "tmux_pane": "%1", "updated_at": upd}))

    def _ctx(self, folder, sid, text):
        (folder / ("%s.ctx.json" % sid)).write_text(text)

    def _load(self, **kw):
        with mock.patch.object(common, "live_panes", return_value=self.PANES):
            return common.load_sessions(**kw)

    def test_note_is_not_listed_as_a_card_nor_deleted_by_prune(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "alive")
            self._ctx(folder, "alive", json.dumps({"pct": 70, "at": time.time()}))
            out = self._load(prune=True)
            self.assertEqual([d["session_id"] for d in out], ["alive"])
            self.assertTrue((folder / "alive.ctx.json").exists())

    def test_note_is_not_listed_as_an_outside_session_either(self):
        """The note is skipped by the file NAME, not by accident.

        This test's payload carries `state` + a fresh `updated_at` on purpose,
        so it would pass every filter in `load_outside_sessions` and be listed
        as a session living outside tmux. With the tee's real payload (just
        pct+at) the test would pass WITHOUT the skip -- it falls out earlier,
        for having no `updated_at` -- and would be proving nothing.
        """
        with temp_sessions_dir() as folder:
            now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            self._ctx(folder, "orphan", json.dumps(
                {"pct": 70, "at": time.time(), "session_id": "orphan",
                 "state": "working", "project": "demo", "updated_at": now}))
            with mock.patch.object(common, "live_panes", return_value={}):
                self.assertEqual(common.load_outside_sessions(), [])

    def test_fresh_note_reaches_the_card(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "alive")
            self._ctx(folder, "alive", json.dumps({"pct": 82, "at": time.time()}))
            self.assertEqual(self._load()[0]["_ctx_pct"], 82)

    def test_stale_note_does_not_count(self):
        # Claude closed a while ago: its last % says nothing about right now.
        with temp_sessions_dir() as folder:
            self._card(folder, "alive")
            self._ctx(folder, "alive", json.dumps(
                {"pct": 82, "at": time.time() - (common.CTX_FRESH_S + 300)}))
            self.assertIsNone(self._load()[0]["_ctx_pct"])

    def test_a_ten_minute_old_note_still_counts(self):
        """The window is wide (15 min) on purpose: the status line repaints on
        INTERACTION, not on a clock, so a claude in the middle of a long turn
        goes minutes without rewriting its note. With a short window the 🧠
        never showed up -- exactly when the context is filling up."""
        with temp_sessions_dir() as folder:
            self._card(folder, "alive")
            self._ctx(folder, "alive",
                      json.dumps({"pct": 82, "at": time.time() - 600}))
            self.assertEqual(self._load()[0]["_ctx_pct"], 82)

    def test_without_a_note_or_with_a_half_written_file(self):
        # The tee writes without atomicity: reading it mid-write happens, and
        # it is not an error -- it is simply "the % is not known".
        for name, text in (("missing", None), ("broken", '{"pct": 8'),
                           ("list", "[1,2]"), ("no_at", '{"pct": 80}')):
            with self.subTest(case=name), temp_sessions_dir() as folder:
                self._card(folder, name)
                if text is not None:
                    self._ctx(folder, name, text)
                self.assertIsNone(self._load()[0]["_ctx_pct"])

    def test_a_percentage_that_is_not_a_percentage_does_not_count(self):
        # Defence in depth: the tee does not validate the range, so a broken
        # payload would reach here. Better to say nothing than to paint
        # "🧠5000%".
        for pct in (5000, -3, 101):
            with self.subTest(pct=pct), temp_sessions_dir() as folder:
                self._card(folder, "alive")
                self._ctx(folder, "alive",
                          json.dumps({"pct": pct, "at": time.time()}))
                self.assertIsNone(self._load()[0]["_ctx_pct"])

    def test_the_valid_extremes_do_count(self):
        # 0 and 100 are legitimate percentages: the range filter must not eat
        # them.
        for pct in (0, 100):
            with self.subTest(pct=pct), temp_sessions_dir() as folder:
                self._card(folder, "alive")
                self._ctx(folder, "alive",
                          json.dumps({"pct": pct, "at": time.time()}))
                self.assertEqual(self._load()[0]["_ctx_pct"], pct)

    def test_read_ctx_pct_with_an_id_that_escapes_the_directory(self):
        """The id ends up being a file name: one with a "/" must not send us
        to read anything outside sessions/.

        At the destination of the jump (`sessions/../outside.ctx.json`) a VALID
        and fresh note is planted: if the id filter disappeared, the read would
        succeed and return 42. Without that bait the file would not exist and
        the test would pass just as well without the filter -- proving nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp)
            sess = outside / "sessions"
            sess.mkdir()
            (outside / "outside.ctx.json").write_text(
                json.dumps({"pct": 42, "at": time.time()}))
            self.assertIsNone(common.read_ctx_pct("../outside", sess_dir=sess))
            self.assertIsNone(common.read_ctx_pct("../../etc/passwd", sess_dir=sess))
            self.assertIsNone(common.read_ctx_pct("", sess_dir=sess))


class TestLoadOutsideSessionsPid(unittest.TestCase):
    """Hook cards of sessions living outside tmux: a dead pid = a ghost."""

    def _card(self, folder, sid, **extra):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        d = {"session_id": sid, "state": "working", "project": "demo",
             "updated_at": now}
        d.update(extra)
        (folder / ("%s.json" % sid)).write_text(json.dumps(d))

    def _load(self):
        # live_panes to {} = no live pane, so no card is discarded for "that
        # one belongs to tmux" and the only filter in play is the pid one.
        with mock.patch.object(common, "live_panes", return_value={}):
            return common.load_outside_sessions()

    def test_dead_pid_is_not_listed(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "ghost", pid=_dead_pid())
            self.assertEqual(self._load(), [])

    def test_live_pid_is_listed(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "alive", pid=os.getpid())
            out = self._load()
            self.assertEqual([d["session_id"] for d in out], ["alive"])

    def test_old_card_without_a_pid_is_listed_anyway(self):
        # Compatibility: cards written before this change carry no pid; there
        # the freshness of updated_at decides, as it did until now.
        with temp_sessions_dir() as folder:
            self._card(folder, "old")
            out = self._load()
            self.assertEqual([d["session_id"] for d in out], ["old"])

    def test_the_reader_does_not_touch_the_ghost_card(self):
        # A reader does not mutate state: the ghost stops being listed, but its
        # file stays exactly as it was (cleaning up is somebody else's job).
        with temp_sessions_dir() as folder:
            self._card(folder, "ghost", pid=_dead_pid())
            before = (folder / "ghost.json").read_text()
            self._load()
            self.assertEqual((folder / "ghost.json").read_text(), before)


class TestReadingNeverCreatesTheStateDirectory(unittest.TestCase):
    """Listing sessions is a read, and a read leaves no trace on disk.

    `load_sessions` runs on every repaint of the tmux bar and every time the
    menu reloads. On a machine where Flightdeck was never installed there is
    nothing to list, and there must still be nothing afterwards: a state
    directory conjured up by a bar is how a tool that was uninstalled looks
    installed again.
    """

    def test_load_sessions_on_a_fresh_state_dir_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "never-created"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": str(state)}), \
                    mock.patch.object(common, "live_panes", return_value={}):
                self.assertEqual(common.load_sessions(), [])
                self.assertEqual(common.load_outside_sessions([]), [])
            self.assertFalse(state.exists())


class TestRawCards(unittest.TestCase):
    """`_raw_cards` is the one reader of the cards directory (the handover asks
    `load_sessions` now, so this is where the rule lives).

    The tee's notes (`<id>.ctx.json`) share that directory and the glob catches
    them: they are NOT cards, and without the skip they would come in as cards
    with no pane -- down that branch `prune` DELETES, and Flightdeck would lose
    the context % of every live session.
    """

    def test_it_reads_the_cards_and_skips_the_notes_and_the_junk(self):
        with temp_sessions_dir() as folder:
            (folder / "good.json").write_text(json.dumps({"session_id": "good"}))
            (folder / "good.ctx.json").write_text(
                json.dumps({"pct": 50, "at": 1.0}))
            (folder / "broken.json").write_text("{this is not json")
            # A JSON that is valid but is not an object: `_path` cannot be hung
            # off a list, and the readers all do `.get`.
            (folder / "list.json").write_text("[1, 2]")
            self.assertEqual([c.get("session_id") for c in common._raw_cards()],
                             ["good"])

    def test_a_directory_that_is_not_there_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "nope")}):
                self.assertEqual(common._raw_cards(), [])


class TestLoadSessionsPid(unittest.TestCase):
    """GREEN rows: a live pane does not prove there is a live claude inside.

    The typical ghost: you kill a claude the hard way (`kill -9`, the terminal
    hangs) and it never gets to send its SessionEnd. The pane is still there
    with the shell, so the card passed the pane filter and the row kept saying
    "● working" forever. The pid is the proof of life.
    """

    PANES = {"%1": {"session": "recomm", "window": "0", "cmd": "zsh",
                    "window_name": "claude"}}

    def _card(self, folder, sid, **extra):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        d = {"session_id": sid, "state": "working", "project": "demo",
             "tmux_session": "recomm", "tmux_pane": "%1", "updated_at": now}
        d.update(extra)
        (folder / ("%s.json" % sid)).write_text(json.dumps(d))

    def _load(self, **kw):
        with mock.patch.object(common, "live_panes", return_value=self.PANES):
            return common.load_sessions(**kw)

    def test_dead_pid_with_a_live_pane_is_not_listed(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "ghost", pid=_dead_pid())
            self.assertEqual(self._load(), [])

    def test_card_without_a_pid_is_listed(self):
        # Same bias as everywhere else in Flightdeck: when in doubt, show it.
        # Cards written before the hook recorded the pid do not carry one.
        with temp_sessions_dir() as folder:
            self._card(folder, "old")
            self.assertEqual([d["session_id"] for d in self._load()], ["old"])

    def test_live_pid_is_listed(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "alive", pid=os.getpid())
            self.assertEqual([d["session_id"] for d in self._load()], ["alive"])

    def test_the_ghost_is_not_deleted_even_with_prune(self):
        """The pane is ALIVE: there may be a claude starting up right now (its
        card is written before the process the card talks about exists).
        Deleting because of a dead pid would be throwing away good state; it is
        skipped and that is all."""
        with temp_sessions_dir() as folder:
            self._card(folder, "ghost", pid=_dead_pid())
            self.assertEqual(self._load(prune=True), [])
            self.assertTrue((folder / "ghost.json").exists())


class TestPruneAlsoDeletesTheNote(unittest.TestCase):
    """When prune takes a card away, it takes its `<id>.ctx.json` too.

    `load_sessions` skips the tee notes by the file NAME, so no prune pass ever
    looked at them: without this, the note of a closed session stayed in
    `sessions/` forever.
    """

    def _pair(self, folder, sid, **extra):
        d = {"session_id": sid, "state": "working", "project": "demo",
             "updated_at": datetime.datetime.now().astimezone().isoformat()}
        d.update(extra)
        (folder / ("%s.json" % sid)).write_text(json.dumps(d))
        (folder / ("%s.ctx.json" % sid)).write_text(
            json.dumps({"pct": 70, "at": time.time()}))

    def _prune(self):
        with mock.patch.object(common, "live_panes", return_value={}):
            common.load_sessions(prune=True)

    def test_card_that_said_goodbye(self):
        with temp_sessions_dir() as folder:
            self._pair(folder, "closed", state="ended")
            self._prune()
            self.assertFalse((folder / "closed.json").exists())
            self.assertFalse((folder / "closed.ctx.json").exists())

    def test_card_without_a_live_pane(self):
        with temp_sessions_dir() as folder:
            self._pair(folder, "orphan", tmux_pane="%99")
            self._prune()
            self.assertFalse((folder / "orphan.json").exists())
            self.assertFalse((folder / "orphan.ctx.json").exists())


class TestParkedConversation(unittest.TestCase):
    """`/background` (or the ← arrow) sends the conversation to a daemon job:
    the PARKED claude stays in the pane with a stale card, and the job (with no
    pane) is the one doing the work. With Claude Code's registry, the pane row
    shows the JOB's state, and the job is not listed separately as "outside
    tmux"."""

    PANES = {"%15": {"session": "retail_specs", "window": "0", "cmd": "2.1.233",
                     "window_name": "x"}}
    REG = [
        {"pid": os.getpid(), "session_id": "afbe6f19-x", "kind": "interactive",
         "tmux": "retail_specs:@12.%15", "parked_job": "e8053ca3", "job_id": None},
        {"pid": os.getppid(), "session_id": "e8053ca3-2805-x", "kind": "bg",
         "tmux": None, "parked_job": None, "job_id": "e8053ca3"},
    ]

    def _cards(self, folder):
        now = datetime.datetime.now().astimezone()
        ago = lambda s: (now - datetime.timedelta(seconds=s)).isoformat(timespec="seconds")
        parked = {"session_id": "afbe6f19-x", "pid": os.getpid(), "project": "retail_specs",
                  "tmux_session": "retail_specs", "tmux_pane": "%15",
                  "state": "needs_attention", "last_event": "Notification",
                  "source": "startup", "updated_at": ago(40000)}
        job = {"session_id": "e8053ca3-2805-x", "pid": os.getppid(), "project": "retail_specs",
               "state": "looping", "last_event": "Stop", "source": "resume",
               "turn": {"start": "fire", "schedule": None, "complete": True},
               "updated_at": ago(30)}
        for d in (parked, job):
            (folder / ("%s.json" % d["session_id"])).write_text(json.dumps(d))

    def _with(self, fn, reg=None):
        with mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(common, "read_registry",
                                  return_value=self.REG if reg is None else reg):
            return fn()

    def test_the_pane_row_carries_the_job_state(self):
        with temp_sessions_dir() as folder:
            self._cards(folder)
            live = self._with(common.load_sessions)
        self.assertEqual(len(live), 1)
        d = live[0]
        self.assertEqual(d["session_id"], "afbe6f19-x")   # still the pane's one
        self.assertEqual(d["_parked"], "e8053ca3-2805-x")  # ...parked in that job
        self.assertEqual(d["state"], "looping")
        self.assertEqual(d["last_event"], "Stop")
        self.assertEqual(d["source"], "resume")
        self.assertEqual(d["turn"]["start"], "fire")
        self.assertLess(d["_age"], 120)   # the activity is the job's, not the stale one

    def test_the_job_is_not_listed_as_outside_tmux(self):
        with temp_sessions_dir() as folder:
            self._cards(folder)
            outside = self._with(common.load_outside_sessions)
        self.assertEqual(outside, [])

    def test_without_the_registry_everything_stays_as_before(self):
        with temp_sessions_dir() as folder:
            self._cards(folder)
            live = self._with(common.load_sessions, reg=[])
            outside = self._with(common.load_outside_sessions, reg=[])
        self.assertEqual(live[0]["state"], "needs_attention")
        self.assertNotIn("_parked", live[0])
        self.assertEqual([f["session_id"] for f in outside], ["e8053ca3-2805-x"])

    def test_if_the_pane_dies_the_job_shows_up_outside_again(self):
        # After the handover (ctrl+c x2 in the pane) the parked one is gone and
        # the job is left without a terminal: then it IS listed outside tmux.
        with temp_sessions_dir() as folder:
            self._cards(folder)
            (folder / "afbe6f19-x.json").unlink()
            outside = self._with(common.load_outside_sessions)
        self.assertEqual([f["session_id"] for f in outside], ["e8053ca3-2805-x"])


class TestTheRegistryFixesTheFalseWait(unittest.TestCase):
    """Claude Code's `Stop` fires at the end of EVERY turn, including when there
    are agents or delegated tasks running that are about to wake the model up a
    second later: the status bar said "N waiting for you" and cleared itself.
    Claude Code's own registry carries `status` (busy/idle/waiting/shell), and
    `busy` includes "background agents active": with Stop + busy, the row is
    painted working and does not count as a wait."""

    PANES = {"%1": {"session": "recomm", "window": "0", "cmd": "2.1.234", "window_name": "x"}}

    def _with(self, folder, card, reg):
        (folder / ("%s.json" % card["session_id"])).write_text(json.dumps(card))
        with mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(common, "read_registry", return_value=reg):
            return common.load_sessions()[0]

    def _card(self, **kw):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        d = {"session_id": "s1", "pid": os.getpid(), "project": "demo", "tmux_session": "recomm",
             "tmux_pane": "%1", "state": "awaiting_input", "last_event": "Stop", "updated_at": now}
        d.update(kw)
        return d

    def _reg(self, status):
        return [{"pid": os.getpid(), "session_id": "s1", "kind": "interactive", "status": status,
                 "tmux": "recomm:@0.%1", "parked_job": None, "job_id": None}]

    def test_stop_with_a_busy_registry_is_painted_working(self):
        with temp_sessions_dir() as folder:
            d = self._with(folder, self._card(), self._reg("busy"))
        self.assertEqual(d["state"], "working")
        self.assertEqual(d["_status_cc"], "busy")

    def test_stop_with_idle_shell_or_waiting_keeps_waiting(self):
        for status in ("idle", "shell", "waiting", None):
            with self.subTest(status=status), temp_sessions_dir() as folder:
                d = self._with(folder, self._card(), self._reg(status))
                self.assertEqual(d["state"], "awaiting_input", status)

    def test_only_the_stop_wait_is_corrected(self):
        # asking / needs attention are exact signals of an open dialog: not
        # touched.
        for state in ("asking", "needs_attention", "looping"):
            with self.subTest(state=state), temp_sessions_dir() as folder:
                d = self._with(folder, self._card(state=state, last_event="Notification"),
                               self._reg("busy"))
                self.assertEqual(d["state"], state)

    def test_without_a_registry_nothing_changes(self):
        with temp_sessions_dir() as folder:
            d = self._with(folder, self._card(), [])
        self.assertEqual(d["state"], "awaiting_input")
        self.assertIsNone(d.get("_status_cc"))


class TestRegistryNameOnOutsideSessions(unittest.TestCase):
    """Sessions living outside tmux (daemon jobs, ⑂ forks, the desktop app) are
    labelled with the NAME Claude Code registers for them, matched by pid: it is
    the only human name that exists (the cwd may be ~/Documents/claude and
    "claude" says nothing)."""

    def test_load_outside_attaches_the_name(self):
        reg = [{"pid": os.getpid(), "session_id": "s1", "kind": "bg", "tmux": None,
                "parked_job": None, "job_id": "s1", "status": "idle",
                "name": "MySensors home automation Arduino ⑂ I want to investigate"}]
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with temp_sessions_dir() as folder:
            (folder / "s1.json").write_text(json.dumps(
                {"session_id": "s1", "pid": os.getpid(), "project": "claude",
                 "state": "awaiting_input", "updated_at": now}))
            with mock.patch.object(common, "live_panes", return_value={}), \
                    mock.patch.object(common, "read_registry", return_value=reg):
                outside = common.load_outside_sessions()
        self.assertEqual(outside[0]["_name_cc"],
                         "MySensors home automation Arduino ⑂ I want to investigate")

    def test_without_a_registry_there_is_no_name_and_nothing_blows_up(self):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with temp_sessions_dir() as folder:
            (folder / "s1.json").write_text(json.dumps(
                {"session_id": "s1", "pid": os.getpid(), "project": "claude",
                 "state": "awaiting_input", "updated_at": now}))
            with mock.patch.object(common, "live_panes", return_value={}), \
                    mock.patch.object(common, "read_registry", return_value=[]):
                outside = common.load_outside_sessions()
        self.assertIsNone(outside[0].get("_name_cc"))


class TestLoadSessionsCodexBranch(unittest.TestCase):
    """NEW here, not ported: `load_sessions` sends a card with `tool == "codex"`
    down a branch of its own (the 🧠 from the rollout, and "working" from the
    rollout's mtime), and that branch imports `flightdeck.turn` lazily. Nothing
    in `test_common` built a codex card, so while `turn.py` was missing the
    branch raised `ModuleNotFoundError` and no test noticed. This pins it.
    """

    PANES = {"%1": {"session": "recomm", "window": "0", "cmd": "zsh",
                    "window_name": "codex"}}

    def _rollout(self, base, sid, stop, minutes_after):
        f = Path(base) / "2026" / "09" / "07" / ("rollout-2026-09-07T01-00-00-%s.jsonl" % sid)
        f.parent.mkdir(parents=True)
        f.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"session_id": sid, "cwd": "/x"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 50000, "output_tokens": 5000},
                "model_context_window": 100000}}}),
        ]) + "\n")
        t = (stop + datetime.timedelta(minutes=minutes_after)).timestamp()
        os.utime(f, (t, t))
        return f

    def test_a_codex_card_gets_its_ctx_and_its_working_from_the_rollout(self):
        from flightdeck import tools
        sid = "019ffd5b-cafe-4000-8000-000000000001"
        # The Stop is ten minutes old; the rollout moved a minute later, which is
        # past ROLLOUT_MARGIN_S: codex is in the middle of a turn.
        stop = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=10)
        with temp_sessions_dir() as folder, tempfile.TemporaryDirectory() as rollouts:
            (folder / ("%s.json" % sid)).write_text(json.dumps(
                {"session_id": sid, "tool": "codex", "pid": os.getpid(),
                 "tmux_session": "recomm", "tmux_pane": "%1", "project": "demo",
                 "state": "awaiting_input", "last_event": "Stop",
                 "updated_at": stop.isoformat(timespec="seconds")}))
            self._rollout(rollouts, sid, stop, minutes_after=1)
            with mock.patch.object(common, "live_panes", return_value=self.PANES), \
                    mock.patch.object(tools, "CODEX_SESSIONS", Path(rollouts)):
                out = common.load_sessions()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_ctx_pct"], 55)
        self.assertEqual((out[0]["state"], out[0]["last_event"]),
                         ("working", "PostToolUse"))


class TestLoadSessionsAgyBranch(unittest.TestCase):
    """An agy card gets its 🧠 from the note, with no branch of its own.

    agy has no context percentage of its own: its transcript carries no tokens
    (measured) and codex's rollout trick does not apply. What gives it one is
    its status line hook, which writes the same `<id>.ctx.json` the claude tee
    writes, keyed by the conversation id. This pins that `load_sessions` needs
    nothing else to show it.
    """

    PANES = {"%1": {"session": "demo", "window": "0", "cmd": "agy",
                    "window_name": "agy"}}

    def test_an_agy_card_takes_its_ctx_from_the_note(self):
        sid = "11111111-2222-3333-4444-555555555555"
        now = datetime.datetime.now().astimezone()
        with temp_sessions_dir() as folder:
            (folder / ("%s.json" % sid)).write_text(json.dumps(
                {"session_id": sid, "tool": "agy", "pid": os.getpid(),
                 "tmux_session": "demo", "tmux_pane": "%1", "project": "demo",
                 "state": "awaiting_input", "last_event": "Stop",
                 "updated_at": now.isoformat(timespec="seconds")}))
            (folder / ("%s.ctx.json" % sid)).write_text(
                json.dumps({"pct": 64, "at": time.time()}))
            with mock.patch.object(common, "live_panes", return_value=self.PANES):
                out = common.load_sessions()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_ctx_pct"], 64)


class _FakeRun:
    """A stand-in for `subprocess.run` that records what it was asked to run.

    `tmux -V` is the one tmux call Flightdeck makes that needs no server, but a
    test still must not reach the real binary: the answer has to be the same on
    a machine's 3.6a, on Ubuntu 22.04's 3.2a and in CI.
    """

    def __init__(self, stdout="", returncode=0, blows_up=None):
        self.stdout, self.returncode, self.blows_up = stdout, returncode, blows_up
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if self.blows_up is not None:
            raise self.blows_up
        return type("R", (), {"returncode": self.returncode,
                              "stdout": self.stdout, "stderr": ""})()


class TestTmuxVersion(unittest.TestCase):
    """`tmux -V`, read once per process: it is what decides whether the floating
    notice may use `display-message -l` (tmux >= 3.4)."""

    def setUp(self):
        common.tmux_version.cache_clear()

    def tearDown(self):
        common.tmux_version.cache_clear()

    def test_it_parses_the_shapes_tmux_actually_prints(self):
        for line, expected in (("tmux 3.2a\n", (3, 2)),
                               ("tmux 3.3a\n", (3, 3)),
                               ("tmux 3.4\n", (3, 4)),
                               ("tmux 3.6a\n", (3, 6)),
                               # A build from git between releases.
                               ("tmux next-3.5\n", (3, 5)),
                               ("tmux 2.9a\n", (2, 9)),
                               # Two digits: 3.10 is NOT 3.1.
                               ("tmux 3.10\n", (3, 10))):
            common.tmux_version.cache_clear()
            with self.subTest(line=line):
                self.assertEqual(common.tmux_version(_FakeRun(line)), expected)

    def test_what_cannot_be_read_comes_back_as_None(self):
        """None is "I do not know", and the notice takes the safe form for it."""
        for case in (_FakeRun("tmux master\n"),       # no number at all
                     _FakeRun(""),                    # said nothing
                     _FakeRun("tmux 3.6a\n", returncode=1),   # it failed
                     _FakeRun(blows_up=FileNotFoundError("no tmux here")),
                     _FakeRun(blows_up=subprocess.TimeoutExpired("tmux", 2))):
            common.tmux_version.cache_clear()
            with self.subTest(case=case.stdout or case.blows_up):
                self.assertIsNone(common.tmux_version(case))

    def test_it_asks_the_tmux_of_the_configured_socket_and_does_not_hang(self):
        # The socket seam is what keeps the suite off the developer's server;
        # `tmux -V` needs no server, but the binary must still be the same one.
        fake = _FakeRun("tmux 3.6a\n")
        with mock.patch.dict(os.environ,
                             {"FLIGHTDECK_TMUX_SOCKET": "versiontest"}):
            common.tmux_version(fake)
        cmd, kw = fake.calls[0]
        self.assertEqual(cmd, ["tmux", "-L", "versiontest", "-V"])
        self.assertTrue(kw.get("timeout"))

    def test_it_only_asks_once_per_process(self):
        """This is read from inside the hooks, which fire on every tool use: one
        `tmux -V` per notice would be a subprocess nobody needs."""
        fake = _FakeRun("tmux 3.6a\n")
        self.assertEqual(common.tmux_version(fake), (3, 6))
        self.assertEqual(common.tmux_version(fake), (3, 6))
        self.assertEqual(len(fake.calls), 1)


class TestNoticeArgs(unittest.TestCase):
    """The floating notice's argv, which is the one place that knows what this
    tmux can do. `-l` (print the text literally) is tmux >= 3.4; below that the
    text is escaped instead, so a session called `#(rm -rf)` is never run."""

    def test_from_3_4_the_text_travels_literally(self):
        self.assertEqual(
            common.notice_args("⏳ #(rm -rf) is waiting for you", (3, 4), 5000,
                               "/dev/ttys001"),
            ["display-message", "-c", "/dev/ttys001", "-d", "5000", "-C", "-l",
             "⏳ #(rm -rf) is waiting for you"])

    def test_below_3_4_the_hashes_are_doubled_instead_of_passing_l(self):
        # tmux 3.2a ships with Ubuntu 22.04 and 3.3a with Debian 12: there
        # `display-message -l` does not exist and the message is read as a
        # FORMAT, so `#(...)` would be a command that gets run. Doubling the `#`
        # is what the status bar already does (`statusbar._escape`).
        for version in ((3, 2), (3, 3)):
            with self.subTest(version=version):
                self.assertEqual(
                    common.notice_args("⏳ #(rm -rf) is waiting for you",
                                       version, 5000, "/dev/ttys001"),
                    ["display-message", "-c", "/dev/ttys001", "-d", "5000",
                     "-C", "⏳ ##(rm -rf) is waiting for you"])

    def test_a_tmux_we_could_not_read_gets_the_safe_form(self):
        # Unknown version = assume the old one. Losing the `#` of a project name
        # is cosmetic; running it is not.
        self.assertEqual(
            common.notice_args("🧠 a#b at 83%", None, 5000, "/dev/ttys001"),
            ["display-message", "-c", "/dev/ttys001", "-d", "5000", "-C",
             "🧠 a##b at 83%"])

    def test_a_later_tmux_keeps_the_literal_flag(self):
        self.assertIn("-l", common.notice_args("x", (4, 0), 5000, "/dev/ttys1"))

    def test_without_a_client_it_does_not_say_c(self):
        self.assertEqual(common.notice_args("hello", (3, 6), 5000),
                         ["display-message", "-d", "5000", "-C", "-l", "hello"])

    def test_the_duration_is_spelled_out_for_the_command_line(self):
        args = common.notice_args("hello", (3, 6), 8000, "/dev/ttys1")
        self.assertEqual(args[args.index("-d") + 1], "8000")


class TestTheRowOfAClaudeThatPredatesTheHooks(unittest.TestCase):
    """Cards are written by the HOOKS, so a claude that was already open when
    Flightdeck was installed has none until it has a turn: on the first day of an
    installation every one of them showed up as a bare `shell`, with no state and
    no age (seen live).

    Claude Code's own registry already knows the pane, the name, the folder and
    whether it is busy, so the row is synthesised from it -- in memory, never
    written, and a real card always wins.
    """

    PANES = {"%9": {"session": "ssr cache", "window": "0", "cmd": "2.1.273",
                    "window_name": "claude"},
             "%1": {"session": "recomm", "window": "0", "cmd": "zsh",
                    "window_name": "shell"}}

    def _entry(self, **kw):
        """A registry entry as `read_registry` normalises it.

        The shape is a real one, read off a live machine (personal values
        shortened): `"tmux": "ssr cache:@9.%9"`, `"name": "ssr cache 2"`,
        `"status": "idle"`. The timestamps are moved to NOW so the age this
        produces can be asserted on.
        """
        now_ms = int(time.time() * 1000)
        d = {"pid": os.getpid(), "session_id": "ef1864c2-x", "cwd": "/w/ssr_cache",
             "kind": "interactive", "tmux": "ssr cache:@9.%9", "parked_job": None,
             "job_id": None, "name": "ssr cache 2", "status": "idle",
             "waiting_for": None, "started_at": now_ms - 3600_000,
             "status_updated_at": now_ms, "updated_at": now_ms}
        d.update(kw)
        return d

    def _load(self, entries, **kw):
        with mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(common, "read_registry", return_value=entries):
            return common.load_sessions(**kw)

    def _card(self, folder, sid, **extra):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        d = {"session_id": sid, "pid": os.getpid(), "state": "working",
             "last_event": "PostToolUse", "project": "demo",
             "tmux_session": "ssr cache", "tmux_pane": "%9", "updated_at": now}
        d.update(extra)
        (folder / ("%s.json" % sid)).write_text(json.dumps(d))

    def test_the_row_is_built_from_the_registry(self):
        with temp_sessions_dir():
            live = self._load([self._entry()])
        self.assertEqual(len(live), 1)
        d = live[0]
        self.assertEqual(d["session_id"], "ef1864c2-x")
        self.assertEqual(d["pid"], os.getpid())
        self.assertEqual(d["tmux_pane"], "%9")
        # The tmux session's name comes from the SERVER, not from the registry's
        # `tmux` string: that one is written when claude starts and a session
        # renamed underneath it would leave the row pointing at a name that is
        # no longer there.
        self.assertEqual(d["tmux_session"], "ssr cache")
        self.assertEqual(d["tool"], "claude")
        self.assertEqual(d["project"], "ssr_cache")
        self.assertEqual(d["title"], "ssr cache 2")
        self.assertEqual(d["cwd"], "/w/ssr_cache")
        self.assertTrue(d["_from_registry"])
        self.assertLess(d["_age"], 60)

    def test_the_status_becomes_the_state(self):
        for status, state in (("busy", "working"), ("idle", "awaiting_input"),
                              ("waiting", "needs_attention"),
                              ("shell", "awaiting_input")):
            with self.subTest(status=status), temp_sessions_dir():
                live = self._load([self._entry(status=status)])
                self.assertEqual(live[0]["state"], state)

    def test_a_status_we_do_not_know_leaves_the_row_without_a_state(self):
        # Inventing one would be inventing a wait: the row still gains its name,
        # its folder and its age, and the badge stays "shell".
        for status in (None, "hibernating"):
            with self.subTest(status=status), temp_sessions_dir():
                live = self._load([self._entry(status=status)])
                self.assertIsNone(live[0].get("state"))
                self.assertEqual(live[0]["title"], "ssr cache 2")

    def test_a_claude_that_has_never_had_a_turn_is_OPEN_not_waiting(self):
        # Claude Code writes `status: "idle"` the moment it registers itself
        # (measured: `statusUpdatedAt` 46 ms after `startedAt`). An idle it has
        # never moved from is an empty box, not a session waiting for you --
        # and this is the trio the `○ open` badge reads.
        now_ms = int(time.time() * 1000)
        with temp_sessions_dir():
            live = self._load([self._entry(started_at=now_ms - 46,
                                           status_updated_at=now_ms)])
        self.assertEqual(live[0]["state"], "working")
        self.assertEqual(live[0]["last_event"], "SessionStart")
        self.assertEqual(live[0]["source"], "startup")

    def test_an_idle_that_stopped_at_the_end_of_a_turn_IS_waiting(self):
        now_ms = int(time.time() * 1000)
        with temp_sessions_dir():
            live = self._load([self._entry(started_at=now_ms - 6000,
                                           status_updated_at=now_ms)])
        self.assertEqual(live[0]["state"], "awaiting_input")
        self.assertIsNone(live[0].get("last_event"))

    def test_a_real_card_for_that_pane_wins(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "real")
            live = self._load([self._entry()])
        self.assertEqual([d["session_id"] for d in live], ["real"])
        self.assertNotIn("_from_registry", live[0])

    def test_a_real_card_for_that_session_wins_from_whatever_pane(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "ef1864c2-x", tmux_pane="%1", tmux_session="recomm")
            live = self._load([self._entry()])
        self.assertEqual([d["tmux_pane"] for d in live], ["%1"])
        self.assertNotIn("_from_registry", live[0])

    def test_an_entry_whose_pane_is_not_live_is_ignored(self):
        for tmux in ("ssr cache:@9.%99", None, "ssr cache"):
            with self.subTest(tmux=tmux), temp_sessions_dir():
                self.assertEqual(self._load([self._entry(tmux=tmux)]), [])

    def test_an_entry_whose_session_name_is_not_this_panes_is_ignored(self):
        """The same pane id on ANOTHER tmux server.

        The registry's destination carries no socket, so a claude running inside
        tmux on an isolated `-L` server (which is how this project measures
        things) has a pane id that may well exist here too -- and its name and
        state would be painted on a stranger's pane. The session name in front of
        the pane id is the cheap tell.

        It is a veto and never a source of truth: a tmux session RENAMED after
        that claude started fails it as well, and there the row simply loses its
        annotation and goes back to the bare `shell` it was before, which is the
        safe direction. A destination with no name to check (there is no `:@`)
        is left alone.
        """
        with temp_sessions_dir():
            self.assertEqual(self._load([self._entry(tmux="expagy:@9.%9")]), [])
        with temp_sessions_dir():
            # And the matching one still comes through, so the guard is not
            # simply switching the feature off.
            self.assertEqual(len(self._load([self._entry()])), 1)

    def test_a_bg_entry_is_ignored(self):
        # A parked conversation's job has no pane of its own: the PANE's row is
        # what shows it (see `_link_parked`), and the registry's `tmux` field on
        # such an entry belongs to the claude that parked it.
        with temp_sessions_dir():
            self.assertEqual(
                self._load([self._entry(kind="bg", job_id="ef1864c2")]), [])

    def test_an_entry_whose_process_is_gone_is_ignored(self):
        # A live pane does not prove there is a live claude in it: a `kill -9`
        # leaves the registry entry behind, and a green row over a shell is
        # exactly what the rest of this module refuses to paint.
        with temp_sessions_dir():
            self.assertEqual(self._load([self._entry(pid=_dead_pid())]), [])

    def test_a_registry_that_cannot_be_read_synthesises_nothing(self):
        with temp_sessions_dir(), \
                mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(common, "read_registry",
                                  side_effect=OSError("permissions")):
            self.assertEqual(common.load_sessions(), [])

    def test_the_synthesised_card_is_never_written_not_even_with_prune(self):
        with temp_sessions_dir() as folder:
            live = self._load([self._entry()], prune=True)
            self.assertEqual(len(live), 1)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [])

    def test_an_entry_with_no_session_id_is_skipped(self):
        """`sessionId` is not guaranteed: `read_registry` lets a null one
        through as None, and a row built on it was a crash waiting to happen --
        `load_sessions` sorts by `session_id` and comparing None with the id of
        another row in the same tmux session raises TypeError. The status bar
        swallows that; the menu's loop does not, and it would fall out to a
        shell. Such an entry has no conversation to point at anyway (the row's
        glyph and its 🧠 both hang off the id), so it is simply not a row.
        """
        with temp_sessions_dir() as folder:
            # A second pane of the SAME tmux session, with a card of its own:
            # that is what puts the two rows side by side in the sort.
            self._card(folder, "real", tmux_pane="%1", tmux_session="ssr cache")
            live = self._load([self._entry(session_id=None)])
        self.assertEqual([d["session_id"] for d in live], ["real"])

    def test_two_entries_with_no_session_id_are_both_skipped(self):
        # Without the skip these two would also be compared against each other.
        with temp_sessions_dir():
            entries = [self._entry(session_id=None),
                       self._entry(session_id=None, pid=os.getppid(),
                                   tmux="recomm:@0.%1")]
            self.assertEqual(self._load(entries), [])

    def test_the_sort_survives_a_card_whose_id_is_present_but_null(self):
        # Belt and braces for the same crash from any other source: a hook card
        # written with `"session_id": null` (an old one, a half-written file).
        # `dict.get(k, "")` does NOT defend against this -- the key IS there.
        with temp_sessions_dir() as folder:
            self._card(folder, "real", tmux_pane="%1")
            (folder / "null-id.json").write_text(json.dumps(
                {"session_id": None, "pid": os.getpid(), "project": "demo",
                 "tmux_session": "ssr cache", "tmux_pane": "%9",
                 "state": "working", "updated_at":
                 datetime.datetime.now().astimezone().isoformat(timespec="seconds")}))
            live = self._load([])
        self.assertEqual(len(live), 2)

    def test_the_context_note_reaches_the_synthesised_row(self):
        # The status line tee writes `<id>.ctx.json` with no hook involved, so a
        # claude that predates the hooks does have a 🧠 to show.
        with temp_sessions_dir() as folder:
            (folder / "ef1864c2-x.ctx.json").write_text(
                json.dumps({"pct": 62, "at": time.time()}))
            live = self._load([self._entry()])
        self.assertEqual(live[0]["_ctx_pct"], 62)


class TestTheProcessTable(unittest.TestCase):
    """The one `ps` a pass makes: who is running, and under whom.

    The flags are the ones both families of `ps` understand -- BSD's (macOS) and
    procps' (Linux) -- and the empty `=` headers are what leaves the output as
    three bare columns, with no title line to skip.
    """

    LINE = " 5168  5116 claude attach 23a53419\n"

    def test_it_reads_pid_ppid_and_the_command_line(self):
        run = _FakeRun(" 5116     1 -zsh\n" + self.LINE)
        self.assertEqual(common.process_table(run=run),
                         [(5116, 1, "-zsh"),
                          (5168, 5116, "claude attach 23a53419")])
        self.assertEqual(run.calls[0][0], ["ps", "-ax", "-o", "pid=,ppid=,args="])

    def test_a_line_that_is_not_one_is_skipped(self):
        for out in ("nonsense\n\n", "x y args\n", "  \n"):
            with self.subTest(out=out):
                self.assertEqual(common.process_table(run=_FakeRun(out)), [])

    def test_a_ps_that_fails_or_blows_up_is_empty_never_an_exception(self):
        self.assertEqual(
            common.process_table(run=_FakeRun(self.LINE, returncode=1)), [])
        self.assertEqual(
            common.process_table(run=_FakeRun(blows_up=OSError("no ps here"))), [])
        self.assertEqual(
            common.process_table(
                run=_FakeRun(blows_up=subprocess.TimeoutExpired("ps", 3))), [])


class TestAttachId(unittest.TestCase):
    """`claude attach <id>` is Claude Code's VIEWER of a conversation that lives
    in a background job (2.1.26x+). Reading it is strict on purpose: what it
    decides is which conversation gets painted on a pane, and the row it would
    replace (a bare `shell`) is not worth a guess."""

    def test_the_shapes_the_viewer_really_has(self):
        # argv[0] as `ps` gave it on a real machine, plus the binary christened
        # with its version (`2.1.269`, which is also what tmux shows in
        # `pane_current_command`), the same reading `tools.is_argv_of` makes of
        # a claude.
        for argv in (["claude", "attach", "23a53419"],
                     ["/opt/homebrew/bin/claude", "attach", "23a53419"],
                     ["2.1.269", "attach", "23a53419"]):
            with self.subTest(argv=argv):
                self.assertEqual(common.attach_id(argv), "23a53419")

    def test_what_is_not_a_viewer(self):
        for argv in ([], ["claude"], ["claude", "attach"], ["-zsh"],
                     ["claude", "--resume", "23a53419"],
                     ["codex", "attach", "23a53419"],
                     ["/bin/sh", "-c", "claude attach 23a53419"],
                     # An id that is not one: it is compared against file stems
                     # and registry ids, and `SAFE_ID` is the same filter the
                     # history's ids go through.
                     ["claude", "attach", "../../etc/passwd"]):
            with self.subTest(argv=argv):
                self.assertIsNone(common.attach_id(argv))


class TestAPaneWatchingABackgroundConversation(unittest.TestCase):
    """A pane running `claude attach <id>` shows the conversation it is watching.

    Measured on a real machine: pane `%2` (tmux session `homeassistant`) has a
    shell with pid 5116 whose child, pid 5168, is `claude attach 23a53419`;
    `pane_current_command` reads `2.1.269`. The registry holds ONE entry for
    that conversation, the JOB's -- `{"pid": 59761, "kind": "bg", "status":
    "idle", "name": "homeassistant 2", "tmux": null, "jobId": "23a53419",
    "sessionId": "23a53419-68a0-..."}` -- with no pane of its own, which is
    exactly what `_registry_cards` skips. Nothing linked the two, so the row
    read `shell`: no name, no state, no age.
    """

    PANES = {"%2": {"session": "homeassistant", "window": "0", "cmd": "2.1.269",
                    "window_name": "claude", "pid": 5116},
             "%1": {"session": "recomm", "window": "0", "cmd": "zsh",
                    "window_name": "shell", "pid": 900}}
    # What `ps -ax -o pid=,ppid=,args=` printed, with the rest of the machine
    # left out.
    PS = [(900, 1, "-zsh"), (5116, 1, "-zsh"),
          (5168, 5116, "claude attach 23a53419")]
    SID = "23a53419-68a0-491b-8621-6446cb284ab0"

    def _job(self, **kw):
        now_ms = int(time.time() * 1000)
        d = {"pid": os.getpid(), "session_id": self.SID,
             "cwd": "/w/homeassistant", "kind": "bg", "tmux": None,
             "parked_job": None, "job_id": "23a53419",
             "name": "homeassistant 2", "status": "idle", "waiting_for": None,
             "started_at": now_ms - 3600_000, "status_updated_at": now_ms,
             "updated_at": now_ms}
        d.update(kw)
        return d

    def _with(self, fn, entries=None, table=None):
        with mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(
                    common, "read_registry",
                    return_value=[self._job()] if entries is None else entries), \
                mock.patch.object(common, "process_table",
                                  return_value=self.PS if table is None else table):
            return fn()

    def _card(self, folder, sid, **extra):
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        d = {"session_id": sid, "pid": os.getpid(), "state": "working",
             "last_event": "PostToolUse", "project": "homeassistant",
             "updated_at": now}
        d.update(extra)
        (folder / ("%s.json" % sid)).write_text(json.dumps(d))

    def test_the_pane_shows_the_conversation_it_is_watching(self):
        with temp_sessions_dir():
            live = self._with(common.load_sessions)
        self.assertEqual(len(live), 1)
        d = live[0]
        self.assertEqual(d["session_id"], self.SID)
        self.assertEqual(d["tmux_pane"], "%2")
        self.assertEqual(d["tmux_session"], "homeassistant")
        # The JOB's pid: the viewer can be closed and reopened, and what has to
        # be alive for this row to mean anything is the conversation.
        self.assertEqual(d["pid"], os.getpid())
        self.assertEqual(d["title"], "homeassistant 2")
        self.assertEqual(d["project"], "homeassistant")
        self.assertEqual(d["state"], "awaiting_input")
        self.assertEqual(d["_attach"], "23a53419")
        # The same key a parked conversation uses, so the row says `· background`
        # with nothing changed in the picker.
        self.assertEqual(d["_parked"], "23a53419")
        self.assertTrue(d["_from_registry"])
        self.assertLess(d["_age"], 60)

    def test_the_jobs_status_becomes_the_state_like_any_registry_row(self):
        for status, state in (("busy", "working"), ("idle", "awaiting_input"),
                              ("waiting", "needs_attention"),
                              ("shell", "awaiting_input")):
            with self.subTest(status=status), temp_sessions_dir():
                live = self._with(common.load_sessions,
                                  entries=[self._job(status=status)])
                self.assertEqual(live[0]["state"], state)

    def test_the_context_note_reaches_the_row(self):
        with temp_sessions_dir() as folder:
            (folder / ("%s.ctx.json" % self.SID)).write_text(
                json.dumps({"pct": 71, "at": time.time()}))
            live = self._with(common.load_sessions)
        self.assertEqual(live[0]["_ctx_pct"], 71)

    def test_a_real_card_for_the_job_is_linked_to_the_pane_not_duplicated(self):
        """The hook did fire for the job: that card is the row, with the pane on it.

        The job's card has no pane of its own (the daemon job runs with no
        TMUX_PANE), so without this it was a `◇ outside tmux` row while the pane
        that was showing it read `shell`: the same conversation twice, and
        neither of the two where the user is looking.
        """
        with temp_sessions_dir() as folder:
            self._card(folder, self.SID, state="working")
            live = self._with(common.load_sessions)
            outside = self._with(common.load_outside_sessions)
        self.assertEqual([d["session_id"] for d in live], [self.SID])
        self.assertEqual(live[0]["tmux_pane"], "%2")
        self.assertEqual(live[0]["tmux_session"], "homeassistant")
        self.assertEqual(live[0]["state"], "working")   # the hook's card wins
        self.assertEqual(live[0]["_parked"], "23a53419")
        self.assertNotIn("_from_registry", live[0])
        self.assertEqual(outside, [])

    def test_a_job_whose_card_says_goodbye_or_whose_process_is_gone_is_not_painted(self):
        with temp_sessions_dir():
            self.assertEqual(
                self._with(common.load_sessions,
                           entries=[self._job(pid=_dead_pid())]), [])
        with temp_sessions_dir() as folder:
            self._card(folder, self.SID, state="ended")
            live = self._with(common.load_sessions)
        # The card said goodbye, so the row falls back to the registry's entry,
        # which is still there and still alive.
        self.assertEqual([d.get("_from_registry") for d in live], [True])

    def test_a_viewer_of_something_that_is_not_there_stays_a_shell(self):
        with temp_sessions_dir():
            self.assertEqual(
                self._with(common.load_sessions,
                           entries=[self._job(job_id="ffffffff",
                                              session_id="ffffffff-x")]), [])

    def test_only_a_background_job_is_a_background_conversation(self):
        # An `interactive` entry carrying that id is a claude of its own, and one
        # with no pane in its `tmux` field is not this pane's (`_registry_cards`
        # is what looks after those).
        with temp_sessions_dir():
            self.assertEqual(
                self._with(common.load_sessions,
                           entries=[self._job(kind="interactive")]), [])

    def test_the_job_is_matched_by_its_id_and_failing_that_by_the_conversations(self):
        # `claude attach` is given the JOB's short id, so `jobId` is the exact
        # match; an entry that carries none is matched on a `sessionId` that
        # starts with it (the short id is that id's first eight characters).
        with temp_sessions_dir():
            live = self._with(common.load_sessions,
                              entries=[self._job(job_id=None)])
        self.assertEqual([d["session_id"] for d in live], [self.SID])

    def test_two_conversations_starting_with_that_id_are_not_guessed(self):
        # Painting the wrong conversation on a pane is worse than leaving the row
        # the bare `shell` it was.
        entries = [self._job(job_id=None),
                   self._job(job_id=None, session_id="23a53419-0000-0000-x")]
        with temp_sessions_dir():
            self.assertEqual(self._with(common.load_sessions, entries=entries), [])

    def test_an_entry_with_no_session_id_is_skipped(self):
        # Same reason as in `_registry_cards`: the sort at the end of
        # `load_sessions` compares ids, and the glyph, the 🧠 and every action
        # hang off that id.
        with temp_sessions_dir():
            self.assertEqual(
                self._with(common.load_sessions,
                           entries=[self._job(session_id=None)]), [])

    def test_a_card_that_already_speaks_for_that_pane_wins(self):
        with temp_sessions_dir() as folder:
            self._card(folder, "real", tmux_pane="%2",
                       tmux_session="homeassistant")
            live = self._with(common.load_sessions)
        self.assertEqual([d["session_id"] for d in live], ["real"])

    def test_the_viewer_is_found_one_level_below_the_panes_shell_too(self):
        # Measured, the viewer is the shell's direct child; the second level is
        # for a shell that wraps the command.
        table = [(5116, 1, "-zsh"),
                 (5200, 5116, "/bin/sh -c claude attach 23a53419"),
                 (5168, 5200, "claude attach 23a53419")]
        with temp_sessions_dir():
            live = self._with(common.load_sessions, table=table)
        self.assertEqual([d["tmux_pane"] for d in live], ["%2"])

    def test_a_pane_whose_own_process_is_the_viewer_counts_too(self):
        # Flightdeck's panes are shell-first, so the measured viewer is the
        # shell's child; a pane somebody opened with the command itself has it as
        # its first process, which is what `#{pane_pid}` reports.
        with temp_sessions_dir():
            live = self._with(common.load_sessions,
                              table=[(5116, 1, "claude attach 23a53419")])
        self.assertEqual([d["tmux_pane"] for d in live], ["%2"])

    def test_deeper_than_that_it_is_not_looked_for(self):
        table = [(5116, 1, "-zsh"), (5200, 5116, "-zsh"), (5300, 5200, "-zsh"),
                 (5168, 5300, "claude attach 23a53419")]
        with temp_sessions_dir():
            self.assertEqual(self._with(common.load_sessions, table=table), [])

    def test_a_ps_that_says_nothing_leaves_every_pane_as_it_was(self):
        for table in ([], [(5116, 1, "-zsh")]):
            with self.subTest(table=table), temp_sessions_dir():
                self.assertEqual(
                    self._with(common.load_sessions, table=table), [])

    def test_a_ps_that_blows_up_is_no_exception(self):
        with temp_sessions_dir(), \
                mock.patch.object(common, "live_panes", return_value=self.PANES), \
                mock.patch.object(common, "read_registry",
                                  return_value=[self._job()]), \
                mock.patch.object(common, "process_table",
                                  side_effect=OSError("no ps here")):
            self.assertEqual(common.load_sessions(), [])

    def test_the_synthesised_row_is_never_written_not_even_with_prune(self):
        with temp_sessions_dir() as folder:
            live = self._with(lambda: common.load_sessions(prune=True))
            self.assertEqual(len(live), 1)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [])


class TestLivePanesCarriesThePanePid(unittest.TestCase):
    """The pane's own process (its shell) is the root of the walk that finds a
    `claude attach` viewer inside the pane, so `list-panes` asks for it."""

    def _panes(self, stdout):
        with mock.patch.object(common, "tmux",
                               lambda *a, **k: _FakeRun(stdout)(list(a))):
            return common.live_panes()

    def test_the_pid_arrives_as_a_number(self):
        panes = self._panes("%2\thomeassistant\t0\t2.1.269\tclaude\t5116\n")
        self.assertEqual(panes["%2"], {"session": "homeassistant", "window": "0",
                                       "cmd": "2.1.269", "window_name": "claude",
                                       "pid": 5116})

    def test_a_pane_with_no_readable_pid_is_still_a_pane(self):
        # Only the viewer walk loses: everything else about the pane is there.
        for line in ("%2\tha\t0\tzsh\tshell\n", "%2\tha\t0\tzsh\tshell\tnope\n"):
            with self.subTest(line=line):
                panes = self._panes(line)
                self.assertEqual(panes["%2"]["session"], "ha")
                self.assertIsNone(panes["%2"]["pid"])
