"""The status line tee: it watches the context % go past and paints what was there.

The tee sits in front of the agent's own status line: it reads the JSON on
stdin, writes the percentage down next to the session card, runs the command
that was configured before (the delegate) handing it the SAME stdin and giving
back its stdout byte for byte, and only THEN sends the 🧠 notice.

Nothing here talks to the real tmux or the real state directory: the two
directories are module-level lookups (`_sessions_dir`, `_delegate_file`) that
the harness replaces, and `notify_tmux` is replaced by a collector.
"""
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
from flightdeck.hooks import context_tee as tee


def payload(**extra):
    """JSON like the one the agent sends its status line on stdin."""
    d = {
        "session_id": "abc-123",
        "cwd": "/tmp",
        "model": {"display_name": "Fable 5"},
        "context_window": {"used_percentage": 42.7},
    }
    d.update(extra)
    return json.dumps(d)


class TestParseCtx(unittest.TestCase):

    def test_it_takes_the_id_and_the_rounded_percentage(self):
        self.assertEqual(tee.parse_ctx(payload()),
                         {"session_id": "abc-123", "pct": 43})

    def test_without_a_context_window_it_is_none(self):
        d = json.loads(payload())
        del d["context_window"]
        self.assertIsNone(tee.parse_ctx(json.dumps(d)))

    def test_a_context_window_without_a_percentage_is_none(self):
        self.assertIsNone(tee.parse_ctx(payload(context_window={"foo": 1})))

    def test_broken_json_is_none(self):
        self.assertIsNone(tee.parse_ctx("{this is not json"))
        self.assertIsNone(tee.parse_ctx(""))
        # Valid JSON that is not an object
        self.assertIsNone(tee.parse_ctx("[1, 2, 3]"))

    def test_without_a_session_id_it_is_none(self):
        d = json.loads(payload())
        del d["session_id"]
        self.assertIsNone(tee.parse_ctx(json.dumps(d)))

    def test_nan_and_infinity_are_none(self):
        # json.loads accepts NaN/Infinity (invalid JSON, but Python parses them)
        # and with those int() blows up: if that escaped, the bar would be left
        # blank on every repaint.
        for raw in ['{"session_id":"abc-123","context_window":{"used_percentage":NaN}}',
                    '{"session_id":"abc-123","context_window":{"used_percentage":Infinity}}',
                    '{"session_id":"abc-123","context_window":{"used_percentage":-Infinity}}',
                    # 1e400 does not fit in a float: json leaves it at inf
                    '{"session_id":"abc-123","context_window":{"used_percentage":1e400}}']:
            self.assertIsNone(tee.parse_ctx(raw), raw)

    def test_a_dangerous_session_id_is_none(self):
        # Nothing that could escape the sessions directory or sneak into a shell.
        for bad in ["../../etc/passwd", "a b", "x;rm -rf /", "", 42]:
            self.assertIsNone(tee.parse_ctx(payload(session_id=bad)), bad)


class TestRecordCtx(unittest.TestCase):

    def test_it_creates_the_file_with_pct_and_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee.record_ctx("s1", 43, sess_dir=tmp, now=1000.0)
            d = json.loads((Path(tmp) / "s1.ctx.json").read_text())
            self.assertEqual(d, {"pct": 43, "at": 1000.0})

    def test_it_keeps_other_keys(self):
        # The notice state ("warned") is kept in this same file: rewriting it
        # whole would lose it and the 80% notice would repeat on every render.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s1.ctx.json"
            f.write_text(json.dumps({"pct": 10, "at": 1.0, "warned": True}))
            tee.record_ctx("s1", 81, sess_dir=tmp, now=2000.0)
            d = json.loads(f.read_text())
            self.assertEqual(d, {"pct": 81, "at": 2000.0, "warned": True})

    def test_a_corrupt_file_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s1.ctx.json"
            f.write_text("{broken")
            tee.record_ctx("s1", 5, sess_dir=tmp, now=3.0)
            self.assertEqual(json.loads(f.read_text()), {"pct": 5, "at": 3.0})

    def test_a_directory_that_does_not_exist_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sessions"
            self.assertIsNotNone(tee.record_ctx("s1", 7, sess_dir=target, now=4.0))
            self.assertTrue((target / "s1.ctx.json").exists())

    def test_if_it_cannot_write_it_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A file where the directory should be: the mkdir fails. The caller
            # must not believe its state was saved.
            blocker = Path(tmp) / "blocker"
            blocker.write_text("I am not a directory")
            self.assertIsNone(
                tee.record_ctx("s1", 7, sess_dir=blocker / "sessions", now=4.0))


class TestReadDelegate(unittest.TestCase):

    def test_no_file_is_the_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tee.read_delegate(Path(tmp) / "does-not-exist"), "")

    def test_an_empty_file_is_the_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "delegate"
            p.write_text("\n")
            self.assertEqual(tee.read_delegate(p), "")

    def test_it_returns_the_first_clean_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "delegate"
            p.write_text("  sh /path/statusline.sh  \n")
            self.assertEqual(tee.read_delegate(p), "sh /path/statusline.sh")


class _FakeStd:
    """A made-up stdin/stdout: it exposes .buffer (bytes) like the real ones."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)

    def write(self, text):
        self.buffer.write(text.encode())

    def flush(self):
        pass


class _RunsTheTee:
    """Runs the tee end to end with the filesystem, the delegate and tmux faked.
    The notices that would have gone to tmux are left in `self.notices`."""

    def _run(self, tmp, stdin_text, delegate=None, sess_dir=None, on_notice=None,
             mode="wrap", columns=None, account=None, lines=None):
        """`mode` is the `statusline.claude` config key: who paints the line.

        It defaults to `wrap` -- the delegate's output, byte for byte -- because
        that is the behaviour every test in this file was written against, and
        it is still a mode the user can choose. The three modes have their own
        class below.

        `columns` is the `COLUMNS` Claude Code exports into the environment of
        every command it launches, this tee among them. Left out there is none,
        which is the suite's default (`tests/__init__.py` clears it) and the
        case where nothing is trimmed.

        `account` and `lines` are the two keys that shape Flightdeck's own line.
        Left out, neither is written at all, which is what a fresh install has:
        the defaults, i.e. three lines with the account segment on.
        """
        if delegate is not None:
            (Path(tmp) / "delegate").write_text(delegate + "\n")
        cfg = Path(tmp) / "config.json"
        section = {"claude": mode}
        if account is not None:
            section["account"] = account
        if lines is not None:
            section["lines"] = lines
        cfg.write_text(json.dumps({"statusline": section}))
        stdin, stdout = _FakeStd(stdin_text.encode()), _FakeStd()
        if not hasattr(self, "notices"):
            self.notices = []
        sessions = Path(sess_dir if sess_dir is not None
                        else Path(tmp) / "sessions")
        # Not one test may talk to the real tmux, nor to the real state directory.
        environ = {"FLIGHTDECK_CONFIG": str(cfg)}
        if columns is not None:
            environ["COLUMNS"] = str(columns)
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(tee, "_sessions_dir", lambda: sessions), \
                mock.patch.object(tee, "_delegate_file",
                                  lambda: Path(tmp) / "delegate"), \
                mock.patch.object(tee, "notify_tmux",
                                  on_notice or (lambda text: self.notices.append(text))), \
                mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout):
            tee.main()
        return stdout.buffer.getvalue()


class TestMain(_RunsTheTee, unittest.TestCase):
    """The tee end to end, with the filesystem and the delegate faked."""

    def test_with_no_delegate_it_paints_the_minimal_line_and_writes_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload())
            self.assertEqual(out.strip(), b"Ctx 43%")
            d = json.loads((Path(tmp) / "sessions" / "abc-123.ctx.json").read_text())
            self.assertEqual(d["pct"], 43)
            self.assertIsInstance(d["at"], float)

    def test_with_no_delegate_and_no_context_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, '{"session_id": "abc-123"}')
            self.assertEqual(out.strip(), b"Ctx n/a")

    def test_with_a_delegate_its_stdout_goes_through_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            # printf with no trailing newline: whatever the delegate paints
            # arrives byte for byte, ANSI colours included.
            out = self._run(tmp, payload(), delegate=r"printf 'line1\033[0m'")
            self.assertEqual(out, b"line1\033[0m")

    def test_the_delegate_gets_the_same_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = payload()
            out = self._run(tmp, text, delegate="cat")
            self.assertEqual(out.decode(), text)

    def test_a_delegate_that_fails_neither_blows_up_nor_loses_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="exit 3")
            self.assertEqual(out, b"")
            d = json.loads((Path(tmp) / "sessions" / "abc-123.ctx.json").read_text())
            self.assertEqual(d["pct"], 43)

    def test_a_nan_does_not_steal_the_delegates_turn(self):
        # Writing the % down may fail; the user's status line may NOT. With a
        # payload that breaks the parsing, the delegate still has to run.
        with tempfile.TemporaryDirectory() as tmp:
            raw = ('{"session_id":"abc-123",'
                   '"context_window":{"used_percentage":NaN}}')
            out = self._run(tmp, raw, delegate="cat")
            self.assertEqual(out.decode(), raw)
            self.assertFalse((Path(tmp) / "sessions").exists())

    def test_if_the_note_blows_up_the_delegate_still_runs(self):
        # A structural safety net: even if record_ctx exploded with something
        # unexpected, the turn belongs to the delegate.
        with tempfile.TemporaryDirectory() as tmp:
            def bomb(*a, **k):
                raise RuntimeError("disk on fire")

            with mock.patch.object(tee, "record_ctx", bomb):
                out = self._run(tmp, payload(), delegate="printf ok")
            self.assertEqual(out, b"ok")

    def test_rubbish_on_stdin_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, "not json at all", delegate="cat")
            self.assertEqual(out, b"not json at all")
            self.assertFalse((Path(tmp) / "sessions").exists())


class TestStatuslineModes(_RunsTheTee, unittest.TestCase):
    """Who paints the line: `own`, `wrap` or `stack` (config `statusline.claude`).

    Whichever it is, the two things the rest of Flightdeck depends on are the
    same: the % is written down BEFORE anything is painted, and the 🧠 notice
    goes out AFTER.
    """

    OURS = "✳ Fable 5"   # the start of Flightdeck's own line for this payload

    def test_own_paints_flightdecks_line_and_ignores_the_delegate(self):
        # Three lines: the tee reads `statusline.lines` from the config on every
        # repaint, the way it reads the mode, and nothing in this config says
        # otherwise -- so it paints what a fresh install paints.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="own").decode()
            self.assertIn(self.OURS, out)
            self.assertIn("Ctx ", out)
            self.assertIn("43%", out)
            self.assertNotIn("DELEGATE", out)
            self.assertEqual(len(out.rstrip("\n").split("\n")), 3)

    def test_the_compressed_layout_reaches_the_tee_too(self):
        # `"statusline": {"lines": 2}` is the opt-out, and it has to work where
        # the line is actually painted: an edited config.json takes effect on
        # the next repaint, with nothing to restart.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own", lines=2).decode()
            self.assertIn(self.OURS, out)
            self.assertEqual(len(out.rstrip("\n").split("\n")), 2)

    def test_own_with_no_delegate_is_the_same_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own").decode()
            self.assertIn(self.OURS, out)
            # And NOT the minimal fallback, which only makes sense in `wrap`.
            self.assertNotIn("Ctx 43%\n", out)

    def test_wrap_is_the_delegate_and_only_the_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="wrap").decode()
            self.assertEqual(out, "DELEGATE")
            self.assertNotIn(self.OURS, out)

    def test_stack_is_ours_first_and_theirs_underneath(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="stack").decode()
            self.assertIn(self.OURS, out)
            self.assertTrue(out.endswith("DELEGATE"), out[-30:])
            # Ours ends in a newline of its own, so the delegate starts clean.
            self.assertEqual(out.split("\n")[-1], "DELEGATE")

    def test_stack_with_no_delegate_does_not_add_the_minimal_line(self):
        # It would be a second, poorer copy of the percentage we just painted.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="stack").decode()
            self.assertIn(self.OURS, out)
            self.assertNotIn("Ctx 43%\n", out)

    def test_a_mode_spelled_wrong_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="onw").decode()
            self.assertIn(self.OURS, out)   # the default is `own`
            self.assertEqual(tee.DEFAULT_MODE, "own")

    def test_the_context_note_is_written_in_every_mode(self):
        for mode in ("own", "wrap", "stack"):
            with tempfile.TemporaryDirectory() as tmp:
                self._run(tmp, payload(), delegate="printf x", mode=mode)
                d = json.loads((Path(tmp) / "sessions" / "abc-123.ctx.json").read_text())
                self.assertEqual(d["pct"], 43, mode)

    def test_the_notice_is_sung_in_every_mode(self):
        for mode in ("own", "wrap", "stack"):
            with tempfile.TemporaryDirectory() as tmp:
                self.notices = []
                self._run(tmp, payload(context_window={"used_percentage": 82}),
                          delegate="printf x", mode=mode)
                self.assertEqual(len(self.notices), 1, mode)
                self.assertIn("82%", self.notices[0])

    def test_a_broken_payload_in_own_mode_paints_nothing_and_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, "not json at all", mode="own")
            self.assertEqual(out, b"")

    def test_the_panes_width_reaches_our_line_through_the_tee(self):
        """Claude Code hands the width over in the ENVIRONMENT, not the payload.

        It exports `COLUMNS` from its own stdout to every command it launches
        (verified in the 2.1.274 binary: the status line goes through the same
        runner as the hooks), so the tee is already standing in it and our line
        trims itself without anybody passing a number down. `no branch` is the
        first rung of the ladder; the context bar never goes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            wide = self._run(tmp, payload(), mode="own", columns=400).decode()
            self.assertIn("no branch", wide)

        with tempfile.TemporaryDirectory() as tmp:
            # This payload's identity line is short (`✳ Fable 5 │ /tmp │ no
            # branch`, 28 cells), so the width that bites it is a narrow one.
            narrow = self._run(tmp, payload(), mode="own", columns=20).decode()
            self.assertNotIn("no branch", narrow)
            self.assertIn("Ctx ", narrow)

    def test_with_no_columns_the_tee_paints_the_whole_line(self):
        # Pinned: a Claude Code whose stdout is not a terminal exports no
        # COLUMNS, and then nothing is trimmed.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own").decode()
            self.assertIn("no branch", out)

    def test_the_account_segment_is_gone_when_it_is_switched_off(self):
        # The one key for a shared screen, read where the line is painted.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own", account=False).decode()
            self.assertIn(self.OURS, out)
            self.assertNotIn("👤", out)

    def test_switched_on_with_nothing_to_read_the_line_is_still_painted(self):
        """The one that matters: the sources are a file and a keychain item,
        and on a machine that has neither the bar may not go quiet.

        The suite's HOME is a sandbox with no `~/.claude.json` in it, so this
        is exactly that machine -- and because there is no address, the plan is
        never asked for and no keychain is touched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own", account=True).decode()
            self.assertIn(self.OURS, out)
            self.assertIn("43%", out)
            self.assertNotIn("👤", out)
            self.assertEqual(len(out.rstrip("\n").split("\n")), 3)

    def test_our_line_blowing_up_does_not_take_the_delegate_with_it(self):
        # In `stack` the user's line is still the sacred one.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("flightdeck.statusline.render_claude",
                            mock.Mock(side_effect=RuntimeError("boom"))):
                out = self._run(tmp, payload(), delegate="printf DELEGATE",
                                mode="stack")
            self.assertEqual(out, b"DELEGATE")


class TestDecideNotice(unittest.TestCase):
    """The pure decision: is it time to sing the 80% notice, and what state do we
    keep? With hysteresis: it announces on CROSSING upwards (>=80) and does not
    announce again until it has come down below 75 -- so a session dancing
    around 79-81 (a /compact that frees little) does not machine-gun you."""

    def test_crossing_the_threshold_announces_once(self):
        # 79 -> 82: the 79 keeps quiet, the 82 sings.
        self.assertEqual(tee.decide_notice(79, {"warned": False}),
                         (False, {"warned": False}))
        self.assertEqual(tee.decide_notice(82, {"warned": False}),
                         (True, {"warned": True}))

    def test_staying_above_does_not_re_announce(self):
        # 82 -> 85: it was already warned; going higher is not news.
        self.assertEqual(tee.decide_notice(85, {"warned": True}),
                         (False, {"warned": True}))

    def test_dropping_below_the_rearm_disarms_the_notice(self):
        # 85 -> 70: a real /compact. It rearms, but does NOT announce on the way
        # down.
        self.assertEqual(tee.decide_notice(70, {"warned": True}),
                         (False, {"warned": False}))

    def test_after_rearming_it_announces_again(self):
        # 70 -> 81: second crossing, second notice.
        self.assertEqual(tee.decide_notice(81, {"warned": False}),
                         (True, {"warned": True}))

    def test_the_dead_band_does_not_rearm(self):
        # Between 75 and 79 (warned) it does not rearm: that is exactly the zone
        # where a breathing context would cross 80 again within two renders.
        for pct in (75, 76, 79):
            self.assertEqual(tee.decide_notice(pct, {"warned": True}),
                             (False, {"warned": True}), pct)

    def test_the_exact_edges(self):
        # 80 announces (>=); 74 rearms (<75).
        self.assertEqual(tee.decide_notice(80, {"warned": False}),
                         (True, {"warned": True}))
        self.assertEqual(tee.decide_notice(74, {"warned": True}),
                         (False, {"warned": False}))
        self.assertEqual(tee.decide_notice(tee.CTX_WARN_TEE, {"warned": False})[0],
                         True)
        self.assertEqual(tee.decide_notice(tee.CTX_REARM, {"warned": True}),
                         (False, {"warned": True}))

    def test_with_no_previous_state_it_is_as_if_not_warned(self):
        # A newborn card (or a .ctx.json with no "warned"): the first crossing
        # announces.
        for previous in (None, {}, {"pct": 81, "at": 1.0}):
            self.assertEqual(tee.decide_notice(81, previous),
                             (True, {"warned": True}))

    def test_a_warned_that_is_not_true_counts_as_not_warned(self):
        # A deliberate bias: faced with an odd value we prefer one notice too
        # many (which corrects itself when the good state is written) over
        # silence for ever.
        for rubbish in ("yes", 1, "true", None):
            self.assertEqual(tee.decide_notice(81, {"warned": rubbish}),
                             (True, {"warned": True}), rubbish)

    def test_an_unknown_pct_neither_announces_nor_touches_the_state(self):
        # With no number there is no decision to make: the state comes out
        # EXACTLY as it went in.
        previous = {"warned": True}
        announce, new = tee.decide_notice(None, previous)
        self.assertFalse(announce)
        self.assertIs(new, previous)
        self.assertEqual(tee.decide_notice(None, None), (False, None))

    def test_a_pct_that_is_not_a_number_does_not_announce(self):
        previous = {"warned": False}
        for rubbish in ("82", object(), [82]):
            announce, new = tee.decide_notice(rubbish, previous)
            self.assertFalse(announce, rubbish)
            self.assertIs(new, previous, rubbish)


class TestTheThresholdsComeFromTheConfig(unittest.TestCase):
    """The two thresholds are `context_warn_pct` / `context_rearm_pct`, read
    when the decision is made so an edited config.json takes effect without
    restarting anything. Same treatment as `statusbar`: the constants stay as
    the FALLBACK."""

    def _with_config(self, data):
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(data))
        return mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)})

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_the_fallbacks_are_the_configs_defaults(self):
        self.assertEqual(tee.CTX_WARN_TEE, config.DEFAULTS["context_warn_pct"])
        self.assertEqual(tee.CTX_REARM, config.DEFAULTS["context_rearm_pct"])

    def test_a_configured_threshold_moves_the_crossing(self):
        with self._with_config({"context_warn_pct": 60, "context_rearm_pct": 50}):
            self.assertEqual(tee.decide_notice(61, {"warned": False}),
                             (True, {"warned": True}))
            self.assertEqual(tee.decide_notice(55, {"warned": True}),
                             (False, {"warned": True}))
            self.assertEqual(tee.decide_notice(49, {"warned": True}),
                             (False, {"warned": False}))

    def test_an_unusable_threshold_falls_back_to_the_default(self):
        """A `context_warn_pct` typed as a word must not leave the session
        without its handover warning: `doctor` complains about the value."""
        with self._with_config({"context_warn_pct": "eighty",
                                "context_rearm_pct": None}):
            self.assertEqual(tee.decide_notice(80, {"warned": False}),
                             (True, {"warned": True}))
            self.assertEqual(tee.decide_notice(74, {"warned": True}),
                             (False, {"warned": False}))


class TestNameForNotice(unittest.TestCase):
    """How the session is named in the notice: first the way the user sees it
    (the tmux session name from its card), then the "project" of that same card,
    then the folder, and last the bare id."""

    def test_the_tmux_session_name_wins(self):
        """The floating notice and the status bar have to call it the SAME.

        The bar and the green rows paint the tmux session name, so a notice that
        said the project ("🧠 payments at 82%") would force the reader to
        work out which of their windows that is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "abc-123.json").write_text(json.dumps(
                {"tmux_session": "pricing", "project": "payments"}))
            self.assertEqual(
                tee.name_for_notice("abc-123", cwd="/x/other", sess_dir=tmp),
                "pricing")

    def test_it_uses_the_cards_project(self):
        # With no tmux session name (an old card, or an agent outside tmux) the
        # project wins.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "abc-123.json").write_text(
                json.dumps({"project": "payments"}))
            self.assertEqual(
                tee.name_for_notice("abc-123", cwd="/x/other", sess_dir=tmp),
                "payments")

    def test_an_empty_session_name_does_not_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "abc-123.json").write_text(json.dumps(
                {"tmux_session": "   ", "project": "payments"}))
            self.assertEqual(
                tee.name_for_notice("abc-123", cwd="/x/other", sess_dir=tmp),
                "payments")

    def test_with_no_card_it_falls_back_to_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                tee.name_for_notice("abc-123", cwd="/Users/j/proj/payments",
                                    sess_dir=tmp), "payments")

    def test_a_broken_card_or_one_without_a_project_falls_back_to_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "abc-123.json").write_text("{broken")
            (Path(tmp) / "def-456.json").write_text(json.dumps({"state": "working"}))
            for sid in ("abc-123", "def-456"):
                self.assertEqual(
                    tee.name_for_notice(sid, cwd="/Users/j/proj/payments",
                                        sess_dir=tmp), "payments", sid)

    def test_a_dangerous_id_does_not_read_outside_the_sessions_directory(self):
        """`parse_ctx` already filters these ids, but this function cannot trust
        its caller: the id ends up being a file name.

        The set-up puts a PERFECTLY valid card exactly where the escape would
        land (`sessions/../secret.json`), so if the id filter disappeared the
        read would work and the notice would say "secret" instead of the folder.
        Without that bait the test would pass with no filter at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp)
            sess = outside / "sessions"
            sess.mkdir()
            (outside / "secret.json").write_text(json.dumps(
                {"tmux_session": "secret", "project": "secret"}))
            self.assertEqual(
                tee.name_for_notice("../secret", cwd="/Users/j/proj/payments",
                                    sess_dir=sess), "payments")
            # And the classic one, in case the filter let absolute paths through.
            self.assertEqual(
                tee.name_for_notice("../../etc/passwd",
                                    cwd="/Users/j/proj/payments",
                                    sess_dir=sess), "payments")

    def test_with_nothing_else_it_uses_the_start_of_the_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tee.name_for_notice("abcdef1234567890", cwd=None,
                                                 sess_dir=tmp), "abcdef12")
            self.assertEqual(tee.name_for_notice("abcdef1234567890", cwd="",
                                                 sess_dir=tmp), "abcdef12")
            # A cwd that is the root has no folder name.
            self.assertEqual(tee.name_for_notice("abcdef1234567890", cwd="/",
                                                 sess_dir=tmp), "abcdef12")

    def test_with_no_usable_id_it_still_returns_a_name(self):
        # The whole tee runs inside a try that swallows: a TypeError here would
        # come out as "this session never announces", which is the failure you
        # do not see.
        with tempfile.TemporaryDirectory() as tmp:
            for sid in (None, "", 0):
                with self.subTest(sid=sid):
                    self.assertEqual(
                        tee.name_for_notice(sid, cwd=None, sess_dir=tmp),
                        "session")
            self.assertEqual(tee.name_for_notice(1234567890123, cwd=None,
                                                 sess_dir=tmp), "12345678")


class _R:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class _FakeSubprocess:
    """A made-up subprocess: it records the tmux calls and answers what it is
    told to, so the real tmux is never touched."""

    def __init__(self, clients="/dev/ttys001\n/dev/ttys002\n", rc=0,
                 list_blows_up=False, display_blows_up=False):
        self.clients, self.rc = clients, rc
        self.list_blows_up, self.display_blows_up = list_blows_up, display_blows_up
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if "list-clients" in cmd:
            if self.list_blows_up:
                raise OSError("no tmux server")
            return _R(self.rc, self.clients)
        if self.display_blows_up:
            raise OSError("a client that left")
        return _R(0, "")

    @property
    def displays(self):
        return [c for c, _ in self.calls if "display-message" in c]


class TestNotifyTmux(unittest.TestCase):
    """Sending the notice to the status bar of EVERY tmux client."""

    def _with(self, fake, text="hello", env=None, version=(3, 6)):
        """`notify_tmux` against a made-up tmux of the version asked for.

        The version is pinned rather than read off the machine: the argv has to
        come out the same here, on Ubuntu 22.04's 3.2a and in CI.
        """
        with mock.patch.object(tee, "subprocess", fake), \
                mock.patch.object(tee, "tmux_version", lambda: version), \
                mock.patch.dict(os.environ,
                                env or {"FLIGHTDECK_TMUX_SOCKET": ""}):
            tee.notify_tmux(text)
        return fake

    def test_one_literal_display_message_per_client(self):
        f = self._with(_FakeSubprocess())
        self.assertEqual(len(f.displays), 2)
        for cmd, kw in f.calls:
            self.assertEqual(kw.get("timeout"), 2)  # no tmux may hang us
        # `-d NOTICE_MS`: without it tmux uses its factory display-time (750 ms:
        # a flash). `-C`: without it the PANE freezes in that client while the
        # notice is up (measured in 3.6a; at 750 ms you do not notice, at 5 s you
        # do). `-l`: literal.
        self.assertEqual(f.displays[0],
                         ["tmux", "display-message", "-c", "/dev/ttys001",
                          "-d", str(tee.NOTICE_MS), "-C", "-l", "hello"])
        self.assertEqual(f.displays[1][3], "/dev/ttys002")

    def test_the_notice_lasts_long_enough_to_read_it(self):
        self.assertGreaterEqual(tee.NOTICE_MS, 3000)

    def test_it_respects_the_test_socket(self):
        f = self._with(_FakeSubprocess(),
                       env={"FLIGHTDECK_TMUX_SOCKET": "handovertest"})
        for cmd, _ in f.calls:
            self.assertEqual(cmd[:3], ["tmux", "-L", "handovertest"])

    def test_with_no_tmux_it_says_nothing(self):
        # tmux off: `list-clients` fails and that is the end of it.
        self.assertEqual(self._with(_FakeSubprocess(list_blows_up=True)).displays, [])
        self.assertEqual(self._with(_FakeSubprocess(rc=1, clients="")).displays, [])

    def test_with_no_clients_connected_there_is_no_display(self):
        self.assertEqual(self._with(_FakeSubprocess(clients="\n")).displays, [])

    def test_a_client_that_fails_does_not_take_the_others_with_it(self):
        f = self._with(_FakeSubprocess(display_blows_up=True))
        self.assertEqual(len(f.displays), 2)  # it tried both

    def test_on_tmux_3_2_it_escapes_the_text_instead_of_passing_l(self):
        # `display-message -l` is tmux >= 3.4 (Ubuntu 22.04 ships 3.2a, Debian 12
        # 3.3a). Without the flag the message is read as a FORMAT, so a session
        # called `#(something)` would be run: the `#` is doubled instead.
        f = self._with(_FakeSubprocess(), text="🧠 #(rm -rf) at 83%",
                       version=(3, 2))
        self.assertEqual(f.displays[0],
                         ["tmux", "display-message", "-c", "/dev/ttys001",
                          "-d", str(tee.NOTICE_MS), "-C",
                          "🧠 ##(rm -rf) at 83%"])

    def test_a_tmux_it_could_not_read_gets_the_safe_form(self):
        f = self._with(_FakeSubprocess(), text="🧠 a#b at 83%", version=None)
        self.assertEqual(f.displays[0][-2:], ["-C", "🧠 a##b at 83%"])

    def test_with_no_clients_it_does_not_even_ask_tmux_its_version(self):
        """Nobody to tell = no `tmux -V`: this runs on every repaint of every
        session, so a subprocess nobody reads is worth avoiding."""
        asked = []
        with mock.patch.object(tee, "subprocess", _FakeSubprocess(clients="\n")), \
                mock.patch.object(tee, "tmux_version",
                                  lambda: asked.append(1) or (3, 6)), \
                mock.patch.dict(os.environ, {"FLIGHTDECK_TMUX_SOCKET": ""}):
            tee.notify_tmux("hello")
        self.assertEqual(asked, [])


class TestTheTeeNoticeDuration(unittest.TestCase):
    """The third part of the cross-check between the three notice emitters. The
    hook's is in `test_session_hook`, the handover's in `test_handover`."""

    def test_a_configured_duration_reaches_display_message(self):
        fake = _FakeSubprocess()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"notice_ms": 8000}))
            with mock.patch.object(tee, "subprocess", fake), \
                    mock.patch.object(tee, "tmux_version", lambda: (3, 6)), \
                    mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
                tee.notify_tmux("hello")
        display = fake.displays[0]
        self.assertEqual(display[display.index("-d") + 1], "8000")

    def test_an_unusable_duration_falls_back(self):
        fake = _FakeSubprocess()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"notice_ms": "five seconds"}))
            with mock.patch.object(tee, "subprocess", fake), \
                    mock.patch.object(tee, "tmux_version", lambda: (3, 6)), \
                    mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
                tee.notify_tmux("hello")
        display = fake.displays[0]
        self.assertEqual(display[display.index("-d") + 1], str(tee.NOTICE_MS))


class TestTheDirectoriesComeFromTheConfig(unittest.TestCase):
    """Both paths come from the state directory rather than from the script's
    own directory, and the delegate keeps the one name the installer
    writes."""

    def test_the_notes_live_next_to_the_session_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}):
                self.assertEqual(tee._sessions_dir(), Path(tmp) / "sessions")

    def test_the_delegate_is_the_statusline_one_under_delegates(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}):
                self.assertEqual(tee._delegate_file(),
                                 Path(tmp) / "delegates" / "statusline")


class TestABrokenPackageStillPaints(unittest.TestCase):
    """The status line is the sacred part, even when we are the broken part.

    Once the tee is installed, Claude Code's status line IS this script: a
    `flightdeck` package that cannot be imported (a half-written file, an
    update caught halfway) would otherwise leave every open session with a
    blank bar and a traceback under it. What still has to happen is the user's
    own line being painted -- or, with no line of their own, the minimal
    `Ctx NN%` read straight off the payload -- and exit 0.

    Run as a subprocess against a copy of the script whose package really does
    raise on import; a flag the code could recognise would not prove anything.
    """

    TEE = config.code_dir() / "flightdeck" / "hooks" / "context_tee.py"

    def _run(self, tmp, stdin_text, delegate=None):
        if delegate is not None:
            delegates = Path(tmp) / "delegates"
            delegates.mkdir(parents=True, exist_ok=True)
            (delegates / "statusline").write_text(delegate + "\n")
        script = broken_package_copy(Path(tmp) / "broken", self.TEE)
        env = {k: v for k, v in os.environ.items()
               if k not in ("FLIGHTDECK_STATE_DIR", "FLIGHTDECK_CONFIG",
                            "FLIGHTDECK_TMUX_SOCKET", "HOME", "PYTHONPATH",
                            "TMUX", "TMUX_PANE")}
        env["FLIGHTDECK_STATE_DIR"] = str(tmp)
        env["FLIGHTDECK_TMUX_SOCKET"] = "flightdeck-teetest-nonexistent"
        env["HOME"] = SUBPROCESS_HOME
        r = subprocess.run([sys.executable, str(script)], input=stdin_text,
                           capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_the_users_own_line_is_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, payload(), delegate="printf 'my own line'")
            self.assertEqual(r.stdout, "my own line")

    def test_with_no_line_of_their_own_the_minimal_one_is_painted(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, payload())
            self.assertEqual(r.stdout.strip(), "Ctx 43%")

    def test_without_a_percentage_the_minimal_line_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, '{"session_id": "abc-123"}')
            self.assertEqual(r.stdout.strip(), "Ctx n/a")

    def test_nothing_of_ours_is_written_and_no_traceback_is_printed(self):
        # The note and the 🧠 notice are extras owned by the very code that is
        # missing. Half a note is worse than none: `read_ctx_pct` would hand the
        # menu a percentage nobody can vouch for.
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, payload(), delegate="printf x")
            self.assertEqual(r.stderr, "")
            self.assertFalse((Path(tmp) / "sessions").exists())


class TestMainNotice(_RunsTheTee, unittest.TestCase):
    """The notice assembled inside the tee: the real sequence of a context that
    fills up, gets compacted and fills up again."""

    def _payload(self, pct, cwd="/Users/j/proj/payments"):
        return payload(context_window={"used_percentage": pct}, cwd=cwd)

    def test_the_whole_sequence_only_announces_on_the_crossings(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = Path(tmp) / "sessions"
            for pct in (79, 82, 85, 70, 81):
                self._run(tmp, self._payload(pct), delegate="printf x",
                          sess_dir=sess)
            self.assertEqual(self.notices, [
                "🧠 payments at 82% — start thinking about a handover",
                "🧠 payments at 81% — start thinking about a handover",
            ])

    def test_the_state_is_saved_next_to_the_pct(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = Path(tmp) / "sessions"
            note = sess / "abc-123.ctx.json"
            self._run(tmp, self._payload(82), delegate="printf x", sess_dir=sess)
            d = json.loads(note.read_text())
            self.assertEqual((d["pct"], d["warned"]), (82, True))
            self._run(tmp, self._payload(70), delegate="printf x", sess_dir=sess)
            d = json.loads(note.read_text())
            self.assertEqual((d["pct"], d["warned"]), (70, False))

    def test_it_uses_the_name_from_the_hooks_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = Path(tmp) / "sessions"
            sess.mkdir()
            (sess / "abc-123.json").write_text(json.dumps({"project": "flightdeck"}))
            self._run(tmp, self._payload(90), delegate="printf x", sess_dir=sess)
            self.assertEqual(
                self.notices,
                ["🧠 flightdeck at 90% — start thinking about a handover"])

    def test_with_no_cwd_and_no_card_it_uses_the_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.dumps({"session_id": "abcdef1234567890",
                              "context_window": {"used_percentage": 91}})
            self._run(tmp, raw, delegate="printf x")
            self.assertEqual(
                self.notices,
                ["🧠 abcdef12 at 91% — start thinking about a handover"])

    def test_if_writing_the_state_fails_nothing_is_announced(self):
        # Writing the % works but writing the state does not: with no "warned"
        # saved the notice would come out on EVERY repaint, so this render keeps
        # quiet.
        with tempfile.TemporaryDirectory() as tmp:
            original = tee.record_ctx
            writes = []

            def half_way(*a, **k):
                writes.append(k.get("extra"))
                return None if k.get("extra") else original(*a, **k)

            with mock.patch.object(tee, "record_ctx", half_way):
                out = self._run(tmp, self._payload(82), delegate="printf ok")
            self.assertEqual(writes, [None, {"warned": True}])
            self.assertEqual(self.notices, [])
            self.assertEqual(out, b"ok")

    def test_if_nothing_can_be_written_nothing_is_announced(self):
        # Not even the % could be written (a file where sessions/ should be makes
        # the mkdir fail): there is no notice either.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("I am not a directory")
            out = self._run(tmp, self._payload(82), delegate="printf ok",
                            sess_dir=blocker / "sessions")
            self.assertEqual(self.notices, [])
            self.assertEqual(out, b"ok")  # and the status line paints the same

    def test_a_tmux_that_blows_up_does_not_take_the_tee_down(self):
        # The status line is already painted when the notice is sent, but a tmux
        # that explodes cannot leave a traceback in the bar either.
        with tempfile.TemporaryDirectory() as tmp:
            def bomb(text):
                raise RuntimeError("tmux on fire")

            out = self._run(tmp, self._payload(82), delegate="printf ok",
                            on_notice=bomb)
            self.assertEqual(out, b"ok")

    def test_the_notice_is_sent_AFTER_painting_the_status_line(self):
        # What is sacred is the user's bar: `display-message` (up to 2 s per
        # client if tmux is stuck) cannot go in front of the render.
        with tempfile.TemporaryDirectory() as tmp:
            painted_when_announcing = []
            self._run(tmp, self._payload(82), delegate="printf ok",
                      on_notice=lambda t: painted_when_announcing.append(
                          sys.stdout.buffer.getvalue()))
            self.assertEqual(painted_when_announcing, [b"ok"])

    def test_with_no_delegate_it_also_announces_and_after_painting(self):
        with tempfile.TemporaryDirectory() as tmp:
            painted_when_announcing = []
            out = self._run(tmp, self._payload(82),
                            on_notice=lambda t: painted_when_announcing.append(
                                (sys.stdout.buffer.getvalue(), t)))
            self.assertEqual(out.strip(), b"Ctx 82%")
            self.assertEqual(
                painted_when_announcing,
                [(b"Ctx 82%\n",
                  "🧠 payments at 82% — start thinking about a handover")])

    def test_a_delegate_that_fails_does_not_take_the_notice_with_it(self):
        # The notice no longer depends on the delegate: they are two separate
        # things.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, self._payload(82), delegate="exit 3")
            self.assertEqual(out, b"")
            self.assertEqual(
                self.notices,
                ["🧠 payments at 82% — start thinking about a handover"])

    def test_below_the_threshold_it_never_announces(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pct in (0, 43, 79):
                self._run(tmp, self._payload(pct), delegate="printf x")
            self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
