"""`scripts/ansi2html.py`: a captured tmux screen -> HTML that looks like it.

The converter is the only piece of the demo-GIF pipeline that can be tested
here: the rest of `scripts/demo_gif.py` needs Chrome, a tmux server and a real
fzf, and lives in the thin-layer-with-no-tests half of the project like the
picker's loop does.

What it has to get right is narrow and worth pinning: the two escape languages a
tmux screen is painted with (ANSI SGR and tmux's own `#[…]` style tags), the
xterm colour table they name colours from, HTML escaping, and the cell widths
that keep a browser's columns where the terminal put them.
"""
import importlib.util
import unittest
from pathlib import Path

# `scripts/` is not a package (it holds two standalone tools, not a library), so
# the module is loaded by path rather than imported by name.
_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ansi2html.py"
_SPEC = importlib.util.spec_from_file_location("ansi2html", _PATH)
a2h = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(a2h)


def runs(text):
    """The (style, text) runs of a one-line screen."""
    lines = a2h.tokenize(text)
    return lines[0] if lines else []


def styles(text):
    return [style for style, _ in runs(text)]


def words(text):
    return [chunk for _, chunk in runs(text)]


class TestThePalette(unittest.TestCase):
    """The xterm 256-colour table, which is where every colour number lands."""

    def test_it_has_all_256(self):
        self.assertEqual(len(a2h.XTERM_256), 256)

    def test_the_theme_sixteen_come_first(self):
        """0-15 are the theme's, not the specification's (see BASE_16), and the
        one that matters is red: xterm's `#cd0000` is unreadable on a dark bar,
        which is where the status line's `🧠 82%` is painted."""
        self.assertEqual(a2h.XTERM_256[:16], a2h.BASE_16)
        self.assertEqual(a2h.XTERM_256[1], "#cd3131")

    def test_the_cube_starts_at_sixteen(self):
        """16 is the corner of the 6x6x6 cube, and its levels are not evenly
        spaced: the first step is 95, the rest are 40 apart."""
        self.assertEqual(a2h.XTERM_256[16], "#000000")
        self.assertEqual(a2h.XTERM_256[21], "#0000ff")
        self.assertEqual(a2h.XTERM_256[231], "#ffffff")

    def test_the_grey_ramp_is_the_last_twenty_four(self):
        self.assertEqual(a2h.XTERM_256[232], "#080808")
        self.assertEqual(a2h.XTERM_256[255], "#eeeeee")

    def test_a_number_outside_the_table_is_no_colour(self):
        self.assertIsNone(a2h.colour_hex(400))
        self.assertIsNone(a2h.colour_hex(None))

    def test_an_explicit_colour_passes_through(self):
        self.assertEqual(a2h.colour_hex("#ff8700"), "#ff8700")


class TestSGR(unittest.TestCase):
    """`ESC[…m`, which is what `capture-pane -e` hands back."""

    def test_a_basic_foreground(self):
        self.assertEqual(styles("\x1b[32mgreen")[0][0], 2)

    def test_a_bright_foreground(self):
        """90-97 are the bright half of the first sixteen: 90 is colour 8, the
        grey the history rows are painted in."""
        self.assertEqual(styles("\x1b[90mgrey")[0][0], 8)

    def test_a_background(self):
        self.assertEqual(styles("\x1b[41mred")[0][1], 1)

    def test_two_hundred_and_fifty_six_colour(self):
        """`38;5;N`, which is how the tool glyphs get their brand colour."""
        self.assertEqual(styles("\x1b[38;5;173m✳")[0][0], 173)

    def test_true_colour(self):
        self.assertEqual(styles("\x1b[38;2;217;119;87mx")[0][0], "#d97757")

    def test_bold_and_dim_and_reset(self):
        style = styles("\x1b[1m\x1b[2mx")[0]
        self.assertTrue(style[2])          # bold
        self.assertTrue(style[3])          # dim
        self.assertEqual(styles("\x1b[1;32m\x1b[0mx")[0], a2h.DEFAULT_STYLE)

    def test_a_bare_reset_escape(self):
        """`ESC[m` with no numbers is a reset, not a no-op."""
        self.assertEqual(a2h.apply_sgr((2, None, True, False, False, False, False), []),
                         a2h.DEFAULT_STYLE)

    def test_twenty_two_takes_off_bold_and_dim_only(self):
        style = a2h.apply_sgr(a2h.DEFAULT_STYLE, ["32", "1", "2", "22"])
        self.assertEqual(style[0], 2)
        self.assertFalse(style[2])
        self.assertFalse(style[3])

    def test_thirty_nine_and_forty_nine_go_back_to_the_defaults(self):
        style = a2h.apply_sgr(a2h.DEFAULT_STYLE, ["31", "42", "39", "49"])
        self.assertIsNone(style[0])
        self.assertIsNone(style[1])

    def test_an_unknown_code_loses_only_itself(self):
        """A screen carrying one escape we do not model should lose that one
        effect, not everything painted after it."""
        style = a2h.apply_sgr(a2h.DEFAULT_STYLE, ["53", "32"])
        self.assertEqual(style[0], 2)

    def test_a_truncated_extended_colour_does_not_eat_the_rest(self):
        """`38;5` with nothing behind it is a screen cut off mid-escape: the
        numbers after it must not be read as a colour."""
        style = a2h.apply_sgr(a2h.DEFAULT_STYLE, ["38", "5"])
        self.assertIsNone(style[0])

    def test_a_cursor_move_is_dropped_not_printed(self):
        """Only `m` changes colour; a still frame has no cursor to move."""
        self.assertEqual(words("a\x1b[2Kb"), ["a", "b"])

    def test_a_title_sequence_is_dropped(self):
        self.assertEqual(words("\x1b]0;a title\x07text"), ["text"])

    def test_the_style_carries_over_to_the_next_line(self):
        """A colour opened at the end of one row is still open on the next, the
        way a terminal reads it."""
        lines = a2h.tokenize("\x1b[32mgreen\nstill green")
        self.assertEqual(lines[1][0][0][0], 2)


class TestTmuxTags(unittest.TestCase):
    """`#[fg=yellow]`, which is what a status line format holds."""

    def test_a_named_colour(self):
        self.assertEqual(styles("#[fg=yellow]⏳ 2 waiting")[0][0], 3)

    def test_a_numbered_colour(self):
        self.assertEqual(styles("#[bg=colour235]bar")[0][1], 235)
        self.assertEqual(a2h.tmux_colour("color36"), 36)

    def test_bold_and_default(self):
        self.assertTrue(styles("#[bold] flightdeck ")[0][2])
        self.assertEqual(
            a2h.apply_tmux_tag((1, 2, True, False, False, False, False), "default"),
            a2h.DEFAULT_STYLE)

    def test_several_words_are_read_left_to_right(self):
        style = a2h.apply_tmux_tag(a2h.DEFAULT_STYLE, "fg=red,bg=colour235,bold")
        self.assertEqual((style[0], style[1], style[2]), (1, 235, True))

    def test_default_then_a_colour_keeps_the_colour(self):
        style = a2h.apply_tmux_tag((2, None, True, False, False, False, False),
                                   "default,fg=red")
        self.assertEqual(style[0], 1)
        self.assertFalse(style[2])

    def test_an_unknown_colour_word_is_the_terminal_default(self):
        self.assertIsNone(a2h.tmux_colour("chartreuse"))
        self.assertIsNone(a2h.tmux_colour("colour999"))

    def test_a_doubled_hash_never_opens_a_style(self):
        """`##[fg=red]` is how tmux spells a literal `#[fg=red]`: it has to come
        out as text. A `#` that is only next to another `#` is left alone,
        because in a captured screen -- which is what this converts -- tmux has
        already expanded its escapes and `foo##2` really is two hashes."""
        self.assertEqual(words("##[fg=red]x"), ["##[fg=red]x"])
        self.assertEqual(words("foo##2"), ["foo##2"])

    def test_a_lone_hash_is_text(self):
        self.assertEqual(words("3 waiting: foo#2"), ["3 waiting: foo#2"])


class TestWidths(unittest.TestCase):
    """The cells tmux counted, which the page has to reproduce exactly."""

    def test_an_emoji_takes_two(self):
        for char in "⏳🧠❓🔁📊":
            self.assertEqual(a2h.cell_width(char), 2, char)

    def test_the_tool_glyphs_and_the_box_drawing_take_one(self):
        for char in "✳⬡✦◇▹─│█":
            self.assertEqual(a2h.cell_width(char), 1, char)

    def test_a_variation_selector_takes_none(self):
        """The invisible `U+FE0F` asks for a colour glyph; the symbol in front
        of it keeps the width the terminal counted."""
        self.assertEqual(a2h.cell_width("️"), 0)
        self.assertEqual(a2h.run_width("⚙️"), 1)

    def test_a_whole_row_adds_up(self):
        self.assertEqual(a2h.run_width("🧠82%"), 5)


class TestHtml(unittest.TestCase):
    def test_text_is_escaped(self):
        self.assertIn("&lt;script&gt;", a2h.body_html("<script>"))
        self.assertIn("&amp;", a2h.body_html("a & b"))

    def test_an_escaped_character_is_not_boxed_as_wide(self):
        """`&` is ASCII: it flows in the monospace grid and gets no box."""
        self.assertNotIn('class="w"', a2h.body_html("a & b"))

    def test_a_colour_becomes_a_span(self):
        self.assertIn('style="color:#0dbc79"', a2h.body_html("\x1b[32mok"))

    def test_a_wide_character_is_boxed_to_its_cells(self):
        self.assertIn('class="w" style="width:2ch"', a2h.body_html("🧠"))

    def test_a_run_of_narrow_non_ascii_is_boxed_to_its_total(self):
        """A run is boxed once, not character by character: a bar of box-drawing
        characters has to stay one continuous line."""
        html = a2h.body_html("──────────")
        self.assertEqual(html.count('class="w"'), 1)
        self.assertIn("width:10ch", html)

    def test_reverse_video_swaps_against_the_page(self):
        """fzf paints the row under the cursor by reversing, and it names only
        one of the two colours: the other has to come from the page."""
        html = a2h.body_html("\x1b[7mcursor")
        self.assertIn("color:var(--bg)", html)
        self.assertIn("background:var(--fg)", html)

    def test_dim_is_an_opacity(self):
        self.assertIn("opacity:.6", a2h.body_html("\x1b[2mfaint"))

    def test_the_page_is_one_standalone_file(self):
        page = a2h.render_page("\x1b[32mhello", title="frame 1")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>frame 1</title>", page)
        self.assertIn("Apple Color Emoji", page)
        self.assertIn("#0dbc79", page)
        # Everything is inline: the screenshot is taken with no network.
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_the_title_is_escaped_too(self):
        self.assertIn("&lt;b&gt;", a2h.render_page("x", title="<b>"))

    def test_an_empty_screen_is_an_empty_page_body(self):
        self.assertEqual(a2h.body_html(""), "")


if __name__ == "__main__":
    unittest.main()
