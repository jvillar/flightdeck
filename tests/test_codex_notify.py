"""codex's notify tee: Flightdeck first, the untouched forward afterwards.

codex has no `Stop` hook event, so this is what turns "the turn is over" into
Flightdeck's `Stop` (it calls the state hook) and then hands the JSON, exactly
as it arrived, to whatever notify command the user had before us.

It is run the way codex runs it: by absolute path, with the event as its one
argument.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import SUBPROCESS_HOME, broken_package_copy
from flightdeck import config

TEE = config.code_dir() / "flightdeck" / "hooks" / "codex_notify.py"


class _TeeHarness:
    """Runs the tee the way codex does: by absolute path, one JSON argument."""

    def _set_up(self, tmp, with_delegate=True):
        """A fake delegate that records what it gets. Returns its log path."""
        log = Path(tmp) / "delegate.log"
        script = Path(tmp) / "delegate.sh"
        script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{}'\n".format(log))
        script.chmod(0o755)
        if with_delegate:
            delegates = Path(tmp) / "delegates"
            delegates.mkdir(parents=True, exist_ok=True)
            (delegates / "codex-notify").write_text(
                json.dumps([str(script), "turn-ended"]))
        return log

    def _env(self, tmp, parent="codex --model gpt-5.6"):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX_PANE", "TMUX", "FLIGHTDECK_TMUX_SOCKET",
                            "FLIGHTDECK_STATE_DIR", "FLIGHTDECK_CONFIG",
                            "FLIGHTDECK_PARENT_ARGV_TEST", "HOME",
                            "PYTHONPATH")}
        env["FLIGHTDECK_TMUX_SOCKET"] = "flightdeck-teetest-nonexistent"
        env["FLIGHTDECK_STATE_DIR"] = str(tmp)
        # A home of its own: the state hook this tee invokes looks the title up
        # in `~/.claude/projects`, and it must not be the real one.
        env["HOME"] = SUBPROCESS_HOME
        env["FLIGHTDECK_CONFIG"] = str(Path(tmp) / "config.json")
        env["FLIGHTDECK_PARENT_ARGV_TEST"] = parent
        return env

    def _run(self, tmp, json_arg, parent="codex --model gpt-5.6", script=TEE):
        r = subprocess.run([sys.executable, str(script), json_arg],
                           capture_output=True, text=True,
                           env=self._env(tmp, parent), timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _wait(self, path, seconds=3.0):
        end = time.time() + seconds
        while time.time() < end:
            if path.exists() and path.read_text().strip():
                return path.read_text()
            time.sleep(0.05)
        return ""


class TestTee(_TeeHarness, unittest.TestCase):

    def test_a_finished_turn_writes_the_card_and_forwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._set_up(tmp)
            event = json.dumps({"type": "agent-turn-complete",
                                "thread-id": "cx-9", "cwd": "/x/retail",
                                "last-assistant-message": "done"})
            self._run(tmp, event)
            card = json.loads(
                (Path(tmp) / "sessions" / "cx-9.json").read_text())
            self.assertEqual((card["state"], card["tool"]),
                             ("awaiting_input", "codex"))
            # the delegate gets ITS args and the untouched JSON, in that order
            self.assertEqual(self._wait(log).splitlines(), ["turn-ended", event])

    def test_an_odd_event_or_an_exec_only_forwards(self):
        for event, parent in (
                (json.dumps({"type": "other"}), "codex"),
                (json.dumps({"type": "agent-turn-complete", "thread-id": "cx-x"}),
                 "codex exec something"),
                ("this is not json", "codex")):
            with self.subTest(event=event[:20]), tempfile.TemporaryDirectory() as tmp:
                log = self._set_up(tmp)
                self._run(tmp, event, parent=parent)
                self.assertFalse((Path(tmp) / "sessions").exists())
                self.assertIn(event, self._wait(log))

    def test_with_no_delegate_it_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set_up(tmp, with_delegate=False)
            self._run(tmp, json.dumps({"type": "agent-turn-complete",
                                       "thread-id": "cx-y"}))
            self.assertTrue((Path(tmp) / "sessions" / "cx-y.json").exists())


class TestABrokenPackageStillForwards(_TeeHarness, unittest.TestCase):
    """The forward is the part that is not ours to break.

    The tee replaces the user's `notify` in codex's config.toml, so from the
    moment it is installed their command is only ever called through us. A
    `flightdeck` package that cannot be imported -- a half-written file, an
    interrupted update -- must therefore still end with their command being
    called with the untouched JSON, and with exit 0, because a notify that
    fails is a notify codex complains about on every turn.
    """

    def _run_broken(self, tmp, json_arg):
        script = broken_package_copy(Path(tmp) / "broken", TEE)
        r = subprocess.run([sys.executable, str(script), json_arg],
                           capture_output=True, text=True,
                           env=self._env(tmp), timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_the_json_still_reaches_the_delegate_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._set_up(tmp)
            event = json.dumps({"type": "agent-turn-complete",
                                "thread-id": "cx-broken", "cwd": "/x/retail"})
            self._run_broken(tmp, event)
            self.assertEqual(self._wait(log).splitlines(), ["turn-ended", event])

    def test_no_card_is_written_and_nothing_is_said(self):
        # The card is ours and the code that writes it is the broken part: half
        # a card is worse than none, and a menu row saying "working" about a
        # codex that finished is the exact false wait this project removed.
        with tempfile.TemporaryDirectory() as tmp:
            log = self._set_up(tmp)
            r = self._run_broken(tmp, json.dumps(
                {"type": "agent-turn-complete", "thread-id": "cx-quiet"}))
            self.assertEqual((r.stdout, r.stderr), ("", ""))
            self.assertFalse((Path(tmp) / "sessions").exists())
            self.assertTrue(self._wait(log))

    def test_with_no_delegate_it_still_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._set_up(tmp, with_delegate=False)
            self._run_broken(tmp, json.dumps({"type": "agent-turn-complete",
                                              "thread-id": "cx-z"}))


if __name__ == "__main__":
    unittest.main()
