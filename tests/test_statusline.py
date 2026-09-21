"""Flightdeck's status line for Claude Code, measured against the reference line.

The reference is the pre-release cockpit's `statusline-command.sh` (251 lines
of POSIX sh + jq, read-only). Its thresholds and colours are measured taste,
not convention, so they are pinned here band by band: a refactor that moves
"orange starts at 70%" has to break a test.

Everything in `flightdeck.statusline` is pure except reading two files (the
transcript, for the turn count, and `.git/HEAD`, for the branch), so the tests
build both in temporary directories. Nothing here reads a real payload, a real
home or real repositories.
"""
import calendar
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import config
from flightdeck import statusline as sl

FIXTURE = Path(__file__).parent / "fixtures" / "claude_statusline.json"

_ANSI = re.compile(r"\033\[[0-9;]*m")


def visible(text):
    """The text as the eye sees it: colours stripped."""
    return _ANSI.sub("", text)


def fixture_payload():
    return json.loads(FIXTURE.read_text())


class _Utc:
    """A context manager pinning the local timezone to UTC.

    The reset times (`↻07:50`) are printed in LOCAL time, which is what the
    human reading the bar wants and what the reference does. A test asserting
    the exact text therefore has to say which timezone it is in, or it passes in
    Madrid and fails in CI.
    """

    def __enter__(self):
        self.previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous
        time.tzset()
        return False


def make_repo(root, head="ref: refs/heads/main\n"):
    """A directory that looks enough like a git repo for the branch reader."""
    git = Path(root) / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head)
    return str(root)


class TestBar(unittest.TestCase):
    """The 10-cell bar, rounding to the nearest cell like the reference."""

    def test_empty_and_full(self):
        self.assertEqual(sl.bar(0, 10), "░" * 10)
        self.assertEqual(sl.bar(100, 10), "█" * 10)

    def test_it_rounds_to_the_nearest_cell(self):
        # (pct * width + 50) // 100: 5% already earns a cell, 4% does not.
        self.assertEqual(sl.bar(5, 10), "█" + "░" * 9)
        self.assertEqual(sl.bar(4, 10), "░" * 10)
        self.assertEqual(sl.bar(80, 10), "█" * 8 + "░" * 2)
        self.assertEqual(sl.bar(54, 10), "█" * 5 + "░" * 5)

    def test_it_clamps_outside_0_100(self):
        self.assertEqual(sl.bar(-20, 10), "░" * 10)
        self.assertEqual(sl.bar(250, 10), "█" * 10)

    def test_rubbish_does_not_blow_up(self):
        # A status line may never crash: an odd payload paints an empty bar.
        self.assertEqual(sl.bar(None, 10), "░" * 10)
        self.assertEqual(sl.bar("nonsense", 10), "░" * 10)

    def test_the_width_is_always_respected(self):
        for pct in range(-10, 111):
            self.assertEqual(len(sl.bar(pct, 10)), 10, pct)


class TestBands(unittest.TestCase):
    """The three colour scales, each on its own thresholds (the reference's).

    They do NOT share numbers on purpose: 55% of context is comfortable, 55% of
    a five-hour limit is not.
    """

    def test_context_green_yellow_orange_red(self):
        for pct, colour in ((0, sl.GREEN), (19, sl.GREEN), (20, sl.YELLOW),
                            (39, sl.YELLOW), (40, sl.ORANGE), (69, sl.ORANGE),
                            (70, sl.RED), (100, sl.RED)):
            self.assertEqual(sl.context_colour(pct), colour, pct)

    def test_rate_green_yellow_orange_red(self):
        for pct, colour in ((0, sl.GREEN), (49, sl.GREEN), (50, sl.YELLOW),
                            (74, sl.YELLOW), (75, sl.ORANGE), (89, sl.ORANGE),
                            (90, sl.RED), (100, sl.RED)):
            self.assertEqual(sl.rate_colour(pct), colour, pct)

    def test_turns_green_yellow_orange_red(self):
        for turns, colour in ((0, sl.GREEN), (59, sl.GREEN), (60, sl.YELLOW),
                              (79, sl.YELLOW), (80, sl.ORANGE), (99, sl.ORANGE),
                              (100, sl.RED), (400, sl.RED)):
            self.assertEqual(sl.turns_colour(turns), colour, turns)

    def test_effort_colours(self):
        self.assertEqual(sl.effort_colour("max"), sl.MAGENTA)
        self.assertEqual(sl.effort_colour("xhigh"), sl.MAGENTA)
        self.assertEqual(sl.effort_colour("high"), sl.CYAN)
        self.assertEqual(sl.effort_colour("medium"), sl.GREEN)
        self.assertEqual(sl.effort_colour("low"), sl.DIM)
        self.assertEqual(sl.effort_colour("something-new"), sl.DIM)


class TestWholePercent(unittest.TestCase):
    """floor(x + 0.5): the same rounding the tee writes into `<id>.ctx.json`, so
    the number in the bar and the one in the menu never disagree."""

    def test_it_rounds_half_up(self):
        self.assertEqual(sl.whole_percent(42.4), 42)
        self.assertEqual(sl.whole_percent(42.5), 43)
        self.assertEqual(sl.whole_percent(80), 80)

    def test_missing_or_odd_is_none(self):
        for value in (None, "", "abc", {}, float("nan"), float("inf")):
            self.assertIsNone(sl.whole_percent(value), value)

    def test_a_number_as_text_still_counts(self):
        # jq's `tostring` made everything text in the reference; a payload that
        # sends "42" instead of 42 must not blank the bar.
        self.assertEqual(sl.whole_percent("42.6"), 43)


class TestFormatTokens(unittest.TestCase):

    def test_under_a_thousand_is_the_plain_number(self):
        self.assertEqual(sl.format_tokens(0), "0")
        self.assertEqual(sl.format_tokens(118), "118")
        self.assertEqual(sl.format_tokens(999), "999")

    def test_thousands_and_millions(self):
        self.assertEqual(sl.format_tokens(1000), "1.0k")
        self.assertEqual(sl.format_tokens(798298), "798.3k")
        self.assertEqual(sl.format_tokens(1500000), "1.5M")

    def test_rubbish_is_zero(self):
        self.assertEqual(sl.format_tokens(None), "0")
        self.assertEqual(sl.format_tokens("lots"), "0")


class TestPrettyDir(unittest.TestCase):

    def test_the_home_becomes_a_tilde(self):
        self.assertEqual(sl.pretty_dir("/home/user/projects/demo", home="/home/user"),
                         "~/projects/demo")

    def test_a_long_path_keeps_only_the_last_two_names(self):
        long = "/home/user/work/clients/acme/services/billing/api"
        self.assertEqual(sl.pretty_dir(long, home="/home/user"), "~/…/billing/api")

    def test_a_sibling_of_home_is_not_a_tilde(self):
        # `/home/username2` must not come out as `~name2`: the reference replaced
        # a bare prefix, this one requires the separator.
        self.assertEqual(sl.pretty_dir("/home/user2/demo", home="/home/user"),
                         "/home/user2/demo")

    def test_the_home_itself(self):
        self.assertEqual(sl.pretty_dir("/home/user", home="/home/user"), "~")

    def test_nothing_is_nothing(self):
        self.assertEqual(sl.pretty_dir("", home="/home/user"), "")
        self.assertEqual(sl.pretty_dir(None, home="/home/user"), "")


class TestGitBranch(unittest.TestCase):
    """The branch WITHOUT running git: `.git/HEAD` is one small read, which is
    also why the reference's 3-second cache is not ported."""

    def test_a_repo_in_the_directory_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sl.git_branch(make_repo(tmp)), "main")

    def test_it_walks_up_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp)
            deep = Path(tmp) / "a" / "b" / "c"
            deep.mkdir(parents=True)
            self.assertEqual(sl.git_branch(str(deep)), "main")

    def test_a_branch_name_with_slashes_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp, head="ref: refs/heads/feature/status-line\n")
            self.assertEqual(sl.git_branch(tmp), "feature/status-line")

    def test_a_detached_head_shows_the_short_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(tmp, head="17bdb2a9f4c1e8b6d5a4c3b2a1908f7e6d5c4b3a\n")
            self.assertEqual(sl.git_branch(tmp), "17bdb2a")

    def test_a_worktree_points_at_another_gitdir(self):
        # In a worktree (and in a submodule) `.git` is a FILE saying where the
        # real one is; its HEAD is the one that answers for this directory.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "store" / "worktrees" / "wt"
            real.mkdir(parents=True)
            (real / "HEAD").write_text("ref: refs/heads/side-quest\n")
            tree = Path(tmp) / "tree"
            tree.mkdir()
            (tree / ".git").write_text("gitdir: %s\n" % real)
            self.assertEqual(sl.git_branch(str(tree)), "side-quest")

    def test_outside_a_repo_it_says_no_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A directory with nothing above it that is a repo... unless the
            # temp dir happens to live inside one, which it does not.
            self.assertEqual(sl.git_branch(tmp), "no branch")

    def test_an_unreadable_head_says_no_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertEqual(sl.git_branch(tmp), "no branch")

    def test_no_directory_at_all(self):
        self.assertEqual(sl.git_branch(None), "no branch")
        self.assertEqual(sl.git_branch(""), "no branch")


class TestBucketLabel(unittest.TestCase):
    """Any bucket the payload carries gets painted, named as shortly as the
    reference named it: a new one (per-model weekly limits) appears the day
    Claude Code starts sending it, without a release here."""

    def test_the_two_known_ones(self):
        self.assertEqual(sl.bucket_label("five_hour"), "5h")
        self.assertEqual(sl.bucket_label("seven_day"), "7d")

    def test_a_per_model_weekly_one(self):
        self.assertEqual(sl.bucket_label("seven_day_opus"), "7d-opus")
        self.assertEqual(sl.bucket_label("seven_day_fable"), "7d-fable")

    def test_an_unknown_one_keeps_its_own_name(self):
        self.assertEqual(sl.bucket_label("monthly_credits"), "monthly-credits")


class TestCountTurns(unittest.TestCase):

    def test_it_counts_the_assistant_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "transcript.jsonl"
            p.write_text(
                '{"type":"user","message":{}}\n'
                '{"type":"assistant","message":{}}\n'
                '{"type":"user","message":{}}\n'
                '{"type":"assistant","message":{}}\n'
                '{"type":"assistant","message":{}}\n')
            self.assertEqual(sl.count_turns(str(p)), 3)

    def test_a_missing_transcript_is_none(self):
        self.assertIsNone(sl.count_turns("/nowhere/at/all.jsonl"))
        self.assertIsNone(sl.count_turns(None))

    def test_undecodable_bytes_do_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_bytes(b'{"type":"assistant"}\n\xff\xfe not text at all\n')
            self.assertEqual(sl.count_turns(str(p)), 1)


class TestRenderClaudeFixture(unittest.TestCase):
    """The whole line against a real payload (anonymised) from Claude Code."""

    def setUp(self):
        self.tz = _Utc().__enter__()
        self.addCleanup(self.tz.__exit__, None, None, None)
        self.payload = fixture_payload()

    def test_the_three_lines_as_the_eye_sees_them(self):
        # The default layout: identity, consumption, tokens. The context bar is
        # 15 cells here, as the reference script painted it.
        out = sl.render_claude(self.payload, account=ACCOUNT)
        self.assertTrue(out.endswith("\n"), repr(out[-5:]))
        first, second, third = visible(out).rstrip("\n").split("\n")
        self.assertEqual(
            first,
            "✳ Fable 5.1 · xhigh │ 👤 user@example.com max"
            " │ /home/user/projects/demo │ no branch")
        self.assertEqual(
            second,
            "Ctx ████████████░░░ 80% │ 5h ░░░░░░░░░░ 3% ↻07:50"
            " │ 7d █████░░░░░ 54% ↻17/09 20:00")
        # No turns: the fixture's transcript path does not exist on this machine.
        self.assertEqual(third, "Tokens: msg 118 │ cache 798.3k │ session 798.4k")

    def test_the_compressed_layout_is_byte_for_byte_what_it_always_was(self):
        # `lines: 2` is the opt-out for whoever liked the port's own layout, and
        # what it owes them is exactly the line they had: both halves on one
        # row, in one order, with a 10-cell context bar.
        out = sl.render_claude(self.payload, lines=2, account={})
        self.assertEqual(
            visible(out),
            "✳ Fable 5.1 · xhigh │ /home/user/projects/demo │ no branch"
            " │ Ctx ████████░░ 80% │ 5h ░░░░░░░░░░ 3% ↻07:50"
            " │ 7d █████░░░░░ 54% ↻17/09 20:00\n"
            "Tokens: msg 118 │ cache 798.3k │ session 798.4k\n")

    def test_the_colours_of_this_payload(self):
        out = sl.render_claude(self.payload)
        # 80% of context is the red band; 3% of the five-hour limit is green;
        # 54% of the weekly one is yellow (rate turns yellow at 50, not at 20).
        self.assertIn(sl.RED + "████████████░░░" + sl.RESET, out)
        self.assertIn(sl.GREEN + "░░░░░░░░░░" + sl.RESET, out)
        self.assertIn(sl.YELLOW + "█████░░░░░" + sl.RESET, out)
        self.assertIn(sl.tool_colour("claude") + "✳ Fable 5.1" + sl.RESET, out)

    def test_only_the_first_line(self):
        out = sl.render_claude(self.payload, lines=1)
        self.assertEqual(len(visible(out).rstrip("\n").split("\n")), 1)
        self.assertIn("Ctx", out)
        self.assertNotIn("Tokens:", out)

    def test_the_model_id_stands_in_for_a_missing_display_name(self):
        del self.payload["model"]["display_name"]
        self.assertIn("✳ claude-fable-5-1", visible(sl.render_claude(self.payload)))

    def test_no_model_at_all(self):
        del self.payload["model"]
        self.assertIn("✳ ?", visible(sl.render_claude(self.payload)))

    def test_without_an_effort_level_that_bit_is_left_out(self):
        del self.payload["effort"]
        first = visible(sl.render_claude(self.payload)).split("\n")[0]
        self.assertTrue(first.startswith("✳ Fable 5.1 │ "), first)


class TestRenderClaudeBands(unittest.TestCase):
    """One payload per band, so a moved threshold breaks a test."""

    def _at(self, ctx_pct, rate_pct=0):
        return {"model": {"display_name": "M"}, "cwd": "/tmp",
                "context_window": {"used_percentage": ctx_pct},
                "rate_limits": {"five_hour": {"used_percentage": rate_pct}}}

    def test_every_context_band(self):
        for pct, colour in ((10, sl.GREEN), (30, sl.YELLOW),
                            (55, sl.ORANGE), (95, sl.RED)):
            out = sl.render_claude(self._at(pct))
            self.assertIn(colour + sl.bar(pct, sl.CONTEXT_BAR_WIDTH) + sl.RESET,
                          out, pct)
            # The same bands at the compressed layout's narrower bar: the
            # colour is the percentage's, never the width's.
            two = sl.render_claude(self._at(pct), lines=2)
            self.assertIn(colour + sl.bar(pct, sl.BAR_WIDTH) + sl.RESET, two, pct)

    def test_every_rate_band(self):
        for pct, colour in ((10, sl.GREEN), (60, sl.YELLOW),
                            (80, sl.ORANGE), (99, sl.RED)):
            out = sl.render_claude(self._at(0, pct))
            self.assertIn("5h " + colour + sl.bar(pct, 10) + sl.RESET, out, pct)

    def test_every_turns_band(self):
        for turns, colour in ((5, sl.GREEN), (70, sl.YELLOW),
                              (85, sl.ORANGE), (120, sl.RED)):
            out = sl.render_claude(self._at(10), turns=turns)
            self.assertIn("turns " + colour + str(turns) + sl.RESET, out, turns)


class TestRenderClaudeFallbacks(unittest.TestCase):
    """What the line says when the payload does not carry something."""

    def test_no_rate_limits_at_all(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp",
                   "context_window": {"used_percentage": 12}}
        self.assertIn("│ limits n/a", visible(sl.render_claude(payload)))

    def test_an_empty_rate_limits_object(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp",
                   "rate_limits": {}, "context_window": {"used_percentage": 12}}
        self.assertIn("│ limits n/a", visible(sl.render_claude(payload)))

    def test_no_context_figure(self):
        # The empty bar is as wide as the full one would have been, in each
        # layout: a shorter `n/a` bar would read as a different scale.
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp"}
        self.assertIn("Ctx ░░░░░░░░░░░░░░░ n/a", visible(sl.render_claude(payload)))
        self.assertIn("Ctx ░░░░░░░░░░ n/a",
                      visible(sl.render_claude(payload, lines=2)))

    def test_a_bucket_without_a_reset_time_has_no_arrow(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp",
                   "rate_limits": {"five_hour": {"used_percentage": 20}}}
        second = visible(sl.render_claude(payload)).split("\n")[1]
        self.assertIn("5h ██░░░░░░░░ 20%", second)
        self.assertNotIn("↻", second)

    def test_a_new_bucket_is_painted_too(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp",
                   "rate_limits": {"five_hour": {"used_percentage": 20},
                                   "seven_day_opus": {"used_percentage": 40}}}
        self.assertIn("7d-opus ████░░░░░░ 40%", visible(sl.render_claude(payload)))

    def test_zero_tokens(self):
        third = visible(sl.render_claude({})).split("\n")[2]
        self.assertEqual(third, "Tokens: msg 0 │ cache 0 │ session 0")

    def test_a_payload_of_rubbish_still_paints_the_whole_layout(self):
        # The one hard rule: whatever arrives, no exception reaches Claude Code.
        for payload in ({}, {"model": "not-a-dict", "context_window": 7,
                             "rate_limits": [1, 2], "effort": "high",
                             "cwd": 42, "transcript_path": 99},
                        {"context_window": {"used_percentage": "NaN"}}):
            for lines, expected in ((None, 3), (3, 3), (2, 2), (1, 1)):
                out = sl.render_claude(payload, lines=lines)
                self.assertEqual(len(visible(out).rstrip("\n").split("\n")),
                                 expected, (payload, lines))


class TestRenderClaudeTurns(unittest.TestCase):

    def test_the_turns_come_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text('{"type":"assistant"}\n' * 7 + '{"type":"user"}\n')
            payload = {"model": {"display_name": "M"}, "cwd": tmp,
                       "transcript_path": str(p)}
            self.assertIn("│ turns 7", visible(sl.render_claude(payload)))

    def test_no_transcript_no_turns(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp",
                   "transcript_path": "/nowhere/t.jsonl"}
        self.assertNotIn("turns", visible(sl.render_claude(payload)))


class TestRenderClaudeBranch(unittest.TestCase):

    def test_the_branch_of_the_payloads_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"model": {"display_name": "M"}, "cwd": make_repo(tmp)}
            # Last on the identity line, where the reference had it.
            first = visible(sl.render_claude(payload)).split("\n")[0]
            self.assertTrue(first.endswith("│ main"), first)
            self.assertIn("│ main │", visible(sl.render_claude(payload, lines=2)))


class TestColumns(unittest.TestCase):
    """The COMPRESSED layout's ladder (`lines: 2`), rung by rung.

    `columns` is the phone: ~80 columns wide, where the full line wraps. With
    both halves sharing one row there is one ladder for all of it, and it sheds
    what can be worked out from elsewhere (the branch is on screen in the shell,
    the folder is in the tmux session's name) before anything that is only here:
    the context bar and the limits never go.

    Pinned at `lines=2` on purpose: this is the layout somebody opts back into,
    and what it owes them is the line they had. The default layout's ladders are
    `TestThreeLineTrimming`, below.
    """

    def _payload(self, tmp):
        return {"model": {"display_name": "Fable 5.1"},
                "effort": {"level": "xhigh"},
                "cwd": make_repo(tmp),
                "context_window": {"used_percentage": 42},
                "rate_limits": {"five_hour": {"used_percentage": 31,
                                              "resets_at": 1789545000},
                                "seven_day": {"used_percentage": 12,
                                              "resets_at": 1789675200}}}

    def test_with_room_nothing_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = visible(sl.render_claude(self._payload(tmp), lines=2,
                                           columns=200))
            self.assertIn("│ main │", out)
            self.assertIn("xhigh", out)

    def test_at_a_phones_width_the_branch_is_gone_and_the_bars_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = visible(sl.render_claude(self._payload(tmp), lines=2,
                                           columns=80))
            self.assertNotIn("│ main │", out)
            self.assertIn("Ctx ", out)
            self.assertIn("5h ", out)
            self.assertIn("7d ", out)

    def test_it_sheds_in_order_one_piece_at_a_time(self):
        """One column narrower than it fits sheds exactly the next thing.

        The widths are measured off the line itself rather than written down,
        because the folder is a temporary path of unpredictable length. What is
        being pinned is the ORDER of TRIM_LADDER: branch, reset times, effort,
        then the folder (first its leaf only, then gone).
        """
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)

            def first_line(columns=None):
                return visible(sl.render_claude(payload, lines=2,
                                                columns=columns)).split("\n")[0]

            step = first_line()
            self.assertIn("│ main │", step)

            step = first_line(len(step) - 1)          # 1: the branch
            self.assertNotIn("│ main │", step)
            self.assertIn("↻", step)
            self.assertIn("xhigh", step)

            step = first_line(len(step) - 1)          # 2: the reset times
            self.assertNotIn("↻", step)
            self.assertIn("xhigh", step)
            self.assertIn(os.path.basename(tmp), step)

            step = first_line(len(step) - 1)          # 3: the effort
            self.assertNotIn("xhigh", step)
            self.assertIn(os.path.basename(tmp), step)

            step = first_line(len(step) - 1)          # 4: the folder's leaf only
            self.assertIn(os.path.basename(tmp), step)
            self.assertNotIn("/", step)

            step = first_line(len(step) - 1)          # 5: the folder
            self.assertNotIn(os.path.basename(tmp), step)
            self.assertIn("Ctx ████░░░░░░ 42%", step)
            self.assertIn("5h ███░░░░░░░ 31%", step)
            self.assertIn("7d █░░░░░░░░░ 12%", step)

    def test_squeezed_hard_the_bars_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = visible(sl.render_claude(self._payload(tmp), lines=2,
                                             columns=20)).split("\n")[0]
            self.assertIn("Ctx ████░░░░░░ 42%", first)
            self.assertIn("5h ███░░░░░░░ 31%", first)
            self.assertNotIn("xhigh", first)

    def test_the_token_line_keeps_its_figures(self):
        # They are short by construction, and cutting numbers in half would be
        # worse than letting the terminal wrap them.
        with tempfile.TemporaryDirectory() as tmp:
            second = visible(sl.render_claude(self._payload(tmp), lines=2,
                                              columns=20)
                             ).rstrip("\n").split("\n")[1]
            self.assertTrue(second.startswith("Tokens: msg "), second)


class TestTerminalWidth(unittest.TestCase):
    """Where claude's line gets a width from, since its payload carries none.

    Claude Code sends the status line command NO width: neither the schema its
    own binary documents (read out of 2.1.274) nor a real payload has anything
    answering to agy's `terminal_width`. What it does have is the ENVIRONMENT --
    it sets `COLUMNS` from `process.stdout.columns` for every command it
    launches, the status line among them -- so the line trims to a MEASUREMENT
    of the pane rather than to a number nobody took. No COLUMNS (Claude Code's
    stdout is not a terminal) means no trim, which is exactly what this line did
    before it could ask.
    """

    def _payload(self, tmp):
        return {"model": {"display_name": "Fable 5.1"},
                "effort": {"level": "xhigh"},
                "cwd": make_repo(tmp),
                "context_window": {"used_percentage": 42},
                "rate_limits": {"five_hour": {"used_percentage": 31,
                                              "resets_at": 1789545000},
                                "seven_day": {"used_percentage": 12,
                                              "resets_at": 1789675200}}}

    def _lines(self, payload, **kw):
        return visible(sl.render_claude(payload, **kw)).rstrip("\n").split("\n")

    def _first(self, payload, **kw):
        return self._lines(payload, **kw)[0]

    def _demo_lines(self, tmp, columns):
        """The demo line as a pane `columns` wide receives it.

        The working directory is moved into a repo of the test's own because
        `demo_payload_claude` reads the real one (the installer shows people
        their own folder), and the line's width would otherwise depend on where
        the suite was run from.
        """
        with mock.patch("os.getcwd", lambda: make_repo(tmp)):
            payload = sl.demo_payload_claude()
        with mock.patch.dict(os.environ, {"COLUMNS": str(columns)}):
            out = sl.render_claude(payload, turns=sl.DEMO_TURNS)
        return visible(out).rstrip("\n").split("\n")

    # ── the reader ───────────────────────────────────────────────────────────

    def test_it_reads_the_columns_claude_code_exports(self):
        self.assertEqual(sl.terminal_width({"COLUMNS": "100"}), 100)

    def test_an_unusable_columns_is_no_width_at_all(self):
        # And NOT a default of our own: a line trimmed to a number nobody
        # measured would hide segments on a terminal with room for them.
        for value in ("", "   ", "wide", "0", "-5", "nan", "inf"):
            self.assertIsNone(sl.terminal_width({"COLUMNS": value}), value)
        self.assertIsNone(sl.terminal_width({}))

    # ── the line ─────────────────────────────────────────────────────────────

    def test_with_columns_in_the_environment_every_line_trims_itself(self):
        # Each of the three is fitted on its own, so the measurement reaches all
        # of them and not just the first.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            with mock.patch.dict(os.environ, {"COLUMNS": "80"}):
                lines = self._lines(payload)
            for line in lines:
                self.assertLessEqual(len(line), 80, line)
            self.assertNotIn("↻", lines[1])     # line 2 spent its resets rung
            self.assertIn("Ctx ", lines[1])

    def test_with_no_columns_nothing_is_trimmed(self):
        # Pinned: the behaviour before this line could ask, and still the one
        # wherever Claude Code's stdout is not a terminal.
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._lines(self._payload(tmp))
            self.assertTrue(lines[0].endswith("│ main"), lines[0])
            self.assertIn("↻", lines[1])

    def test_an_explicit_columns_beats_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            with mock.patch.dict(os.environ, {"COLUMNS": "40"}):
                self.assertIn("│ main", self._first(payload, columns=400))

    def test_columns_zero_still_means_do_not_trim(self):
        # `render_agy` promises the same thing in the same word, and the two
        # have to stay swappable for a caller that does not know which tool it
        # is holding.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            with mock.patch.dict(os.environ, {"COLUMNS": "40"}):
                self.assertIn("│ main", self._first(payload, columns=0))

    # ── the demo, which is what the GIF photographs ──────────────────────────

    def test_the_demo_fits_a_hundred_column_pane(self):
        # The defect the demo GIF caught: untrimmed, the compressed line was
        # ~137 cells and the 100-column pane wrapped its tail
        # (`7d ░░░ 12% ↻20/09 11:50`) onto a row of its own. Three lines do not
        # make that go away -- line 2 carries both buckets and their resets.
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._demo_lines(tmp, 100)
            self.assertEqual(len(lines), 3)
            for line in lines:
                self.assertLessEqual(len(line), 100, line)

    def test_the_demo_sheds_each_lines_ladder_in_order(self):
        # Line 1 loses the branch (it is in the shell's own prompt), line 2 the
        # reset times; the bars are the reason the line is painted and stay.
        with tempfile.TemporaryDirectory() as tmp:
            whole = self._demo_lines(tmp, 0)        # 0 is not a width: no trim
            self.assertTrue(whole[0].endswith("│ main"), whole[0])
            self.assertIn("↻", whole[1])

            # 70 is wide enough for line 1 and not for line 2, which is the
            # point of fitting them separately: line 1 keeps its branch while
            # line 2 spends its resets rung and keeps both buckets.
            seventy = self._demo_lines(tmp, 70)
            self.assertTrue(seventy[0].endswith("│ main"), seventy[0])
            self.assertNotIn("↻", seventy[1])
            self.assertIn("Ctx ██████░░░░░░░░░ 42%", seventy[1])
            self.assertIn("5h ███░░░░░░░ 31%", seventy[1])
            self.assertIn("7d █░░░░░░░░░ 12%", seventy[1])

            # And at 40 line 1 loses the branch, the first rung of its own.
            self.assertNotIn("│ main", self._demo_lines(tmp, 40)[0])

    def test_a_very_narrow_pane_still_paints_the_model_and_the_bar(self):
        # What is under every ladder: the tool and its model on line 1, the
        # context bar on line 2. Twenty columns cannot hold either whole, and
        # they are painted anyway rather than cut.
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._demo_lines(tmp, 20)
            self.assertEqual(lines[0], "✳ Opus 4.6 · high")
            self.assertEqual(lines[1], "Ctx ██████░░░░░░░░░ 42%")

    def test_the_token_line_sheds_only_the_turns(self):
        # Cutting the numbers in half would be worse than letting the terminal
        # wrap them, so the turn count -- which the menu's row also shows -- is
        # the one thing that can go, and only when the line does not fit.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("turns 37", self._demo_lines(tmp, 100)[2])
            narrow = self._demo_lines(tmp, 20)[2]
            self.assertTrue(narrow.startswith("Tokens: msg "), narrow)
            self.assertNotIn("turns", narrow)


class TestConfiguredLines(unittest.TestCase):
    """`statusline.lines`: 3 (the reference layout), 2 (compressed) or 1."""

    def _cfg(self, value):
        return {"statusline": {"claude": "own", "lines": value}}

    def test_the_default_is_three(self):
        self.assertEqual(config.DEFAULTS["statusline"]["lines"], 3)
        self.assertEqual(sl.configured_lines(), 3)      # the config in force

    def test_the_three_it_accepts(self):
        for value, expected in ((1, 1), (2, 2), (3, 3), ("2", 2)):
            self.assertEqual(sl.configured_lines(self._cfg(value)), expected,
                             repr(value))

    def test_anything_else_is_the_default(self):
        # A typo leaves the user with the line everybody else has, which is the
        # rule the status line MODES already follow -- never a blank bar.
        for value in (0, 4, -1, "many", "", None, True, False, [], {}, 2.5):
            self.assertEqual(sl.configured_lines(self._cfg(value)), 3,
                             repr(value))

    def test_a_config_of_the_wrong_kind_is_the_default(self):
        for cfg in ({"statusline": "own"}, {"statusline": []}, {}, "x", 7):
            self.assertEqual(sl.configured_lines(cfg), 3, repr(cfg))

    def test_the_renderers_ask_the_config_when_nobody_says(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp"}
        for value, expected in ((3, 3), (2, 2), (1, 1)):
            with mock.patch.object(sl, "_current_config",
                                   lambda v=value: self._cfg(v)):
                self.assertEqual(
                    len(sl.render_claude(payload).rstrip("\n").split("\n")),
                    expected, value)
                self.assertEqual(
                    len(sl.render_agy(payload).rstrip("\n").split("\n")),
                    expected, value)

    def test_an_argument_beats_the_config(self):
        payload = {"model": {"display_name": "M"}, "cwd": "/tmp"}
        with mock.patch.object(sl, "_current_config", lambda: self._cfg(2)):
            self.assertEqual(
                len(sl.render_claude(payload, lines=3).rstrip("\n").split("\n")), 3)

    def test_the_config_is_read_once_per_render(self):
        # `lines` and the account are two questions with one answer: a second
        # read would be a second file read per repaint of every session, and a
        # config.json edited between them could paint half a line in each shape.
        reads = []

        def counted():
            reads.append(1)
            return config.DEFAULTS

        payload = {"model": {"display_name": "M"}, "cwd": "/tmp"}
        with mock.patch.object(sl, "_current_config", counted):
            sl.render_claude(payload)
        self.assertEqual(len(reads), 1)

    def test_nothing_is_read_when_the_caller_answered_both(self):
        def never():
            raise AssertionError("the config must not be read here")

        with mock.patch.object(sl, "_current_config", never):
            sl.render_claude({}, lines=2, account={})
            sl.render_agy({}, lines=2, account={})


class TestThreeLineTrimming(unittest.TestCase):
    """The default layout at four pane widths, line by line.

    Each line is fitted on its own, so the question at every width is the same
    three times: does it fit, and did it shed the right thing? The widths are
    the ones worth knowing -- a wide terminal, the GIF's pane, a laptop split in
    two, and a phone over SSH.
    """

    WIDTHS = (140, 100, 80, 60)

    # A folder deep enough for `pretty_dir` to shorten it and for the ladder to
    # have something to spend: `~/…/tmpXXXXXXXX/very-long-project-name`, 38
    # cells wherever the suite runs (macOS puts temporary directories under
    # /var/folders and Linux under /tmp; both are past DIR_MAX before this).
    PROJECT = "very-long-project-name"

    def _cwd(self, tmp):
        make_repo(tmp)
        path = os.path.join(tmp, self.PROJECT)
        os.makedirs(path, exist_ok=True)
        return path

    def _claude(self, tmp):
        return {"model": {"display_name": "Fable 5.1"},
                "effort": {"level": "xhigh"},
                "cwd": self._cwd(tmp),
                "context_window": {"used_percentage": 88},
                "rate_limits": {"five_hour": {"used_percentage": 11,
                                              "resets_at": 1789545000},
                                "seven_day": {"used_percentage": 34,
                                              "resets_at": 1789675200},
                                "seven_day_fable": {"used_percentage": 52,
                                                    "resets_at": 1789675200}}}

    def _lines(self, payload, columns, tool="claude", turns=716):
        render = (sl.render_claude if tool == "claude" else sl.render_agy)
        kw = {"turns": turns} if tool == "claude" else {}
        out = render(payload, columns=columns, account=sl.DEMO_ACCOUNT, **kw)
        return out.rstrip("\n").split("\n")

    def test_every_line_of_both_tools_fits_every_width(self):
        with tempfile.TemporaryDirectory() as tmp, _Utc():
            agy = dict(sl.demo_payload_agy(), cwd=make_repo(tmp))
            for tool, payload in (("claude", self._claude(tmp)), ("agy", agy)):
                for width in self.WIDTHS:
                    lines = self._lines(payload, width, tool=tool)
                    self.assertEqual(len(lines), 3, (tool, width))
                    for line in lines:
                        self.assertLessEqual(sl._visible_len(line), width,
                                             (tool, width, visible(line)))

    def test_line_one_sheds_the_branch_then_the_folder_then_the_effort(self):
        with tempfile.TemporaryDirectory() as tmp, _Utc():
            payload = self._claude(tmp)

            def at(columns):
                return visible(self._lines(payload, columns)[0])

            self.assertTrue(at(0).endswith("│ main"), at(0))
            self.assertTrue(at(140).endswith("│ main"), at(140))
            self.assertTrue(at(100).endswith("│ main"), at(100))
            # 80: the branch has gone and the folder is down to its leaf, which
            # is the name that identifies the project.
            self.assertNotIn("│ main", at(80))
            self.assertIn("│ %s" % self.PROJECT, at(80))
            self.assertNotIn("/", at(80))
            # 60: no folder at all. 40: not even the effort. The account is
            # still there in both: it is the last thing the line sheds.
            self.assertNotIn(self.PROJECT, at(60))
            self.assertIn("xhigh", at(60))
            self.assertNotIn("xhigh", at(40))
            for columns in (80, 60, 40):
                self.assertIn("👤 user@example.com max", at(columns))
                self.assertIn("✳ Fable 5.1", at(columns))

    def test_line_one_loses_the_account_only_when_nothing_else_is_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = sl._parts(self._claude(tmp), None, account=sl.DEMO_ACCOUNT,
                              ctx_cells=sl.CONTEXT_BAR_WIDTH)
            spent = visible(sl._identity(parts, drop=sl.TRIM_IDENTITY[:-1]))
            self.assertEqual(spent, "✳ Fable 5.1 │ 👤 user@example.com max")
            self.assertEqual(visible(sl._identity(parts, drop=sl.TRIM_IDENTITY)),
                             "✳ Fable 5.1")

    def test_line_two_sheds_the_resets_then_buckets_from_the_end(self):
        with tempfile.TemporaryDirectory() as tmp, _Utc():
            payload = self._claude(tmp)

            def at(columns):
                return visible(self._lines(payload, columns)[1])

            self.assertIn("↻", at(0))
            self.assertIn("7d-fable", at(0))
            # 100: the reset times go and all three buckets stay.
            self.assertNotIn("↻", at(100))
            for name in ("5h", "7d ", "7d-fable"):
                self.assertIn(name, at(100))
            # 80 and 60: buckets from the END, so the one listed first survives.
            self.assertNotIn("7d-fable", at(80))
            self.assertIn("7d ", at(80))
            self.assertNotIn("7d ", at(60))
            self.assertIn("5h", at(60))
            # And the context bar is never a rung: it is the reason for the line.
            for columns in (0,) + self.WIDTHS:
                self.assertIn("Ctx █████████████░░ 88%", at(columns))

    def test_line_three_sheds_only_the_turns_and_only_if_it_must(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._claude(tmp)
            for columns in self.WIDTHS:
                self.assertIn("turns 716", visible(self._lines(payload, columns)[2]))
            narrow = visible(self._lines(payload, 40)[2])
            self.assertEqual(narrow, "Tokens: msg 0 │ cache 0 │ session 0")

    def test_agys_identity_line_carries_its_own_account_folder_and_branch(self):
        # agy's payload has a `cwd` like claude's, so its identity line is the
        # same line: the branch is read from that folder, not sent by agy.
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(sl.demo_payload_agy(), cwd=make_repo(tmp),
                           email="user@example.com", plan_tier="Starter")
            first = visible(sl.render_agy(payload, columns=0).split("\n")[0])
            self.assertTrue(first.startswith("✦ Gemini 3.8 Flash (High) · high"),
                            first)
            self.assertIn("👤 user@example.com Starter", first)
            self.assertTrue(first.endswith("│ main"), first)

    def test_agys_second_line_carries_its_buckets_and_its_state(self):
        with tempfile.TemporaryDirectory() as tmp, _Utc():
            payload = dict(sl.demo_payload_agy(), cwd=make_repo(tmp))
            second = visible(sl.render_agy(payload, columns=0).split("\n")[1])
            self.assertTrue(second.startswith("Ctx ██████░░░░░░░░░ 38%"), second)
            self.assertIn("3p-7d", second)
            self.assertIn("gemini-7d", second)
            self.assertIn("working", second)      # its agent state, last
            # And the state is the first thing that line sheds, before a bucket.
            narrow = visible(sl.render_agy(payload, columns=80).split("\n")[1])
            self.assertNotIn("working", narrow)
            self.assertIn("3p-7d", narrow)


class TestAgyWidth(unittest.TestCase):
    """Where agy's line gets a width from when its payload carries none.

    agy IS the tool that says how wide its pane is -- `terminal_width` was
    measured in a real payload -- so that number wins. The fallback is for a
    payload nobody's pane produced, which is exactly what `--demo` builds: with
    no width at all its line came out at 178 cells and the terminal wrapped it,
    in the one place whose whole job is to show somebody what the line looks
    like.
    """

    def _first(self, payload, **kw):
        # The COMPRESSED layout (`lines: 2`), which is where `agy_ladder` lives:
        # its `mode` and `bucket` rungs exist because agy's two halves on one row
        # do not fit a 100-column pane. The default layout's ladders are
        # `TestThreeLineTrimming`, above.
        kw.setdefault("lines", 2)
        return visible(sl.render_agy(payload, **kw)).split("\n")[0]

    def _demo(self, tmp):
        """The demo payload, with the working directory moved into a repo of
        this test's own: `demo_payload_agy` reads the real one."""
        with mock.patch("os.getcwd", lambda: make_repo(tmp)):
            return sl.demo_payload_agy()

    # -- the reader -----------------------------------------------------------

    def test_the_payload_is_what_agy_says_and_it_wins(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
            self.assertEqual(sl.agy_width({"terminal_width": 57}), 57)

    def test_with_no_width_in_the_payload_it_falls_back_to_columns(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
            self.assertEqual(sl.agy_width({}), 100)

    def test_a_width_under_one_cell_is_not_a_width(self):
        # agy sending 0 means it could not size its pane either, so the
        # environment gets its turn -- the same floor `terminal_width` applies.
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
            for value in (0, -5, "", "wide", None):
                self.assertEqual(sl.agy_width({"terminal_width": value}), 100,
                                 repr(value))

    def test_with_neither_there_is_no_width(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLUMNS", None)
            self.assertIsNone(sl.agy_width({}))

    # -- the line -------------------------------------------------------------

    def test_with_no_columns_and_no_width_nothing_is_trimmed(self):
        # Pinned: the behaviour before the line could ask, and still the answer
        # for a payload that arrives without the key outside a terminal.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {}, clear=False):
            payload = self._demo(tmp)
            # Inside the patch: the suite's `__init__` has already taken COLUMNS
            # out of the environment, but a test that pops it for itself has to
            # put it back, or the next one inherits this one's environment.
            os.environ.pop("COLUMNS", None)
            line = self._first(payload)
            self.assertIn("│ main │", line)
            self.assertIn("↻", line)
            self.assertIn("Gemini 3.8 Flash (High)", line)

    def test_columns_makes_the_demo_line_shed_the_whole_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._demo(tmp)
            with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
                trimmed = self._first(payload)
            whole = self._first(payload, columns=0)   # 0 is not a width
            # Sixty-odd cells lighter. Not pinned exactly: `whole` carries the
            # folder, whose length is this test's temporary directory's.
            self.assertLess(len(trimmed), len(whole) - 50)
            for gone in ("│ main │", "↻", "· high"):
                self.assertNotIn(gone, trimmed)
            # The bars are the reason the line is painted, so they stay.
            self.assertIn("Ctx ████░░░░░░ 38%", trimmed)
            self.assertIn("3p-7d █░░░░░░░░░ 12%", trimmed)
            self.assertIn("gemini-7d █████░░░░░ 54%", trimmed)

    def test_the_payloads_own_width_is_what_the_line_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(self._demo(tmp), terminal_width=400)
            with mock.patch.dict(os.environ, {"COLUMNS": "40"}):
                self.assertIn("│ main │", self._first(payload))

    # -- the rungs agy has that claude has not --------------------------------

    def _cells(self, payload, columns):
        with mock.patch.dict(os.environ, {"COLUMNS": str(columns)}):
            first = sl.render_agy(payload, lines=2).split("\n")[0]
        return sl._visible_len(first), visible(first)

    def test_a_hundred_column_pane_fits_and_keeps_both_buckets(self):
        """The defect the demo GIF caught, closed.

        With only claude's five rungs agy's line was ~106 cells and the pane
        broke `working` across two rows. The `mode` rung alone is enough at 100,
        which is the point of shedding that word FIRST: the buckets are numbers
        about the account and they stay.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cells, plain = self._cells(self._demo(tmp), 100)
            self.assertLessEqual(cells, 100)
            self.assertNotIn("working", plain)
            self.assertIn("3p-7d", plain)
            self.assertIn("gemini-7d", plain)
            self.assertIn("Ctx ████░░░░░░ 38%", plain)

    def test_the_buckets_go_one_at_a_time_and_from_the_end(self):
        # `gemini-7d` before `3p-7d`, so what survives is the bucket the line
        # listed first rather than whichever happened to be last.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._demo(tmp)
            cells, plain = self._cells(payload, 80)
            self.assertLessEqual(cells, 80)
            self.assertNotIn("gemini-7d", plain)
            self.assertIn("3p-7d", plain)

    def test_the_model_and_the_bar_are_what_is_left_at_the_bottom(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._demo(tmp)
            for columns in (60, 20):
                cells, plain = self._cells(payload, columns)
                self.assertNotIn("3p-7d", plain)
                self.assertIn("Gemini 3.8 Flash (High)", plain)
                self.assertIn("Ctx ████░░░░░░ 38%", plain)

    def test_the_ladder_has_one_bucket_rung_per_bucket(self):
        # Declared rungs would spend depth on buckets that never arrived, or run
        # out on a future agy that sends three. And the account is under agy's
        # own rungs, not over them: `with_account_last` is the one place that
        # decides where it goes, for every line of every layout.
        parts = sl._parts(sl.demo_payload_agy(), None, tool="agy")
        self.assertEqual(sl.agy_ladder(parts),
                         sl.TRIM_SHARED + ("mode", "bucket", "bucket", "account"))
        self.assertEqual(sl.agy_ladder({"buckets": []}),
                         sl.TRIM_SHARED + ("mode", "account"))
        self.assertEqual(sl.TRIM_LADDER, sl.TRIM_SHARED + ("account",))
        self.assertEqual(sl.TRIM_IDENTITY[-1], "account")

    def test_shedding_leaves_the_cost_alone(self):
        # The cost is not a rung: never seen in a real payload, and four
        # characters wide.
        buckets = [{"kind": "quota", "short": "a", "full": "a"},
                   {"kind": "quota", "short": "b", "full": "b"},
                   {"kind": "mode", "short": "working", "full": "working"},
                   {"kind": "cost", "short": "$1.00", "full": "$1.00"}]
        kept = sl._kept_limits(buckets, ("mode", "bucket"))
        self.assertEqual([b["short"] for b in kept], ["a", "$1.00"])

    def test_a_segment_with_no_kind_counts_as_a_bucket(self):
        # Every one of them was a bucket before the rungs existed.
        kept = sl._kept_limits([{"short": "a", "full": "a"},
                                {"short": "b", "full": "b"}], ("bucket",))
        self.assertEqual([b["short"] for b in kept], ["a"])

    def test_shedding_more_buckets_than_there_are_is_not_an_error(self):
        self.assertEqual(sl._kept_limits([{"kind": "quota", "short": "a",
                                           "full": "a"}],
                                         ("bucket", "bucket", "bucket")), [])


class TestDemo(unittest.TestCase):

    def test_the_demo_payload_has_what_the_line_paints(self):
        p = sl.demo_payload_claude()
        self.assertEqual(p["context_window"]["used_percentage"], 42)
        self.assertEqual(p["rate_limits"]["five_hour"]["used_percentage"], 31)
        self.assertEqual(p["rate_limits"]["seven_day"]["used_percentage"], 12)
        self.assertIn("display_name", p["model"])

    def test_the_demo_stands_in_the_real_working_directory(self):
        # So the folder and the branch in the preview are the user's own.
        self.assertEqual(sl.demo_payload_claude()["cwd"], os.getcwd())

    def test_the_demo_resets_are_always_in_the_future(self):
        # It is shown at install time: a limit that reset yesterday would look
        # like a bug in the tool the user is about to say yes to.
        p = sl.demo_payload_claude()
        for bucket in p["rate_limits"].values():
            self.assertGreater(bucket["resets_at"], time.time())

    def test_it_renders_three_lines_with_turns(self):
        out = visible(sl.render_claude(sl.demo_payload_claude(), columns=0,
                                       turns=sl.DEMO_TURNS))
        first, second, third = out.rstrip("\n").split("\n")
        self.assertTrue(first.startswith("✳ Opus 4.6 · high"), first)
        self.assertIn("Ctx ██████░░░░░░░░░ 42%", second)
        self.assertIn("5h ███░░░░░░░ 31%", second)
        self.assertIn("7d █░░░░░░░░░ 12%", second)
        self.assertIn("turns %d" % sl.DEMO_TURNS, third)

    def test_the_demo_carries_the_example_account(self):
        # The demo is the full line the installer sells, so the segment is in
        # it whatever the config says -- from an address that belongs to
        # nobody (example.com is the documentation domain, RFC 2606).
        out = visible(sl.render_claude(sl.demo_payload_claude(),
                                       turns=sl.DEMO_TURNS,
                                       account=sl.DEMO_ACCOUNT))
        self.assertIn("👤 user@example.com max", out)


# The account as a test hands it in: the renderers take it injected, so nothing
# here reads a home, a config or a keychain unless the test says to.
ACCOUNT = {"email": "user@example.com", "plan": "max"}


class TestAccountEnabled(unittest.TestCase):
    """The switch. It is off unless `config.json` says exactly `true`.

    Privacy decides every degraded case: an address is never painted on a guess
    about what somebody meant to write.
    """

    def _cfg(self, value):
        return {"statusline": {"claude": "own", "account": value}}

    def test_a_hand_built_config_with_no_key_is_off(self):
        # Only `config.load()` supplies the default, and what it supplies is
        # `true`; a dict that did not come through it has said nothing, and
        # nothing is not permission to paint somebody's address.
        self.assertFalse(sl.account_enabled({"statusline": {"claude": "own"}}))

    def test_on_only_for_the_boolean(self):
        self.assertTrue(sl.account_enabled(self._cfg(True)))
        for value in (False, "true", "yes", 1, "", []):
            self.assertFalse(sl.account_enabled(self._cfg(value)), repr(value))

    def test_a_statusline_section_of_the_wrong_kind_is_off(self):
        for cfg in ({"statusline": "own"}, {"statusline": []}, {}, "x", 7):
            self.assertFalse(sl.account_enabled(cfg), repr(cfg))

    def test_with_no_config_given_it_reads_the_one_in_force(self):
        # The suite's config.json has no `account` key, so what is in force is
        # the default -- and the default is ON: the line Flightdeck paints is
        # the reference line, complete.
        self.assertTrue(sl.account_enabled())
        self.assertIs(config.DEFAULTS["statusline"]["account"], True)


class TestAccountSources(unittest.TestCase):
    """Where the two values come from.

    Nothing here touches the real home, and the macOS keychain is a callable
    the test hands in: no test of this suite may ask the developer's own.
    """

    def _home(self, tmp, claude_json=None, credentials=None):
        home = Path(tmp)
        if claude_json is not None:
            (home / ".claude.json").write_text(claude_json, encoding="utf-8")
        if credentials is not None:
            (home / ".claude").mkdir(parents=True, exist_ok=True)
            (home / ".claude" / ".credentials.json").write_text(credentials,
                                                                encoding="utf-8")
        return home

    def _never(self, argv):
        raise AssertionError("the keychain must not be asked here")

    # -- the address ---------------------------------------------------------

    def test_the_address_comes_from_claude_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, json.dumps(
                {"oauthAccount": {"emailAddress": "user@example.com"}}))
            self.assertEqual(sl.account_email(home), "user@example.com")

    def test_no_file_is_no_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sl.account_email(Path(tmp)), "")

    def test_anything_that_is_not_an_address_is_no_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ("{not json", '{"oauthAccount": "jose"}',
                         '{"oauthAccount": {}}',
                         '{"oauthAccount": {"emailAddress": 7}}',
                         '{"oauthAccount": {"emailAddress": "  "}}',
                         "[]", '"x"', ""):
                home = self._home(tmp, text)
                self.assertEqual(sl.account_email(home), "", text)

    # -- the plan ------------------------------------------------------------

    def test_the_plan_comes_from_the_credentials_file(self):
        # The file is tried first in both systems: it costs half a millisecond
        # against the keychain's fifteen, and being under HOME it is what lets
        # a test (and the demo) answer with a home of its own.
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, credentials=json.dumps(
                {"claudeAiOauth": {"subscriptionType": "max"}}))
            self.assertEqual(sl.account_plan(home, self._never), "max")

    def test_without_the_file_the_keychain_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            asked = []

            def run(argv):
                asked.append(argv)
                return json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}})

            self.assertEqual(sl.account_plan(Path(tmp), run), "pro")
            self.assertEqual(asked[0][:2], ["security", "find-generic-password"])
            self.assertIn(sl.KEYCHAIN_SERVICE, asked[0])

    def test_a_keychain_that_blows_up_is_a_plan_nobody_knows(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(argv):
                raise OSError("no such program")

            self.assertEqual(sl.account_plan(Path(tmp), boom), "")

    def test_a_keychain_that_answers_nothing_useful(self):
        # An item that is not there (`security` exits non-zero and our runner
        # gives back ""), a keychain holding something else, a key that moved.
        with tempfile.TemporaryDirectory() as tmp:
            for answer in ("", "not json", "[]", "null",
                           '{"claudeAiOauth": {}}',
                           '{"claudeAiOauth": "max"}'):
                self.assertEqual(
                    sl.account_plan(Path(tmp), lambda argv, r=answer: r), "",
                    answer)


class TestClaudeAccount(unittest.TestCase):
    """What claude's line asks for when nobody injected an account."""

    def _home(self, tmp):
        home = Path(tmp)
        (home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "user@example.com"}}),
            encoding="utf-8")
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"subscriptionType": "max"}}), encoding="utf-8")
        return home

    def test_off_is_nothing_at_all_and_nothing_is_read(self):
        def never(argv):
            raise AssertionError("nothing may be read while the segment is off")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sl.claude_account({"statusline": {"account": False}},
                                                self._home(tmp), never))

    def test_on_it_is_the_address_and_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                sl.claude_account({"statusline": {"account": True}},
                                  self._home(tmp), None),
                {"email": "user@example.com", "plan": "max"})

    def test_an_address_with_no_plan_is_still_an_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude.json").write_text(json.dumps(
                {"oauthAccount": {"emailAddress": "user@example.com"}}),
                encoding="utf-8")
            self.assertEqual(
                sl.claude_account({"statusline": {"account": True}}, home,
                                  lambda argv: ""),
                {"email": "user@example.com", "plan": ""})

    def test_no_address_is_no_account_even_with_a_plan(self):
        # An empty `👤` says nothing and costs a separator.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir(parents=True, exist_ok=True)
            (home / ".claude" / ".credentials.json").write_text(json.dumps(
                {"claudeAiOauth": {"subscriptionType": "max"}}), encoding="utf-8")
            self.assertIsNone(sl.claude_account({"statusline": {"account": True}},
                                                home, None))


class TestAgyAccount(unittest.TestCase):
    """agy carries both values in its own payload, so there is no file and no
    keychain here -- but it is the SAME switch. One option called
    `statusline.account` that answered in one tool's line and not in the
    other's would read as a bug in whichever one stayed quiet."""

    ON = {"statusline": {"account": True}}

    def test_it_comes_out_of_agys_own_payload(self):
        self.assertEqual(
            sl.agy_account({"email": "user@example.com",
                            "plan_tier": "Antigravity Starter Quota"}, self.ON),
            {"email": "user@example.com", "plan": "Antigravity Starter Quota"})

    def test_the_same_switch_covers_both_tools(self):
        self.assertIsNone(sl.agy_account({"email": "user@example.com"},
                                         {"statusline": {"account": False}}))

    def test_a_payload_with_no_address(self):
        for payload in ({}, {"email": ""}, {"email": 7}, {"plan_tier": "x"},
                        None, "rubbish"):
            self.assertIsNone(sl.agy_account(payload, self.ON), repr(payload))


class TestAccountSegment(unittest.TestCase):
    """`👤 user@example.com max` between the model and the folder, painted the
    way the reference painted it: the address in blue, the plan dimmed."""

    def _payload(self, tmp):
        return {"model": {"display_name": "Fable 5.1"},
                "effort": {"level": "xhigh"},
                "cwd": make_repo(tmp),
                "context_window": {"used_percentage": 42},
                "rate_limits": {"five_hour": {"used_percentage": 31,
                                              "resets_at": 1789545000}}}

    def test_it_sits_between_the_model_and_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = visible(sl.render_claude(self._payload(tmp), account=ACCOUNT)
                            ).split("\n")[0]
            self.assertIn("xhigh │ 👤 user@example.com max │ ", first)

    def test_the_address_is_blue_and_the_plan_is_dim(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = sl.render_claude(self._payload(tmp), account=ACCOUNT
                                     ).split("\n")[0]
            self.assertIn("%s👤 user@example.com%s" % (sl.BLUE, sl.RESET), first)
            self.assertIn("%smax%s" % (sl.DIM, sl.RESET), first)

    def test_an_account_with_no_plan_is_just_the_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = visible(sl.render_claude(
                self._payload(tmp), account={"email": "user@example.com"})
            ).split("\n")[0]
            self.assertIn("👤 user@example.com │ ", first)

    def test_no_address_paints_no_segment_and_no_spare_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            plain = sl.render_claude(payload, account={})
            self.assertNotIn("👤", plain)
            self.assertEqual(plain, sl.render_claude(payload,
                                                     account={"plan": "max"}))

    def test_with_nothing_to_find_there_is_no_segment(self):
        # The switch is on by default, so the line asks -- and the suite's home
        # has no `~/.claude.json` in it. No address is no segment, not an empty
        # `👤` and not a spare separator.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            self.assertEqual(sl.render_claude(payload),
                             sl.render_claude(payload, account={}))
            self.assertNotIn("👤", sl.render_claude(payload))

    def test_the_opt_out_takes_the_segment_off_both_lines(self):
        # `"statusline": {"account": false}` is the key for a shared screen.
        # One switch, both tools: an option that answered in one and not in the
        # other would read as a bug in whichever one stayed quiet.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sl, "_current_config",
                                  lambda: {"statusline": {"account": False}}):
            claude = sl.render_claude(self._payload(tmp))
            agy = sl.render_agy(dict(sl.demo_payload_agy(),
                                     email="user@example.com",
                                     plan_tier="max"), columns=0)
        self.assertNotIn("👤", claude)
        self.assertNotIn("👤", agy)
        self.assertNotIn("user@example.com", agy)

    def test_the_mark_is_measured_as_the_two_cells_it_takes(self):
        # Everything else the line paints is one cell; 👤 is an emoji and
        # terminals give it two. Measured short, the line would wrap in a pane
        # the ladder had just decided it fitted.
        self.assertEqual(sl._visible_len("👤"), 2)
        self.assertEqual(sl._visible_len("\033[34m👤 a\033[0m"), 4)

    def test_the_account_outlives_every_other_rung(self):
        # It is the LAST thing shed: for somebody juggling two accounts it is
        # the segment that changes, and the folder and the branch are on screen
        # elsewhere. What is under it is only what is never shed at all.
        with tempfile.TemporaryDirectory() as tmp:
            parts = sl._parts(self._payload(tmp), None, account=ACCOUNT)
            for depth in range(len(sl.TRIM_LADDER)):
                line = visible(sl._assemble(parts, drop=sl.TRIM_LADDER[:depth]))
                self.assertIn("👤", line, sl.TRIM_LADDER[:depth])
            last = visible(sl._assemble(parts, drop=sl.TRIM_LADDER))
            self.assertNotIn("👤", last)
            self.assertIn("Ctx ", last)

    def test_the_account_outlives_every_rung_of_the_identity_line_too(self):
        # The same sentence on the default layout's line 1, where the account
        # now competes with the branch, the folder and the effort instead of
        # with the bars. `with_account_last` is why it is the same sentence.
        with tempfile.TemporaryDirectory() as tmp:
            parts = sl._parts(self._payload(tmp), None, account=ACCOUNT,
                              ctx_cells=sl.CONTEXT_BAR_WIDTH)
            for depth in range(len(sl.TRIM_IDENTITY)):
                line = visible(sl._identity(parts, drop=sl.TRIM_IDENTITY[:depth]))
                self.assertIn("👤", line, sl.TRIM_IDENTITY[:depth])
            last = visible(sl._identity(parts, drop=sl.TRIM_IDENTITY))
            self.assertNotIn("👤", last)
            self.assertIn("Fable 5.1", last)

    def test_the_demo_line_keeps_the_account_in_a_hundred_column_pane(self):
        # The GIF's session frame is 100 cells wide and that picture is what
        # the account segment is being shown in.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("os.getcwd", lambda: make_repo(tmp)):
                payload = sl.demo_payload_claude()
            first = sl.render_claude(payload, columns=100, turns=sl.DEMO_TURNS,
                                     account=sl.DEMO_ACCOUNT).split("\n")[0]
            self.assertLessEqual(sl._visible_len(first), 100)
            self.assertIn("👤 user@example.com max", visible(first))

    def test_agys_line_paints_it_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(sl.demo_payload_agy(), cwd=make_repo(tmp))
            first = visible(sl.render_agy(payload, columns=0, account=ACCOUNT)
                            ).split("\n")[0]
            self.assertIn("👤 user@example.com max", first)

    def test_agys_line_sheds_its_own_rungs_before_the_account(self):
        """What survives on agy's line as the pane narrows, with the account on.

        The rung order is the whole point: agy has more to shed than claude
        (the agent state, then the quota buckets one at a time from the end),
        and every one of them goes BEFORE the address. What is left under the
        account is what is never shed at all -- the model and the context bar.
        """
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(sl.demo_payload_agy(), cwd=make_repo(tmp))

            def at(columns):
                # The compressed layout, which is where agy's own rungs live.
                first = sl.render_agy(payload, lines=2, columns=columns,
                                      account=sl.DEMO_ACCOUNT).split("\n")[0]
                self.assertLessEqual(sl._visible_len(first), columns)
                return visible(first)

            wide = at(120)
            self.assertIn("👤 user@example.com max", wide)
            self.assertIn("3p-7d", wide)          # a bucket still there
            self.assertNotIn("working", wide)     # the agent state went first

            hundred = at(100)
            self.assertIn("👤 user@example.com max", hundred)
            self.assertIn("Ctx ████░░░░░░ 38%", hundred)

            narrow = at(80)
            self.assertIn("👤 user@example.com max", narrow)
            self.assertNotIn("3p-7d", narrow)     # both buckets gone before it
            self.assertNotIn("gemini-7d", narrow)
            self.assertIn("Gemini 3.8 Flash (High)", narrow)
            self.assertIn("Ctx ████░░░░░░ 38%", narrow)

            # And only when nothing else is left does the address go.
            last = at(60)
            self.assertNotIn("👤", last)
            self.assertIn("Gemini 3.8 Flash (High)", last)
            self.assertIn("Ctx ████░░░░░░ 38%", last)

    def test_agys_line_reads_its_own_payload_when_the_switch_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(sl.demo_payload_agy(), cwd=make_repo(tmp),
                           email="user@example.com", plan_tier="Starter")
            with mock.patch.object(sl, "account_enabled", lambda cfg=None: True):
                first = visible(sl.render_agy(payload, columns=0)).split("\n")[0]
            self.assertIn("👤 user@example.com Starter", first)

    def test_claudes_line_reads_the_sources_when_the_switch_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    sl, "claude_account",
                    lambda cfg=None, home=None, run=None: dict(ACCOUNT)):
                first = visible(sl.render_claude(self._payload(tmp))
                                ).split("\n")[0]
            self.assertIn("👤 user@example.com max", first)


class _Captures:
    """Runs `main` with stdin and stdout replaced, and gives back what it wrote."""

    def _main(self, argv, stdin_text=""):
        out = io.StringIO()
        stdin = io.StringIO(stdin_text)
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stdin", stdin):
            rc = sl.main(argv)
        return rc, out.getvalue()


class TestMain(_Captures, unittest.TestCase):

    def test_claude_reads_the_payload_from_stdin(self):
        # And paints the configured layout, which by default is three lines:
        # the command is what `statusline.claude: own` runs.
        with _Utc():
            rc, out = self._main(["claude"], FIXTURE.read_text())
        self.assertEqual(rc, 0)
        self.assertIn("✳ Fable 5.1", visible(out))
        self.assertEqual(len(visible(out).rstrip("\n").split("\n")), 3)

    def test_the_demo_prints_a_caption_and_the_lines(self):
        rc, out = self._main(["--demo"])
        self.assertEqual(rc, 0)
        lines = visible(out).rstrip("\n").split("\n")
        self.assertIn("Flightdeck status line", lines[0])
        self.assertIn("Claude Code", lines[0])
        self.assertIn("Ctx ", out)
        self.assertIn("Tokens: msg ", out)

    def test_broken_json_exits_0_with_an_empty_line(self):
        # A status line that fails takes the bar down for that render; one that
        # raises gets its traceback painted where the bar should be.
        for text in ("", "   ", "not json at all", "[1,2,3]", "null"):
            rc, out = self._main(["claude"], text)
            self.assertEqual(rc, 0, text)
            self.assertEqual(out, "\n", repr(text))

    def test_a_render_that_blows_up_still_exits_0(self):
        with mock.patch.object(sl, "render_claude",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            rc, out = self._main(["claude"], FIXTURE.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "\n")

    def test_an_unknown_tool_says_so(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            rc, _ = self._main(["nosuchtool"], "{}")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err.getvalue())

    def test_no_arguments_says_so(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            rc, _ = self._main([], "")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err.getvalue())


class _Installed:
    """The three flags of `flightdeck statusline`, on throwaway files.

    `SETTINGS_PATH`, `FLIGHTDECK_CONFIG` and `FLIGHTDECK_STATE_DIR` all point
    inside one temporary directory: these tests write a status line mode and a
    settings.json, and neither may be the ones the person running the suite is
    working in.
    """

    def _managed(self, argv, settings=None, install=None):
        """Run `main(argv)` with everything pointed at temps. -> (rc, out, err)."""
        from flightdeck.install import claude as installer
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            if settings is not None:
                path.write_text(json.dumps(settings))
            env = {"FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                   "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}
            with mock.patch.object(installer, "SETTINGS_PATH", path), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                if install is not None:
                    installer.install(install)
                out.truncate(0), out.seek(0)     # the installer's own output
                rc = sl.main(argv)
                written = installer._read_settings()
                mode = installer.configured_mode()
        self.written, self.mode = written, mode
        return rc, out.getvalue(), err.getvalue()


class TestModeFlag(_Installed, unittest.TestCase):

    def test_it_writes_the_mode_and_says_which(self):
        for mode in ("own", "wrap", "stack"):
            rc, out, _ = self._managed(["--mode", mode])
            self.assertEqual(rc, 0, mode)
            self.assertEqual(self.mode, mode)
            self.assertIn(mode, out)

    def test_an_equals_sign_works_too(self):
        rc, _, _ = self._managed(["--mode=stack"])
        self.assertEqual((rc, self.mode), (0, "stack"))

    def test_a_mode_that_does_not_exist_is_one_line_and_no_traceback(self):
        rc, _, err = self._managed(["--mode", "fancy"])
        self.assertEqual(rc, 2)
        self.assertIn("fancy", err)
        self.assertNotIn("Traceback", err)

    def test_the_mode_with_no_value_is_a_usage_error(self):
        rc, _, err = self._managed(["--mode"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_a_config_it_cannot_write_is_one_line_and_exit_1(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "config.json"
            broken.write_text('{"menu_port": 43000,,}')
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(broken)}), \
                    mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                rc = sl.main(["--mode", "own"])
            self.assertEqual(broken.read_text(), '{"menu_port": 43000,,}')
        self.assertEqual(rc, 1)
        self.assertEqual(len(err.getvalue().rstrip("\n").split("\n")), 1)
        self.assertNotIn("Traceback", err.getvalue())


class TestRestoreFlag(_Installed, unittest.TestCase):

    def test_it_puts_the_users_own_command_back(self):
        rc, out, _ = self._managed(["--restore"],
                                   settings={"statusLine": "sh mine.sh"},
                                   install="own")
        self.assertEqual(rc, 0)
        self.assertEqual(self.written["statusLine"],
                         {"type": "command", "command": "sh mine.sh"})
        self.assertIn("sh mine.sh", out)

    def test_with_nothing_to_restore_it_says_so_and_still_exits_0(self):
        # Nothing went wrong: there simply was no line of theirs to give back.
        rc, out, _ = self._managed(["--restore"], settings={}, install="own")
        self.assertEqual(rc, 0)
        self.assertIn("uninstall", out)


class TestStatusFlag(_Installed, unittest.TestCase):

    def test_it_names_the_mode_and_the_saved_line(self):
        rc, out, _ = self._managed(["--status"],
                                   settings={"statusLine": "sh mine.sh"},
                                   install="stack")
        self.assertEqual(rc, 0)
        self.assertIn("stack", out)
        self.assertIn("sh mine.sh", out)
        self.assertIn("yes", out)          # the tee is registered

    def test_with_nothing_installed_it_says_the_default_and_no(self):
        rc, out, _ = self._managed(["--status"], settings={})
        self.assertEqual(rc, 0)
        self.assertIn("own", out)          # the default mode
        self.assertIn("no", out)           # the tee is not registered

    def test_a_settings_file_it_cannot_read_prints_nothing_at_all(self):
        # All or nothing: two lines of report followed by an error line reads
        # as though the report were true up to there.
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{ not json at all")
            from flightdeck.install import claude as installer
            env = {"FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                   "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}
            with mock.patch.object(installer, "SETTINGS_PATH", path), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                rc = sl.main(["--status"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().rstrip("\n").split("\n")), 1)

    def test_the_flags_do_not_paint_a_status_line(self):
        # They are a management command sharing the entry point; printing a
        # bar's worth of ANSI into someone's terminal would be nonsense.
        rc, out, _ = self._managed(["--status"], settings={})
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)


# ── agy (Antigravity) ────────────────────────────────────────────────────────
# AGY_FIXTURE is a REAL payload, captured from agy 1.1.28 with paths, id and
# address anonymised.
# Asserting against it rather than against a hand-written dict is the point: the
# adapter is only worth what the measurement says.
AGY_FIXTURE = Path(__file__).parent / "fixtures" / "agy_statusline.json"


def agy_payload():
    return json.loads(AGY_FIXTURE.read_text())


class TestRenderAgy(unittest.TestCase):
    """What agy's payload turns into, segment by segment.

    Read in the COMPRESSED layout (`lines: 2`), where every segment lands on one
    row and one assertion can see all of them. The segments themselves are the
    same objects in both layouts -- `_quota_segments`, `_mode_segments` and
    `_cost_segments` are built once, in `_parts` -- so this is where they are
    pinned, and `TestThreeLineTrimming` pins how the default splits them across
    agy's three lines.
    """

    def first_line(self, payload=None, **kw):
        """UNTRIMMED, because these tests are about what the line says.

        The fixture is a real payload and carries a `terminal_width`, which the
        line obeys: with the account segment on it no longer fits that width,
        so without `columns=0` half of these assertions would be measuring the
        ladder instead of the segment they name. `fitted_line` is the one that
        lets the width bite.
        """
        payload = agy_payload() if payload is None else payload
        kw.setdefault("lines", 2)
        kw.setdefault("columns", 0)
        return visible(sl.render_agy(payload, **kw)).rstrip("\n").split("\n")[0]

    def fitted_line(self, payload=None, **kw):
        """Trimmed to whatever width the payload -- or the caller -- gives."""
        payload = agy_payload() if payload is None else payload
        kw.setdefault("lines", 2)
        return visible(sl.render_agy(payload, **kw)).rstrip("\n").split("\n")[0]

    def test_it_paints_agys_glyph_model_and_effort(self):
        line = self.first_line()
        self.assertIn("✦ Gemini 3.8 Flash (High)", line)
        self.assertIn("· high", line)     # model.effort, not claude's effort.level
        self.assertIn("✦", sl.render_agy(agy_payload(), lines=2, columns=0))
        # agy's colour (69), not claude's clay
        self.assertIn("\033[38;5;69m",
                      sl.render_agy(agy_payload(), lines=2, columns=0))

    def test_the_context_bar_comes_from_used_percentage(self):
        # The measurement's punchline: agy sends claude's own key, a float
        # (2.447509765625), so the bar and the % need no conversion.
        self.assertIn("Ctx ░░░░░░░░░░ 2%", self.first_line())

    def test_the_context_falls_back_to_remaining_percentage(self):
        payload = agy_payload()
        del payload["context_window"]["used_percentage"]
        self.assertIn("Ctx ███████░░░ 68%",
                      self.first_line(dict(payload, context_window=dict(
                          payload["context_window"], remaining_percentage=32))))

    def test_it_paints_the_folder(self):
        self.assertIn("/home/user/projects/demo", self.first_line())

    def test_the_quota_buckets_are_the_limits_slot(self):
        # remaining_fraction is what is LEFT: the bar shows what is spent, the
        # way claude's used_percentage does, or the two tools' lines would read
        # in opposite directions.
        payload = agy_payload()
        payload["quota"]["gemini-weekly"]["remaining_fraction"] = 0.37
        with _Utc():
            line = self.first_line(payload)
        self.assertIn("3p-7d ░░░░░░░░░░ 0%", line)
        self.assertIn("gemini-7d ██████░░░░ 63%", line)

    def test_the_reset_time_is_agys_rfc3339_string_in_local_time(self):
        # agy sends `2026-09-23T04:55:56Z`, claude sends an epoch: same ↻ out.
        with _Utc():
            self.assertIn("↻23/09 04:55", self.first_line())

    def test_a_reset_time_that_cannot_be_read_still_paints_the_bucket(self):
        payload = agy_payload()
        payload["quota"]["3p-weekly"]["reset_time"] = "whenever"
        payload["quota"]["gemini-weekly"]["reset_time"] = None
        line = self.first_line(payload)
        self.assertIn("3p-7d", line)
        self.assertIn("gemini-7d", line)
        self.assertNotIn("↻", line)

    def test_a_bucket_with_no_fraction_is_left_out(self):
        # Unlike claude's (painted at 0%): there, a missing percentage still
        # means a bucket that exists; here the fraction IS the bucket.
        payload = agy_payload()
        payload["quota"]["3p-weekly"] = {"reset_in_seconds": 10}
        line = self.first_line(payload)
        self.assertNotIn("3p-7d", line)
        self.assertIn("gemini-7d", line)

    def test_with_no_quota_at_all_it_says_limits_are_not_available(self):
        payload = agy_payload()
        del payload["quota"]
        payload["agent_state"] = None
        self.assertIn(sl.LIMITS_NA, self.first_line(payload))

    def test_the_agent_state_is_painted(self):
        self.assertIn("idle", self.first_line())
        self.assertIn("working",
                      self.first_line(dict(agy_payload(), agent_state="working")))

    def test_the_sandbox_shows_only_when_it_is_on(self):
        self.assertNotIn("sandbox", self.first_line())
        self.assertIn("sandbox",
                      self.first_line(dict(agy_payload(),
                                           sandbox={"enabled": True})))

    def test_the_cost_is_painted_as_a_number_or_as_an_object(self):
        # Never measured (nothing on a Starter Quota plan) and `omitempty` in
        # agy's own tags, so both shapes the binary suggests are accepted.
        self.assertIn("$0.42", self.first_line(dict(agy_payload(), cost=0.42)))
        self.assertIn("$1.30",
                      self.first_line(dict(agy_payload(),
                                           cost={"estimated": 1.3})))
        self.assertIn("$2.00",
                      self.first_line(dict(agy_payload(), cost={"total": 2})))
        for odd in ("free", {}, {"estimated": "lots"}, None, True):
            self.assertNotIn("$", self.first_line(dict(agy_payload(), cost=odd)))

    def test_the_email_comes_from_the_payload_now_that_the_segment_is_on(self):
        # The fixture is a real payload, so it carries the signed-in address and
        # plan; the default paints them, as the reference line does. What takes
        # them off a shared screen is `"statusline": {"account": false}`.
        out = visible(sl.render_agy(agy_payload(), lines=2, columns=0))
        self.assertIn("👤 user@example.com Antigravity Starter Quota", out)
        with mock.patch.object(sl, "_current_config",
                               lambda: {"statusline": {"account": False}}):
            off = visible(sl.render_agy(agy_payload(), lines=2, columns=0))
        self.assertNotIn("user@example.com", off)

    def test_the_token_line_is_the_tokens_without_turns(self):
        # agy's transcript_path points at a directory that does not exist in
        # 1.1.28, and its transcript is a diary of steps with no message rows:
        # there is no turn count to show and none is invented.
        out = visible(sl.render_agy(agy_payload(), lines=2))
        second = out.rstrip("\n").split("\n")[1]
        self.assertEqual(second,
                         "Tokens: msg 18.2k │ cache 0 │ session 25.7k")
        self.assertNotIn("turns", out)

    def test_a_turn_that_has_not_finished_has_no_usage_yet(self):
        # current_usage arrives as null until the first turn lands (measured).
        payload = agy_payload()
        payload["context_window"]["current_usage"] = None
        second = visible(sl.render_agy(payload, lines=2)
                         ).rstrip("\n").split("\n")[1]
        self.assertIn("msg 0", second)
        self.assertIn("cache 0", second)

    def test_one_line_when_asked_for_one(self):
        out = sl.render_agy(agy_payload(), lines=1)
        self.assertEqual(len(out.rstrip("\n").split("\n")), 1)
        self.assertTrue(out.endswith("\n"))

    def test_it_trims_to_the_terminal_width_agy_sends(self):
        # agy is the one tool that says how wide the pane is; with no terminal
        # of our own to ask, taking its word beats wrapping the line. The
        # branch is the first rung of the ladder, the context bar is never
        # dropped however narrow it gets.
        wide = self.fitted_line(dict(agy_payload(), terminal_width=400))
        narrow = self.fitted_line(dict(agy_payload(), terminal_width=54))
        self.assertIn("no branch", wide)
        self.assertNotIn("no branch", narrow)
        self.assertIn("Ctx ", narrow)
        # At 54 the agy tail has been spent as well (`TRIM_TAIL_AGY`): the word
        # the agent is doing first, then both quota buckets from the end. What
        # stands at the bottom is the model and the bar, by design.
        self.assertNotIn("3p-7d", narrow)

    def test_an_explicit_width_beats_the_one_in_the_payload(self):
        payload = dict(agy_payload(), terminal_width=40)
        self.assertIn("no branch", self.fitted_line(payload, columns=400))
        self.assertNotIn("no branch", self.fitted_line(payload))

    def test_columns_zero_means_do_not_trim_as_it_does_for_claude(self):
        # The two renderers have to be swappable for a caller that does not know
        # which tool it is holding, and 0 is `render_claude`'s "no width given".
        payload = dict(agy_payload(), terminal_width=40)
        self.assertIn("no branch", self.fitted_line(payload, columns=0))

    def test_an_unusable_terminal_width_simply_does_not_trim(self):
        for width in ("wide", 0, -5, None, True):
            line = self.fitted_line(dict(agy_payload(), terminal_width=width))
            self.assertIn("no branch", line)
            self.assertIn("3p-7d", line)

    def test_an_empty_payload_paints_a_line_instead_of_raising(self):
        for payload in ({}, {"context_window": "nonsense"}, None, [1, 2, 3]):
            for lines in (None, 3, 2, 1):
                out = visible(sl.render_agy(payload, lines=lines))
                self.assertIn("Ctx", out)
                self.assertIn(sl.LIMITS_NA, out)

    def test_the_agy_demo_is_honest_and_ready_to_show(self):
        payload = sl.demo_payload_agy()
        # Its folder is the real working directory, for the same reason as
        # claude's: the demo is shown while asking whether to use this line.
        self.assertEqual(payload["cwd"], os.getcwd())
        self.assertIn("Ctx ", visible(sl.render_agy(payload, lines=2)))
        # Nothing invented: no cost, because its shape was never measured.
        self.assertNotIn("cost", payload)
        for bucket in payload["quota"].values():
            self.assertLessEqual(bucket["remaining_fraction"], 1)
            self.assertIn("Z", bucket["reset_time"])


class TestManagingAgyFromTheCommandLine(unittest.TestCase):
    """The three management flags with `--tool agy`.

    The one that matters is `--mode`: for agy half the mode lives in agy's own
    settings (`stack_with_default`), so a CLI that wrote only Flightdeck's
    config would give two silent wrong states -- a `stack` that paints like
    `own`, and an `own` with agy still stacking its default line.

    Everything is temporary: agy's settings, Flightdeck's config and the state
    directory.
    """

    def _managed(self, argv, settings=None, install=None):
        """Run `main(argv)` with agy's files pointed at temps. -> (rc, out, err)."""
        from flightdeck.install import agy as installer
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            if settings is not None:
                path.write_text(json.dumps(settings))
            env = {"FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                   "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}
            with mock.patch.object(installer, "SETTINGS_PATH", path), \
                    mock.patch.object(installer, "HOOKS_JSON",
                                      Path(tmp) / "hooks.json"), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                if install is not None:
                    installer.install(install)
                out.truncate(0), out.seek(0)     # the installer's own output
                rc = sl.main(argv)
                self.written = installer._read_settings()
                self.mode = installer.configured_mode()
        return rc, out.getvalue(), err.getvalue()

    def test_the_mode_writes_both_halves(self):
        rc, out, _ = self._managed(["--mode", "stack", "--tool", "agy"],
                                   settings={}, install="own")
        self.assertEqual((rc, self.mode), (0, "stack"))
        self.assertIs(self.written["statusLine"]["stack_with_default"], True)
        self.assertIn("stack", out)

    def test_leaving_stack_clears_the_flag(self):
        rc, _, _ = self._managed(["--mode=own", "--tool=agy"],
                                 settings={}, install="stack")
        self.assertEqual((rc, self.mode), (0, "own"))
        self.assertNotIn("stack_with_default", self.written["statusLine"])

    def test_status_reports_agys_line(self):
        rc, out, _ = self._managed(["--status", "--tool", "agy"], settings={},
                                   install="own")
        self.assertEqual(rc, 0)
        self.assertIn("mode: own", out)
        self.assertIn("status line registered: yes", out)

    def test_restore_goes_to_agys_installer(self):
        rc, out, _ = self._managed(
            ["--restore", "--tool", "agy"],
            settings={"statusLine": {"type": "command", "command": "sh mine.sh"}},
            install="own")
        self.assertEqual(rc, 0)
        self.assertEqual(self.written["statusLine"]["command"], "sh mine.sh")
        self.assertIn("restored", out)

    def test_a_tool_there_is_no_line_for_is_a_usage_error(self):
        for argv in (["--status", "--tool", "codex"], ["--mode", "own", "--tool"],
                     ["--status", "--tool=nonsense"]):
            rc, _, err = self._managed(argv, settings={})
            self.assertEqual(rc, 2, argv)
            self.assertIn("usage", err)

    def test_without_the_flag_it_is_still_claudes(self):
        # claude's settings go to a temp of their own: no test may read (or
        # write) the settings.json of whoever is running the suite.
        from flightdeck.install import claude as other
        with tempfile.TemporaryDirectory() as elsewhere:
            with mock.patch.object(other, "SETTINGS_PATH",
                                   Path(elsewhere) / "settings.json"):
                rc, out, _ = self._managed(["--status"], settings={},
                                           install="own")
        self.assertEqual(rc, 0)
        # claude's word, and claude's answer: agy's line is registered in the
        # temp settings, claude's tee is not.
        self.assertIn("tee registered: no", out)


class TestEpochFromIso(unittest.TestCase):

    def test_agys_utc_z(self):
        # `calendar.timegm` and not `time.mktime`: the string is UTC whatever
        # timezone the suite runs in.
        self.assertEqual(sl.epoch_from_iso("2026-09-23T04:55:56Z"),
                         calendar.timegm((2026, 9, 23, 4, 55, 56, 0, 0, 0)))

    def test_an_offset_and_fractional_seconds(self):
        self.assertEqual(sl.epoch_from_iso("2026-09-23T06:55:56+02:00"),
                         sl.epoch_from_iso("2026-09-23T04:55:56Z"))
        self.assertEqual(sl.epoch_from_iso("2026-09-23T04:55:56.500Z"),
                         sl.epoch_from_iso("2026-09-23T04:55:56Z") + 0.5)

    def test_anything_else_is_none(self):
        for text in ("", "   ", "whenever", None, 17, {}, "2026-13-45T99:99:99Z"):
            self.assertIsNone(sl.epoch_from_iso(text))


class TestMainAgy(_Captures, unittest.TestCase):

    def test_agy_reads_the_payload_from_stdin(self):
        # Three lines by default, the same layout claude's command paints.
        with _Utc():
            rc, out = self._main(["agy"], AGY_FIXTURE.read_text())
        self.assertEqual(rc, 0)
        self.assertIn("✦ Gemini 3.8 Flash (High)", visible(out))
        self.assertEqual(len(visible(out).rstrip("\n").split("\n")), 3)

    def test_broken_json_for_agy_exits_0_with_an_empty_line(self):
        for text in ("", "not json at all", "[1,2,3]", "null"):
            rc, out = self._main(["agy"], text)
            self.assertEqual(rc, 0, text)
            self.assertEqual(out, "\n", repr(text))

    def test_an_agy_render_that_blows_up_still_exits_0(self):
        with mock.patch.object(sl, "render_agy",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            rc, out = self._main(["agy"], AGY_FIXTURE.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "\n")

    def test_the_demo_shows_both_tools_each_with_its_caption(self):
        rc, out = self._main(["--demo"])
        self.assertEqual(rc, 0)
        text = visible(out)
        self.assertIn("Claude Code", text)
        self.assertIn("Antigravity", text)
        self.assertIn("✳", text)
        self.assertIn("✦", text)
        # Each demo is three lines under its own caption, and one line at the
        # bottom says how to take the account off: 2 captions + 6 lines + 1.
        self.assertEqual(len([l for l in text.split("\n") if l.strip()]), 9)

    def test_the_demo_shows_the_account_and_says_how_to_take_it_off(self):
        # It is the full line Flightdeck ships, so both tools' lines carry the
        # segment -- from an address that belongs to nobody -- and the note
        # underneath is the one key for a shared screen.
        rc, out = self._main(["--demo"])
        self.assertEqual(rc, 0)
        text = visible(out)
        self.assertEqual(text.count("👤 user@example.com max"), 2)
        note = text.rstrip("\n").split("\n")[-1]
        self.assertIn("account", note)
        self.assertIn("statusline", note)
        self.assertIn("false", note)

    def test_the_demo_is_the_default_layout_whatever_the_config_says(self):
        # A preview that quietly matched a config the user has not agreed to
        # would be a preview of the wrong thing: it shows what Flightdeck ships.
        with mock.patch.object(sl, "_current_config",
                               lambda: {"statusline": {"lines": 2,
                                                       "account": False}}):
            rc, out = self._main(["--demo"])
        self.assertEqual(rc, 0)
        text = visible(out)
        self.assertEqual(len([l for l in text.split("\n") if l.strip()]), 9)
        self.assertEqual(text.count("👤 user@example.com max"), 2)

    def test_it_can_be_narrowed_to_the_tools_a_machine_has(self):
        # What `flightdeck install` shows before it asks: a line for a tool
        # nobody has installed is a picture of nothing they can use.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sl._demo(("claude",))
        text = visible(buf.getvalue())
        self.assertIn("Claude Code", text)
        self.assertNotIn("Antigravity", text)


if __name__ == "__main__":
    unittest.main()
