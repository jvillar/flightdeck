"""The bash command (`bin/flightdeck`).

It is the only piece of Flightdeck that is not Python, and almost everything it
does is call tmux. So these tests do two things:

- Run the subcommands that have no side effects (`--help`, `--version`, the
  internal `_port`, the two refresh ones) and check what they answer.
- Run `init` and `quit` against a tmux server **of their own** (`-L fdtest`),
  created and killed here. No test ever talks to the server the developer is
  working in: every tmux call in this file carries `-L fdtest`, and the command
  is given `FLIGHTDECK_TMUX_SOCKET` so it does the same.

The command with NO arguments is never run here: it attaches a terminal to the
menu, which a test has no business doing.
"""

import atexit
import json
import os
import time
import sys
import select
import pty
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import config

CMD = config.code_dir() / "bin" / "flightdeck"

# Every run of the command happens from HERE, and never from the repository root:
# a user runs the command from wherever they happen to be, and a cwd that is not
# the code directory is what keeps a broken computation of that directory (a
# symlink not followed, say) from passing unnoticed. The opposite case -- a cwd
# holding a `flightdeck/` package that must NOT win over the code -- is
# `TestTheCodeDirectoryWinsOverTheCwd`.
NEUTRAL_CWD = tempfile.mkdtemp(prefix="flightdeck-cli-cwd-")
atexit.register(shutil.rmtree, NEUTRAL_CWD, True)

# The tmux server this file is allowed to touch. It is created in setUp and
# killed in tearDown; the name is deliberately not one anybody would use by hand.
SOCKET = "fdtest"

# A socket nothing listens on: `tmux -L <this> has-session` answers "no server",
# which is exactly the state `init` has to refuse to work in.
DEAD_SOCKET = "fdtest-no-such-server"

# The port the refresh tests are allowed to POST to. It must NOT be the default
# (42707): the developer running the suite may have a real menu listening there,
# and a `track-current+reload(...)` reaching it would replace the list in the
# menu they are looking at. Anything that answers on this port gets a reload it
# did not ask for, so it is a port no menu of theirs will be using.
TEST_PORT = 43117


def _run(args, cmd=None, cwd=None, **env_extra):
    """The command, with the environment pointed away from anything real.

    `cmd` runs a different path to the same script (a symlink to it), which is
    how the command is reached once it is installed into PATH.
    """
    env = dict(os.environ)
    # The default for every run: a socket with no server behind it. A test that
    # wants the real (isolated) one passes it explicitly.
    env["FLIGHTDECK_TMUX_SOCKET"] = DEAD_SOCKET
    env.update(env_extra)
    # A test that moves HOME gets that home's XDG directories too, unless it
    # named one itself. Without this the two travel apart: `quit` sources
    # `$XDG_CONFIG_HOME/tmux/tmux.conf` when the home has no `.tmux.conf`, so a
    # test with a temporary HOME on a machine that exports `XDG_CONFIG_HOME`
    # would run the developer's real tmux config on the test server. The suite's
    # own sandbox covers the runs that do not move HOME (`tests/__init__.py`).
    if "HOME" in env_extra:
        for name, tail in (("XDG_CONFIG_HOME", (".config",)),
                           ("XDG_DATA_HOME", (".local", "share")),
                           ("XDG_STATE_HOME", (".local", "state"))):
            if name not in env_extra:
                env[name] = os.path.join(env_extra["HOME"], *tail)
    return subprocess.run([str(cmd or CMD)] + list(args), capture_output=True,
                          text=True, env=env, timeout=30, cwd=cwd or NEUTRAL_CWD)


def _path_with_a_fzf_above_the_floor(tmp):
    """PATH with a stub `fzf` first, one that reports a version above the floor.

    The run ends with the doctor, and the doctor grades whatever `fzf` the
    machine has: on a runner whose own is below the floor (Ubuntu 22.04's apt
    ships 0.29) that is a ✗ and the run exits 1 -- about the runner, not about
    the install these tests are watching. The stub keeps the verdict about the
    install; the doctor's own grading of fzf has its own tests.
    """
    bindir = Path(tmp) / "fzf-bin"
    bindir.mkdir()
    stub = bindir / "fzf"
    stub.write_text("#!/bin/sh\necho 0.74.4\n")
    stub.chmod(0o755)
    return "%s:%s" % (bindir, os.environ.get("PATH", ""))


def _tmux(*args):
    """A tmux command on OUR server. Never the developer's."""
    return subprocess.run(["tmux", "-L", SOCKET] + list(args),
                          capture_output=True, text=True, timeout=30)


def _config_file(folder, **values):
    """A `config.json` in `folder`, and the environment pointing the command at it."""
    path = Path(folder) / "config.json"
    path.write_text(json.dumps(values))
    return {"FLIGHTDECK_CONFIG": str(path)}


class TestTheScriptItself(unittest.TestCase):
    """What the file is, and the subcommands that only print."""

    def test_the_script_parses(self):
        # `bash -n` reads the whole file without running a line of it: it is the
        # cheapest guard against a quoting mistake in a branch no test reaches
        # (`restart`, or the menu-per-window one).
        r = subprocess.run(["bash", "-n", str(CMD)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_it_is_executable(self):
        # The state hook launches it by path (`Popen([command_path(), ...])`),
        # which needs the bit set: without it the menus would never reload on
        # their own, and the failure is swallowed by design so nobody would hear.
        self.assertTrue(os.access(str(CMD), os.X_OK))

    def test_help_prints_the_usage_and_exits_0(self):
        r = _run(["--help"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: flightdeck", r.stdout)

    def test_the_usage_names_every_subcommand(self):
        # The help and the dispatch drifting apart is the classic way a command
        # grows an option nobody can find.
        out = _run(["--help"]).stdout
        for name in ("init", "restart", "quit", "install", "update", "uninstall",
                     "doctor", "pin", "statusline", "goto-menu", "refresh-menu",
                     "refresh-menus", "--version"):
            self.assertIn(name, out)

    def test_version_prints_something(self):
        r = _run(["--version"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("flightdeck "), r.stdout)
        self.assertTrue(any(c.isdigit() for c in r.stdout), r.stdout)

    def test_the_version_is_the_one_in_the_VERSION_file(self):
        # One source for both languages: the bash reads the file and
        # `config.version()` reads the same one. Stamped in two places they
        # would drift the first time only one of them was bumped.
        r = _run(["--version"])
        self.assertEqual(r.stdout.strip(), "flightdeck %s" % config.version())

    def test_an_unknown_subcommand_is_an_error_with_the_usage(self):
        r = _run(["definitely-not-a-subcommand"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage: flightdeck", r.stderr)
        self.assertEqual(r.stdout, "")

    # `update` and `uninstall` used to answer "not available yet" here, and the
    # test for it ran them with no HOME of its own -- harmless while they only
    # printed a line, and a REAL uninstall against the developer's home the
    # moment they landed. They are tested in
    # `TestUninstallAndUpdateReachTheirModules` below, where everything the two
    # can reach is a temporary directory.


class TestInstall(unittest.TestCase):
    """`install` reaches `flightdeck.install`, arguments and exit code included.

    It WRITES, so everything it can reach is a throwaway: HOME (hence
    `~/.claude`, `~/.codex` and `~/.gemini`), Flightdeck's config file and its
    state directory -- the suite-wide sandbox is shared with every other test,
    and a mode or a pin written into it would leak. The tmux socket is the dead
    one `_run` sets, so the key check finds no server, and `--yes` means nothing
    is ever waiting on a stdin that is not there.
    """

    def _install(self, tmp, *args):
        home = Path(tmp) / "home"
        home.mkdir()
        return _run(["install"] + list(args), HOME=str(home),
                    FLIGHTDECK_CONFIG=str(Path(tmp) / "config.json"),
                    FLIGHTDECK_STATE_DIR=str(Path(tmp) / "state"),
                    PATH=_path_with_a_fzf_above_the_floor(tmp)), home

    def test_it_sets_up_a_bare_home_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, home = self._install(tmp, "--yes")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("session_hook.py",
                          (home / ".claude" / "settings.json").read_text())
            self.assertIn("Next steps", r.stdout)

    def test_an_option_it_does_not_know_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _home = self._install(tmp, "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


class TestUninstallAndUpdateReachTheirModules(unittest.TestCase):
    """The two commands are branches that hand everything to python.

    Neither is asked to do anything here: both are given a word they do not know,
    which the module answers with its usage and exit code 2 before removing or
    downloading a thing. That is the whole point -- it proves the arguments and
    the exit code travel, on a command whose real job is deleting files.

    Everything they could reach is a throwaway anyway: HOME, the data directory
    (so the code directory is a source checkout and is left alone), the state and
    the config, and `_run`'s dead tmux socket.
    """

    def _env(self, tmp):
        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        return {"HOME": str(home),
                "XDG_DATA_HOME": str(Path(tmp) / "share"),
                "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}

    def test_uninstall_passes_its_words_and_its_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(["uninstall", "--everything"], **self._env(tmp))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("usage: flightdeck uninstall", r.stderr)

    def test_update_passes_its_words_and_its_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(["update", "--nightly"], **self._env(tmp))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("usage: flightdeck update", r.stderr)

    def test_neither_is_the_not_available_yet_message_any_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            for word in ("uninstall", "update"):
                r = _run([word, "--everything"], **self._env(tmp))
                self.assertNotIn("not available yet", r.stderr, word)


class TestDoctor(unittest.TestCase):
    """`doctor` reaches `flightdeck.doctor` and its exit code comes back whole.

    HOME points at an empty temporary directory, so the report is about a
    machine with no `~/.claude`, no `~/.codex` and no `~/.gemini` rather than
    about the developer's -- and it is the same fixture on any machine.
    """

    def _doctor(self, home):
        return _run(["doctor"], HOME=str(home))

    def test_it_runs_and_reports_on_this_machine(self):
        with tempfile.TemporaryDirectory() as home:
            r = self._doctor(home)
        # The exit code is the CONTRACT and not a property of this machine: 1
        # exactly when something is ✗. Asserting a flat 0 would go red on a CI
        # runner with no fzf installed, which is the very thing doctor is for.
        self.assertEqual(r.returncode, 1 if "✗" in r.stdout else 0, r.stdout)
        self.assertIn("flightdeck %s" % config.version(), r.stdout)
        for name in ("system:", "tmux:", "fzf:", "python3:", "locale:"):
            self.assertIn(name, r.stdout)

    def test_in_an_empty_home_it_says_the_agents_are_not_set_up(self):
        # Deterministic wherever the suite runs: an empty HOME has no Claude
        # Code settings, so our hooks and our status line cannot be registered.
        with tempfile.TemporaryDirectory() as home:
            r = self._doctor(home)
        self.assertIn("! Claude Code hooks:", r.stdout)
        self.assertIn("flightdeck install", r.stdout)
        self.assertIn("! status line (Claude Code):", r.stdout)
        # And with no link in `~/.local/bin`, that is a warning and never a ✗.
        self.assertNotIn("✗ flightdeck on PATH", r.stdout)

    def test_it_does_not_start_a_tmux_server_to_read_the_keys(self):
        """The doctor writes nothing -- not even the socket tmux makes to answer.

        Measured on tmux 3.6a: with no server, `list-keys` exits 0, prints the
        STOCK table and leaves a socket file behind (a momentary server), while
        `has-session` answers rc 1 and leaves none. `TMUX_TMPDIR` puts that
        whole tree in a directory of the test's own, so the proof is simply
        that no socket appears in it.
        """
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as tmux_tmp:
            r = _run(["doctor"], HOME=str(home), TMUX_TMPDIR=tmux_tmp)
            sockets = [p for p in Path(tmux_tmp).rglob("*") if p.is_socket()]
        self.assertIn("no live tmux server", r.stdout)
        self.assertEqual(sockets, [], "doctor started a tmux server")

    def test_an_argument_it_does_not_know_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as home:
            r = _run(["doctor", "--json"], HOME=str(home))
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


class TestStatusline(unittest.TestCase):
    """`statusline` reaches `flightdeck.statusline`, arguments and all.

    Same two-line branch as `pin` (shift, run python, pass the exit code on), so
    what is pinned is that the arguments survive the shell and that a status
    line asked for a payload NEVER answers with a failure: Claude Code paints
    whatever comes back where the bar goes.
    """

    def test_the_demo_prints_the_two_lines(self):
        r = _run(["statusline", "--demo"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Flightdeck status line", r.stdout)
        self.assertIn("Ctx ", r.stdout)
        self.assertIn("Tokens: msg ", r.stdout)

    def test_claude_reads_the_payload_from_stdin(self):
        payload = json.dumps({"model": {"display_name": "Opus 5"},
                              "cwd": NEUTRAL_CWD,
                              "context_window": {"used_percentage": 42}})
        r = subprocess.run([str(CMD), "statusline", "claude"], input=payload,
                           capture_output=True, text=True, timeout=30,
                           cwd=NEUTRAL_CWD,
                           env=dict(os.environ,
                                    FLIGHTDECK_TMUX_SOCKET=DEAD_SOCKET))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Opus 5", r.stdout)
        self.assertIn("42%", r.stdout)

    def test_rubbish_on_stdin_is_still_a_success(self):
        r = subprocess.run([str(CMD), "statusline", "claude"], input="nonsense",
                           capture_output=True, text=True, timeout=30,
                           cwd=NEUTRAL_CWD,
                           env=dict(os.environ,
                                    FLIGHTDECK_TMUX_SOCKET=DEAD_SOCKET))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "\n")

    def test_an_unknown_tool_keeps_pythons_own_exit_code(self):
        r = _run(["statusline", "nosuchtool"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


class TestPin(unittest.TestCase):
    """`pin` reaches `flightdeck.pins`, and its answer comes back untouched.

    The command is a two-line branch (shift, run python, pass the exit code on),
    so what is worth pinning here is exactly that: the listing arrives, a write
    lands in the config the environment points at, and a refusal keeps its own
    exit code instead of being flattened to 0 or 1 by the shell.
    """

    def test_pin_list_prints_the_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(["pin", "list"], **_config_file(tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        for preset in ("top", "htop", "btop", "cswap", "lazygit", "lazydocker", "k9s"):
            self.assertIn(preset, r.stdout)

    def test_pin_with_no_arguments_lists_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp)
            self.assertEqual(_run(["pin"], **env).stdout,
                             _run(["pin", "list"], **env).stdout)

    def test_pin_add_and_remove_write_the_config_the_environment_points_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            path = Path(tmp) / "config.json"
            r = _run(["pin", "add", "htop"], **env)
            self.assertEqual(r.returncode, 0, r.stderr)
            written = json.loads(path.read_text())
            self.assertEqual([p["name"] for p in written["pins"]], ["htop"])
            # The rest of the file is left as it was: `pin` writes one key.
            self.assertEqual(written["menu_port"], 43000)
            self.assertEqual(_run(["pin", "remove", "htop"], **env).returncode, 0)
            self.assertEqual(json.loads(path.read_text())["pins"], [])

    def test_a_refusal_keeps_its_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(["pin", "add", "nope"], **_config_file(tmp))
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a preset", r.stderr)
        self.assertEqual(r.stdout, "")


class TestThePortFormula(unittest.TestCase):
    """The other half of the pin `test_picker.test_the_bash_command_reads_the_
    same_base_port` left open.

    The bash runs the `flightdeck.config` module with `menu_port` rather than
    carrying the port as a literal, so the pin is: the base the bash
    ends up using is the number that command prints, and a per-window menu
    (`flightdeck-N`) adds its own number to it, exactly like `picker.port_for`.
    """

    def test_the_bash_reads_the_same_base_as_the_config_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            from_cli = subprocess.run(
                ["python3", "-m", "flightdeck.config", "menu_port"],
                capture_output=True, text=True, timeout=30,
                cwd=str(config.code_dir()), env={**os.environ, **env})
            self.assertEqual(from_cli.stdout.strip(), "43000")
            r = _run(["_port", "flightdeck"], **env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), from_cli.stdout.strip())

    def test_a_leading_zero_is_read_in_base_ten(self):
        """`flightdeck-08` must answer base+8, not a bash arithmetic error.

        Measured before the fix: bash reads a leading zero as OCTAL, so `08` and
        `09` are not digits in that base -- `$(( PORT + 08 ))` died with "value
        too great for base", the port came out EMPTY and the error was painted
        over whatever tmux had on screen, which is the exact failure the
        all-digits guard was written to close. `flightdeck-010` was the quiet
        half of it: a valid octal, silently answering base+8.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            for name, expected in (("flightdeck-08", "43008"),
                                   ("flightdeck-09", "43009"),
                                   ("flightdeck-010", "43010"),
                                   ("flightdeck-007", "43007")):
                with self.subTest(name=name):
                    r = _run(["_port", name], **env)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stderr, "")
                    self.assertEqual(r.stdout.strip(), expected)

    def test_a_per_window_menu_adds_its_own_number(self):
        # Two fzf cannot listen on the same port (measured: the second
        # came out with "failed to listen" and its pane fell to a shell), which
        # is why every menu has one of its own.
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            self.assertEqual(_run(["_port", "flightdeck-2"], **env).stdout.strip(),
                             "43002")
            self.assertEqual(_run(["_port", "flightdeck-7"], **env).stdout.strip(),
                             "43007")

    def test_a_session_whose_suffix_is_not_a_number_gets_the_base_port(self):
        """A tmux session can be called anything, `flightdeck-2x` included.

        The glob that spots a per-window menu (`flightdeck-[0-9]*`) matches that
        name too, and `$(( PORT + 2x ))` is a bash arithmetic error ("value too
        great for base"): the port came out EMPTY, so the reload went to
        `localhost:` and the error was printed over whatever tmux had on screen.
        The suffix has to be digits and nothing else.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            for name in ("flightdeck-2x", "flightdeck-", "flightdeck-2.5",
                         "flightdeck-08notes"):
                with self.subTest(name=name):
                    r = _run(["_port", name], **env)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stderr, "")
                    self.assertEqual(r.stdout.strip(), "43000")

    def test_a_config_it_cannot_use_falls_back_to_the_default(self):
        # Same rule as every threshold on the Python side: a `menu_port` spelled
        # as a word is `doctor`'s business. The menu has to open anyway, so the
        # config CLI prints the default and the bash uses it.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("this is not json")
            r = _run(["_port", "flightdeck"], FLIGHTDECK_CONFIG=str(path))
            self.assertEqual(r.stdout.strip(), str(config.DEFAULTS["menu_port"]))

    def test_without_python_at_all_it_still_answers(self):
        # The fallback that matters on a broken machine: with nothing to ask,
        # the command still knows which port to talk to. bash is named by its
        # absolute path because the shebang itself (`/usr/bin/env bash`) is a
        # PATH lookup, and this run has no PATH to look in.
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is not installed")
        r = subprocess.run([bash, str(CMD), "_port", "flightdeck"],
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": "/nonexistent", "HOME": os.environ["HOME"]})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(config.DEFAULTS["menu_port"]))


class TestItFindsItsCodeDirectoryThroughASymlink(unittest.TestCase):
    """`$0` is not the script when the command is installed into PATH.

    The installer links it into `~/.local/bin`, and `dirname` of the
    LINK is that directory, not the code: without following the link the code
    directory would come out as `~/.local`, and every python call, the menu loop
    and the status bar would be pointing at nothing. The port read is what makes
    this falsifiable -- with the code directory lost, `flightdeck.config` cannot
    be imported and the answer falls back to the default instead of the
    configured 43000.
    """

    def test_through_an_absolute_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            link = Path(tmp) / "flightdeck"
            link.symlink_to(CMD)
            self.assertEqual(_run(["_port", "flightdeck"], cmd=link, **env).stdout.strip(),
                             "43000")
            self.assertEqual(_run(["_port", "flightdeck-2"], cmd=link, **env).stdout.strip(),
                             "43002")
            # And the subcommands that do not need the code directory still work
            # through the link, which is how a user meets the command at all.
            version = _run(["--version"], cmd=link, **env)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertTrue(version.stdout.startswith("flightdeck "), version.stdout)

    def test_through_a_chain_with_a_relative_target(self):
        # A link to a link, the second one relative: the loop has to follow every
        # hop, and a relative target is relative to the LINK's own directory, not
        # to the working directory the command happens to be run from.
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=43000)
            first = Path(tmp) / "flightdeck"
            first.symlink_to(CMD)
            nested = Path(tmp) / "nested"
            nested.mkdir()
            second = nested / "flightdeck"
            second.symlink_to(os.path.relpath(str(first), str(nested)))
            r = _run(["_port", "flightdeck-3"], cmd=second, **env)
            self.assertEqual(r.stdout.strip(), "43003", r.stderr)


class TestADownloadedFzfComesFirst(unittest.TestCase):
    """`install.sh` may put an fzf in `<data>/bin`; this is what uses it.

    A machine whose own fzf is missing or too old is offered the published
    binary, which lands beside `current` and not in any system directory.
    Nothing else puts that directory on a PATH, so if this command did not,
    the downloaded fzf would sit there unused and the menu would go on
    failing for the reason the download was offered to fix.

    The command is COPIED into the fake `<data>/current` rather than linked: the
    code directory is worked out by following `$0` through its links, so a link
    would resolve back to the repository and there would be no `<data>` in the
    picture at all.
    """

    def _installed(self, tmp, with_fzf):
        data = Path(tmp) / "share" / "flightdeck"
        (data / "current" / "bin").mkdir(parents=True)
        command = data / "current" / "bin" / "flightdeck"
        shutil.copy2(str(CMD), str(command))
        if with_fzf:
            (data / "bin").mkdir(parents=True)
            fzf = data / "bin" / "fzf"
            fzf.write_text("#!/bin/sh\necho 0.74.4\n")
            fzf.chmod(0o755)
        return data, command

    def test_it_goes_to_the_front_of_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, command = self._installed(tmp, with_fzf=True)
            r = _run(["_path"], cmd=command)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().split(os.pathsep)[0],
                             str(data / "bin"))

    def test_nothing_happens_when_there_is_no_downloaded_fzf(self):
        # A source checkout: `$(dirname "$SM")/bin` is whatever sits next to the
        # clone, and prepending somebody else's `bin/` to the PATH of every
        # agent the cockpit launches would be a considerable surprise.
        with tempfile.TemporaryDirectory() as tmp:
            data, command = self._installed(tmp, with_fzf=False)
            before = os.environ["PATH"]
            r = _run(["_path"], cmd=command)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), before)
            self.assertNotIn(str(data / "bin"), r.stdout)


class TestTheRefreshIsAlwaysQuiet(unittest.TestCase):
    """Both refresh subcommands exit 0 whatever they find.

    They are called from inside tmux (the `client-session-changed` hook) and from
    the state hook. A non-zero rc there is announced by tmux on screen with a
    "returned N" that explains nothing to anybody, and the fzf may simply not be
    listening yet.
    """

    def test_refresh_menu_exits_0_with_no_server_and_nothing_listening(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=TEST_PORT)
            self.assertEqual(_run(["refresh-menu", "flightdeck"], **env).returncode, 0)
            # Without an argument it is the main menu, which is what the tmux
            # hook falls back to.
            self.assertEqual(_run(["refresh-menu"], **env).returncode, 0)

    def test_refresh_menus_exits_0_with_no_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _config_file(tmp, menu_port=TEST_PORT)
            self.assertEqual(_run(["refresh-menus"], **env).returncode, 0)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TestInitAndQuitOnAServerOfOurOwn(unittest.TestCase):
    """`init` and `quit` against `-L fdtest`, created and killed by the test."""

    def setUp(self):
        _tmux("kill-server")  # whatever a previous run may have left
        r = _tmux("new-session", "-d", "-s", "scratch", "sleep 120")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        _tmux("kill-server")

    def _init(self):
        r = _run(["init"], FLIGHTDECK_TMUX_SOCKET=SOCKET)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _key(self, table, key):
        """What `key` is bound to in `table`, or "" when it is bound to nothing.

        The whole table is listed and the line for `key` picked out by its
        position in `bind-key [-r] -T <table> <key> ...`, not by searching the
        listing for the key's name: tmux's own bindings carry whole commands in
        their body (the `prefix <` menu mentions ` n ` inside it), so a plain
        search matches the wrong line. And the table is listed rather than
        asked for the one key (`list-keys -T <table> <key>`) because tmux 3.7
        answers that form with nothing at all, rc 0 (measured on 3.7c; 3.6a
        prints the line): the three tests here went red on the first CI run,
        on a runner with 3.7c, while the same bindings listed fine.
        """
        r = _tmux("list-keys", "-T", table)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        # "Bound to nothing" is only an answer when the table was read in the
        # shape matched below; a tmux that printed bindings some other way would
        # otherwise make every key look unbound, and the `quit` test would pass
        # for the wrong reason.
        self.assertTrue(any(re.match(r"^bind-key\s+(?:-r\s+)?-T\s+%s\s"
                                     % re.escape(table), line)
                            for line in lines), r.stdout)
        pattern = re.compile(r"^bind-key\s+(?:-r\s+)?-T\s+%s\s+%s\s"
                             % (re.escape(table), re.escape(key)))
        return "".join(line + "\n" for line in lines if pattern.match(line))

    def test_init_binds_shift_enter_and_turns_extended_keys_on(self):
        self._init()
        bound = self._key("root", "S-Enter")
        self.assertIn("send-keys", bound)
        self.assertIn("@flightdeck_agent", bound)   # the mark the doctor reads
        self.assertEqual(_tmux("show", "-sv", "extended-keys").stdout.strip(), "on")

    def test_quit_takes_shift_enter_and_extended_keys_back(self):
        self._init()
        _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET)
        self.assertEqual(self._key("root", "S-Enter"), "")
        self.assertEqual(_tmux("show", "-sv", "extended-keys").stdout.strip(), "off")

    def test_init_binds_the_keys_and_the_status_bar(self):
        r = self._init()
        self.assertIn("flightdeck: tmux configured", r.stdout)

        # F12 is a ROOT binding (no prefix): the toggle between the menu and the
        # session you came from.
        f12 = self._key("root", "F12")
        self.assertIn("goto-menu", f12)
        self.assertIn("switch-client -l", f12)

        # prefix+j is the one-way trip to the menu, for keyboards without a
        # comfortable F12 (the phone's).
        self.assertIn("goto-menu", self._key("prefix", "j"))

        # prefix+n is the handover, and it overrides tmux's stock `next-window`
        # on purpose: the cockpit hides the window list.
        n = self._key("prefix", "n")
        self.assertIn("flightdeck.handover", n)
        self.assertNotIn("next-window", n)

        # The bar is ours, and the hook that reloads a menu's list when a client
        # enters it is installed.
        self.assertIn("flightdeck.statusbar",
                      _tmux("show-options", "-g", "status-right").stdout)
        self.assertIn("refresh-menu", _tmux("show-hooks", "-g").stdout)

    def test_init_makes_the_terminal_tab_show_the_conversation_title(self):
        # `set-titles-string` reads `@title`, the tmux SESSION option the state
        # hook writes the conversation's human title into ("pricing 3" says more
        # than "pricing"), falling back to the session name, which is the user's
        # own. The two sides spelling that option differently would leave every
        # terminal tab saying "tmux", which is what it said before any of this.
        self._init()
        titles = _tmux("show-options", "-g", "set-titles-string").stdout
        self.assertIn("@title", titles)
        self.assertIn("#S", titles)
        self.assertIn("on", _tmux("show-options", "-g", "set-titles").stdout)

    def test_init_is_idempotent(self):
        self._init()
        before = (_tmux("list-keys", "-T", "root").stdout,
                  _tmux("list-keys", "-T", "prefix").stdout,
                  _tmux("show-options", "-g", "status-right").stdout,
                  _tmux("show-hooks", "-g").stdout)
        self._init()
        after = (_tmux("list-keys", "-T", "root").stdout,
                 _tmux("list-keys", "-T", "prefix").stdout,
                 _tmux("show-options", "-g", "status-right").stdout,
                 _tmux("show-hooks", "-g").stdout)
        self.assertEqual(before, after)

    def test_init_without_a_live_server_fails_and_says_so(self):
        # Options, bindings and hooks live INSIDE the tmux server: with no server
        # there is nowhere to install them, and claiming success would be a lie.
        r = _run(["init"], FLIGHTDECK_TMUX_SOCKET=DEAD_SOCKET)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no live tmux server", r.stderr)

    def test_quit_gives_tmux_its_keys_back_and_leaves_the_work_alone(self):
        self._init()
        r = _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cockpit off", r.stdout)

        # prefix+n is tmux's own again, F12 is nobody's, and the bar is back to
        # whatever tmux says it is.
        self.assertIn("next-window", self._key("prefix", "n"))
        self.assertEqual(self._key("root", "F12"), "")
        self.assertNotIn("flightdeck.statusbar",
                         _tmux("show-options", "-g", "status-right").stdout)
        self.assertNotIn("refresh-menu", _tmux("show-hooks", "-g").stdout)

        # And the promise that matters: the work sessions carry on.
        self.assertIn("scratch", _tmux("list-sessions", "-F", "#{session_name}").stdout)

    def test_quit_gives_back_the_users_own_tmux_config_not_the_factory_one(self):
        """R17: `set -gu` is not enough — it restores tmux's defaults, not theirs.

        Somebody with a status bar of their own in `~/.tmux.conf` had it wiped by
        `quit`: our `status-left` came out and tmux's stock one went in, and the
        bar they had spent an afternoon on was gone until they reloaded the file
        by hand. So the reset is followed by sourcing their config again.
        """
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".tmux.conf").write_text('set -g status-left "MINE"\n')
            _tmux("source-file", str(Path(home) / ".tmux.conf"))
            _run(["init"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            self.assertNotIn("MINE",
                             _tmux("show-options", "-g", "status-left").stdout)

            r = _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("MINE", _tmux("show-options", "-g", "status-left").stdout)

    def test_the_config_under_XDG_CONFIG_HOME_counts_too(self):
        # tmux 3.1 and later look in `$XDG_CONFIG_HOME/tmux/tmux.conf` as well,
        # and plenty of people keep it there.
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home) / "cfg" / "tmux" / "tmux.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text('set -g status-left "XDG"\n')
            _run(["init"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home,
                 XDG_CONFIG_HOME=str(Path(home) / "cfg"))
            self.assertIn("XDG", _tmux("show-options", "-g", "status-left").stdout)

    def test_with_no_config_of_their_own_quit_still_works(self):
        # The common case: no `.tmux.conf` anywhere, so there is nothing to
        # source and the stock values are the right answer.
        with tempfile.TemporaryDirectory() as home:
            _run(["init"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            r = _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("flightdeck.statusbar",
                             _tmux("show-options", "-g", "status-right").stdout)

    def test_an_exported_XDG_CONFIG_HOME_is_never_sourced_into_this_server(self):
        """The developer's own tmux config must not be run by a test.

        `stock_tmux` falls through to `$XDG_CONFIG_HOME/tmux/tmux.conf` when the
        home has no `.tmux.conf`, and a machine that exports that variable
        (routine on Linux and in CI) would hand the test server the real file --
        bindings, `run-shell` lines and all. `_run` gives a moved HOME its own
        XDG directories, and this is what says so: the variable is exported here,
        pointing at a config with a recognisable option, and it must not arrive.
        """
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as theirs:
            conf = Path(theirs) / "tmux" / "tmux.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text('set -g status-left "THEIRS"\n')
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": theirs}):
                self._init()
                r = _run(["quit"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("THEIRS",
                             _tmux("show-options", "-g", "status-left").stdout)

    def test_uninstall_does_the_tmux_half_before_it_hands_over(self):
        """`uninstall` shares `quit`'s tmux steps, and they happen first.

        Everything it can reach afterwards is a throwaway: HOME, the data
        directory (so the code is a source checkout and is left where it is),
        the state and the config. What is asserted here is the tmux half.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            self._init()
            r = _run(["uninstall"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=str(home),
                     XDG_DATA_HOME=str(Path(tmp) / "share"),
                     FLIGHTDECK_CONFIG=str(Path(tmp) / "config.json"),
                     FLIGHTDECK_STATE_DIR=str(Path(tmp) / "state"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("next-window", self._key("prefix", "n"))
            self.assertEqual(self._key("root", "F12"), "")
            self.assertNotIn("flightdeck.statusbar",
                             _tmux("show-options", "-g", "status-right").stdout)
            # the cockpit is off BEFORE the python says a word about the agents
            self.assertLess(r.stdout.index("cockpit off"),
                            r.stdout.index("uninstall"))
            # and the work session the fixture started is still there
            self.assertIn("scratch",
                          _tmux("list-sessions", "-F", "#{session_name}").stdout)

    def test_doctor_reads_our_keys_off_a_real_server(self):
        """The doctor's own fixtures are hand-written; this is tmux's real output.

        `init` binds the three keys here, and the doctor reads them back through
        `list-keys`. If tmux ever changed how it prints a binding, every
        collision test in `test_doctor.py` would go on passing and this would
        not.
        """
        self._init()
        with tempfile.TemporaryDirectory() as home:
            r = _run(["doctor"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=str(home))
        self.assertIn("✓ tmux keys:", r.stdout)

    def test_doctor_names_a_key_somebody_else_took(self):
        self._init()
        _tmux("bind-key", "-n", "F12", "display-message", "mine")
        with tempfile.TemporaryDirectory() as home:
            r = _run(["doctor"], FLIGHTDECK_TMUX_SOCKET=SOCKET, HOME=str(home))
        self.assertIn("! tmux keys:", r.stdout)
        self.assertIn("F12", r.stdout)


class TestTheCodeDirectoryWinsOverTheCwd(unittest.TestCase):
    """Run from a directory holding a `flightdeck/` package, the command still
    runs ITS code.

    `python3 -m` puts the cwd first on sys.path, ahead of PYTHONPATH, so from a
    clone of the repository the installed command used to import the clone's
    package instead of its own: `install.sh` run from a checkout reported the
    checkout as its code, and `uninstall` then refused to remove links that
    "do not point at Flightdeck's code". A decoy package whose `config` answers
    a port nothing else would is what tells the two apart.
    """

    def test_a_decoy_package_in_the_cwd_is_not_the_one_that_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoy = Path(tmp) / "flightdeck"
            decoy.mkdir()
            (decoy / "__init__.py").write_text("")
            (decoy / "config.py").write_text("print('31337')\n")
            r = _run(["_port"], cwd=tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(r.stdout.strip(), "31337", r.stdout)
        self.assertTrue(r.stdout.strip().isdigit(), r.stdout)


if __name__ == "__main__":
    unittest.main()


def _process_called(name, tmp):
    """An executable whose PROCESS NAME is `name`, or None when this machine
    cannot make one. It copies its stdin to its stdout, like `cat`.

    tmux's `#{pane_current_command}` is the process name the kernel keeps: on
    Linux the last component of the path given to exec, so a symlink to `cat`
    is enough; on macOS the executable's own name, so it has to be a real file,
    and a copy of `/bin/cat` will not run (its signature) -- a two-line C
    program compiled on the spot does, when there is a compiler.
    """
    exe = Path(tmp) / name
    cat = shutil.which("cat")
    if sys.platform != "darwin" and cat:
        os.symlink(cat, str(exe))
        return str(exe)
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        return None
    src = Path(tmp) / "catlike.c"
    src.write_text("#include <unistd.h>\nint main(void){char b[64];int n;"
                   "while((n=read(0,b,64))>0)write(1,b,n);return 0;}\n")
    done = subprocess.run([cc, "-o", str(exe), str(src)], capture_output=True)
    return str(exe) if done.returncode == 0 else None


class TestShiftEnterOnAServerOfOurOwn(unittest.TestCase):
    """Shift+Enter, from a terminal to the pane, through `-L fdtest`.

    A fake terminal on a pty attaches to the server and types Shift+Enter the
    way a terminal with extended keys does (`ESC [ 27;2;13 ~`, xterm's
    modifyOtherKeys form). In a pane whose process is called like Claude Code's
    (`2.1.278` on macOS: the binary's own name) that has to arrive as a
    backslash and a newline -- Claude Code's "new line in any terminal" -- and
    in any other pane as a plain newline, because there Shift+Enter is Enter.
    The pane runs a `cat`, in cooked mode, so what it wrote is what the tty
    line discipline delivered: `\\\n` or `\n`.
    """

    def setUp(self):
        _tmux("kill-server")
        self.tmp = tempfile.mkdtemp(prefix="flightdeck-shift-enter-")

    def tearDown(self):
        _tmux("kill-server")
        shutil.rmtree(self.tmp, True)

    def _typed_into(self, command):
        """Start the server with `command` in its pane, run `init`, attach a
        fake terminal, type Shift+Enter, and answer what the pane received."""
        out = Path(self.tmp) / "received.txt"
        r = _tmux("new-session", "-d", "-x", "80", "-y", "24",
                  "%s > '%s'" % (command, out))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _run(["init"], FLIGHTDECK_TMUX_SOCKET=SOCKET)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid, fd = pty.fork()
        if pid == 0:                                   # the fake terminal
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", ["tmux", "-L", SOCKET, "attach"])
        try:
            deadline = time.time() + 1.5               # let tmux draw first
            while time.time() < deadline:
                if select.select([fd], [], [], 0.05)[0]:
                    try:
                        os.read(fd, 4096)
                    except OSError:
                        break
            os.write(fd, b"\x1b[27;2;13~")
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if select.select([fd], [], [], 0.05)[0]:
                    try:
                        os.read(fd, 4096)
                    except OSError:
                        break
        finally:
            _tmux("kill-server")
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)
        return out.read_bytes() if out.exists() else b""

    def test_in_a_claude_code_pane_it_is_a_new_line(self):
        exe = _process_called("2.1.278", self.tmp)
        if exe is None:
            self.skipTest("no way to make a process called 2.1.278 here")
        got = self._typed_into("'%s'" % exe)
        self.assertEqual(got, b"\\\n")

    def test_anywhere_else_it_is_enter(self):
        got = self._typed_into("cat")
        self.assertEqual(got, b"\n")

