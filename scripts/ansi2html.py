#!/usr/bin/env python3
"""Terminal text -> one standalone HTML page that looks like that terminal.

It understands the two things a tmux screen is painted with:

- **ANSI SGR escapes** (`ESC[…m`), which is what `tmux capture-pane -e -p`
  gives back: the 8 basic colours, the bright ones, `38;5;N` / `48;5;N` from the
  256-colour cube, `38;2;r;g;b`, bold, dim, italic, underline and reverse.
- **tmux style tags** (`#[fg=colour235,bg=red,bold]`), which is what a status
  line format holds before tmux turns it into colour. They set the same state as
  the SGR escapes, so a bar composed from `#{T:status-right}` renders like the
  rest.

The two are for two different inputs, and only one of them is on the pipeline's
path. A CAPTURE carries SGR escapes: tmux has already turned its formats into
colour, which is what `demo_gif.py` photographs. The tag half is for a FORMAT
STRING somebody expands themselves (`display-message -p '#{T:status-right}'`),
which is how the bar would have to be built without a client to capture. It is
kept and tested because it is the half a different pipeline would need and
because this file is usable on its own.

NEEDS: nothing but python 3.9's standard library. The page it writes is
screenshotted by headless Chrome in `scripts/demo_gif.py`; see that file for the
whole pipeline and how to re-run it:

    python3 scripts/demo_gif.py --out docs/img

On its own it converts stdin to a page on stdout:

    tmux -L <socket> capture-pane -e -p -t <pane> | python3 scripts/ansi2html.py > frame.html

**Why every non-ASCII character is boxed.** tmux lays its screen out in cells
and it already decided that `🧠` takes two of them and `─` takes one. A browser
knows nothing about that: it gives every glyph the advance its font happens to
have, and one emoji that renders a fraction wide drags the whole rest of the
line out of its column. So each run of non-ASCII characters is wrapped in an
inline-block whose width is exactly the number of cells tmux counted
(`cell_width`), and the next column starts where the terminal put it. ASCII is
left alone: in a monospace font it is already one cell per character.
"""

import html
import re
import sys
import unicodedata

# ---------------------------------------------------------------------------
# The 256-colour table every colour number lands in. 16-231 are a 6x6x6 cube and
# 232-255 a 24-step grey ramp, both of them fixed by the xterm specification and
# built here rather than typed out: 240 hand-written hex strings are 240 chances
# to fat-finger one.
#
# The first sixteen are not fixed by anything -- they are exactly the part of the
# table a terminal THEME chooses, which is why the same `ESC[31m` is scarlet in
# one terminal and maroon in another. The ones below are Visual Studio Code's
# default dark terminal theme, picked for one reason: the page's background is
# `#1e1e1e`, which is the background that theme was drawn against. xterm's own
# originals were tried first and one of them does not survive a dark background
# -- its red is `#cd0000`, and the status bar's `🧠 payments 82%` came out at
# about a 2:1 contrast ratio, which is a warning nobody can read. Changing what
# the STATUS BAR asks for was never an option: that is the product's decision,
# and a screenshot does not get to edit it.
# ---------------------------------------------------------------------------

BASE_16 = (
    "#000000", "#cd3131", "#0dbc79", "#e5e510",
    "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
    "#666666", "#f14c4c", "#23d18b", "#f5f549",
    "#3b8eea", "#d670d6", "#29b8db", "#e5e5e5",
)

# The six levels of each channel in the colour cube. They are not evenly spaced:
# the first step is 95 and the rest are 40 apart, which is xterm's own table.
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def _build_palette():
    palette = list(BASE_16)
    for r in _CUBE_LEVELS:
        for g in _CUBE_LEVELS:
            for b in _CUBE_LEVELS:
                palette.append("#%02x%02x%02x" % (r, g, b))
    for i in range(24):
        level = 8 + i * 10
        palette.append("#%02x%02x%02x" % (level, level, level))
    return tuple(palette)


XTERM_256 = _build_palette()

# The colour words tmux accepts in a `#[fg=…]` tag, in the order of the palette.
COLOUR_NAMES = {
    "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4,
    "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10, "brightyellow": 11,
    "brightblue": 12, "brightmagenta": 13, "brightcyan": 14, "brightwhite": 15,
}


def colour_hex(value):
    """`#rrggbb` for a palette index or an already-explicit colour.

    `None` means "the terminal's default", which is the page's own foreground or
    background and therefore not a colour this function can name.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if 0 <= value < len(XTERM_256):
        return XTERM_256[value]
    return None


# ---------------------------------------------------------------------------
# The drawing state. A plain tuple rather than an object: every run of text
# carries one, they are compared constantly to decide where a run ends, and a
# tuple compares and hashes for free.
# ---------------------------------------------------------------------------

# (fg, bg, bold, dim, italic, underline, reverse)
DEFAULT_STYLE = (None, None, False, False, False, False, False)

_FG, _BG, _BOLD, _DIM, _ITALIC, _UNDER, _REVERSE = range(7)


def _with(style, index, value):
    out = list(style)
    out[index] = value
    return tuple(out)


def apply_sgr(style, params):
    """The style after one `ESC[…m`, whose numbers arrive as a list of strings.

    An empty parameter list is `ESC[m`, which every terminal reads as a reset.
    Unknown codes are ignored rather than guessed at: a screen with one escape
    we do not model should lose that one effect, not everything after it.
    """
    if not params:
        return DEFAULT_STYLE
    i = 0
    while i < len(params):
        raw = params[i]
        try:
            code = int(raw or "0")
        except ValueError:
            i += 1
            continue
        if code == 0:
            style = DEFAULT_STYLE
        elif code == 1:
            style = _with(style, _BOLD, True)
        elif code == 2:
            style = _with(style, _DIM, True)
        elif code == 3:
            style = _with(style, _ITALIC, True)
        elif code == 4:
            style = _with(style, _UNDER, True)
        elif code == 7:
            style = _with(style, _REVERSE, True)
        elif code == 22:
            style = _with(_with(style, _BOLD, False), _DIM, False)
        elif code == 23:
            style = _with(style, _ITALIC, False)
        elif code == 24:
            style = _with(style, _UNDER, False)
        elif code == 27:
            style = _with(style, _REVERSE, False)
        elif 30 <= code <= 37:
            style = _with(style, _FG, code - 30)
        elif 90 <= code <= 97:
            style = _with(style, _FG, code - 90 + 8)
        elif code == 39:
            style = _with(style, _FG, None)
        elif 40 <= code <= 47:
            style = _with(style, _BG, code - 40)
        elif 100 <= code <= 107:
            style = _with(style, _BG, code - 100 + 8)
        elif code == 49:
            style = _with(style, _BG, None)
        elif code in (38, 48):
            slot = _FG if code == 38 else _BG
            value, used = _extended_colour(params, i)
            style = _with(style, slot, value) if used else style
            i += used if used else 1
            continue
        i += 1
    return style


def _extended_colour(params, i):
    """(colour, parameters consumed) for a `38`/`48` sequence at `params[i]`.

    `(None, 0)` when it is truncated, which is what a screen cut off mid-escape
    looks like: the caller then skips one parameter and carries on rather than
    reading the rest of the line as colour numbers.
    """
    def number(pos):
        try:
            return int(params[pos])
        except (IndexError, ValueError):
            return None

    kind = number(i + 1)
    if kind == 5:
        index = number(i + 2)
        return (index, 3) if index is not None and 0 <= index <= 255 else (None, 0)
    if kind == 2:
        r, g, b = number(i + 2), number(i + 3), number(i + 4)
        if None in (r, g, b):
            return None, 0
        return "#%02x%02x%02x" % (r & 255, g & 255, b & 255), 5
    return None, 0


# One word of a tmux `#[…]` tag: `fg=red`, `bg=colour235`, `bold`, `default`.
_TMUX_WORD = re.compile(r"^(fg|bg)=(.+)$")


def tmux_colour(word):
    """The palette index (or `#rrggbb`) a tmux colour word means, or None.

    `default` and anything unrecognised come back as None, which is the
    terminal's own colour -- the same thing tmux does with a name it cannot
    resolve.
    """
    word = (word or "").strip().lower()
    if word in COLOUR_NAMES:
        return COLOUR_NAMES[word]
    if word.startswith("colour") or word.startswith("color"):
        digits = word.split("colour")[-1].split("color")[-1]
        try:
            index = int(digits)
        except ValueError:
            return None
        return index if 0 <= index <= 255 else None
    if re.match(r"^#[0-9a-f]{6}$", word):
        return word
    return None


def apply_tmux_tag(style, body):
    """The style after one tmux `#[…]` tag, whose inside arrives as `body`.

    tmux separates the words with commas or spaces and reads them left to
    right, so `#[default,fg=red]` ends up red. `default` and `none` clear
    everything, exactly as `ESC[0m` does.
    """
    for word in re.split(r"[,\s]+", body or ""):
        word = word.strip()
        if not word:
            continue
        low = word.lower()
        if low in ("default", "none"):
            style = DEFAULT_STYLE
            continue
        if low == "bold":
            style = _with(style, _BOLD, True)
            continue
        if low in ("nobold", "normal"):
            style = _with(style, _BOLD, False)
            continue
        if low == "dim":
            style = _with(style, _DIM, True)
            continue
        if low == "italics" or low == "italic":
            style = _with(style, _ITALIC, True)
            continue
        if low == "underscore" or low == "underline":
            style = _with(style, _UNDER, True)
            continue
        if low == "reverse":
            style = _with(style, _REVERSE, True)
            continue
        match = _TMUX_WORD.match(low)
        if match:
            slot = _FG if match.group(1) == "fg" else _BG
            style = _with(style, slot, tmux_colour(match.group(2)))
    return style


# `ESC[…m` and the other CSI sequences a capture can carry (a cursor move, an
# erase). Only `m` changes colour; the rest are dropped, because a still frame
# has no cursor to move.
_CSI = re.compile(r"\x1b\[([0-9;:]*)([A-Za-z])")
# An OSC (`ESC]…BEL` or `ESC]…ESC\`), which is how a title is set. Never drawn.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# A tmux style tag -- but NOT one whose `#` has another `#` in front of it.
#
# That lookbehind is the whole rule for hashes here, and it is chosen for the
# input that actually flows through this file, which is a CAPTURED SCREEN. There,
# tmux has already expanded its own escapes, so `#` means `#` and a session
# deduplicated to `foo##2` has two of them. Reading `##` as one would corrupt a
# name. The cost is at the other end: in a raw FORMAT string, where `##` is how
# tmux spells one `#`, that doubling is left as it was written. What the
# lookbehind does buy is the case that matters in both languages -- `##[fg=red]`
# is tmux's way of writing a literal `#[fg=red]`, and it must never be read as a
# style.
_TMUX_TAG = re.compile(r"(?<!#)#\[([^\]]*)\]")


def tokenize(text):
    """The screen as lines of `(style, text)` runs, ready to be turned into HTML.

    Line endings are the split: `\\r\\n` and a lone `\\r` count as one, because a
    capture of a pane a program wrote with `\\r\\n` should not come out
    double-spaced. The style CARRIES OVER from one line to the next, which is
    what a terminal does -- a colour opened at the end of one row is still open
    on the next.
    """
    style = DEFAULT_STYLE
    lines = []
    for raw in re.split(r"\r\n|\r|\n", text or ""):
        runs = []
        pending = []

        def flush():
            if pending:
                runs.append((style, "".join(pending)))
                del pending[:]

        raw = _OSC.sub("", raw)
        position = 0
        while position < len(raw):
            csi = _CSI.search(raw, position)
            tag = _TMUX_TAG.search(raw, position)
            # Whichever comes first. A `#[` that is really a literal `##[`
            # never matched in the first place: `_TMUX_TAG`'s lookbehind is what
            # settles it, and there is no unescaping pass afterwards on purpose
            # (see that pattern's comment).
            nxt = None
            if csi and (not tag or csi.start() <= tag.start()):
                nxt = ("csi", csi)
            elif tag:
                nxt = ("tag", tag)
            # Anything else -- a lone `#`, a doubled one, a `[` -- is text and
            # falls through to the plain-text branch below.
            if nxt is None:
                pending.append(raw[position:])
                break
            kind, match = nxt
            if match.start() > position:
                pending.append(raw[position:match.start()])
            flush()
            if kind == "csi":
                if match.group(2) == "m":
                    style = apply_sgr(style, [p for p in match.group(1).split(";")]
                                      if match.group(1) else [])
            else:
                style = apply_tmux_tag(style, match.group(1))
            position = match.end()
        flush()
        lines.append([(s, t) for s, t in runs if t])
    return lines


# ---------------------------------------------------------------------------
# Widths, and turning runs into HTML.
# ---------------------------------------------------------------------------

def cell_width(char):
    """How many terminal cells that character takes: 0, 1 or 2.

    The same rule tmux uses: combining marks and zero-width joiners take no
    cell, East Asian Wide and Fullwidth characters (which is where the emoji
    live) take two, everything else takes one. A variation selector -- the
    invisible `U+FE0F` that asks for the colour form of a symbol -- takes none,
    and the symbol in front of it keeps its own width, which is what the
    terminal counted.
    """
    if not char:
        return 0
    code = ord(char)
    if code in (0x200D, 0xFE0E, 0xFE0F) or 0x1F3FB <= code <= 0x1F3FF:
        return 0
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def run_width(text):
    """The cells a piece of text occupies."""
    return sum(cell_width(c) for c in text)


def css_for(style):
    """The inline CSS one style needs, or "" when it needs none.

    Reverse video is applied here rather than left to a CSS filter, because the
    swap has to happen against the PAGE's colours when the style names only one
    of the two -- which is exactly how fzf paints the row under the cursor.
    """
    fg, bg = style[_FG], style[_BG]
    if style[_REVERSE]:
        fg, bg = (bg if bg is not None else "var(--bg)"), (fg if fg is not None else "var(--fg)")
    rules = []
    # `colour_hex` hands any string straight back, which is what carries the
    # `var(--bg)` above through untouched.
    fg_hex, bg_hex = colour_hex(fg), colour_hex(bg)
    if fg_hex:
        rules.append("color:%s" % fg_hex)
    if bg_hex:
        rules.append("background:%s" % bg_hex)
    if style[_BOLD]:
        rules.append("font-weight:700")
    if style[_DIM]:
        rules.append("opacity:.6")
    if style[_ITALIC]:
        rules.append("font-style:italic")
    if style[_UNDER]:
        rules.append("text-decoration:underline")
    return ";".join(rules)


def _boxed(text):
    """`text` with every run of non-ASCII characters pinned to its cell width.

    This is the whole reason the page lines up (see the note at the top of the
    file). ASCII is returned as it is: a monospace font already gives it one
    cell each.
    """
    out = []
    buffer = []

    def flush_wide():
        if buffer:
            chunk = "".join(buffer)
            out.append('<span class="w" style="width:%dch">%s</span>'
                       % (run_width(chunk), html.escape(chunk)))
            del buffer[:]

    for char in text:
        if ord(char) < 128:
            flush_wide()
            out.append(html.escape(char))
        else:
            buffer.append(char)
    flush_wide()
    return "".join(out)


def spans(runs):
    """One line of runs as HTML. An empty line still gets a newline of its own."""
    out = []
    for style, text in runs:
        css = css_for(style)
        body = _boxed(text)
        out.append('<span style="%s">%s</span>' % (css, body) if css else body)
    return "".join(out)


def body_html(text):
    """Every line of a screen as the inside of the page's `<pre>`."""
    return "\n".join(spans(line) for line in tokenize(text))


# The font stack. The monospace faces come first so ASCII is laid out on the
# grid; the emoji face is last and only ever reached by a character none of the
# others has.
FONT_STACK = ('"JetBrains Mono", Menlo, "SF Mono", ui-monospace, monospace, '
              '"Apple Color Emoji"')

BACKGROUND = "#1e1e1e"
FOREGROUND = "#d6d6d6"

# Percent formatting, not `str.format`, precisely because the template is full
# of CSS braces and there is no `%` anywhere in it.
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>%(title)s</title><style>
:root { --bg: %(bg)s; --fg: %(fg)s; }
html, body { margin: 0; padding: 0; background: var(--bg); }
body { padding: %(margin)dpx; }
pre.term {
  display: inline-block; margin: 0; padding: %(pad)dpx;
  background: var(--bg); color: var(--fg);
  font-family: %(font)s;
  font-size: %(size)spx; line-height: %(leading)s;
  white-space: pre; font-variant-ligatures: none;
  -webkit-font-smoothing: antialiased;
  border-radius: 6px;
}
.w { display: inline-block; text-align: left; }
</style></head><body><pre class="term">%(body)s</pre></body></html>
"""


def render_page(text, title="frame", font_size=15, leading=1.25, padding=18,
                margin=12, background=BACKGROUND, foreground=FOREGROUND):
    """A whole page for one captured screen.

    The block is anchored at the TOP LEFT, `margin` pixels in, rather than
    centred in the viewport. Centring cost an afternoon: a screenshot two lines
    high asks for a window shorter than the one Chrome is willing to make, so
    the page was laid out in a taller viewport, the block was centred in THAT,
    and the picture -- cropped to the size that was asked for -- came out
    empty. Anchored, whatever the viewport turns out to be, the block is in it.
    """
    return _PAGE % {
        "title": html.escape(title), "bg": background, "fg": foreground,
        "font": FONT_STACK, "size": font_size, "leading": leading,
        "pad": padding, "margin": margin, "body": body_html(text),
    }


def main(argv=None):
    """stdin -> a page on stdout."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    sys.stdout.write(render_page(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
