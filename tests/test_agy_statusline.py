"""agy's status line: the same tee, in front of Antigravity's own line.

Measured: agy feeds its `/statusline <command>` a JSON on stdin shaped almost
exactly like Claude Code's, `context_window.used_percentage` included. So this
hook is the claude tee with three differences, and each of them has a test here:

- the mode it obeys is `statusline.agy`,
- the command it delegates to is saved in `delegates/agy-statusline`,
- `stack` paints OURS ALONE: agy stacks its own default line underneath by
  itself when the installer writes `stack_with_default: true`.

Nothing here reaches the real tmux, the real state directory or the real agy:
`FLIGHTDECK_STATE_DIR` and `FLIGHTDECK_CONFIG` point into a temporary directory
and the notices are collected instead of displayed.
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
from flightdeck import common, config
from flightdeck.hooks import agy_statusline as agy_tee
from flightdeck.hooks import context_tee as tee

FIXTURE = Path(__file__).parent / "fixtures" / "agy_statusline.json"
SESSION = "11111111-2222-3333-4444-555555555555"


def payload(**extra):
    """The measured payload, with whatever a test wants to change."""
    d = json.loads(FIXTURE.read_text())
    for key, value in extra.items():
        d[key] = value
    return json.dumps(d)


def with_context(**fields):
    """The measured payload with its context_window replaced."""
    d = json.loads(FIXTURE.read_text())
    d["context_window"] = fields
    return json.dumps(d)


class _FakeStd:
    """A made-up stdin/stdout: it exposes .buffer (bytes) like the real ones."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)

    def write(self, text):
        self.buffer.write(text.encode())

    def flush(self):
        pass


class _RunsTheAgyTee:
    """Runs the hook end to end over a temporary state directory."""

    def _run(self, tmp, stdin_text, delegate=None, mode="own", lines=None):
        state = Path(tmp) / "state"
        (state / "delegates").mkdir(parents=True, exist_ok=True)
        if delegate is not None:
            (state / "delegates" / "agy-statusline").write_text(delegate + "\n")
        cfg = Path(tmp) / "config.json"
        section = {"agy": mode}
        if lines is not None:
            section["lines"] = lines
        cfg.write_text(json.dumps({"statusline": section}))
        stdin, stdout = _FakeStd(stdin_text.encode()), _FakeStd()
        if not hasattr(self, "notices"):
            self.notices = []
        env = {"FLIGHTDECK_CONFIG": str(cfg), "FLIGHTDECK_STATE_DIR": str(state),
               "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(tee, "notify_tmux",
                                  lambda text: self.notices.append(text)), \
                mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout):
            agy_tee.main()
        return stdout.buffer.getvalue()

    def note(self, tmp, session=SESSION):
        path = Path(tmp) / "state" / "sessions" / ("%s.ctx.json" % session)
        return json.loads(path.read_text())


class TestParseCtx(unittest.TestCase):

    def test_the_measured_payload_gives_the_id_and_the_rounded_percentage(self):
        # 2.447509765625 -> 2, and the id is the conversation's uuid.
        self.assertEqual(agy_tee.parse_ctx(payload()),
                         {"session_id": SESSION, "pct": 2})

    def test_it_falls_back_to_remaining_percentage(self):
        # agy sends both halves; only `used_percentage` was seen missing in no
        # payload at all, so this is insurance, not a measurement.
        raw = with_context(remaining_percentage=32.4)
        self.assertEqual(agy_tee.parse_ctx(raw), {"session_id": SESSION, "pct": 68})

    def test_used_percentage_wins_over_remaining(self):
        raw = with_context(used_percentage=10, remaining_percentage=50)
        self.assertEqual(agy_tee.parse_ctx(raw)["pct"], 10)

    def test_without_any_figure_it_is_none(self):
        self.assertIsNone(agy_tee.parse_ctx(with_context(context_window_size=1)))
        self.assertIsNone(agy_tee.parse_ctx(with_context()))

    def test_rubbish_is_none(self):
        for raw in ("", "   ", "{not json", "[1,2,3]", "null",
                    '{"context_window":{"used_percentage":5}}',   # no id
                    '{"session_id":"../../escape","context_window":'
                    '{"used_percentage":5}}',
                    '{"session_id":"x","context_window":'
                    '{"remaining_percentage":"most of it"}}'):
            self.assertIsNone(agy_tee.parse_ctx(raw), raw)


class TestModes(_RunsTheAgyTee, unittest.TestCase):

    OURS = "✦ Gemini 3.8 Flash (High)"

    def test_own_paints_flightdecks_line_and_ignores_the_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="own").decode()
            self.assertIn(self.OURS, out)
            self.assertIn("Ctx ", out)
            self.assertNotIn("DELEGATE", out)
            # The three-line layout, which is what the config in force says.
            self.assertEqual(len(out.rstrip("\n").split("\n")), 3)

    def test_the_compressed_layout_reaches_agys_tee_too(self):
        # One `statusline.lines` for both tools: an opt-out that answered in
        # claude's line and not in agy's would read as a bug in agy's.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="own", lines=2).decode()
            self.assertIn(self.OURS, out)
            self.assertEqual(len(out.rstrip("\n").split("\n")), 2)

    def test_wrap_is_the_delegate_and_only_the_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="wrap").decode()
            self.assertEqual(out, "DELEGATE")

    def test_the_delegate_gets_the_same_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = payload()
            self.assertEqual(self._run(tmp, text, delegate="cat",
                                       mode="wrap").decode(), text)

    def test_wrap_with_no_delegate_still_paints_the_minimal_line(self):
        # The delegates directory is disposable state; if that file goes, a
        # blank line under the pane would be a silent, undiagnosable downgrade.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), mode="wrap").decode()
            self.assertEqual(out.strip(), "Ctx 2%")

    def test_stack_paints_ours_alone(self):
        # THE difference with claude's tee: there, `stack` runs the delegate
        # underneath. Here agy stacks its OWN default line by itself, because
        # the installer wrote `stack_with_default: true` -- running the delegate
        # as well would paint a third line nobody asked for.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="stack").decode()
            self.assertIn(self.OURS, out)
            self.assertNotIn("DELEGATE", out)

    def test_a_mode_spelled_wrong_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="printf DELEGATE",
                            mode="onw").decode()
            self.assertIn(self.OURS, out)
        self.assertEqual(agy_tee.DEFAULT_MODE, config.DEFAULTS["statusline"]["agy"])

    def test_it_reads_agys_key_and_not_claudes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            state = Path(tmp) / "state"
            state.mkdir()
            cfg.write_text(json.dumps({"statusline": {"claude": "wrap",
                                                      "agy": "own"}}))
            with mock.patch.dict(os.environ,
                                 {"FLIGHTDECK_CONFIG": str(cfg),
                                  "FLIGHTDECK_STATE_DIR": str(state)}):
                self.assertEqual(agy_tee.statusline_mode(), "own")

    def test_the_note_is_written_in_every_mode(self):
        for mode in ("own", "wrap", "stack"):
            with tempfile.TemporaryDirectory() as tmp:
                self._run(tmp, payload(), delegate="printf x", mode=mode)
                self.assertEqual(self.note(tmp)["pct"], 2, mode)


class TestTheNote(_RunsTheAgyTee, unittest.TestCase):

    def test_the_note_is_the_one_the_menu_reads(self):
        # The whole point of this hook: 🧠 for agy, which its transcript could
        # never give (measured: not a token anywhere in it).
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, with_context(used_percentage=64))
            state = Path(tmp) / "state"
            with mock.patch.dict(os.environ,
                                 {"FLIGHTDECK_STATE_DIR": str(state)}):
                self.assertEqual(common.read_ctx_pct(SESSION), 64)

    def test_it_is_written_before_anything_is_painted(self):
        # Same rule as the claude tee: a status line that blows up must not
        # take the note with it.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("flightdeck.statusline.render_agy",
                            mock.Mock(side_effect=RuntimeError("boom"))):
                out = self._run(tmp, payload(), mode="own")
            self.assertEqual(out, b"")
            self.assertEqual(self.note(tmp)["pct"], 2)

    def test_it_does_not_overwrite_the_notice_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "state" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / ("%s.ctx.json" % SESSION)).write_text(
                json.dumps({"pct": 90, "at": 0, "warned": True}))
            self._run(tmp, with_context(used_percentage=77))
            note = self.note(tmp)
            self.assertEqual(note["pct"], 77)
            self.assertIs(note["warned"], True)   # still above the rearm (75)


class TestTheNotice(_RunsTheAgyTee, unittest.TestCase):

    def test_crossing_80_sings_once_naming_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "state" / "sessions"
            sessions.mkdir(parents=True)
            # The card agy's state hook writes, keyed by the conversation id.
            (sessions / ("%s.json" % SESSION)).write_text(
                json.dumps({"session_id": SESSION, "tool": "agy",
                            "tmux_session": "agy work 3"}))
            self.notices = []
            self._run(tmp, with_context(used_percentage=83))
            self.assertEqual(len(self.notices), 1)
            self.assertIn("agy work 3", self.notices[0])
            self.assertIn("83%", self.notices[0])
            self.assertIs(self.note(tmp)["warned"], True)
            # And not again while it stays up there.
            self._run(tmp, with_context(used_percentage=85))
            self.assertEqual(len(self.notices), 1)

    def test_below_the_threshold_it_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.notices = []
            self._run(tmp, payload())
            self.assertEqual(self.notices, [])

    def test_the_notice_goes_out_after_the_line_is_painted(self):
        order = []

        def note_it(text):
            order.append("notice")

        with tempfile.TemporaryDirectory() as tmp:
            self.notices = []
            state = Path(tmp) / "state"
            (state / "delegates").mkdir(parents=True)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"statusline": {"agy": "own"}}))
            stdout = _FakeStd()
            stdout.write = lambda text: order.append("paint")
            env = {"FLIGHTDECK_CONFIG": str(cfg),
                   "FLIGHTDECK_STATE_DIR": str(state)}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(tee, "notify_tmux", note_it), \
                    mock.patch.object(sys, "stdin",
                                      _FakeStd(with_context(
                                          used_percentage=83).encode())), \
                    mock.patch.object(sys, "stdout", stdout):
                agy_tee.main()
        self.assertEqual(order[-1], "notice")
        self.assertIn("paint", order)


class TestTheNoticeOnAnOldTmux(unittest.TestCase):
    """agy's 🧠 notice is sent by the claude tee's `notify_tmux`, so it inherits
    the same degrade: `display-message -l` is tmux >= 3.4, and below that the
    text is escaped (`#` -> `##`) so a session name with a `#(...)` in it is
    never run as a format. This pins that agy really does travel that road --
    the notice its own tests collect never gets as far as an argv."""

    class _FakeSubprocess:
        """A made-up tmux: one client, and it records every argv."""

        def __init__(self):
            self.calls = []

        def run(self, cmd, **kw):
            self.calls.append(list(cmd))
            out = "/dev/ttys001\n" if "list-clients" in cmd else ""
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    def _displays(self, version, session_name):
        fake = self._FakeSubprocess()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            (state / "sessions").mkdir(parents=True)
            (state / "delegates").mkdir(parents=True)
            # The card agy's state hook leaves: it is where the notice gets the
            # name it calls the session by.
            (state / "sessions" / ("%s.json" % SESSION)).write_text(json.dumps(
                {"session_id": SESSION, "tool": "agy",
                 "tmux_session": session_name}))
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"statusline": {"agy": "own"}}))
            env = {"FLIGHTDECK_CONFIG": str(cfg),
                   "FLIGHTDECK_STATE_DIR": str(state),
                   "FLIGHTDECK_TMUX_SOCKET": ""}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(tee, "subprocess", fake), \
                    mock.patch.object(tee, "tmux_version", lambda: version), \
                    mock.patch.object(sys, "stdin",
                                      _FakeStd(with_context(
                                          used_percentage=83).encode())), \
                    mock.patch.object(sys, "stdout", _FakeStd()):
                agy_tee.main()
        return [c for c in fake.calls if "display-message" in c]

    def test_on_3_2_the_hashes_are_doubled_and_l_is_not_passed(self):
        display = self._displays((3, 2), "agy #(rm -rf) 3")[0]
        self.assertNotIn("-l", display)
        self.assertEqual(display[-1],
                         "🧠 agy ##(rm -rf) 3 at 83% — start thinking about a "
                         "handover")

    def test_on_3_6_it_travels_literally(self):
        display = self._displays((3, 6), "agy #(rm -rf) 3")[0]
        self.assertEqual(display[-2:],
                         ["-l", "🧠 agy #(rm -rf) 3 at 83% — start thinking "
                          "about a handover"])


class TestItNeverGetsInTheWay(_RunsTheAgyTee, unittest.TestCase):
    """agy runs this on every repaint: whatever happens, exit 0 and no traceback."""

    def test_rubbish_on_stdin_paints_nothing_and_does_not_blow_up(self):
        for raw in ("", "   ", "not json at all", "[1,2,3]", "null"):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(self._run(tmp, raw, mode="own"), b"", raw)

    def test_an_empty_object_paints_the_skeleton_rather_than_nothing(self):
        # A payload with no keys is agy's bug, not ours: the line degrades to
        # what it can say (same as the claude tee) instead of going blank.
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, "{}", mode="own").decode()
            self.assertIn("Ctx", out)
            self.assertFalse((Path(tmp) / "state" / "sessions").exists())

    def test_a_delegate_that_fails_neither_blows_up_nor_loses_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(tmp, payload(), delegate="exit 3", mode="wrap")
            self.assertEqual(out, b"")
            self.assertEqual(self.note(tmp)["pct"], 2)

    def test_a_tmux_that_blows_up_does_not_take_the_hook_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"statusline": {"agy": "own"}}))
            env = {"FLIGHTDECK_CONFIG": str(cfg),
                   "FLIGHTDECK_STATE_DIR": str(state)}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(tee, "notify_tmux",
                                      mock.Mock(side_effect=OSError("no tmux"))), \
                    mock.patch.object(sys, "stdin",
                                      _FakeStd(with_context(
                                          used_percentage=83).encode())), \
                    mock.patch.object(sys, "stdout", _FakeStd()):
                agy_tee.main()   # must simply return

    def test_the_script_exits_0_whatever_happens(self):
        # The module's guard, the one that makes the contract true from the
        # command line and not only from a test calling main().
        source = (config.code_dir() / "flightdeck" / "hooks"
                  / "agy_statusline.py").read_text()
        self.assertIn("sys.exit(0)", source)


class TestABrokenPackageStillPaints(unittest.TestCase):
    """The status line is the sacred part, even when we are the broken part.

    Once this hook is installed, agy's status line IS this script: a
    `flightdeck` package that cannot be imported -- a half-written file, an
    `update` caught halfway -- would otherwise leave every open agy with a blank
    line and a traceback under it, INCLUDING the user's own delegated line,
    which is the precise harm the claude tee's guard exists to prevent.
    Measured before the fix: rc 1, nothing painted.

    Same shape as `test_context_tee.TestABrokenPackageStillPaints`: a
    subprocess against a copy of the script whose package really raises on
    import, so there is no flag the code could recognise and be gentle about.
    """

    HOOK = config.code_dir() / "flightdeck" / "hooks" / "agy_statusline.py"

    def _run(self, tmp, stdin_text, delegate=None):
        if delegate is not None:
            delegates = Path(tmp) / "delegates"
            delegates.mkdir(parents=True, exist_ok=True)
            (delegates / "agy-statusline").write_text(delegate + "\n")
        script = broken_package_copy(Path(tmp) / "broken", self.HOOK)
        env = {k: v for k, v in os.environ.items()
               if k not in ("FLIGHTDECK_STATE_DIR", "FLIGHTDECK_CONFIG",
                            "FLIGHTDECK_TMUX_SOCKET", "HOME", "PYTHONPATH",
                            "TMUX", "TMUX_PANE")}
        env["FLIGHTDECK_STATE_DIR"] = str(tmp)
        env["FLIGHTDECK_TMUX_SOCKET"] = "flightdeck-agytest-nonexistent"
        env["HOME"] = SUBPROCESS_HOME
        r = subprocess.run([sys.executable, str(script)], input=stdin_text,
                           capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_the_users_own_line_is_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, payload(), delegate="printf 'my own agy line'")
            self.assertEqual(r.stdout, "my own agy line")

    def test_with_no_line_of_their_own_the_minimal_one_is_painted(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, with_context(used_percentage=43))
            self.assertEqual(r.stdout.strip(), "Ctx 43%")

    def test_the_remaining_half_is_understood_here_too(self):
        # Same fallback `parse_ctx` has: a version that stops sending the used
        # half must not cost the percentage.
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, with_context(remaining_percentage=30))
            self.assertEqual(r.stdout.strip(), "Ctx 70%")

    def test_without_a_percentage_the_minimal_line_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, '{"session_id": "abc-123"}')
            self.assertEqual(r.stdout.strip(), "Ctx n/a")

    def test_nothing_of_ours_is_written_and_no_traceback_is_printed(self):
        # The note and the 🧠 notice are extras owned by the very code that is
        # missing; a note nobody can vouch for would have the menu showing a
        # percentage out of nowhere.
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, payload(), delegate="printf x")
            self.assertEqual(r.stderr, "")
            self.assertFalse((Path(tmp) / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
