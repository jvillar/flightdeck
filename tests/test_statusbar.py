"""`flightdeck.statusbar`: the one line tmux paints in its `status-right`.

The entries are the picker's (`build_entries`): the unit is the TMUX SESSION
(kind "tmux"), and the tool running inside it contributes `project` and
`state`. The pinned, outside-tmux and history rows are never a wait you can
attend to from here. Per-account usage is deliberately not on the bar: waits
only.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import statusbar as sb

class TestRenderStatusLine(unittest.TestCase):
    def test_a_wait_in_yellow(self):
        entries = [{"kind": "tmux", "name": "foo", "project": "foo",
                    "state": "awaiting_input"}]
        s = sb.render_status_line(entries)
        self.assertEqual(s, "#[fg=yellow]⏳ 1 waiting: foo#[default]")

    def test_the_session_name_wins_not_the_project(self):
        """The name was chosen by the human; the project can be generic
        ("claude" if you started in a directory called `claude` itself)."""
        entries = [{"kind": "tmux", "name": "flightdeck tests", "project": "claude",
                    "state": "needs_attention"}]
        s = sb.render_status_line(entries)
        self.assertIn("flightdeck tests", s)
        self.assertNotIn("claude", s)

    def test_looping_is_not_a_wait(self):
        """A session in `/loop` (state "looping") is waiting for its timer, not
        for you: the bar does not count it."""
        entries = [{"kind": "tmux", "name": "watch", "project": "retail",
                    "state": "looping"}]
        self.assertEqual(sb.render_status_line(entries), "")

    def test_without_waits_the_bar_stays_clean(self):
        """With nobody waiting, an EMPTY line: no '0 waiting', no colour."""
        self.assertEqual(sb.render_status_line([]), "")

    def test_the_tmux_name_when_no_session_is_identified(self):
        """Without `project` (an incomplete hook card) the tmux name shows."""
        entries = [{"kind": "tmux", "name": "shop_web", "project": None,
                    "state": "awaiting_input"}]
        self.assertIn("1 waiting: shop_web", sb.render_status_line(entries))

    def test_only_tmux_sessions_count(self):
        """A session outside tmux, or one from the history, is not a wait."""
        entries = [
            {"kind": "outside", "name": None, "project": "phone",
             "state": "awaiting_input"},
            {"kind": "recent", "name": None, "project": "old", "state": None},
            # The menu's fixed row (a "pin"): whatever the picker calls it, it
            # is not kind "tmux".
            {"kind": "pin", "name": "accounts", "project": None, "state": None},
            {"kind": "tmux", "name": "shell", "project": None, "state": None},
        ]
        self.assertEqual(sb.render_status_line(entries), "")

    def test_the_hash_of_a_name_is_escaped(self):
        """In the bar `#` opens tmux formatting: unescaped, a name like
        `dedup#2` (or worse, one with `#[`) would eat or repaint the line."""
        entries = [{"kind": "tmux", "name": "odd#[fg=red]", "project": None,
                    "state": "awaiting_input"}]
        s = sb.render_status_line(entries)
        self.assertIn("odd##[fg=red]", s)
        self.assertEqual(s.count("#[fg=yellow]"), 1)   # ours is still alone

    def test_at_most_three_names(self):
        entries = [{"kind": "tmux", "name": "s%d" % i, "project": "p%d" % i,
                    "state": "awaiting_input"} for i in range(5)]
        s = sb.render_status_line(entries)
        self.assertIn("5 waiting: s0, s1, s2#[default]", s)
        self.assertNotIn("s3", s)

    def test_a_single_line(self):
        entries = [{"kind": "tmux", "name": "a", "project": "a",
                    "state": "needs_attention"}]
        self.assertNotIn("\n", sb.render_status_line(entries))


class TestContextOnTheBar(unittest.TestCase):
    """From CTX_WARN up the bar says in RED that a session is running out of
    context, with the NAME of the tmux session (like the waits: it is how the
    human knows it) so they know which one to go and /compact.

    It goes BEHIND the waits on purpose: a session stopped and waiting for you
    is more urgent than one working with a full context.
    """

    def setUp(self):
        """An EMPTY config of this class's own, so the threshold is the default.

        `render_status_line` reads `context_warn_pct` on every repaint, and the
        numbers below (79 keeps quiet, 82 warns) are the default's. Without
        this they would be answering whatever `FLIGHTDECK_CONFIG` happened to
        point at -- the suite's sandbox usually, but a developer or a CI job
        that exports their own config file wins over it, and the failure would
        look like a rendering bug.
        """
        tmp = tempfile.TemporaryDirectory(prefix="flightdeck-statusbar-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text("{}", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tmux(self, name, pct, state=None):
        return {"kind": "tmux", "name": name, "project": "p",
                "state": state, "ctx_pct": pct}

    def test_it_warns_in_red_with_the_session_name(self):
        s = sb.render_status_line([self._tmux("payments", 82)])
        self.assertEqual(s, "#[fg=red]🧠 payments 82%#[default]")

    def test_it_keeps_quiet_below_the_threshold(self):
        for pct in (None, 0, 79, sb.CTX_WARN - 1):
            with self.subTest(pct=pct):
                self.assertEqual(sb.render_status_line([self._tmux("payments", pct)]), "")

    def test_right_at_the_threshold_it_already_warns(self):
        s = sb.render_status_line([self._tmux("payments", sb.CTX_WARN)])
        self.assertIn("🧠 payments %d%%" % sb.CTX_WARN, s)

    def test_first_who_waits_for_you_then_the_context(self):
        entries = [self._tmux("full", 91),
                   self._tmux("stopped", None, state="awaiting_input")]
        s = sb.render_status_line(entries)
        self.assertEqual(
            s, "#[fg=yellow]⏳ 1 waiting: stopped#[default] | #[fg=red]🧠 full 91%#[default]")
        self.assertLess(s.index("waiting"), s.index("🧠"))

    def test_at_most_two_and_the_tightest_ones(self):
        """Two already fill the width of a phone. If there are more, the ones
        named are the worst off (not the first in the list, which is tmux
        order)."""
        s = sb.render_status_line([self._tmux("s%d" % i, 80 + i) for i in range(4)])
        self.assertEqual(s.count("🧠"), 2)
        self.assertEqual(s, "#[fg=red]🧠 s3 83%#[default] | #[fg=red]🧠 s2 82%#[default]")

    def test_the_hash_of_the_name_is_escaped(self):
        s = sb.render_status_line([self._tmux("odd#[fg=green]", 85)])
        self.assertIn("odd##[fg=green]", s)
        self.assertEqual(s.count("#[fg=red]"), 1)   # ours is still alone

    def test_only_tmux_sessions_count(self):
        """A session outside tmux with a full context cannot be attended to
        from here, so it is not announced."""
        entries = [{"kind": "outside", "name": None, "project": "phone",
                    "state": "working", "ctx_pct": 95},
                   {"kind": "recent", "name": None, "project": "old",
                    "state": None, "ctx_pct": 99}]
        self.assertEqual(sb.render_status_line(entries), "")

    def test_a_single_line(self):
        entries = [self._tmux("a", 95, state="awaiting_input"), self._tmux("b", 88)]
        self.assertNotIn("\n", sb.render_status_line(entries))

    def test_old_entries_without_the_field_do_not_blow_up(self):
        # The bar is also painted with entries from a build_entries that did
        # not know about ctx_pct yet (or from an old test): no field, nothing.
        self.assertEqual(sb.render_status_line([{"kind": "tmux", "name": "a",
                                                 "project": "a", "state": None}]), "")


class TestThresholdFromTheConfig(unittest.TestCase):
    """The % comes from `context_warn_pct` in config.json, read on every
    repaint, rather than from a constant of this file.
    """

    def _with_config(self, value):
        tmp = tempfile.TemporaryDirectory(prefix="flightdeck-statusbar-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps({"context_warn_pct": value}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tmux(self, name, pct):
        return {"kind": "tmux", "name": name, "project": "p", "state": None,
                "ctx_pct": pct}

    def test_the_configured_threshold_is_the_one_in_force(self):
        self._with_config(90)
        self.assertEqual(sb.render_status_line([self._tmux("payments", 85)]), "")
        self.assertIn("🧠 payments 90%", sb.render_status_line([self._tmux("payments", 90)]))

    def test_an_unusable_threshold_falls_back_to_the_default(self):
        """A config the user typed wrong ("high" instead of 80) must not leave
        the bar blank on every repaint: `doctor` is the one that complains
        about types, the bar keeps working with the default."""
        self._with_config("high")
        self.assertIn("🧠 payments 82%", sb.render_status_line([self._tmux("payments", 82)]))


class TestGather(unittest.TestCase):
    """`gather` with its sources mocked, but with the REAL `build_entries`.

    That way the arity and the ORDER of its arguments are pinned -- the old
    two-argument call, or the arguments swapped around, break these tests again.
    Using the real one is safe because it only turns data into rows; the one
    thing it reads from outside is `context_show_pct`, the 🧠 threshold of a
    green row, which nothing here asserts on."""

    LIVE = [{"session_id": "S1", "tmux_session": "foo", "project": "foo",
             "state": "awaiting_input"}]
    TMUX = [{"name": "foo", "id": "$1", "activity": 100.0, "attached": True}]

    def _patch(self, target, value):
        p = mock.patch(target, return_value=value)
        self.addCleanup(p.stop)
        return p.start()

    def setUp(self):
        self._patch("flightdeck.common.load_sessions", self.LIVE)
        self._patch("flightdeck.common.list_tmux_sessions", self.TMUX)
        self.history = self._patch("flightdeck.history.list_recent_sessions", [])

    def test_gather_builds_the_entries(self):
        """Regression: `gather` used to call `build_entries(live, recent)` (the
        old arity) and blew up with TypeError, so the bar came out blank."""
        entries = sb.gather()
        self.assertEqual([e["name"] for e in entries if e["kind"] == "tmux"], ["foo"])
        # And the line those entries produce already counts the wait.
        self.assertIn("1 waiting: foo", sb.render_status_line(entries))

    def test_a_pinned_session_never_reaches_the_bar(self):
        """A pin's tmux session is infrastructure, not a session waiting for you.

        `gather` has to hand `build_entries` the configured pins for it to know
        that: without them the session pinned as `accounts` came through as an
        ordinary green row, and a `cswap tui` or a `htop` could end up named in
        the bar among the sessions waiting for you.
        """
        tmp = tempfile.TemporaryDirectory(prefix="flightdeck-statusbar-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps({"pins": [
            {"name": "accounts", "label": "⚙ accounts", "session": "accounts",
             "command": "cswap tui"}]}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._patch("flightdeck.common.list_tmux_sessions",
                    self.TMUX + [{"name": "accounts", "id": "$2",
                                  "activity": 500.0, "attached": False}])
        # The pinned session claims the wait, to make the test falsifiable: if
        # it leaked through as a green row, the bar would name it.
        self._patch("flightdeck.common.load_sessions",
                    self.LIVE + [{"session_id": "S2", "tmux_session": "accounts",
                                  "project": "accounts", "state": "awaiting_input"}])
        entries = sb.gather()
        self.assertEqual([e["name"] for e in entries if e["kind"] == "tmux"], ["foo"])
        self.assertEqual(sb.render_status_line(entries),
                         "#[fg=yellow]⏳ 1 waiting: foo#[default]")

    def test_gather_does_not_scan_the_history(self):
        """The bar only paints tmux sessions, so it does not pay for the rest.

        Walking the transcripts is thousands of files (~16k stat, ~98 MB) and
        the bar repaints every few seconds: working out the recent ones only to
        throw them away cost ~0.4s on EVERY repaint.
        """
        entries = sb.gather()
        self.history.assert_not_called()
        self.assertEqual([e for e in entries if e["kind"] == "recent"], [])


if __name__ == "__main__":
    unittest.main()
