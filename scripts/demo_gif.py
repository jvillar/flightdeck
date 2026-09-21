#!/usr/bin/env python3
"""Render the demo GIF and the two stills in `docs/img`, from the real menu.

    python3 scripts/demo_gif.py --out docs/img

One frame can be re-shot on its own and put back in its place. The other nine
are read out of the GIF that is already there and put back as they were, so
re-shooting one does not re-roll every age, clock time and cursor position in
the rest of it:

    python3 scripts/demo_gif.py --out docs/img --only accounts

Every frame is a photograph of Flightdeck actually running: a throwaway tmux
server with invented sessions on it, a real `fzf` menu over them, real
keystrokes (Enter, F12, Ctrl-L, Esc) and the real state hook fed a synthetic
`Stop`, which sends the real floating notice. Nothing in a frame is drawn by
hand. The one piece of scenery is the short conversation excerpt inside the
session pane, which is written to read as a demo; the status line underneath it
is the real `flightdeck statusline claude` over the real demo payload.

NEEDS
  - Google Chrome (headless, for the screenshots).
  - Pillow, for assembling the GIF. It is NOT a dependency of Flightdeck and
    must not become one: point `--python` (or `PILLOW_PYTHON`) at an interpreter
    that has it, and this script re-invokes itself there for that one step.
  - tmux and fzf, which Flightdeck needs anyway.

WHAT IT TOUCHES
  Nothing of yours. It runs on the tmux socket `fdshots` (plus `fdshots-host`
  for the client that looks at it), with HOME, the state directory and the
  config file all inside a temporary directory it makes and removes. It kills
  both servers and its own `sleep` processes on the way out, including after a
  failure. Your own tmux server, `~/.claude`, `~/.codex` and `~/.gemini` are
  never read and never written.

HOW A FRAME IS TAKEN
  A second, UNCONFIGURED tmux server holds one 100x30 pane running
  `tmux -L fdshots attach`. That makes a real client, so `capture-pane -e -p` on
  the host pane returns the whole cockpit screen at once -- the fzf list AND the
  status bar tmux painted under it, AND any floating notice on top. Composing
  the bar from `#{T:status-right}` would have given the same line without ever
  proving tmux agreed to paint it. `scripts/ansi2html.py` turns that capture
  into HTML, Chrome screenshots it, and Pillow puts the frames together.

MASKING
  Every captured frame goes through `mask()` before it is rendered: e-mail
  addresses become `user1@example.com`, `user2@example.com`... and any real home
  directory becomes `~`. The demo runs on invented data, so this catches what
  leaks from a program that reads something else -- which the pinned row does on
  purpose, being the one thing here run against the real home
  (`_write_cswap_wrapper`). Its
  organisation in brackets is masked too, and the rules step OVER the capture's
  escape sequences rather than into them. The one address left alone is one at
  a reserved documentation domain (`@example.com`, RFC 2606): the status line's
  👤 segment is fed a fake account of exactly that shape, and renumbering it
  would hide nothing while stopping the picture showing what the code paints.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ansi2html  # noqa: E402  (after the sys.path line, on purpose)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# The throwaway servers. `fdshots` is the cockpit; `fdshots-host` only holds the
# client that looks at it and is never given Flightdeck's keys, so an F12 sent
# into it travels through to the cockpit instead of being caught on the way.
SOCKET = "fdshots"
HOST_SOCKET = "fdshots-host"

# 32 rows and not the 30 this started with: the menu grew a two-line detail
# strip under the list, and fzf's preview window costs three rows (two lines
# plus its border). At 30 that came straight off the list, which then ended
# exactly on its last row with nothing to spare -- and the first frame exists to
# show one of every kind of row, so it cannot be one row away from losing the
# bottom of the history. Two more rows give the list its 23 rows and a spare,
# and cost the GIF about 7% in pixels. 34 was tried and left four blank rows
# between the last row and the strip, which reads as the picture having run out
# of things to say.
COLUMNS, ROWS = 100, 32
# The window is one row shorter than the screen: the client draws the status bar
# in the row the window does not have. Sessions are made at this size so
# attaching a client does not resize them and reflow what is already on screen.
WINDOW_ROWS = ROWS - 1

# Far from the default 42707, so a menu of the developer's cannot be reloaded by
# anything here.
MENU_PORT = 43900

FONT_SIZE = 15
LEADING = 1.25
PADDING = 18
# Menlo advances 1233/2048 of an em. The window has to be big enough for 100 of
# those plus the padding, and the page centres the block inside whatever is left
# over, so a font with a slightly different advance shifts nothing.
CHAR_RATIO = 0.6021
MARGIN = 12
# The status line still is a picture of two lines, so it is given a little less
# room around them than a whole screen gets.
STILL_PADDING = 14

# The GIF is published on a README: it has to load, and GitHub is not a video
# host. Flat terminal text compresses well enough that the whole palette is
# affordable -- and it is needed. At 128 the shared palette spent its colours on
# the text and left the status line's orange context bar a muddy tan. 255 rather
# than 256: `optimize=True` delta-encodes every frame after the first and wants
# an index free for the pixels it does not repaint.
GIF_COLOURS = 255

HOLD_MS = 2000       # how long a frame you are meant to read stays up
STRIP_MS = 1500      # long enough to read the detail strip, short enough to move on
STEP_MS = 600        # and one that is only a move between two of them


# ---------------------------------------------------------------------------
# The storyboard's data: the sessions the menu lists, and the history behind
# them. Everything here is invented.
# ---------------------------------------------------------------------------

# Live tmux sessions, in the order the menu will show them (most recently used
# first, which is what `build_entries` sorts on -- they are created in reverse so
# the activity times come out this way round).
GREEN = [
    {"session": "payments", "tool": "claude", "state": "working",
     "last_event": "PostToolUse", "project": "payments",
     "title": "payments api", "ctx": 82, "age": 40},
    {"session": "recommender", "tool": "claude", "state": "asking",
     "last_event": "PreToolUse", "project": "recommender",
     "title": "recommender v2", "age": 12},
    {"session": "pricing 3", "tool": "claude", "state": "awaiting_input",
     "last_event": "Stop", "project": "pricing", "title": "pricing 3",
     "ctx": 61, "age": 8},
    {"session": "forecasting 14", "tool": "claude", "state": "working",
     "last_event": "UserPromptSubmit", "project": "forecasting",
     "title": "forecasting 14", "age": 95},
    {"session": "codex sandbox", "tool": "codex", "state": "working",
     "last_event": "PostToolUse", "project": "sandbox", "age": 150},
    {"session": "agy notes", "tool": "agy", "state": "working",
     "last_event": "PostToolUse", "project": "notes", "ctx": 63, "age": 220},
    # The conversation in this pane was sent to the background: its row shows
    # what the JOB is doing, and says `background`.
    {"session": "mailer", "tool": "claude", "state": "awaiting_input",
     "last_event": "Stop", "project": "mailer", "title": "mailer",
     "parked": True, "age": 300},
    # Opened and still: `working` whose last event is the one that started it.
    {"session": "docs site", "tool": "claude", "state": "working",
     "last_event": "SessionStart", "source": "startup", "project": "docs-site",
     "title": "docs site", "age": 20},
    {"session": "retail mrp", "tool": "claude", "state": "looping",
     "last_event": "Stop", "project": "retail", "title": "retail mrp",
     "age": 70},
    # No card at all: a tmux session with nothing but a shell in it.
    {"session": "scratch", "tool": None},
]

# The closed conversations behind them. `ago` is in seconds and becomes the
# file's modification time, which is what the history ranks and ages by.
HISTORY = [
    {"tool": "claude", "project": "warehouse", "ago": 2 * 3600 + 5 * 60,
     "title": "Cycle count discrepancies"},
    {"tool": "codex", "project": "api", "ago": 3 * 3600 + 40 * 60},
    {"tool": "agy", "project": "notes", "ago": 4 * 3600 + 15 * 60,
     "title": "Draft the release notes"},
    {"tool": "claude", "project": "search", "ago": 5 * 3600 + 30 * 60,
     "title": "Typo tolerance in the product index"},
    {"tool": "claude", "project": "onboarding", "ago": 7 * 3600,
     "title": "Welcome email copy"},
    {"tool": "codex", "project": "infra", "ago": 9 * 3600 + 20 * 60},
    # The row the last part of the demo filters for and launches with Ctrl-L.
    {"tool": "claude", "project": "pricing", "ago": 18 * 3600 + 40 * 60,
     "title": "Rework the margin calculation"},
    {"tool": "claude", "project": "checkout", "ago": 26 * 3600,
     "title": "Split the payment webhook"},
    {"tool": "agy", "project": "scraper", "ago": 30 * 3600,
     "title": "Parse the supplier CSV"},
    {"tool": "claude", "project": "billing", "ago": 52 * 3600,
     "title": "Dunning emails, second attempt"},
    {"tool": "claude", "project": "mobile", "ago": 61 * 3600,
     "title": "Offline cart, first pass"},
]

FILTER_QUERY = "margin"   # what gets typed to bring that row to the top

# The pinned row: a tool of the user's own, opened in its own tmux session by
# pressing Enter on it.
#
# It is the ONE thing in the demo that runs against the real home, deliberately:
# under the demo home the account switcher has nothing to show, and a dashboard
# reading "No managed accounts yet" does not demonstrate an account switcher.
# So it is given `HOME` explicitly, and everything it prints goes through
# `Masker` before it is rendered -- addresses, the organisation in brackets,
# paths and the login name. `cswap tui` READS; the demo never presses a key
# inside it (the next keystroke is F12), and nothing here writes to the keychain
# or to `~/.claude-swap-backup`.
#
# It is written PLAINLY, which is the point: the detail strip under the list
# shows a pinned row's command, and this is the one a real user would configure.
# The real home is supplied by a wrapper of the same name early on the demo's
# PATH (`_write_cswap_wrapper`), not by an `env HOME=...` in front of it -- that
# form worked, but the strip then read `pin: env HOME='~' cswap tui`, which
# advertises the demo's own scaffolding in a picture meant to show the product.
PIN = {"name": "accounts", "label": "⚙ accounts", "session": "accounts",
       "command": "cswap tui"}
PIN_COMMAND = PIN["command"]

# The conversation excerpt in the session pane. This is the ONE piece of scenery
# in the whole GIF and it is written to read as a demo. What goes under it is
# not scenery: it is `flightdeck statusline claude` over the real demo payload.
EXCERPT = """\
> add a margin column to the weekly pricing report

  Read   reports/weekly_pricing.py
  Edit   reports/weekly_pricing.py
  Edit   tests/test_weekly_pricing.py
  Bash   python -m pytest tests/test_weekly_pricing.py -q   ->   14 passed

  The report now carries a margin_pct column between revenue and units, and the
  two tests that assert on the header row were updated to match. The sample data
  in the demo fixture still loads.

  Anything else, or shall we hand over?
"""


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# The organisation the account switcher prints in brackets after each address.
#
# Two guards, and both are needed. The lookbehind refuses a `[` that an ESC put
# there: `\x1b[0m` is an escape, not a bracket, and a bare `\[[^\]]*\]` would
# have started inside one and eaten every colour code up to the next `]`. And
# the content may hold no `[` of its own, which is what stops a match from
# spanning an escape sequence -- every one of them carries a `[`.
#
# It is deliberately NOT anchored on the masked address in front of it, which
# was the first attempt and did not fire: the switcher paints the address and
# the organisation in different colours, so there is an escape between them and
# the two never appear side by side in one piece of text.
_ORG = re.compile(r"(?<!\x1b)\[[^\]\[\x1b]{1,80}\]")
# An escape sequence: a colour change, or a title. Masking must step OVER these,
# never into them -- `\x1b[38;5;173m` is not text and a rule that rewrote part
# of it would corrupt the whole rest of the line.
_ESCAPE = re.compile(r"(\x1b\[[0-9;:]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))")


class Masker:
    """Takes anything real out of a captured screen, before it is rendered.

    E-mail addresses are numbered in the order they are first seen, so the same
    address is always the same fictitious one across frames, and any home
    directory -- the demo's, or the one belonging to whoever is running this --
    becomes `~`.
    """

    def __init__(self, homes=()):
        self.seen = {}
        self.homes = sorted({str(h) for h in homes if h}, key=len, reverse=True)
        self.user = getpass.getuser()

    def _email(self, match):
        address = match.group(0)
        # The reserved documentation domains (RFC 2606) belong to nobody, and
        # the demo's own fake account is written with one. Renumbering it to
        # `user1@example.com` would hide nothing and would stop the picture
        # showing the address the code actually paints.
        if address.lower().endswith((".example.com", "@example.com",
                                     "@example.org", "@example.net")):
            return address
        if address not in self.seen:
            self.seen[address] = "user%d@example.com" % (len(self.seen) + 1)
        return self.seen[address]

    def _mask(self, text):
        """The rules, in order. Addresses go before the organisation, because
        the organisation rule recognises a row by the masked address in front
        of it -- and because the switcher spells one organisation as
        "<address>'s Organization", which is the address twice over."""
        for home in self.homes:     # longest first: a home inside another home
            text = text.replace(home, "~")
        text = _EMAIL.sub(self._email, text)
        if self.user:
            text = re.sub(r"\b%s\b" % re.escape(self.user), "user", text)
        return text

    def __call__(self, text):
        """Mask the TEXT of a screen and leave its escape sequences alone.

        The capture is text and colour interleaved, and every rule here is a
        regular expression over the text. Run across the whole string they would
        reach into `\x1b[38;5;173m`, which is not text: the organisation rule in
        particular starts at a `[`, and an escape sequence starts at a `[` too.
        So the string is split on the escapes and only the pieces between them
        are rewritten.
        """
        out = "".join(part if index % 2 else self._mask(part)
                      for index, part in enumerate(_ESCAPE.split(text or "")))
        # The organisation runs over the WHOLE string rather than piece by
        # piece: its bracket and the address it belongs to are painted in
        # different colours, so an escape sits between them and the bracket is
        # in a piece of its own. `_ORG` carries its own guards against escapes.
        return _ORG.sub("[example]", out)


# ---------------------------------------------------------------------------
# The demo machine
# ---------------------------------------------------------------------------

class Demo:
    """The throwaway world every frame is taken in, and the tools to drive it."""

    def __init__(self, work, keep=False):
        self.work = Path(work)
        self.keep = keep
        self.home = self.work / "home"
        self.state = self.work / "state"
        self.config = self.work / "config.json"
        self.project = self.home / "work" / "demo"
        self.frames_dir = self.work / "frames"
        self.sleepers = []
        self.panes = {}
        self.host_pane = None
        self.payload = {}
        self.cursor = 0
        self.cards = {}
        # Both spellings of each path: `/var/folders/…` and the
        # `/private/var/folders/…` it resolves to on macOS are the same
        # directory and either can turn up in a program's output.
        self.mask = Masker([self.home, self.work, Path.home(),
                            self.home.resolve(), self.work.resolve(),
                            Path.home().resolve()])

    # -- environment --------------------------------------------------------

    def env(self, **extra):
        """The environment everything in the demo runs with.

        `PATH` carries the repository's `bin` so the last frame can type
        `flightdeck statusline --demo` the way the README tells people to, and
        `PYTHONPATH` carries the repository so `python3 -m flightdeck.…` works
        from any working directory -- which is what a tmux binding, a status bar
        and an fzf reload all are.
        """
        out = dict(os.environ)
        out.update({
            "HOME": str(self.home),
            # The demo's own `bin` FIRST: it holds the `cswap` wrapper that
            # gives the pinned row the real home (`_write_cswap_wrapper`), and
            # the repository's `bin` behind it so the last frame can type
            # `flightdeck statusline --demo` the way the README tells people to.
            "PATH": "%s:%s:%s" % (self.work / "bin", ROOT / "bin",
                                  os.environ.get("PATH", "")),
            "PYTHONPATH": str(ROOT),
            "SHELL": "/bin/zsh",
            "TERM": "screen-256color",
            "FLIGHTDECK_TMUX_SOCKET": SOCKET,
            "FLIGHTDECK_STATE_DIR": str(self.state),
            "FLIGHTDECK_CONFIG": str(self.config),
            "FLIGHTDECK_PROJECTS_DIR": str(self.home / "work"),
            # Every pane in the demo is this wide, and Claude Code is what
            # normally exports this (from its own stdout) to the status line
            # command it launches. Set rather than inherited: the width decides
            # what `render_claude` trims, so the developer's own window must not
            # be what the recorded line fits itself to.
            "COLUMNS": str(COLUMNS),
        })
        # The XDG variables are SET, not defaulted: every Flightdeck path falls
        # back to one of them when its own variable is missing, so on a machine
        # that exports them the demo would otherwise reach the real ones.
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                     "XDG_CACHE_HOME"):
            out[name] = str(self.home / ".local" / name.lower())
        for name in ("TMUX", "TMUX_PANE"):
            out.pop(name, None)
        out.update({k: str(v) for k, v in extra.items()})
        return out

    # -- tmux ---------------------------------------------------------------

    def tmux(self, *args, **kw):
        return self._tmux(SOCKET, args, **kw)

    def host(self, *args, **kw):
        return self._tmux(HOST_SOCKET, args, **kw)

    def _tmux(self, socket, args, check=True):
        result = subprocess.run(["tmux", "-L", socket] + [str(a) for a in args],
                                capture_output=True, text=True, env=self.env())
        if check and result.returncode != 0:
            raise RuntimeError("tmux -L %s %s: %s"
                               % (socket, " ".join(str(a) for a in args),
                                  (result.stderr or "").strip()))
        return result.stdout

    # -- building the world -------------------------------------------------

    def sleeper(self):
        """A live process, so a card's `pid` is one Flightdeck can verify.

        The cards say who is alive, and `common.load_sessions` believes the pid
        over the card: a session whose process has gone is not listed at all, so
        invented pids would have made an empty menu.
        """
        process = subprocess.Popen(["sleep", "3600"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.sleepers.append(process)
        return process.pid

    def _write_cswap_wrapper(self):
        """A `cswap` of our own, early on the demo's PATH, that runs the real
        one against the REAL home.

        The pinned row is the one thing here that reads the real home's own
        data, because an account switcher with no accounts demonstrates
        nothing. The home has to be given to it somehow, and doing it in the
        pin's command put `env HOME=...` in front of a line the menu
        PAINTS. This way the demo's config holds the command a real user would
        write, and where the home comes from is a fact about this machine's
        PATH -- the same kind of scaffolding as the temporary home itself, and
        not something anyone reads off a frame.

        `preflight` has already refused to start if `cswap` is not installed, so
        `which` cannot come back empty here.
        """
        real = shutil.which(PIN_COMMAND.split()[0])
        wrapper = self.work / "bin" / PIN_COMMAND.split()[0]
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/sh\nexec env HOME=%s %s \"$@\"\n"
                           % (shell_quote(str(Path.home())),
                              shell_quote(str(real))), encoding="utf-8")
        wrapper.chmod(0o755)

    def build_home(self):
        for path in (self.home, self.state, self.project, self.frames_dir,
                     self.home / ".claude" / "projects",
                     self.home / ".claude" / "sessions",
                     self.home / ".codex" / "sessions" / "2026" / "09" / "16",
                     self.home / ".gemini" / "antigravity-cli" / "conversations",
                     self.home / ".gemini" / "antigravity-cli" / "cache"):
            path.mkdir(parents=True, exist_ok=True)
        # A prompt with no machine name and no user name in it: the last frame
        # types a command at it.
        # No history file: zsh writes one when the pane is killed, which is
        # AFTER this directory has been removed -- it came back with a
        # `.zsh_history` in it and nothing else.
        (self.home / ".zshrc").write_text(
            "PROMPT='%F{108}~/work/demo%f $ '\n"
            "unset HISTFILE\nSAVEHIST=0\nunsetopt PROMPT_SP\n",
            encoding="utf-8")
        # Both the status line and the menu's detail strip read the branch out of
        # `.git/HEAD` rather than asking git, so this is all a branch needs. It
        # is done for EVERY project the demo invents, not just the one the
        # session pane sits in: the strip shows the folder and branch of
        # whatever row the cursor is on, and a folder that does not exist would
        # have shown a path with nothing after it.
        for folder in {self.project} | {self.home / "work" / entry["project"]
                                        for entry in GREEN + HISTORY
                                        if entry.get("project")}:
            (folder / ".git").mkdir(parents=True, exist_ok=True)
            (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                  encoding="utf-8")
        self._write_cswap_wrapper()
        self._write_account_sources()
        self.config.write_text(json.dumps({
            "menu_port": MENU_PORT,
            "projects_dir": str(self.home / "work"),
            "pins": [dict(PIN)],
            # No `statusline` section: the three lines and the 👤 segment are
            # the defaults now, and the picture has to be of what somebody gets
            # without editing anything.
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _write_account_sources(self):
        """The fake `~/.claude.json` and credentials the 👤 segment reads.

        The address and the plan come from `statusline.DEMO_ACCOUNT`, which is
        where the `--demo` frame gets its own, so the three pictures of this
        line cannot end up showing two different accounts. They are read because
        the segment is ON by default, not because the demo's config asks for it.

        Both files are inside the demo's home and hold an address that belongs
        to nobody. The real `~/.claude.json` is never opened, and the real
        keychain cannot be reached either: the plan is read from the file when
        there is one, and `security` run with a home of its own finds no item
        at all (measured on this Mac: it exits 44 having printed nothing).
        """
        account = json.loads(self._helper(
            [sys.executable, "-c",
             "import json;from flightdeck.statusline import DEMO_ACCOUNT;"
             "print(json.dumps(DEMO_ACCOUNT))"],
            "asking for the demo account"))
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": account["email"]}}),
            encoding="utf-8")
        (self.home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"subscriptionType": account["plan"]}}),
            encoding="utf-8")

    def write_transcript(self, session_id, cwd, title, mtime=None):
        """A Claude Code transcript, in the shape `history.session_meta` reads."""
        folder = self.home / ".claude" / "projects" / re.sub(r"[/.]", "-", cwd)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s.jsonl" % session_id)
        rows = [{"type": "user", "cwd": cwd, "sessionId": session_id}]
        if title:
            rows.append({"type": "custom-title", "customTitle": title,
                         "sessionId": session_id})
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def build_history(self):
        """The grey rows: one file per closed conversation, per tool."""
        now = time.time()
        agy_meta = {}
        for index, entry in enumerate(HISTORY):
            session_id = str(uuid.uuid4())
            when = now - entry["ago"]
            cwd = str(self.home / "work" / entry["project"])
            if entry["tool"] == "claude":
                self.write_transcript(session_id, cwd, entry.get("title"), when)
            elif entry["tool"] == "codex":
                # A rollout, where the file NAME carries the id and the first
                # useful row is the `session_meta`. `source` must not be "exec":
                # that is codex's non-interactive mode and the history skips it.
                path = (self.home / ".codex" / "sessions" / "2026" / "09" / "16"
                        / ("rollout-2026-09-16T%02d-00-00-%s.jsonl"
                           % (index, session_id)))
                path.write_text(json.dumps({
                    "type": "session_meta",
                    "payload": {"session_id": session_id, "cwd": cwd,
                                "source": "cli"}}) + "\n", encoding="utf-8")
                os.utime(path, (when, when))
            else:
                # agy's conversation file is opaque -- only its name and its
                # date are read -- and what is readable lives in the JSON cache
                # beside it.
                path = (self.home / ".gemini" / "antigravity-cli"
                        / "conversations" / ("%s.db" % session_id))
                path.write_bytes(b"SQLite format 3\x00")
                os.utime(path, (when, when))
                agy_meta[session_id] = {"summary": {
                    "Title": entry.get("title"),
                    "WorkspaceURIs": ["file://%s" % cwd]}}
        (self.home / ".gemini" / "antigravity-cli" / "cache"
         / "conversation_metadata.json").write_text(
            json.dumps({"conversations": agy_meta}), encoding="utf-8")

    def start_server(self):
        """The cockpit's tmux server, with the demo's environment inside it.

        Panes inherit the SERVER's environment, not the client's, so everything
        the menu and the hooks need has to be here before the first session is
        made.
        """
        self.tmux("new-session", "-d", "-s", "bootstrap",
                  "-x", COLUMNS, "-y", WINDOW_ROWS, "sleep", "3600")
        self.tmux("set", "-g", "escape-time", "10")
        # The size a session is born with when nobody says otherwise. The picker
        # creates a pinned row's session without one -- that is the product's
        # business, not ours -- so it was taking tmux's stock 80x24, and the
        # program inside drew itself 80 columns wide inside a 100-column pane.
        # It never got any wider: a curses program redraws when it is signalled
        # or when a key is pressed, and the demo presses no key inside somebody
        # else's tool. Born at the right size, it draws right the first time.
        self.tmux("set", "-g", "default-size", "%dx%d" % (COLUMNS, WINDOW_ROWS))

    def start_sessions(self):
        """One tmux session per green row, newest last so the order comes out right."""
        self.statusline_payload()
        for entry in reversed(GREEN):
            name = entry["session"]
            if name == "pricing 3":
                # The session the demo walks into: the excerpt, then the REAL
                # status line under it, then a process that keeps the pane alive.
                command = "%s; exec sleep 3600" % self.session_pane_command()
                self.tmux("new-session", "-d", "-s", name, "-x", COLUMNS,
                          "-y", WINDOW_ROWS, "-c", str(self.project),
                          "/bin/sh", "-c", command)
            elif name == "scratch":
                # A shell whose prompt starts near the bottom of the pane, the
                # way a terminal you have been working in looks. The last frame
                # types a command at it.
                # The blank lines are counted from the pane's height, not fixed:
                # the command and its output are about a dozen rows, so the
                # prompt starts low enough that they end near the bottom. With
                # a number written in, the pane grew and the last frame's text
                # ended up floating in the middle of an empty screen.
                self.tmux("new-session", "-d", "-s", name, "-x", COLUMNS,
                          "-y", WINDOW_ROWS, "-c", str(self.project),
                          "/bin/sh", "-c",
                          "i=0; while [ $i -lt %d ]; do echo; i=$((i+1)); done;"
                          " exec ${SHELL:-/bin/sh}" % (WINDOW_ROWS - 12))
            else:
                self.tmux("new-session", "-d", "-s", name, "-x", COLUMNS,
                          "-y", WINDOW_ROWS, "sleep", "3600")
            # A full second apart, and not a fraction: tmux keeps a session's
            # activity time in WHOLE seconds, so ten sessions made inside one
            # second all sort equal and the menu falls back to tmux's own
            # (alphabetical) order instead of "most recently used first".
            time.sleep(1.05)
        self.tmux("kill-session", "-t", "=bootstrap")
        for line in self.tmux("list-panes", "-a", "-F",
                              "#{session_name}\t#{pane_id}").splitlines():
            session, _, pane = line.partition("\t")
            self.panes[session] = pane

    def _helper(self, argv, what):
        """Run one of Flightdeck's own modules and hand back its stdout.

        It checks the exit code and quotes stderr, because the callers read the
        output as something structured -- JSON, or the picker's list -- and a
        module that died left them explaining the WRONG failure: a crashed
        `picker feed` came out as "no menu row matching 'accounts'", which sends
        you looking at the storyboard instead of at the traceback.
        """
        done = subprocess.run(argv, cwd=str(self.project), env=self.env(),
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError("%s failed (exit %d): %s"
                               % (what, done.returncode,
                                  (done.stderr or "").strip() or "no detail"))
        return done.stdout

    def statusline_payload(self):
        """The demo payload, asked for from inside the demo project.

        `statusline.demo_payload_claude()` reads the working directory on
        purpose (the installer shows people their own folder), so it is asked
        for it there -- otherwise the line would print the path this script
        happens to be run from. It is asked ONCE and kept: the payload's reset
        times are relative to the moment it is built, and two of them would put
        two different clock times on two pictures of the same line.
        """
        self.payload = json.loads(self._helper(
            [sys.executable, "-c",
             "import json;from flightdeck.statusline import demo_payload_claude;"
             "print(json.dumps(demo_payload_claude()))"],
            "building the demo payload"))
        self.payload["transcript_path"] = str(self._write_transcript())

    def _write_transcript(self):
        """A transcript for the demo payload, so its line carries the turns.

        `demo_payload_claude` leaves `transcript_path` out, and `render_claude`
        counts the turns by READING it -- so the session pane and the still
        painted a line with no `turns` segment while the `--demo` frame, which
        is handed `DEMO_TURNS` outright, showed one. Three pictures of the same
        line and two of them missing a piece.

        `count_turns` counts the rows carrying `"type":"assistant"` and reads
        nothing else out of them, so that is all these rows carry. The number
        comes from `statusline.DEMO_TURNS`, which is where the `--demo` frame
        gets its own: one source, so the three pictures cannot drift apart.
        """
        turns = int(self._helper(
            [sys.executable, "-c",
             "from flightdeck.statusline import DEMO_TURNS;print(DEMO_TURNS)"],
            "asking for the demo turn count").strip())
        path = self.work / "demo-transcript.jsonl"
        path.write_text('{"type":"assistant"}\n' * turns, encoding="utf-8")
        return path

    def statusline_text(self, width=COLUMNS):
        """The real status line for that payload, from the real command.

        `width` is handed over as `COLUMNS`, which is where the line gets its
        width: Claude Code's payload carries no number (agy's `terminal_width`
        has no counterpart there), so what it exports into the environment is
        the only thing the line can trim to. The default is the width of the
        panes here, so what goes INTO a pane fits it -- untrimmed the line is
        ~137 cells and the pane wrapped its tail onto a row of its own, which
        reads as a fault in the tool rather than as the terminal doing its job.

        `width=None` takes `COLUMNS` out of the environment, and then nothing is
        trimmed. That is what the STILL wants: it is a picture of the line, not
        of a pane, and the README paragraph beside it promises the folder AND
        the branch AND the reset times. Trimmed, the still had shed the branch
        and the `↻` times, so the sentence described something the picture did
        not contain. The two calls are kept apart even where they now agree:
        with the line split over three rows neither of them is near 100 cells,
        and the day a payload carries a fourth rate-limit bucket the pane is
        where it has to give way, not the still.
        """
        env = self.env()
        if width:
            env["COLUMNS"] = str(width)
        else:
            env.pop("COLUMNS", None)
        return subprocess.run(
            [sys.executable, "-m", "flightdeck.statusline", "claude"],
            input=json.dumps(self.payload), cwd=str(self.project),
            env=env, capture_output=True, text=True).stdout

    def session_pane_command(self):
        """The shell line that paints the session pane, bottom-aligned.

        A terminal fills from the bottom, so the excerpt is pushed down with
        blank lines: text sitting at the top of an otherwise empty screen is the
        one thing that would give away that nobody had been typing here. The
        padding is worked out from the status line's own height rather than
        written down, which is why the conversation simply moved up a row when
        the line went from two to three.
        """
        body = self.work / "excerpt.txt"
        status = self.statusline_text()
        lines = EXCERPT.rstrip("\n").split("\n") + [""] + status.rstrip("\n").split("\n")
        pad = max(0, (WINDOW_ROWS - 1) - len(lines))
        body.write_text("\n" * pad + "\n".join(lines) + "\n", encoding="utf-8")
        return "cat %s" % shell_quote(str(body))

    def write_cards(self):
        """The session cards the hooks would have written, and the registry
        entries Claude Code would have written next to them."""
        sessions = self.state / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        registry = self.home / ".claude" / "sessions"
        now = time.time()

        def card(session_id, **fields):
            fields["session_id"] = session_id
            (sessions / ("%s.json" % session_id)).write_text(
                json.dumps(fields), encoding="utf-8")

        def stamp(age):
            return time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(now - age)) + local_offset()

        for entry in GREEN:
            if entry["tool"] is None:
                continue
            session_id = str(uuid.uuid4())
            pid = self.sleeper()
            self.cards[entry["session"]] = session_id
            cwd = str(self.home / "work" / entry["project"])
            fields = {"state": entry["state"], "project": entry["project"],
                      "cwd": cwd, "pid": pid,
                      "tmux_pane": self.panes[entry["session"]],
                      "tmux_session": entry["session"],
                      "last_event": entry["last_event"],
                      "updated_at": stamp(entry.get("age", 30))}
            if entry.get("source"):
                fields["source"] = entry["source"]
            if entry["tool"] != "claude":
                fields["tool"] = entry["tool"]
            card(session_id, **fields)
            if entry.get("title") and entry["tool"] == "claude":
                self.write_transcript(session_id, cwd, entry["title"])
            if entry.get("ctx"):
                (sessions / ("%s.ctx.json" % session_id)).write_text(
                    json.dumps({"session_id": session_id, "pct": entry["ctx"],
                                "at": now, "warned": False}), encoding="utf-8")
            if entry.get("parked"):
                # The pane's conversation went to the background: Claude Code
                # records the link by pid, and the job has a card of its own
                # whose id starts with the short one in the registry.
                short = "7c1d4a9e"
                job_id = short + "-2b55-4f01-9a3e-6d20b1c47f88"
                card(job_id, state="working", project=entry["project"],
                     cwd=cwd, pid=self.sleeper(), last_event="PostToolUse",
                     updated_at=stamp(14))
                (registry / ("%d.json" % pid)).write_text(json.dumps({
                    "pid": pid, "sessionId": session_id, "cwd": cwd,
                    "kind": "interactive", "parkedJobId": short,
                    "name": entry["title"]}), encoding="utf-8")

        # One agent alive OUTSIDE tmux: a card with no pane, and the name Claude
        # Code registered for it (which is what the dimmed row shows).
        outside_id = str(uuid.uuid4())
        outside_pid = self.sleeper()
        card(outside_id, state="working", project="release",
             cwd=str(self.home / "work" / "release"), pid=outside_pid,
             last_event="PostToolUse", updated_at=stamp(480))
        (registry / ("%d.json" % outside_pid)).write_text(json.dumps({
            "pid": outside_pid, "sessionId": outside_id, "kind": "interactive",
            "cwd": str(self.home / "work" / "release"),
            "name": "review the release notes"}), encoding="utf-8")

    def start_menu(self):
        """`flightdeck restart`: it makes the `flightdeck` session when there is
        none and installs the bar, the keys and the refresh hook on this
        server -- the same call somebody makes to pick up new code."""
        subprocess.run([str(ROOT / "bin" / "flightdeck"), "restart"],
                       env=self.env(), capture_output=True, text=True, check=True)

    def attach(self):
        """A real client looking at the cockpit, inside a pane we can photograph.

        The host server is left at its factory settings and its status bar is
        turned off: the pane then has all 30 rows, and the only thing drawing in
        them is the cockpit's own client.
        """
        self.host("new-session", "-d", "-s", "host", "-x", COLUMNS, "-y", ROWS,
                  "tmux", "-L", SOCKET, "attach", "-t", "=flightdeck")
        self.host("set", "-g", "status", "off")
        self.host("set", "-g", "escape-time", "10")
        self.host_pane = self.host("display-message", "-p", "-t", "host",
                                   "#{pane_id}").strip()

    # -- driving it ---------------------------------------------------------

    def keys(self, *keys):
        self.host("send-keys", "-t", self.host_pane, *keys)

    def type(self, text):
        self.host("send-keys", "-t", self.host_pane, "-l", "--", text)

    def screen(self):
        return self.host("capture-pane", "-e", "-p", "-t", self.host_pane)

    def plain(self):
        return self.host("capture-pane", "-p", "-t", self.host_pane)

    def wait_for(self, needle, timeout=20, absent=False, required=True):
        """Wait until the screen does (or stops) showing `needle`.

        `required=False` waits for something the shot is better WITH and can
        survive without -- a redraw that may already have happened -- and
        answers False instead of raising when it does not come.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (needle in self.plain()) != absent:
                time.sleep(0.35)      # let the repaint finish
                return True
            time.sleep(0.2)
        if not required:
            return False
        raise RuntimeError("timed out waiting for %r (absent=%s)" % (needle, absent))

    def menu_ready(self):
        """Wait for the menu and remember that its cursor is back at the top.

        Every round of the picker is a NEW fzf, so coming back from a session
        the cursor is on the first row again, whatever it was on before.
        """
        self.wait_for("session>")
        self.cursor = 0

    def move_to(self, needle):
        """Put the cursor on the row matching `needle`, from wherever it is.

        Relative, because the storyboard now walks the list rather than only
        walking down it: the detail strip is shown changing on a row of another
        tool first, and the row the demo goes into is above that one.
        """
        target = self.row_index(needle)
        key = "Down" if target > self.cursor else "Up"
        for _ in range(abs(target - self.cursor)):
            self.keys(key)
            time.sleep(0.08)
        self.cursor = target
        time.sleep(0.5)   # the strip is a preview: let fzf repaint it

    def row_index(self, needle):
        """How many rows down the menu's list `needle` sits (0 = the top one).

        Asked of `picker feed`, which is the very list the menu is showing, so
        the cursor is moved by counting rows rather than by guessing at an order
        that depends on when each session was last touched.
        """
        feed = self._helper([sys.executable, "-m", "flightdeck.picker", "feed"],
                            "asking the picker for its list")
        for index, line in enumerate(feed.splitlines()):
            if needle in line:
                return index
        raise RuntimeError("no menu row matching %r" % needle)

    def fire_stop_hook(self, session):
        """The real state hook, fed a synthetic `Stop` for one session.

        This is what makes the notice frame real: the hook writes the card,
        notices that the badge changed, tells the open menu to reload and sends
        `⏳ <session> is waiting for you` to every client of this server -- which
        is the client being photographed.
        """
        session_id = self.cards[session]
        env = self.env(TMUX_PANE=self.panes[session],
                       # The hook checks its parent's argv to ignore a
                       # `claude --print`. Its parent here is this script, whose
                       # own arguments are none of its business, so it is told
                       # through the seam the tests use.
                       FLIGHTDECK_PARENT_ARGV_TEST="claude")
        subprocess.run([sys.executable,
                        str(ROOT / "flightdeck" / "hooks" / "session_hook.py"),
                        "Stop"],
                       input=json.dumps({"session_id": session_id,
                                         "hook_event_name": "Stop",
                                         "cwd": str(self.home / "work" / "forecasting")}),
                       env=env, capture_output=True, text=True)

    # -- cleanup ------------------------------------------------------------

    def close(self):
        for socket in (HOST_SOCKET, SOCKET):
            subprocess.run(["tmux", "-L", socket, "kill-server"],
                           capture_output=True, env=self.env())
        for process in self.sleepers:
            try:
                process.terminate()
            except OSError:
                pass
        for process in self.sleepers:
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        # A moment for the panes tmux has just killed to finish dying: one of
        # them is a shell, and a shell writes on its way out.
        time.sleep(0.5)
        if not self.keep:
            shutil.rmtree(self.work, ignore_errors=True)


def local_offset():
    """This machine's UTC offset as `+HH:MM`, which is what `now_iso` writes."""
    offset = -(time.altzone if time.daylight and time.localtime().tm_isdst
               else time.timezone)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return "%s%02d:%02d" % (sign, offset // 3600, (offset % 3600) // 60)


def shell_quote(text):
    return "'%s'" % text.replace("'", "'\\''")


# ---------------------------------------------------------------------------
# The shoot
# ---------------------------------------------------------------------------

# The storyboard, in order. The position in this tuple IS the frame's position
# in the GIF, which is what lets one of them be re-shot and put back in its
# place (`--only`).
FRAMES = ("menu", "detail", "cursor", "session", "notice", "back", "pin",
          "accounts", "filter", "flags", "statusline")


def frame_name(key):
    """`accounts` -> `07-accounts`: numbered so the files sort as they play."""
    return "%02d-%s" % (FRAMES.index(key) + 1, key)


class Frame:
    def __init__(self, key, text, ms):
        self.key, self.name, self.text, self.ms = key, frame_name(key), text, ms
        self.index = FRAMES.index(key)
        self.png = None


def shoot(demo, only=None):
    """Walk the storyboard, photographing the screen at each stop.

    `only` keeps just that one frame. The WALK still happens in full, because
    there is no way to stand in front of the accounts pane without first opening
    the menu and pressing Enter on its row -- and because a frame reached by a
    shortcut would not be a picture of the same thing. What `only` buys is that
    the other nine are not re-photographed, so re-shooting one does not re-roll
    every age, clock time and cursor position in the rest of the GIF. They are
    not quite untouched: the shared palette is rebuilt around the new frame, and
    measured that moves them by about 0.1 of one colour level out of 255.
    """
    frames = []

    def take(key, ms):
        if only and key != only:
            return None
        frame = Frame(key, demo.mask(demo.screen()), ms)
        frames.append(frame)
        return frame

    demo.menu_ready()
    take("menu", HOLD_MS)

    # 2. The strip under the list follows the cursor. It is shown on a row of
    #    ANOTHER tool, because that is where the change is easiest to see: the
    #    folder and its branch, then codex's glyph, the project, the state in
    #    plain words, the age and the short id, where the row above said claude.
    demo.move_to("codex sandbox")
    take("detail", STRIP_MS)

    # 3. and back up onto the session that is waiting for you.
    demo.move_to("pricing 3")
    take("cursor", STEP_MS)

    # 4. Enter: inside that session, with Flightdeck's own status line under it.
    demo.keys("Enter")
    demo.wait_for("margin_pct")
    take("session", HOLD_MS)

    # 4. Another session finishes its turn. The hook is real, so the notice is.
    demo.fire_stop_hook("forecasting 14")
    demo.wait_for("is waiting for you", timeout=10)
    take("notice", HOLD_MS)

    # 5. F12 back to the menu, where that row now reads WAITING FOR YOU. Waits
    #    for the notice to go first: a keystroke would dismiss it early and the
    #    next frame would be taken mid-repaint.
    demo.wait_for("is waiting for you", timeout=12, absent=True)
    demo.keys("F12")
    demo.menu_ready()
    demo.tmux("refresh-client", "-S")     # repaint the bar now, not in 5 s
    time.sleep(0.6)
    take("back", HOLD_MS)

    # A pinned row: your own tool, in its own tmux session. The strip says what
    # the row runs and which session it lives in.
    demo.move_to("accounts")
    take("pin", STEP_MS)
    demo.keys("Enter")
    demo.wait_for("Switch account", timeout=25)
    # And then wait for it to REDRAW. The picker makes a pin's session with no
    # size of its own, so tmux gives it the 80x24 it gives any detached session,
    # and the program inside draws once at that before the client's resize
    # reaches it. Photographed there, the frame came out 80 columns wide inside
    # a 100-column pane, with its rows wrapped and its bars short. The separator
    # rule spans the pane, so a long one is the proof that the second drawing
    # has landed; `required=False` because on a fast redraw it is already true.
    demo.wait_for("─" * (COLUMNS - 10), timeout=15, required=False)
    time.sleep(1.2)
    take("accounts", HOLD_MS)

    # 8-9. Back, filter the history, and launch a closed conversation your way.
    demo.keys("F12")
    demo.menu_ready()
    demo.type(FILTER_QUERY)
    time.sleep(0.8)
    take("filter", STEP_MS)
    demo.keys("C-l")
    demo.wait_for("flags>")
    time.sleep(0.4)
    take("flags", HOLD_MS)
    demo.keys("Escape")
    demo.menu_ready()

    # The status line on its own, both tools.
    demo.tmux("switch-client", "-t", "=scratch")
    demo.wait_for("~/work/demo $")
    demo.type("flightdeck statusline --demo")
    time.sleep(0.4)
    demo.keys("Enter")
    demo.wait_for("Antigravity (agy)", timeout=20)
    time.sleep(0.6)
    take("statusline", HOLD_MS + 600)
    return frames


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def page_size(columns, rows, padding=PADDING):
    """The Chrome window a block of `columns` x `rows` cells needs.

    Even numbers on both sides: the GIF is this halved, and an odd pixel there
    is a row of text resampled across two of them.
    """
    width = columns * FONT_SIZE * CHAR_RATIO + 2 * padding + 2 * MARGIN
    height = rows * FONT_SIZE * LEADING + 2 * padding + 2 * MARGIN
    return (int(width) // 2 * 2 + 2, int(height) // 2 * 2 + 2)


def window_size():
    """The window every GIF frame is taken in: a whole 100x30 screen."""
    return page_size(COLUMNS, ROWS)


def screenshot(html_path, png_path, size, scale=2):
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=%d" % scale,
                    "--window-size=%d,%d" % size,
                    "--screenshot=%s" % png_path,
                    "--virtual-time-budget=1500",
                    html_path.as_uri()],
                   capture_output=True, text=True)
    if not png_path.exists():
        raise RuntimeError("Chrome wrote no screenshot for %s" % html_path.name)


def render(frames, directory, size):
    for frame in frames:
        html_path = directory / ("%s.html" % frame.name)
        html_path.write_text(
            ansi2html.render_page(frame.text, title=frame.name,
                                  font_size=FONT_SIZE, leading=LEADING,
                                  padding=PADDING, margin=MARGIN),
            encoding="utf-8")
        frame.png = directory / ("%s.png" % frame.name)
        screenshot(html_path, frame.png, size)


# How many rows the status line still is. It is Flightdeck's default layout
# (`statusline.LINES_DEFAULT`), spelled here as a number the picture is sized by
# and checked against what the command actually printed: a still one row short
# would crop the tokens off the bottom without failing.
STILL_LINES = 3


def statusline_still(demo, directory):
    """The Claude Code status line on its own: its three lines, a little padding.

    Taken from the command's own output rather than cropped out of a frame, so
    the picture is of the LINE: cropped, it would carry the pane's padding, the
    cursor and whatever the excerpt above it left behind, and the image would be
    sized by the terminal instead of by the line.

    And asked for with NO width (`width=None`), so nothing is trimmed: the
    README paragraph beside this picture promises the model, the folder, the
    branch, the context bar, the limits with the time they reset, and the
    tokens. At 100 columns the line sheds the branch and the `↻` times, and the
    still would then be missing two of the things the sentence above it names.
    """
    wanted = [l.rstrip()
              for l in demo.mask(demo.statusline_text(width=None)).split("\n")
              if l.strip()][:STILL_LINES]
    if len(wanted) < STILL_LINES:
        raise RuntimeError("the status line did not come out in %d lines"
                           % STILL_LINES)
    html_path = directory / "statusline.html"
    html_path.write_text(
        ansi2html.render_page("\n".join(wanted), title="status line",
                              font_size=FONT_SIZE, leading=LEADING,
                              padding=STILL_PADDING, margin=MARGIN),
        encoding="utf-8")
    columns = max(ansi2html.run_width(_strip(line)) for line in wanted)
    png = directory / "statusline.png"
    screenshot(html_path, png, page_size(columns, STILL_LINES, STILL_PADDING))
    return png


def _strip(line):
    return re.sub(r"\x1b\[[0-9;:]*[A-Za-z]", "", line)


# ---------------------------------------------------------------------------
# The GIF (the one step that needs Pillow)
# ---------------------------------------------------------------------------

def assemble(spec_path):
    """Put the frames together. Run in whichever interpreter has Pillow."""
    from PIL import Image
    # Pillow moved these onto enums and kept the old names as aliases; both
    # spellings are accepted so the script is not pinned to one Pillow.
    _LANCZOS = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS
    _MEDIANCUT = getattr(Image, "MEDIANCUT", None) or Image.Quantize.MEDIANCUT
    _NO_DITHER = getattr(Image, "NONE", None) or Image.Dither.NONE

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    size = tuple(spec["size"])

    def load(png):
        image = Image.open(png).convert("RGB")
        return image if image.size == size else image.resize(size, _LANCZOS)

    if spec.get("splice") is None:
        images = [load(frame["png"]) for frame in spec["frames"]]
        durations = [frame["ms"] for frame in spec["frames"]]
    else:
        # One frame re-shot: the other nine are read back out of the GIF that is
        # already there and put back unchanged. `seek` + `convert` composites,
        # which matters because the frames after the first are stored as the box
        # that changed rather than as whole pictures.
        images, durations = [], []
        with Image.open(spec["out"]) as existing:
            if existing.size != size:
                raise SystemExit(
                    "demo_gif: %s is %dx%d and the frames are %dx%d -- the "
                    "geometry changed, so render the whole GIF, not one frame."
                    % ((spec["out"],) + existing.size + size))
            for index in range(existing.n_frames):
                existing.seek(index)
                images.append(existing.convert("RGB"))
                # A GIF frame is allowed to carry no duration, and Pillow hands
                # that back as None -- which `save()` cannot use. It only
                # happens to a GIF written by something else, but this reads one
                # off the disk and has no say in who wrote it.
                durations.append(existing.info.get("duration") or HOLD_MS)
        index = spec["splice"]
        if not 0 <= index < len(images):
            raise SystemExit("demo_gif: %s has no frame %d to replace"
                             % (spec["out"], index))
        images[index] = load(spec["frames"][0]["png"])
        durations[index] = spec["frames"][0]["ms"]
    # ONE palette for the whole GIF, built from every frame at once. With a
    # palette per frame the colours shift slightly between them and the whole
    # picture shimmers as it loops.
    montage = Image.new("RGB", (size[0], size[1] * len(images)))
    for index, image in enumerate(images):
        montage.paste(image, (0, index * size[1]))
    palette = montage.quantize(colors=spec["colours"], method=_MEDIANCUT)
    frames = [image.quantize(palette=palette, dither=_NO_DITHER)
              for image in images]
    # No `disposal`: Pillow crops the frames after the first to the box that
    # actually changed, and a disposal that clears the rest to the background
    # would blank everything it cropped away.
    frames[0].save(spec["out"], save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    return 0


def preflight():
    """Everything the walk needs, checked BEFORE the walk starts.

    The walk takes about eighty seconds and most of what it needs is only
    reached at the end of it, so a missing program used to be found in the worst
    possible place. Chrome was only touched inside `screenshot()`, after every
    frame had been photographed; and a missing `cswap` never failed at all, it
    made `wait_for` sit there until it timed out on "Switch account", which
    reads like the storyboard is wrong rather than like a program is not
    installed.

    One line per problem, all of them at once: being told about Chrome and then
    about `cswap` on the next run is two wasted minutes.
    """
    missing = []
    for tool, why in (("tmux", "the demo runs on a tmux server of its own"),
                      ("fzf", "the menu IS an fzf"),
                      (PIN_COMMAND.split()[0],
                       "the pinned row runs it")):
        if shutil.which(tool) is None:
            missing.append("%s is not on PATH -- %s" % (tool, why))
    if not Path(CHROME).exists():
        missing.append("no Chrome at %s -- it is what takes the screenshots"
                       % CHROME)
    if missing:
        raise SystemExit("\n".join("demo_gif: " + line for line in missing))


def pillow_python(explicit=None):
    """An interpreter with Pillow: the one asked for, or this one if it has it."""
    if explicit:
        return explicit
    if os.environ.get("PILLOW_PYTHON"):
        return os.environ["PILLOW_PYTHON"]
    try:
        import PIL  # noqa: F401
        return sys.executable
    except ImportError:
        raise SystemExit(
            "demo_gif: Pillow is needed to assemble the GIF and is not in %s.\n"
            "          Point --python (or PILLOW_PYTHON) at an interpreter that"
            " has it." % sys.executable)


# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "assemble":       # the Pillow half, re-entered
        return assemble(argv[1])

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="docs/img",
                        help="where the GIF and the stills go")
    parser.add_argument("--python", default=None,
                        help="an interpreter with Pillow, for the GIF")
    parser.add_argument("--keep", action="store_true",
                        help="leave the working directory and the frames behind")
    parser.add_argument("--only", choices=FRAMES, default=None,
                        help="re-shoot ONE frame and put it back in the GIF "
                             "that is already there, leaving the others alone")
    parser.add_argument("--colours", type=int, default=GIF_COLOURS)
    options = parser.parse_args(argv)

    out = Path(options.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    preflight()
    assembler = pillow_python(options.python)
    gif = out / "demo.gif"
    if options.only and not gif.exists():
        raise SystemExit("demo_gif: --only %s needs %s to already exist; "
                         "render the whole thing first." % (options.only, gif))

    # Resolved: on macOS `/var` is a symlink to `/private/var`, and an
    # unresolved HOME would not be the prefix of the working directory the
    # status line reports -- so it would print an absolute path instead of `~`.
    work = Path(tempfile.mkdtemp(prefix="flightdeck-demo-")).resolve()
    demo = Demo(work, keep=options.keep)
    try:
        demo.build_home()
        demo.build_history()
        demo.start_server()
        demo.start_sessions()
        demo.write_cards()
        demo.start_menu()
        demo.attach()
        frames = shoot(demo, options.only)
        if not frames:
            raise SystemExit("demo_gif: the walk finished without taking %s"
                             % options.only)
        size = window_size()
        render(frames, demo.frames_dir, size)
        # Each still belongs to one frame, so `--only` refreshes a still exactly
        # when it has re-shot the frame that still came from.
        taken = {frame.key: frame for frame in frames}
        if "menu" in taken:
            shutil.copyfile(taken["menu"].png, out / "menu.png")
        if "statusline" in taken:
            shutil.copyfile(statusline_still(demo, demo.frames_dir),
                            out / "statusline.png")
        spec = demo.frames_dir / "gif.json"
        spec.write_text(json.dumps({
            "frames": [{"png": str(f.png), "ms": f.ms} for f in frames],
            "out": str(gif), "size": list(size),
            "colours": options.colours,
            "splice": frames[0].index if options.only else None},
            ), encoding="utf-8")
        subprocess.run([assembler, str(Path(__file__).resolve()), "assemble",
                        str(spec)], check=True)
    finally:
        demo.close()

    if options.only:
        print("demo.gif      %6.1f KB  frame %d (%s) replaced"
              % (gif.stat().st_size / 1024, frames[0].index + 1, options.only))
    else:
        print("demo.gif      %6.1f KB  %d frames"
              % (gif.stat().st_size / 1024, len(frames)))
    for name in ("menu.png", "statusline.png"):
        print("%-13s %6.1f KB" % (name, (out / name).stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
