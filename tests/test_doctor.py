"""`flightdeck doctor`: the one place that decides what "healthy" means.

Nothing here reads the machine the suite runs on. Every check takes its inputs
from an `Env` of injected probes (a fake `which`, a fake `run`, a dict for the
environment, a temporary home), and the checks that read another tool's files
point that tool's module constant at a temporary file -- the same way
`test_install_claude.py` does. A doctor that read the developer's real
`~/.claude` would report on their machine instead of on the fixture, and would
say something different on a CI runner.

The doctor is read only by construction: no test here asserts that nothing was
written, because there is nothing that writes. What IS asserted is the one thing
that would betray it -- `check_state_dir` must not create the state directory it
is asked about.
"""

import contextlib
import importlib
import io
import json
import os
import stat
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from flightdeck import common, config, doctor
from flightdeck.install import agy, claude, codex, read_json_object

# What a fake `subprocess.run` gives back. The doctor only ever reads these two.
Result = namedtuple("Result", "returncode stdout")


def runner(**answers):
    """A fake `subprocess.run` keyed by a token of the argv.

    `runner(V=(0, "tmux 3.6a"))` answers any command carrying `-V`. The tokens
    are matched against the argv LIST and not against the joined line, because
    `-V` is a substring of `--version` and the two must not answer for each
    other.
    """
    table = {"V": "-V", "version": "--version", "keys": "list-keys",
             "has": "has-session"}

    def run(argv, **kwargs):
        for name, result in answers.items():
            if table[name] in list(argv):
                return Result(*result)
        return Result(1, "")
    return run


def which_finding(*commands):
    """A fake `shutil.which` that only knows `commands`."""
    known = set(commands)
    return lambda cmd: ("/usr/bin/%s" % cmd) if cmd in known else None


NOTHING = which_finding()


def env_with(**overrides):
    """A default `Env` with the probes a test wants replaced.

    Built from `default_env()` so a field added later is present here too,
    pointing at the real machine, which is what makes a test that forgot to
    inject one fail loudly rather than quietly.
    """
    return doctor.default_env()._replace(**overrides)


def named(results, name):
    """The one check called `name`, or None."""
    return next((c for c in results if c.name == name), None)


# A tmux `list-keys` listing, in the shape tmux 3.6a really prints (measured on
# an isolated server): `bind-key [-r] -T <table> <key> <command>`,
# padded with spaces, and a `-r` before the table for the repeatable ones.
STOCK_KEYS = """\
bind-key    -T prefix       c                 new-window
bind-key    -T prefix       n                 next-window
bind-key -r -T prefix       Up                select-pane -U
bind-key    -T copy-mode-vi j                 send-keys -X cursor-down
bind-key    -T root         MouseDown1Pane    select-pane -t = \\; send-keys -M
"""

# The same server after `flightdeck init`. The commands are the real ones the
# bash command installs, path and all.
OUR_KEYS = """\
bind-key    -T prefix       c                 new-window
bind-key    -T prefix       j                 run-shell -b "'/x/bin/flightdeck' goto-menu"
bind-key    -T prefix       n                 run-shell -b "PYTHONPATH='/x' python3 -m flightdeck.handover handover '#{pane_id}'"
bind-key    -T root         F12               if-shell -F '#{m/r:^flightdeck(-[0-9]+)?$,#{session_name}}' 'switch-client -l' 'run-shell -b "\\"/x/bin/flightdeck\\" goto-menu"'
"""

# Somebody else got there first: F12 opens their notes, prefix j is their pane
# jump, prefix n is a window they renamed.
TAKEN_KEYS = """\
bind-key    -T prefix       j                 select-pane -D
bind-key    -T prefix       n                 send-keys C-n
bind-key    -T root         F12               display-popup -E "vim ~/notes.md"
"""


@contextlib.contextmanager
def temp_config(**values):
    """A throwaway `config.json` holding `values`, and the environment on it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
            yield path


@contextlib.contextmanager
def raw_config(text):
    """The same, for a config file that is not valid JSON at all."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
            yield path


_SANDBOX = []


def setUpModule():
    """Point every agent's configuration at an empty sandbox, for the whole file.

    The same net `tests/__init__.py` casts over the state directory and the tmux
    socket, and for the same reason: the three installer modules resolve their
    paths from `Path.home()` when they are imported, so a check run without this
    would read the developer's real `~/.claude`, `~/.codex` and `~/.gemini` --
    and answer differently on their machine than on anybody else's. A test that
    wants a file with something in it patches the path again, on top of this.
    """
    empty = Path(tempfile.mkdtemp(prefix="flightdeck-doctor-tests-")) / "nothing"
    for module, attribute in ((claude, "SETTINGS_PATH"), (codex, "HOOKS_JSON"),
                              (codex, "CONFIG_TOML"), (agy, "HOOKS_JSON"),
                              (agy, "SETTINGS_PATH")):
        patch = mock.patch.object(module, attribute, empty / attribute.lower())
        patch.start()
        _SANDBOX.append(patch)


def tearDownModule():
    while _SANDBOX:
        _SANDBOX.pop().stop()


def claude_settings(path=None, stale=False, status_line="ours"):
    """A `settings.json` with Flightdeck registered, built by the installer itself.

    Using `register_hooks` rather than a hand-written literal is deliberate: the
    fixture then carries whatever shape the installer really writes, so a change
    in one cannot leave the other testing a file nobody produces.

    `stale` rewrites every path of ours to another directory, which is what a
    moved code directory looks like from here.
    """
    settings = claude.register_hooks({})
    if status_line == "ours":
        settings["statusLine"] = {"type": "command",
                                  "command": "python3 '%s'" % claude.TEE}
    elif status_line == "other":
        settings["statusLine"] = {"type": "command", "command": "sh ~/mine.sh"}
    if stale:
        settings = json.loads(json.dumps(settings)
                              .replace(str(config.code_dir()), "/gone/flightdeck"))
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings


class TestTheSystem(unittest.TestCase):
    """macOS, Linux, WSL2 or Windows -- and Windows is the one that cannot work."""

    def test_macos(self):
        env = env_with(platform="darwin", uname="Darwin host 25.2.0", environ={})
        self.assertEqual(doctor.system(env), "darwin")
        self.assertIs(doctor.check_system(env).ok, True)

    def test_plain_linux(self):
        env = env_with(platform="linux", uname="Linux host 6.1.0", environ={},
                       proc_version=lambda: "Linux version 6.1.0 (gcc 12)")
        self.assertEqual(doctor.system(env), "linux")
        self.assertIs(doctor.check_system(env).ok, True)

    def test_wsl2_is_linux_whose_proc_version_says_microsoft(self):
        # The only way to tell WSL2 from a plain Linux, and the spec's own
        # (§4.1). Case-insensitive: the string has been spelled both ways.
        env = env_with(platform="linux", uname="Linux host 5.15.0", environ={},
                       proc_version=lambda: "Linux version 5.15.0-microsoft-standard-WSL2")
        self.assertEqual(doctor.system(env), "wsl2")
        self.assertIs(doctor.check_system(env).ok, True)

    def test_windows_native_by_uname(self):
        env = env_with(platform="msys", uname="MINGW64_NT-10.0 host", environ={})
        self.assertEqual(doctor.system(env), "windows")

    def test_windows_native_by_the_os_variable(self):
        env = env_with(platform="linux", uname="whatever",
                       environ={"OS": "Windows_NT"})
        self.assertEqual(doctor.system(env), "windows")

    def test_windows_native_by_sys_platform(self):
        env = env_with(platform="win32", uname="whatever", environ={})
        self.assertEqual(doctor.system(env), "windows")

    def test_windows_is_a_failure_that_points_at_wsl2(self):
        # Flightdeck is tmux and POSIX shells: native Windows is not a degraded
        # mode, it is a different machine. The line has to say where to go.
        check = doctor.check_system(env_with(platform="win32", uname="", environ={}))
        self.assertIs(check.ok, False)
        self.assertIn("WSL2", check.detail)
        self.assertIn("wsl --install", check.fix)


class TestTmux(unittest.TestCase):

    def setUp(self):
        # `tmux_version` is cached for the life of the process and the cache is
        # keyed by the `run` it was given: without clearing it, one test's fake
        # answer can be handed to the next.
        common.tmux_version.cache_clear()

    def tearDown(self):
        common.tmux_version.cache_clear()

    def test_missing_tmux_is_a_failure(self):
        check = doctor.check_tmux(env_with(which=NOTHING))
        self.assertIs(check.ok, False)
        self.assertIn("tmux", check.fix)

    def test_below_the_minimum_is_a_failure(self):
        # 3.2 is the floor: `new-session -e` and `display-message -d` are 3.2.
        env = env_with(which=which_finding("tmux"), run=runner(V=(0, "tmux 3.1c")))
        check = doctor.check_tmux(env)
        self.assertIs(check.ok, False)
        self.assertIn("3.1", check.detail)
        self.assertIn("3.2", check.detail)

    def test_3_2_works_with_the_notice_degraded(self):
        # It works, and the floating notice is escaped instead of literal
        # (`display-message -l` arrived in 3.4). A warning, not a failure.
        env = env_with(which=which_finding("tmux"), run=runner(V=(0, "tmux 3.2a")))
        check = doctor.check_tmux(env)
        self.assertIsNone(check.ok)
        self.assertIn("3.2", check.detail)

    def test_3_4_and_up_is_clean(self):
        env = env_with(which=which_finding("tmux"), run=runner(V=(0, "tmux 3.6a")))
        check = doctor.check_tmux(env)
        self.assertIs(check.ok, True)
        self.assertIn("3.6", check.detail)

    def test_a_version_it_cannot_read_is_a_warning(self):
        env = env_with(which=which_finding("tmux"), run=runner(V=(0, "who knows")))
        self.assertIsNone(doctor.check_tmux(env).ok)


class TestFzf(unittest.TestCase):

    def test_missing_fzf_is_a_failure(self):
        check = doctor.check_fzf(env_with(which=NOTHING))
        self.assertIs(check.ok, False)
        self.assertIn("fzf", check.fix)

    def test_below_0_36_is_a_failure(self):
        env = env_with(which=which_finding("fzf"), run=runner(version=(0, "0.35.1\n")))
        check = doctor.check_fzf(env)
        self.assertIs(check.ok, False)
        self.assertIn("0.36", check.detail)

    def test_the_middle_layer_works_without_a_stable_cursor(self):
        env = env_with(which=which_finding("fzf"), run=runner(version=(0, "0.42.0\n")))
        check = doctor.check_fzf(env)
        self.assertIsNone(check.ok)
        self.assertIn("reload", check.detail)
        self.assertIn("--id-nth", check.detail)

    def test_0_71_and_up_has_everything(self):
        # The Homebrew suffix is part of the real output and the parser has to
        # step over it -- it is the same rule the picker uses.
        env = env_with(which=which_finding("fzf"),
                       run=runner(version=(0, "0.72.0 (Homebrew)\n")))
        check = doctor.check_fzf(env)
        self.assertIs(check.ok, True)
        self.assertIn("--id-nth", check.detail)

    def test_an_fzf_that_does_not_answer_is_a_warning(self):
        env = env_with(which=which_finding("fzf"), run=runner(version=(1, "")))
        self.assertIsNone(doctor.check_fzf(env).ok)


class TestPython(unittest.TestCase):

    def test_3_9_is_the_floor(self):
        self.assertIs(doctor.check_python(env_with(python_version=(3, 9))).ok, True)
        self.assertIs(doctor.check_python(env_with(python_version=(3, 13))).ok, True)

    def test_older_is_a_failure(self):
        check = doctor.check_python(env_with(python_version=(3, 8)))
        self.assertIs(check.ok, False)
        self.assertIn("3.9", check.detail)


class TestTheAgents(unittest.TestCase):

    def test_claude_is_what_the_cockpit_is_for(self):
        check = doctor.check_agents(env_with(which=which_finding("claude")))
        self.assertIs(check.ok, True)

    def test_codex_and_agy_are_optional_and_named_when_they_are_there(self):
        check = doctor.check_agents(
            env_with(which=which_finding("claude", "codex", "agy")))
        self.assertIs(check.ok, True)
        for tool in ("claude", "codex", "agy"):
            self.assertIn(tool, check.detail)

    def test_no_claude_is_a_warning_not_a_failure(self):
        # Flightdeck still runs: the menu, the pins and the tmux keys work with
        # no agent installed at all. So it is a warning.
        check = doctor.check_agents(env_with(which=NOTHING))
        self.assertIsNone(check.ok)
        self.assertIn("claude", check.detail)


class TestTheCommandOnPath(unittest.TestCase):

    def _home(self, tmp, linked):
        home = Path(tmp)
        if linked:
            local = home / ".local" / "bin"
            local.mkdir(parents=True)
            (local / "flightdeck").write_text("#!/bin/sh\n")
        return home

    def test_linked_and_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, linked=True)
            env = env_with(home=home, which=which_finding("flightdeck"),
                           environ={"PATH": str(home / ".local" / "bin")})
            self.assertIs(doctor.check_command_on_path(env).ok, True)

    def test_linked_but_not_resolvable_is_a_failure(self):
        # The installer linked the command and the line it added to the rc file
        # never took: every binding, hook and status line points at a command
        # the shell cannot find.
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp, linked=True)
            env = env_with(home=home, which=NOTHING,
                           environ={"PATH": "/usr/bin:/bin"})
            check = doctor.check_command_on_path(env)
            self.assertIs(check.ok, False)
            self.assertIn(".local/bin", check.detail)
            self.assertIn("PATH", check.fix)

    def test_a_source_checkout_on_path_is_fine(self):
        # A developer's machine: no link in `~/.local/bin`, the command
        # reached through a checkout. Nothing is wrong with that.
        with tempfile.TemporaryDirectory() as tmp:
            env = env_with(home=self._home(tmp, linked=False),
                           which=which_finding("flightdeck"),
                           environ={"PATH": "/usr/bin"})
            self.assertIs(doctor.check_command_on_path(env).ok, True)

    def test_not_installed_at_all_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = env_with(home=self._home(tmp, linked=False), which=NOTHING,
                           environ={"PATH": "/usr/bin"})
            self.assertIsNone(doctor.check_command_on_path(env).ok)


class TestTheConfigFile(unittest.TestCase):

    def test_no_config_at_all_is_the_normal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nothing" / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
                check = doctor.check_config(env_with(config_path=path))
        self.assertIs(check.ok, True)

    def test_a_good_config_passes(self):
        with temp_config(menu_port=43000) as path:
            check = doctor.check_config(env_with(config_path=path))
        self.assertIs(check.ok, True)

    def test_broken_json_is_a_warning_and_says_why(self):
        # An invalid config takes nothing down -- it degrades to the
        # defaults everywhere and the doctor is where it gets said out loud.
        with raw_config("this is not json") as path:
            check = doctor.check_config(env_with(config_path=path))
        self.assertIsNone(check.ok)
        self.assertIn("invalid JSON", check.detail)

    def test_a_number_written_as_a_word_is_a_warning(self):
        # Every reader falls back silently on these, which is why their
        # docstrings say the doctor is the place that complains.
        with temp_config(menu_port="forty-two thousand") as path:
            check = doctor.check_config(env_with(config_path=path))
        self.assertIsNone(check.ok)
        self.assertIn("menu_port", check.detail)

    def test_a_boolean_is_not_a_number(self):
        # `True` is an int in Python and would sail through an isinstance check
        # -- and `menu_port: true` is a port nobody can listen on.
        with temp_config(notice_ms=True) as path:
            check = doctor.check_config(env_with(config_path=path))
        self.assertIsNone(check.ok)
        self.assertIn("notice_ms", check.detail)

    def test_every_numeric_key_is_watched(self):
        for key in doctor.INT_KEYS:
            with self.subTest(key=key):
                with temp_config(**{key: "nope"}) as path:
                    check = doctor.check_config(env_with(config_path=path))
                self.assertIsNone(check.ok)
                self.assertIn(key, check.detail)


class TestTheStateDirectory(unittest.TestCase):

    def test_an_existing_writable_directory_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = doctor.check_state_dir(env_with(state_dir=Path(tmp)))
        self.assertIs(check.ok, True)

    def test_a_directory_that_is_not_there_yet_is_judged_by_its_parent(self):
        # Asking where the state lives must not create it: a machine where
        # Flightdeck was never installed cannot grow a state directory because
        # somebody ran the doctor.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / "flightdeck"
            check = doctor.check_state_dir(env_with(state_dir=target))
            self.assertIs(check.ok, True)
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root can write into a directory with no write bit")
    def test_a_directory_it_cannot_write_into_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp) / "locked"
            locked.mkdir()
            os.chmod(str(locked), stat.S_IRUSR | stat.S_IXUSR)
            try:
                check = doctor.check_state_dir(env_with(state_dir=locked))
            finally:
                os.chmod(str(locked), stat.S_IRWXU)
        self.assertIs(check.ok, False)
        self.assertIn("writable", check.detail)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root can write into a directory with no write bit")
    def test_a_missing_directory_under_a_locked_parent_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp) / "locked"
            locked.mkdir()
            os.chmod(str(locked), stat.S_IRUSR | stat.S_IXUSR)
            try:
                check = doctor.check_state_dir(env_with(state_dir=locked / "state"))
            finally:
                os.chmod(str(locked), stat.S_IRWXU)
        self.assertIs(check.ok, False)


class TestTheClaudeProbe(unittest.TestCase):
    """`claude.installed_state`: what `status()` prints, as data."""

    def test_a_full_installation_reads_as_live(self):
        state = claude.installed_state(claude_settings())
        self.assertEqual(sorted(state["live"]),
                         sorted(claude.EVENTS + ["PreToolUse"]))
        self.assertEqual(state["stale"], [])
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["matcher"], claude.PRETOOL_MATCHER)
        self.assertEqual(state["status_line"], "ours")

    def test_an_empty_settings_file_reads_as_missing(self):
        state = claude.installed_state({})
        self.assertEqual(state["live"], [])
        self.assertEqual(state["status_line"], "none")

    def test_a_moved_code_directory_reads_as_stale_not_as_somebody_elses(self):
        # The entry still names `session_hook.py`, so it is ours; it points
        # somewhere else, so every hook fails in silence.
        state = claude.installed_state(claude_settings(stale=True))
        self.assertEqual(state["live"], [])
        self.assertEqual(len(state["stale"]), len(claude.EVENTS) + 1)
        self.assertIn("/gone/flightdeck", state["stale"][0][1])
        self.assertEqual(state["status_line"], "stale")

    def test_somebody_elses_status_line_is_not_ours(self):
        state = claude.installed_state(claude_settings(status_line="other"))
        self.assertEqual(state["status_line"], "other")
        self.assertIn("mine.sh", state["status_line_command"])

    def test_a_stale_matcher_comes_back_as_it_is(self):
        settings = claude_settings()
        claude.our_entry(settings["hooks"]["PreToolUse"])["matcher"] = "Bash"
        self.assertEqual(claude.installed_state(settings)["matcher"], "Bash")

    def test_a_hand_edited_file_of_any_shape_is_survived(self):
        # settings.json is edited by hand, and the doctor is precisely what
        # somebody runs when theirs is a mess. None of these may raise.
        for hooks in ({"Stop": ["nonsense"]}, {"Stop": [{"hooks": ["x"]}]},
                      {"Stop": 7}, [1, 2, 3], "hooks", None):
            with self.subTest(hooks=hooks):
                state = claude.installed_state({"hooks": hooks})
                self.assertEqual(state["live"], [])
                self.assertIsNone(state["matcher"])

    def test_an_unreadable_settings_file_is_an_error_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{ not json")
            with mock.patch.object(claude, "SETTINGS_PATH", path):
                state = claude.installed_state()
        self.assertTrue(state["error"])
        self.assertEqual(state["live"], [])


class TestTheClaudeHooksCheck(unittest.TestCase):

    @contextlib.contextmanager
    def _settings(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            claude_settings(path, **kwargs)
            with mock.patch.object(claude, "SETTINGS_PATH", path):
                yield path

    def test_a_full_installation_passes(self):
        with self._settings():
            self.assertIs(doctor.check_claude_hooks(env_with()).ok, True)

    def test_stale_hooks_are_a_failure(self):
        # The registered command names our script and points at a directory
        # that is not here: every event fails in silence, so the menu and the
        # bar go on showing a session that stopped moving hours ago.
        with self._settings(stale=True):
            check = doctor.check_claude_hooks(env_with())
        self.assertIs(check.ok, False)
        self.assertIn("stale", check.detail.lower())
        self.assertIn("flightdeck install", check.fix)

    def test_not_installed_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(claude, "SETTINGS_PATH",
                                   Path(tmp) / "settings.json"):
                check = doctor.check_claude_hooks(env_with())
        self.assertIsNone(check.ok)
        self.assertIn("flightdeck install", check.fix)

    def test_a_stale_matcher_is_a_warning(self):
        with self._settings() as path:
            settings = json.loads(path.read_text())
            claude.our_entry(settings["hooks"]["PreToolUse"])["matcher"] = "Bash"
            path.write_text(json.dumps(settings))
            check = doctor.check_claude_hooks(env_with())
        self.assertIsNone(check.ok)
        self.assertIn("matcher", check.detail)


class TestTheClaudeStatusLineCheck(unittest.TestCase):

    @contextlib.contextmanager
    def _installed(self, status_line="ours", delegate="sh ~/mine.sh", stale=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            claude_settings(path, status_line=status_line, stale=stale)
            env = {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                   "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(claude, "SETTINGS_PATH", path):
                if delegate is not None:
                    file = claude.delegate_file()
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(delegate)
                yield path

    def test_registered_with_its_delegate_passes(self):
        with self._installed():
            check = doctor.check_claude_status_line(env_with())
        self.assertIs(check.ok, True)
        self.assertIn("own", check.detail)   # the mode in force

    def test_a_missing_delegate_file_is_a_warning(self):
        # The delegate is the only copy of the status line the user had: the
        # state directory is disposable, that file is not.
        with self._installed(delegate=None):
            check = doctor.check_claude_status_line(env_with())
        self.assertIsNone(check.ok)
        self.assertIn("delegate", check.detail)

    def test_an_empty_delegate_is_normal(self):
        # An empty file is what the installer writes for someone who had no
        # status line at all. Nothing was lost, so nothing is said.
        with self._installed(delegate=""):
            self.assertIs(doctor.check_claude_status_line(env_with()).ok, True)

    def test_a_tee_pointing_elsewhere_is_a_failure(self):
        with self._installed(stale=True):
            check = doctor.check_claude_status_line(env_with())
        self.assertIs(check.ok, False)
        self.assertIn("/gone/flightdeck", check.detail)

    def test_not_registered_is_a_warning(self):
        with self._installed(status_line="other"):
            check = doctor.check_claude_status_line(env_with())
        self.assertIsNone(check.ok)
        self.assertIn("mine.sh", check.detail)


class TestTheCodexProbeAndItsStaleEntries(unittest.TestCase):
    """codex's `is_ours` by FILE NAME."""

    def _hooks(self, path):
        """A hooks.json with our entries, written by the installer's own builder."""
        return codex.hooks_with_flightdeck({})

    def test_an_entry_of_ours_from_another_directory_is_still_ours(self):
        # Recognised by the whole path, a moved code directory read as somebody
        # else's entry: the installer appended a second one and codex ran two
        # hooks per event, one of them failing.
        entry = {"hooks": [{"type": "command",
                            "command": "python3 '/gone/flightdeck/flightdeck/"
                                       "hooks/session_hook.py' Stop --tool codex"}]}
        self.assertTrue(codex.is_ours(entry))
        self.assertFalse(codex.points_here(entry))

    def test_somebody_elses_hook_is_not_ours(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /opt/theirs.py"}]}
        self.assertFalse(codex.is_ours(entry))

    def test_a_stale_entry_is_replaced_on_install_and_never_duplicated(self):
        stale = {"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "python3 '/gone/flightdeck/flightdeck/hooks/"
                                   "session_hook.py' Stop --tool codex"}]}]}}
        combined = codex.hooks_with_flightdeck(stale)
        self.assertEqual(len(combined["hooks"]["Stop"]), 1)
        self.assertIn(str(codex.HOOK), combined["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_the_probe_separates_live_from_stale(self):
        state = codex.installed_state(hooks=self._hooks(None), toml_text="")
        self.assertEqual(sorted(state["live"]), sorted(codex.EVENTS))
        self.assertEqual(state["stale"], [])
        self.assertEqual(state["notify"], "none")

        moved = json.loads(json.dumps(self._hooks(None))
                           .replace(str(config.code_dir()), "/gone/flightdeck"))
        state = codex.installed_state(hooks=moved, toml_text="")
        self.assertEqual(state["live"], [])
        self.assertEqual(len(state["stale"]), len(codex.EVENTS))

    def test_the_probe_reads_the_notify_and_the_items(self):
        text = 'notify = ["python3", "%s"]\n[tui]\nstatus_line = %s\n' % (
            codex.TEE, json.dumps(codex.STATUS_LINE_ITEMS))
        state = codex.installed_state(hooks={}, toml_text=text)
        self.assertEqual(state["notify"], "ours")
        self.assertEqual(state["status_line_items"], "ours")

    def test_a_config_toml_that_cannot_be_decoded_is_an_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(b"notify = [\"\xff\xfe\"]\n")
            with mock.patch.object(codex, "CONFIG_TOML", path):
                state = codex.installed_state(hooks={})
        self.assertTrue(state["error"])

    def test_a_hooks_json_that_cannot_be_parsed_is_an_error_not_an_empty_file(self):
        """"There is nothing here" and "I cannot read this" are opposite findings.

        Read as an empty file, a corrupt hooks.json comes out as "Flightdeck is
        not registered" — and the action that follows from that, a reinstall,
        rebuilds the file and drops the entries of the user's it could not read.
        """
        for text in ("{ not json", "[]", '"a string"'):
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "hooks.json"
                    path.write_text(text)
                    with mock.patch.object(codex, "HOOKS_JSON", path):
                        state = codex.installed_state(toml_text="")
                self.assertTrue(state["error"], state)
                self.assertIn(str(path), state["error"])
                self.assertEqual(state["live"], [])

    def test_a_missing_hooks_json_is_not_an_error(self):
        # Nothing installed yet is the normal state of a fresh machine, and the
        # answer to it IS `flightdeck install`.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(codex, "HOOKS_JSON", Path(tmp) / "hooks.json"):
                state = codex.installed_state(toml_text="")
        self.assertIsNone(state["error"])

    def test_both_unreadable_files_are_reported_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            toml = Path(tmp) / "config.toml"
            hooks.write_text("{ not json")
            toml.write_bytes(b"notify = [\"\xff\xfe\"]\n")
            with mock.patch.object(codex, "HOOKS_JSON", hooks), \
                    mock.patch.object(codex, "CONFIG_TOML", toml):
                state = codex.installed_state()
        self.assertIn(str(hooks), state["error"])
        self.assertIn(str(toml), state["error"])


class TestTheCodexCheck(unittest.TestCase):

    @contextlib.contextmanager
    def _files(self, hooks=None, toml=""):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            toml_path = Path(tmp) / "config.toml"
            hooks_path.write_text(json.dumps(hooks if hooks is not None else {}))
            toml_path.write_text(toml)
            with mock.patch.object(codex, "HOOKS_JSON", hooks_path), \
                    mock.patch.object(codex, "CONFIG_TOML", toml_path), \
                    mock.patch.dict(os.environ,
                                    {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}):
                yield

    def test_a_full_installation_passes(self):
        toml = 'notify = ["python3", "%s"]\n' % codex.TEE
        with self._files(hooks=codex.hooks_with_flightdeck({}), toml=toml):
            env = env_with(which=which_finding("claude", "codex"))
            self.assertIs(doctor.check_codex(env).ok, True)

    def test_stale_entries_are_a_failure(self):
        moved = json.loads(json.dumps(codex.hooks_with_flightdeck({}))
                           .replace(str(config.code_dir()), "/gone/flightdeck"))
        with self._files(hooks=moved):
            check = doctor.check_codex(env_with(which=which_finding("codex")))
        self.assertIs(check.ok, False)
        self.assertIn("stale", check.detail.lower())

    def test_not_installed_is_a_warning(self):
        with self._files():
            check = doctor.check_codex(env_with(which=which_finding("codex")))
        self.assertIsNone(check.ok)

    def test_a_file_it_cannot_read_is_never_answered_with_reinstall(self):
        # The claude check has said this from the start ("fix it by hand;
        # Flightdeck never overwrites one it cannot read") and codex has to say
        # the same: `install` reads an unparseable hooks.json as an EMPTY one
        # and rebuilds it, so "not registered" is both the wrong diagnosis and
        # the dangerous advice.
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text("{ not json")
            with mock.patch.object(codex, "HOOKS_JSON", hooks), \
                    mock.patch.object(codex, "CONFIG_TOML", Path(tmp) / "config.toml"):
                check = doctor.check_codex(env_with(which=which_finding("codex")))
        self.assertIsNone(check.ok)
        self.assertIn("invalid JSON", check.detail)
        self.assertNotIn("not registered", check.detail)
        self.assertIn("by hand", check.fix)

    def test_a_stale_hook_still_wins_over_an_unreadable_config_toml(self):
        # The error branch comes before "not registered" and AFTER the stale
        # one: a stale hook is proven and is a ✗, and an unreadable config.toml
        # says nothing about the hooks, which were read perfectly well.
        moved = json.loads(json.dumps(codex.hooks_with_flightdeck({}))
                           .replace(str(config.code_dir()), "/gone/flightdeck"))
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            toml = Path(tmp) / "config.toml"
            hooks.write_text(json.dumps(moved))
            toml.write_bytes(b"notify = [\"\xff\xfe\"]\n")
            with mock.patch.object(codex, "HOOKS_JSON", hooks), \
                    mock.patch.object(codex, "CONFIG_TOML", toml):
                check = doctor.check_codex(env_with(which=which_finding("codex")))
        self.assertIs(check.ok, False)
        self.assertIn("stale", check.detail.lower())

    def test_codex_is_skipped_altogether_when_it_is_not_on_path(self):
        # An optional tool nobody has is not a finding: the line would be noise
        # on every machine that only runs Claude Code.
        results = doctor.checks(env_with(which=which_finding("claude", "tmux", "fzf")))
        self.assertIsNone(named(results, "codex"))
        self.assertIsNone(named(results, "agy"))


class TestTheAgyProbeAndCheck(unittest.TestCase):

    @contextlib.contextmanager
    def _files(self, hooks=None, settings=None, delegate=None):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            settings_path = Path(tmp) / "settings.json"
            hooks_path.write_text(json.dumps(hooks if hooks is not None else {}))
            settings_path.write_text(json.dumps(settings if settings is not None else {}))
            env = {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                   "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json")}
            with mock.patch.object(agy, "HOOKS_JSON", hooks_path), \
                    mock.patch.object(agy, "SETTINGS_PATH", settings_path), \
                    mock.patch.dict(os.environ, env):
                if delegate is not None:
                    file = agy.delegate_file()
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(delegate)
                yield

    def _installed_settings(self):
        return {"statusLine": {"type": "command",
                               "command": agy.status_line_command()}}

    def test_the_probe_sees_our_named_key(self):
        state = agy.installed_state(hooks=agy.hooks_with_flightdeck({}), settings={})
        self.assertTrue(state["present"])
        self.assertTrue(state["enabled"])
        self.assertEqual(sorted(state["live"]), sorted(agy.EVENTS))

    def test_the_probe_sees_a_disabled_key(self):
        hooks = agy.hooks_with_flightdeck({})
        hooks[agy.NAME]["enabled"] = False
        state = agy.installed_state(hooks=hooks, settings={})
        self.assertFalse(state["enabled"])

    def test_the_probe_separates_stale_handlers(self):
        moved = json.loads(json.dumps(agy.hooks_with_flightdeck({}))
                           .replace(str(config.code_dir()), "/gone/flightdeck"))
        state = agy.installed_state(hooks=moved, settings={})
        self.assertEqual(state["live"], [])
        self.assertEqual(len(state["stale"]), len(agy.EVENTS))

    def test_the_probe_reads_the_status_line(self):
        state = agy.installed_state(hooks={}, settings=self._installed_settings())
        self.assertEqual(state["status_line"], "ours")

    def test_a_full_installation_passes(self):
        with self._files(hooks=agy.hooks_with_flightdeck({}),
                         settings=self._installed_settings()):
            env = env_with(which=which_finding("agy"))
            self.assertIs(doctor.check_agy(env).ok, True)

    def test_a_disabled_key_is_a_warning(self):
        hooks = agy.hooks_with_flightdeck({})
        hooks[agy.NAME]["enabled"] = False
        with self._files(hooks=hooks, settings=self._installed_settings()):
            check = doctor.check_agy(env_with(which=which_finding("agy")))
        self.assertIsNone(check.ok)
        self.assertIn("disabled", check.detail.lower())

    def test_stale_handlers_are_a_failure(self):
        moved = json.loads(json.dumps(agy.hooks_with_flightdeck({}))
                           .replace(str(config.code_dir()), "/gone/flightdeck"))
        with self._files(hooks=moved, settings={}):
            check = doctor.check_agy(env_with(which=which_finding("agy")))
        self.assertIs(check.ok, False)

    def test_a_hooks_json_that_cannot_be_parsed_is_an_error(self):
        # agy's file holds the user's OTHER named hooks beside ours, so reading
        # a corrupt one as empty is the worst of the three: a reinstall writes
        # a file with nothing but our key in it.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            path.write_text("{ not json")
            with mock.patch.object(agy, "HOOKS_JSON", path):
                state = agy.installed_state(settings={})
        self.assertTrue(state["error"])
        self.assertIn(str(path), state["error"])
        self.assertFalse(state["present"])

    def test_a_missing_hooks_json_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(agy, "HOOKS_JSON", Path(tmp) / "hooks.json"):
                state = agy.installed_state(settings={})
        self.assertIsNone(state["error"])

    def test_a_file_it_cannot_read_is_never_answered_with_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text("{ not json")
            with mock.patch.object(agy, "HOOKS_JSON", hooks), \
                    mock.patch.object(agy, "SETTINGS_PATH", Path(tmp) / "settings.json"):
                check = doctor.check_agy(env_with(which=which_finding("agy")))
        self.assertIsNone(check.ok)
        self.assertIn("invalid JSON", check.detail)
        self.assertNotIn("not registered", check.detail)
        self.assertIn("by hand", check.fix)

    def test_an_unreadable_hooks_json_is_not_the_status_lines_problem(self):
        # agy is the one tool whose hooks and whose status line live in two
        # files. A hooks.json nobody can parse says nothing about the line, and
        # a status line check that reported it would send the user to the wrong
        # file.
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            settings = Path(tmp) / "settings.json"
            hooks.write_text("{ not json")
            settings.write_text(json.dumps(self._installed_settings()))
            env = {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                   "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json")}
            with mock.patch.object(agy, "HOOKS_JSON", hooks), \
                    mock.patch.object(agy, "SETTINGS_PATH", settings), \
                    mock.patch.dict(os.environ, env):
                file = agy.delegate_file()
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("")
                check = doctor.check_agy_status_line(env_with())
        self.assertIs(check.ok, True)
        self.assertNotIn("hooks.json", check.detail)

    def test_the_agy_status_line_is_its_own_line(self):
        with self._files(hooks=agy.hooks_with_flightdeck({}),
                         settings=self._installed_settings(), delegate=""):
            check = doctor.check_agy_status_line(env_with(which=which_finding("agy")))
        self.assertIs(check.ok, True)
        self.assertIn("agy", check.name)


class TestKeyCollisions(unittest.TestCase):
    """The pure half, shared with `flightdeck install`."""

    def test_a_stock_server_has_no_collisions(self):
        # Stock tmux: `n` is next-window, `j` and F12 are bound to nothing. None
        # of that is somebody's choice to warn about.
        self.assertEqual(doctor.key_collisions(STOCK_KEYS), [])

    def test_our_own_bindings_are_not_collisions(self):
        self.assertEqual(doctor.key_collisions(OUR_KEYS), [])

    def test_somebody_elses_bindings_are_reported_with_what_they_are(self):
        found = doctor.key_collisions(TAKEN_KEYS)
        self.assertEqual([label for label, _ in found],
                         ["F12", "prefix j", "prefix n"])
        self.assertIn("notes.md", dict(found)["F12"])
        self.assertIn("select-pane", dict(found)["prefix j"])

    def test_a_repeatable_binding_is_parsed_too(self):
        # `bind-key -r -T prefix ...`: the flag sits before the table.
        listing = "bind-key -r -T prefix n resize-pane -D\n"
        self.assertEqual(doctor.key_collisions(listing), [("prefix n", "resize-pane -D")])

    def test_the_same_key_in_another_table_is_a_different_key(self):
        # `j` in copy-mode-vi is tmux's own cursor movement and has nothing to
        # do with the prefix table.
        self.assertEqual(doctor.key_collisions(
            "bind-key -T copy-mode-vi j send-keys -X cursor-down\n"), [])

    def test_empty_output_is_no_collisions(self):
        self.assertEqual(doctor.key_collisions(""), [])
        self.assertEqual(doctor.key_collisions(None), [])

    def test_bindings_reports_what_is_there(self):
        self.assertEqual(doctor.bindings(STOCK_KEYS),
                         {"F12": None, "prefix j": None, "prefix n": "next-window"})


class TestTheBindingsCheck(unittest.TestCase):

    def test_no_live_server_is_a_warning_that_says_how_to_start_one(self):
        # MEASURED (tmux 3.6a): with no server, `list-keys` still
        # exits 0 and prints tmux's STOCK table -- and creates a socket file
        # doing it, a transient server for a doctor that must start nothing. So
        # the question is asked with `has-session`, which answers rc 1 and
        # leaves no socket behind. This fake answers both, the way tmux does.
        check = doctor.check_bindings(env_with(run=runner(has=(1, ""),
                                                          keys=(0, STOCK_KEYS))))
        self.assertIsNone(check.ok)
        self.assertIn("no live tmux server", check.detail)
        self.assertIn("flightdeck", check.fix)

    def test_our_three_keys_pass(self):
        check = doctor.check_bindings(
            env_with(run=runner(has=(0, ""), keys=(0, OUR_KEYS))))
        self.assertIs(check.ok, True)

    def test_missing_bindings_are_a_warning(self):
        # They are reapplied on every entry to the menu, so this is a warning
        # and its fix is simply to enter it.
        check = doctor.check_bindings(
            env_with(run=runner(has=(0, ""), keys=(0, STOCK_KEYS))))
        self.assertIsNone(check.ok)
        self.assertIn("F12", check.detail)

    def test_collisions_are_a_warning_naming_what_was_there(self):
        check = doctor.check_bindings(
            env_with(run=runner(has=(0, ""), keys=(0, TAKEN_KEYS))))
        self.assertIsNone(check.ok)
        self.assertIn("notes.md", check.detail)

    def test_it_asks_has_session_before_it_asks_for_the_keys(self):
        """The socket file is the proof: `list-keys` makes one, `has-session` does not.

        Pinned as the ORDER of the calls, because that is what the doctor's
        promise rests on -- it may not create a tmux server, not even the
        momentary one tmux spins up to answer a question about key tables.
        """
        asked = []

        def run(argv, **kwargs):
            asked.append("list-keys" if "list-keys" in argv else " ".join(argv[-1:]))
            return Result(1, "")

        doctor.check_bindings(env_with(run=run))
        self.assertEqual(asked, ["has-session"])


class TestPins(unittest.TestCase):

    def test_no_pins_is_fine(self):
        with temp_config():
            self.assertIs(doctor.check_pins(env_with(which=NOTHING)).ok, True)

    def test_a_pin_whose_command_is_gone_is_a_warning_with_the_install_hint(self):
        with temp_config(pins=[{"name": "htop", "label": "📊 htop",
                                "command": "htop", "session": "htop"}]):
            check = doctor.check_pins(env_with(which=NOTHING, platform="darwin"))
        self.assertIsNone(check.ok)
        self.assertIn("htop", check.detail)
        self.assertIn("brew install htop", check.fix)

    def test_a_pin_whose_command_is_there_passes(self):
        with temp_config(pins=[{"name": "htop", "command": "htop",
                                "session": "htop"}]):
            check = doctor.check_pins(env_with(which=which_finding("htop")))
        self.assertIs(check.ok, True)

    def test_a_pin_of_their_own_gets_no_invented_hint(self):
        with temp_config(pins=[{"name": "notes", "label": "📝 notes",
                                "command": "nvim ~/notes.md", "session": "notes"}]):
            check = doctor.check_pins(env_with(which=NOTHING))
        self.assertIsNone(check.ok)
        self.assertIn("notes", check.detail)


class TestLocale(unittest.TestCase):

    def test_a_utf8_locale_passes(self):
        for value in ("en_US.UTF-8", "C.utf8", "es_ES.UTF8"):
            with self.subTest(value=value):
                self.assertIs(
                    doctor.check_locale(env_with(environ={"LANG": value})).ok, True)

    def test_lc_all_wins_over_lang(self):
        env = env_with(environ={"LC_ALL": "C", "LANG": "en_US.UTF-8"})
        self.assertIsNone(doctor.check_locale(env).ok)

    def test_no_locale_at_all_is_a_warning(self):
        check = doctor.check_locale(env_with(environ={}))
        self.assertIsNone(check.ok)
        self.assertIn("UTF-8", check.detail)


@contextlib.contextmanager
def ascii_default_encoding():
    """Make a plain `open()` decode as ASCII, whatever this Python's plumbing is.

    Which module `io` asks for the default text encoding has moved: 3.9 asks
    `_bootlocale`, 3.10 asks `locale`, and from 3.11 it is worked out in C and
    cannot be patched at all. So every candidate is patched and the caller
    CHECKS that the patch bit rather than trusting that it did.
    """
    with contextlib.ExitStack() as stack:
        for name in ("_bootlocale", "locale"):
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
            if hasattr(module, "getpreferredencoding"):
                stack.enter_context(mock.patch.object(
                    module, "getpreferredencoding", lambda *a, **k: "ascii"))
        yield


class TestReadingAnAgentsFileWhateverTheLocaleIs(unittest.TestCase):
    """The doctor's three probes read the user's files as UTF-8, always.

    Read with the locale's encoding instead, a perfectly good `hooks.json` with
    an accent in a path comes back as "cannot read" on a machine whose locale is
    `C` -- and the doctor then tells its user to go and fix a file that has
    nothing wrong with it. It is the same rule `config.load_raw` already
    follows, and the locale is the one thing here nobody chose.
    """

    CONTENT = '{"hooks": {"Stop": ["ñandú"]}}'

    def test_a_utf8_file_is_an_object_even_when_the_locale_says_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            path.write_text(self.CONTENT, encoding="utf-8")
            with ascii_default_encoding():
                try:
                    path.read_text()
                except UnicodeDecodeError:
                    pass   # the control: a locale read really does fail here
                else:
                    self.skipTest("this Python decides the default text encoding"
                                  " in C: the locale cannot be faked in-process")
                data, error = read_json_object(path)
            self.assertIsNone(error)
            self.assertEqual(data["hooks"]["Stop"], ["ñandú"])


class TestTheWsl2Note(unittest.TestCase):

    def test_the_note_is_only_there_on_wsl2(self):
        linux = env_with(platform="linux", uname="Linux", environ={},
                         proc_version=lambda: "Linux version 6.1.0")
        self.assertIsNone(named(doctor.checks(linux), "WSL2"))

    def test_on_wsl2_it_says_where_claude_code_lives_and_what_sends_f12(self):
        wsl = env_with(platform="linux", uname="Linux", environ={},
                       proc_version=lambda: "Linux version 5.15-microsoft-WSL2")
        note = named(doctor.checks(wsl), "WSL2")
        self.assertIsNotNone(note)
        self.assertIsNone(note.ok)
        self.assertIn("F12", note.detail)
        self.assertIn("WSL", note.detail)


class TestTheWholeRun(unittest.TestCase):

    def test_the_checks_come_in_the_documented_order(self):
        results = doctor.checks(env_with(which=which_finding("claude", "codex", "agy")))
        names = [c.name for c in results]
        for earlier, later in (("system", "tmux"), ("tmux", "fzf"),
                               ("fzf", "python3"), ("python3", "agents"),
                               ("config.json", "state directory"),
                               ("state directory", "Claude Code hooks"),
                               ("Claude Code hooks", "codex")):
            self.assertLess(names.index(earlier), names.index(later),
                            "%s must come before %s" % (earlier, later))

    def test_a_probe_that_blows_up_becomes_a_line_and_not_a_traceback(self):
        def explode(*_args, **_kwargs):
            raise RuntimeError("the probe is broken")

        results = doctor.checks(env_with(which=explode))
        # Every check still answered, and the one that used `which` says why.
        self.assertTrue(any("the probe is broken" in c.detail for c in results))
        self.assertTrue(all(isinstance(c, doctor.Check) for c in results))

    def test_main_exits_0_when_nothing_failed(self):
        good = [doctor.Check("one", True, "fine", None),
                doctor.Check("two", None, "worth knowing", "do this")]
        with mock.patch.object(doctor, "checks", lambda env: good):
            rc, out = run_main()
        self.assertEqual(rc, 0)
        self.assertIn("✓ one", out)
        self.assertIn("! two", out)
        self.assertIn("do this", out)

    def test_main_exits_1_when_something_failed(self):
        bad = [doctor.Check("one", False, "broken", "fix it")]
        with mock.patch.object(doctor, "checks", lambda env: bad):
            rc, out = run_main()
        self.assertEqual(rc, 1)
        self.assertIn("✗ one", out)

    def test_main_prints_the_version_in_its_header(self):
        with mock.patch.object(doctor, "checks", lambda env: []):
            _rc, out = run_main()
        self.assertIn(config.version(), out)

    def test_main_refuses_arguments_it_does_not_know(self):
        rc, _out = run_main("--json")
        self.assertEqual(rc, 2)

    def test_main_on_this_machine_never_raises(self):
        # The real environment, whatever it is: the doctor's whole job is to
        # survive a broken machine and describe it.
        rc, out = run_main()
        self.assertIn(rc, (0, 1))
        self.assertIn("tmux", out)


class TestTheVersionFile(unittest.TestCase):
    """One source for the bash `--version` and for python."""

    def test_the_file_is_there_and_holds_a_version(self):
        path = config.code_dir() / "VERSION"
        self.assertTrue(path.exists(), "the root VERSION file is missing")
        self.assertRegex(path.read_text().strip(), r"^\d+\.\d+\.\d+")

    def test_config_version_reads_it(self):
        self.assertEqual(config.version(),
                         (config.code_dir() / "VERSION").read_text().strip())

    def test_a_missing_file_is_unknown_and_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "code_dir", lambda: Path(tmp)):
                self.assertEqual(config.version(), "unknown")


def run_main(*argv):
    """`doctor.main(argv)` with its output captured -> (rc, stdout + stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = doctor.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


if __name__ == "__main__":
    unittest.main()
