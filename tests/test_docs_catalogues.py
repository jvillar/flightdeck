"""The Ctrl-L flag catalogues are quoted in the user documentation twice over.

`picker.COMMON_FLAGS`, `COMMON_FLAGS_CODEX` and `COMMON_FLAGS_AGY` are what the
menu actually offers; `docs/GUIDE.md` shows them as the screen a user sees and
`docs/TOOLS.md` lists them per tool. Nothing kept the three copies together, so a
flag added, dropped or renamed in the code left two documents quietly lying about
what the key does. This is the thread that ties them.

Two extractions, each the simplest one the shape of the document allows:

- **GUIDE.md**: every fenced block whose lines, once the `flags>` prompt line and
  the courtesy "(none …)" row are set aside, ALL start with `--`. That is a
  catalogue and nothing else in the document looks like it. The rows are compared
  whole -- `picker.flag_line` renders them, brackets and all -- so the
  explanations are pinned along with the flags.
- **TOOLS.md**: the sentence "Ctrl-L's list for <tool>: `--a`, `--b`…", read to
  the end of its paragraph. There the flags are quoted without their
  explanations, so only the flags are compared, and in order.

A release carries `docs/`, so this runs from an unpacked installation too; if the
documents are not there, there is nothing to check and the test says so.
"""
import re
import unittest

from flightdeck import config, picker

DOCS = config.code_dir() / "docs"

# What each catalogue is called in TOOLS.md's "Ctrl-L's list for …" sentence.
BY_NAME = {"Claude Code": picker.COMMON_FLAGS, "codex": picker.COMMON_FLAGS_CODEX,
           "agy": picker.COMMON_FLAGS_AGY}

# Indented too: the GUIDE's first catalogue sits inside a numbered step, so its
# fence carries the list item's three spaces.
_FENCE = re.compile(r"^[ \t]*```", re.M)
_BACKTICKED = re.compile(r"`([^`]+)`")
# The sentence and the rest of its paragraph (the list wraps over two lines).
_TOOLS_LIST = re.compile(r"Ctrl-L's list for ([^:]+):(.*?)\n\n", re.S)


def _read(name):
    path = DOCS / name
    if not path.exists():
        raise unittest.SkipTest("%s is not here (an unpacked release has no docs/)"
                                % path)
    return path.read_text(encoding="utf-8")


def _fenced_catalogues(text):
    """The fenced blocks of GUIDE.md that are flag catalogues, rows and all.

    -> [(row, row, …), …], one tuple per block, in the document's order.
    """
    out = []
    parts = _FENCE.split(text)
    # An odd index is inside a fence (split alternates outside/inside).
    for block in parts[1::2]:
        rows = []
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("flags>") or line == picker._NO_FLAGS_ROW:
                continue
            rows.append(line)
        if rows and all(row.startswith("--") for row in rows):
            out.append(tuple(rows))
    return out


def _prose_catalogues(text):
    """TOOLS.md's per-tool lists. -> {tool name as written: [flag, …]}."""
    return {name.strip(): _BACKTICKED.findall(body)
            for name, body in _TOOLS_LIST.findall(text)}


class TestTheCatalogueTheDocumentsQuote(unittest.TestCase):
    def test_the_guides_screens_are_the_three_catalogues(self):
        blocks = _fenced_catalogues(_read("GUIDE.md"))
        expected = [tuple(picker.flag_line(f, n) for f, n in table)
                    for table in (picker.COMMON_FLAGS, picker.COMMON_FLAGS_CODEX,
                                  picker.COMMON_FLAGS_AGY)]
        # Sorted, so moving a block within the document is not a failure while
        # changing a row inside one is.
        self.assertEqual(sorted(blocks), sorted(expected))

    def test_tools_lists_every_flag_of_every_catalogue_and_no_others(self):
        listed = _prose_catalogues(_read("TOOLS.md"))
        self.assertEqual(sorted(listed), sorted(BY_NAME),
                         "TOOLS.md names tools this test does not know")
        for name, table in BY_NAME.items():
            with self.subTest(tool=name):
                self.assertEqual(listed[name], [f for f, _ in table])

    def test_the_extraction_finds_something(self):
        """A guard on the test itself: an extraction that silently matches
        nothing would pass every assertion above by comparing two empty lists,
        and the day somebody reformats the documents is the day it stops
        catching anything."""
        self.assertEqual(len(_fenced_catalogues(_read("GUIDE.md"))), 3)
        self.assertEqual(len(_prose_catalogues(_read("TOOLS.md"))), 3)


if __name__ == "__main__":
    unittest.main()
