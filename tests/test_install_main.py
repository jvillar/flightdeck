"""`flightdeck install`: the one command that wires the three installers together.

Almost everything here runs with the installers REPLACED by recorders, because
what this module decides is an order, two questions and an exit code -- the
writing itself is tested in `test_install_claude.py`, `test_install_codex.py`,
`test_install_agy.py` and `test_install_migrate.py`. The two questions are
driven through the injected `ask`/`isatty`, so no test ever touches a real
stdin, and the tmux probe through an injected `run`, so none of them reaches a
server.

The one end-to-end run (`TestARealRun`) points HOME at an empty temporary
directory and gives it a config and a state directory of its own: on a
developer's machine `claude`, `codex` and `agy` ARE on PATH, so that run really
does write into that throwaway home -- which is the point of it.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import config, doctor, history, pins, registry
from flightdeck.install import __main__ as install_main
from flightdeck.install import agy as agy_install
from flightdeck.install import claude as claude_install
from flightdeck.install import codex as codex_install
from tests import SUBPROCESS_HOME


class FakeInstaller:
    """One of the three installers, recording what it was asked to do.

    Two methods and no more, which is the whole surface the orchestrator uses:
    it asks whether a mode was already chosen, and it installs. What `mode=None`
    then means -- read what they had and apply `default_mode` -- is the
    installer's own business, and is tested where that code lives.
    """

    def __init__(self, calls, name, mode_in_file=None, raises=None):
        self.calls = calls
        self.name = name
        self._mode_in_file = mode_in_file
        self._raises = raises

    def install(self, *args, **kwargs):
        self.calls.append((self.name, "install", args, kwargs))
        if self._raises is not None:
            raise self._raises
        return ["something"]

    def mode_in_file(self):
        return self._mode_in_file


class FakeMigrate:
    """The migration, recording which tool it was run for."""

    def __init__(self, calls, raises=None):
        self.calls = calls
        self._raises = raises

    def _one(self, tool):
        self.calls.append((tool, "migrate", (), {}))
        if self._raises is not None:
            raise self._raises
        return []

    def migrate_claude(self):
        return self._one("claude")

    def migrate_codex(self):
        return self._one("codex")

    def migrate_agy(self):
        return self._one("agy")


class FakeDoctor:
    """The doctor's printer, with the exit code a test wants.

    `key_collisions` is the REAL one: it is pure, and `install` shares it with
    the doctor on purpose -- the install says what it is about to override, the
    doctor says it afterwards, and there is one function deciding.
    """

    key_collisions = staticmethod(doctor.key_collisions)

    def __init__(self, calls, code=0):
        self.calls = calls
        self.code = code

    def main(self, argv=None):
        self.calls.append(("doctor", "main", (), {}))
        return self.code


def asking(*answers):
    """An `ask` that gives those answers in order, recording the questions."""
    asked = []
    answers = list(answers)

    def ask(question):
        asked.append(question)
        return answers.pop(0) if answers else True
    ask.asked = asked
    return ask


def never_asked(question):
    raise AssertionError("it asked when it should not have: %r" % question)


def which_finding(*commands):
    """A fake `shutil.which` that only knows `commands`."""
    known = set(commands)
    return lambda cmd: ("/usr/bin/%s" % cmd) if cmd in known else None


def tmux_answering(returncode=0, stdout=""):
    """A fake `subprocess.run` for the tmux probe, recording the argv it got."""
    seen = []

    class Result:
        def __init__(self, code, out):
            self.returncode, self.stdout = code, out

    def run(argv, **kwargs):
        seen.append(list(argv))
        if "has-session" in argv:
            return Result(returncode, "")
        return Result(0, stdout)
    run.seen = seen
    return run


@contextlib.contextmanager
def orchestrator(calls, which=None, run=None, doctor_code=0, migrate=None,
                 claude=None, codex=None, agy=None):
    """Every collaborator of the orchestrator replaced. -> nothing.

    The three installers, the migration and the doctor are all module
    attributes, which is what makes this possible without a dependency
    container: the module under test is the only place that names them.
    """
    with mock.patch.object(install_main, "claude",
                           claude or FakeInstaller(calls, "claude")), \
            mock.patch.object(install_main, "codex",
                              codex or FakeInstaller(calls, "codex")), \
            mock.patch.object(install_main, "agy",
                              agy or FakeInstaller(calls, "agy")), \
            mock.patch.object(install_main, "migrate",
                              migrate or FakeMigrate(calls)), \
            mock.patch.object(install_main, "doctor",
                              FakeDoctor(calls, doctor_code)):
        yield


def run_main(argv=(), **kwargs):
    """`main` with its output captured. -> (exit code, what it printed)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = install_main.main(list(argv), **kwargs)
    return code, out.getvalue(), err.getvalue()


ALL_THREE = which_finding("codex", "agy")


class TestTheOrder(unittest.TestCase):
    """Claude Code, then codex, then agy, then pins, keys and the doctor.

    And within each tool: the migration FIRST, the installer after. The other
    way round Flightdeck's tee would be registered in front of the OLD tee and
    save that as the delegate -- a tee calling a tee.
    """

    def test_each_tool_is_migrated_before_it_is_installed(self):
        calls = []
        with orchestrator(calls):
            run_main(["--yes"], which=ALL_THREE, run=tmux_answering(returncode=1))
        self.assertEqual([(tool, what) for tool, what, _a, _k in calls],
                         [("claude", "migrate"), ("claude", "install"),
                          ("codex", "migrate"), ("codex", "install"),
                          ("agy", "migrate"), ("agy", "install"),
                          ("doctor", "main")])

    def test_a_tool_that_is_not_on_PATH_is_skipped_with_one_line(self):
        calls = []
        with orchestrator(calls):
            _code, out, _err = run_main(["--yes"], which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertEqual([tool for tool, _w, _a, _k in calls],
                         ["claude", "claude", "doctor"])
        self.assertIn("codex: not found, skipped", out)
        self.assertIn("agy: not found, skipped", out)

    def test_claude_code_is_installed_even_when_it_is_not_on_PATH(self):
        # Flightdeck's hooks go into a settings file, not into the binary: an
        # install on a machine where `claude` is not installed YET is a normal
        # thing to do, and the doctor is what says the binary is missing.
        calls = []
        with orchestrator(calls):
            run_main(["--yes"], which=which_finding(),
                     run=tmux_answering(returncode=1))
        self.assertIn(("claude", "install"), [(t, w) for t, w, _a, _k in calls])


class TestTheStatusLineQuestion(unittest.TestCase):

    def _run(self, calls, ask, isatty=True, **kwargs):
        with orchestrator(calls, **kwargs):
            return run_main([], ask=ask, isatty=lambda: isatty,
                            which=which_finding(), run=tmux_answering(returncode=1))

    def test_the_demo_is_printed_before_the_question(self):
        calls = []
        ask = asking(True)
        _code, out, _err = self._run(calls, ask)
        self.assertIn("Flightdeck status line", out)
        self.assertIn("Use the Flightdeck status line for Claude Code?", ask.asked[0])
        self.assertIn("backed up and restored on uninstall", ask.asked[0])

    def test_yes_means_flightdecks_own_line(self):
        calls = []
        self._run(calls, asking(True))
        self.assertEqual(self._mode(calls, "claude"), "own")

    def test_no_means_the_one_they_already_had(self):
        calls = []
        self._run(calls, asking(False))
        self.assertEqual(self._mode(calls, "claude"), "wrap")

    def test_with_no_tty_nobody_is_asked_and_the_installer_decides(self):
        calls = []
        self._run(calls, never_asked, isatty=False)
        # `mode=None` is how the installer is told "you decide": it reads what
        # they had and applies `default_mode` itself.
        self.assertIsNone(self._mode(calls, "claude"))

    def test_with_yes_nobody_is_asked_either(self):
        calls = []
        with orchestrator(calls):
            run_main(["--yes"], ask=never_asked, isatty=lambda: True,
                     which=which_finding(), run=tmux_answering(returncode=1))
        self.assertIsNone(self._mode(calls, "claude"))

    def test_the_environment_can_say_it_too(self):
        calls = []
        with orchestrator(calls), \
                mock.patch.dict(os.environ, {"FLIGHTDECK_YES": "1"}):
            run_main([], ask=never_asked, isatty=lambda: True,
                     which=which_finding(), run=tmux_answering(returncode=1))
        self.assertIsNone(self._mode(calls, "claude"))

    def test_a_mode_already_chosen_is_never_asked_about_again(self):
        # `flightdeck update` reinstalls. Asking again -- or writing a default
        # over it -- would undo a choice they made on purpose.
        calls = []
        chosen = FakeInstaller(calls, "claude", mode_in_file="stack")
        _code, out, _err = self._run(calls, never_asked, claude=chosen)
        self.assertIsNone(self._mode(calls, "claude"))
        self.assertIn("stack", out)

    def test_agy_is_asked_about_separately_and_by_its_name(self):
        calls = []
        ask = asking(True, False)
        with orchestrator(calls):
            run_main([], ask=ask, isatty=lambda: True, which=which_finding("agy"),
                     run=tmux_answering(returncode=1))
        self.assertIn("Antigravity (agy)", ask.asked[1])
        self.assertEqual(self._mode(calls, "agy"), "wrap")

    def test_agy_is_not_asked_about_when_it_is_not_installed(self):
        calls = []
        ask = asking(True)
        with orchestrator(calls):
            run_main([], ask=ask, isatty=lambda: True, which=which_finding(),
                     run=tmux_answering(returncode=1))
        self.assertEqual(len(ask.asked), 1)

    def _mode(self, calls, tool):
        """The `mode` the named installer's `install()` was called with."""
        for name, what, _args, kwargs in calls:
            if name == tool and what == "install":
                return kwargs.get("mode")
        raise AssertionError("%s was never installed" % tool)


class TestCodexGetsForce(unittest.TestCase):

    def test_force_is_passed_through(self):
        calls = []
        with orchestrator(calls):
            run_main(["--yes", "--force"], which=which_finding("codex"),
                     run=tmux_answering(returncode=1))
        kwargs = [k for name, what, _a, k in calls
                  if name == "codex" and what == "install"][0]
        self.assertEqual(kwargs, {"force": True})

    def test_without_it_codex_is_installed_gently(self):
        calls = []
        with orchestrator(calls):
            run_main(["--yes"], which=which_finding("codex"),
                     run=tmux_answering(returncode=1))
        kwargs = [k for name, what, _a, k in calls
                  if name == "codex" and what == "install"][0]
        self.assertEqual(kwargs, {"force": False})


class TestKeepLegacy(unittest.TestCase):
    """`--keep-legacy` skips the migration and changes nothing else.

    One machine and one week of it: Flightdeck is installed beside the
    pre-release cockpit it was ported from, the manual checklist is run with
    both menus alive, and only then does a reinstall WITHOUT the flag take the
    old entries out. For anyone else there are no legacy entries and it is a
    no-op.
    """

    def _run(self, argv):
        calls = []
        with orchestrator(calls):
            code, out, err = run_main(argv, which=ALL_THREE,
                                      run=tmux_answering(returncode=1))
        return calls, code, out, err

    def test_no_tool_is_migrated(self):
        calls, _code, _out, _err = self._run(["--yes", "--keep-legacy"])
        self.assertEqual([(tool, what) for tool, what, _a, _k in calls],
                         [("claude", "install"), ("codex", "install"),
                          ("agy", "install"), ("doctor", "main")])

    def test_and_it_says_the_old_entries_are_staying(self):
        _calls, _code, out, _err = self._run(["--yes", "--keep-legacy"])
        self.assertIn("legacy entries kept", out)
        self.assertIn("stay beside Flightdeck's", out)

    def test_without_it_nothing_is_said_and_the_migration_runs(self):
        calls, _code, out, _err = self._run(["--yes"])
        self.assertNotIn("legacy entries kept", out)
        self.assertIn(("claude", "migrate"), [(t, w) for t, w, _a, _k in calls])

    def test_the_exit_code_is_the_one_it_would_have_been(self):
        _calls, code, _out, _err = self._run(["--yes", "--keep-legacy"])
        self.assertEqual(code, 0)


class TestThePins(unittest.TestCase):
    """Detected presets are offered, and `--yes` pins them."""

    @contextlib.contextmanager
    def _config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
                yield path

    def _run(self, which, ask, argv=()):
        calls = []
        with orchestrator(calls):
            return run_main(argv, ask=ask, isatty=lambda: True, which=which,
                            run=tmux_answering(returncode=1))

    def test_it_names_what_it_found_and_pins_it_on_yes(self):
        with self._config():
            ask = asking(True, True)     # the status line, then the pins
            _code, out, _err = self._run(which_finding("htop", "cswap"), ask)
            self.assertIn("Found htop and cswap — pin them to the menu?",
                          ask.asked[-1])
            names = [p["name"] for p in config.load_raw()["pins"]]
            self.assertEqual(names, ["htop", "cswap"])
            self.assertIn("pinned", out)

    def test_one_tool_is_asked_about_in_the_singular(self):
        with self._config():
            ask = asking(True, True)
            self._run(which_finding("htop"), ask)
            self.assertIn("Found htop — pin it to the menu?", ask.asked[-1])

    def test_three_are_joined_the_english_way(self):
        with self._config():
            ask = asking(True, False)
            self._run(which_finding("top", "htop", "cswap"), ask)
            self.assertIn("Found top, htop and cswap —", ask.asked[-1])

    def test_no_means_nothing_is_written(self):
        with self._config():
            self._run(which_finding("htop"), asking(True, False))
            self.assertNotIn("pins", config.load_raw())

    def test_yes_pins_them_without_asking(self):
        with self._config():
            self._run(which_finding("htop"), never_asked, argv=["--yes"])
            self.assertEqual([p["name"] for p in config.load_raw()["pins"]], ["htop"])

    def test_with_nothing_detected_it_says_so_and_moves_on(self):
        with self._config():
            _code, out, _err = self._run(which_finding(), asking(True))
            self.assertIn("pins", out.lower())
            self.assertNotIn("pins", config.load_raw())

    def test_something_already_pinned_is_not_offered_again(self):
        with self._config() as path:
            path.write_text(json.dumps({"pins": [
                {"name": "htop", "label": "📊 htop", "session": "htop",
                 "command": "htop"}]}))
            _code, out, _err = self._run(which_finding("htop"), asking(True),
                                         argv=["--yes"])
            self.assertEqual(len(config.load_raw()["pins"]), 1)
            self.assertNotIn("Found htop", out)


class TestTheKeyCollisions(unittest.TestCase):
    """Only when a server answers, and `has-session` is asked FIRST."""

    TAKEN = ("bind-key -T prefix n select-window -t :+\n"
             "bind-key -T prefix c new-window\n")

    def _run(self, run, argv=("--yes",)):
        calls = []
        with orchestrator(calls):
            return run_main(argv, which=which_finding(), run=run)

    def test_with_no_server_it_says_so_and_asks_nothing_else(self):
        run = tmux_answering(returncode=1)
        _code, out, _err = self._run(run)
        self.assertIn("no live tmux server", out)
        self.assertEqual([argv for argv in run.seen if "list-keys" in argv], [])

    def test_has_session_comes_before_list_keys(self):
        # Measured on 3.6a: with no server, `list-keys` exits 0, prints the
        # STOCK table AND leaves a socket file behind. `has-session` leaves none.
        run = tmux_answering(returncode=0, stdout=self.TAKEN)
        self._run(run)
        self.assertIn("has-session", run.seen[0])
        self.assertIn("list-keys", run.seen[1])

    def test_it_names_the_key_and_what_was_bound_to_it(self):
        _code, out, _err = self._run(tmux_answering(returncode=0, stdout=self.TAKEN))
        self.assertIn("prefix n", out)
        self.assertIn("select-window -t :+", out)
        self.assertIn("handover", out)

    def test_tmuxs_own_bindings_are_not_a_collision(self):
        stock = "bind-key -T prefix n next-window\n"
        _code, out, _err = self._run(tmux_answering(returncode=0, stdout=stock))
        self.assertNotIn("was bound to", out)


class TestTheExitCode(unittest.TestCase):

    def test_zero_when_everything_went_in_and_the_doctor_is_happy(self):
        calls = []
        with orchestrator(calls):
            code, _out, _err = run_main(["--yes"], which=ALL_THREE,
                                        run=tmux_answering(returncode=1))
        self.assertEqual(code, 0)

    def test_zero_with_no_agent_installed_at_all(self):
        # What CI's smoke test does: a bare Ubuntu with no `claude`. Warnings,
        # and an exit code that says the install itself worked.
        calls = []
        with orchestrator(calls):
            code, _out, _err = run_main(["--yes"], which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertEqual(code, 0)

    def test_one_when_the_doctor_found_something_broken(self):
        calls = []
        with orchestrator(calls, doctor_code=1):
            code, _out, _err = run_main(["--yes"], which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertEqual(code, 1)

    def test_an_installer_that_refuses_is_one_line_and_exit_1(self):
        calls = []
        broken = FakeInstaller(calls, "codex",
                               raises=config.ConfigError("hooks.json is a mess"))
        with orchestrator(calls, codex=broken):
            code, out, _err = run_main(["--yes"], which=which_finding("codex"),
                                       run=tmux_answering(returncode=1))
        self.assertEqual(code, 1)
        self.assertIn("✗ codex: hooks.json is a mess", out)

    def test_what_failed_is_said_BEFORE_the_next_steps(self):
        """The one line with something to do in it must not be buried.

        Printed after "Next steps" it was the last thing on screen, under a
        cheerful list of what to try now -- and it is the only line that says
        part of the install did not happen.
        """
        calls = []
        broken = FakeInstaller(calls, "codex",
                               raises=config.ConfigError("hooks.json is a mess"))
        with orchestrator(calls, codex=broken):
            _code, out, _err = run_main(["--yes"], which=which_finding("codex"),
                                        run=tmux_answering(returncode=1))
        self.assertLess(out.index("could not be set up"), out.index("Next steps"))

    def test_and_the_doctor_still_runs_afterwards(self):
        calls = []
        broken = FakeInstaller(calls, "codex", raises=config.ConfigError("nope"))
        with orchestrator(calls, codex=broken):
            run_main(["--yes"], which=which_finding("codex"),
                     run=tmux_answering(returncode=1))
        self.assertIn(("doctor", "main"), [(t, w) for t, w, _a, _k in calls])

    def test_a_migration_that_refuses_stops_that_tool_and_no_other(self):
        calls = []
        with orchestrator(calls,
                          migrate=FakeMigrate(calls, config.ConfigError("broken"))):
            code, out, _err = run_main(["--yes"], which=which_finding(),
                                       run=tmux_answering(returncode=1))
        self.assertEqual(code, 1)
        self.assertIn("✗ Claude Code: broken", out)
        # the installer was never reached: half a migration is not installed over
        self.assertEqual([(t, w) for t, w, _a, _k in calls],
                         [("claude", "migrate"), ("doctor", "main")])


class TestTheWords(unittest.TestCase):

    def test_an_unknown_flag_is_the_usage_on_stderr_and_exit_2(self):
        calls = []
        with orchestrator(calls):
            code, out, err = run_main(["--dry-run"], which=which_finding(),
                                      run=tmux_answering(returncode=1))
        self.assertEqual(code, 2)
        self.assertIn("usage", err)
        self.assertEqual(out, "")
        self.assertEqual(calls, [])

    def test_the_next_steps_are_the_last_thing_it_says(self):
        calls = []
        with orchestrator(calls):
            _code, out, _err = run_main(["--yes"], which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertIn("flightdeck", out)
        self.assertIn("F12", out)
        self.assertIn("prefix + n", out)
        self.assertIn("restart", out.lower())
        # the PATH line belongs to install.sh, which is the only thing that
        # knows whether it added one
        self.assertNotIn("~/.local/bin", out)

    def test_codex_is_told_it_will_be_asked_to_trust_the_hooks(self):
        calls = []
        with orchestrator(calls):
            _code, out, _err = run_main(["--yes"], which=which_finding("codex"),
                                        run=tmux_answering(returncode=1))
        self.assertIn("Trust all and continue", out)

    def test_and_is_not_told_so_when_codex_is_not_there(self):
        calls = []
        with orchestrator(calls):
            _code, out, _err = run_main(["--yes"], which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertNotIn("Trust all and continue", out)


class TestTheSuiteCannotReachTheRealHome(unittest.TestCase):
    """The structural net, pinned where it can be read.

    Every path the three installers WRITE is worked out from `Path.home()` when
    the module is imported, which is before any test has had a chance to patch
    anything. Today every in-process run of an installer is wrapped in fakes, so
    this is a net and not a hole -- but a net nobody tests is a net that quietly
    stops being there, and what is on the other side of it is the developer's
    own `~/.claude`, `~/.codex` and `~/.gemini`.
    """

    def test_home_is_the_sandbox_and_no_test_may_unset_it(self):
        self.assertEqual(os.environ.get("HOME"), SUBPROCESS_HOME)

    def test_the_three_installers_write_inside_it(self):
        for path in (claude_install.SETTINGS_PATH, codex_install.CODEX_DIR,
                     agy_install.HOOKS_JSON, agy_install.SETTINGS_PATH,
                     registry.REG_DIR, history.HOME_PROJECTS):
            self.assertTrue(str(path).startswith(SUBPROCESS_HOME + os.sep),
                            "%s is outside the sandbox" % path)


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


class TestARealRun(unittest.TestCase):
    """The whole command, for real, against a home of its own.

    Nothing is patched: the real installers write into a temporary `~/.claude`,
    `~/.codex` and `~/.gemini`, and the config and state directories are
    temporary too -- the suite-wide sandbox is shared with every other test, and
    a mode or a pin written into it would leak.
    """

    def test_it_installs_into_a_throwaway_home_and_exits_0(self):
        code_dir = config.code_dir()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({"HOME": str(Path(tmp) / "home"),
                        "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                        "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                        "PYTHONPATH": str(code_dir),
                        "PATH": _path_with_a_fzf_above_the_floor(tmp),
                        # the suite's dead socket: no tmux server is touched
                        "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket"})
            (Path(tmp) / "home").mkdir()
            result = subprocess.run(
                ["python3", "-m", "flightdeck.install", "--yes"],
                capture_output=True, text=True, env=env, timeout=120, cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            settings = Path(tmp) / "home" / ".claude" / "settings.json"
            self.assertIn("session_hook.py", settings.read_text())
            # and it ends by reporting on the machine
            self.assertIn("doctor", result.stdout)

    def test_a_second_run_writes_nothing_new(self):
        code_dir = config.code_dir()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({"HOME": str(Path(tmp) / "home"),
                        "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                        "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                        "PYTHONPATH": str(code_dir),
                        "PATH": _path_with_a_fzf_above_the_floor(tmp),
                        "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket"})
            (Path(tmp) / "home").mkdir()
            argv = ["python3", "-m", "flightdeck.install", "--yes"]
            subprocess.run(argv, capture_output=True, text=True, env=env,
                           timeout=120, cwd=tmp)
            settings = Path(tmp) / "home" / ".claude" / "settings.json"
            snapshot = settings.read_text()
            backups = len(list(settings.parent.glob("*.bak-flightdeck-*")))
            result = subprocess.run(argv, capture_output=True, text=True, env=env,
                                    timeout=120, cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(settings.read_text(), snapshot)
            self.assertEqual(len(list(settings.parent.glob("*.bak-flightdeck-*"))),
                             backups)


class TestTheQuestionItself(unittest.TestCase):
    """`_ask` is the only thing here that touches a real stdin."""

    def _answer(self, typed):
        with mock.patch("builtins.input", lambda prompt="": typed):
            return install_main._ask("Ready?")

    def test_enter_is_yes(self):
        self.assertTrue(self._answer(""))

    def test_y_is_yes_and_n_is_no(self):
        self.assertTrue(self._answer("y"))
        self.assertFalse(self._answer("n"))
        self.assertFalse(self._answer("  NO  "))

    def test_the_end_of_stdin_takes_the_default_it_showed(self):
        # `[Y/n]` is on the screen when the Ctrl-D lands.
        with mock.patch("builtins.input", mock.Mock(side_effect=EOFError)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(install_main._ask("Ready?"))

    def test_a_ctrl_c_is_not_an_answer(self):
        # It travels on to the guard at the bottom of the module, which turns it
        # into one line and exit 130 instead of a traceback.
        with mock.patch("builtins.input", mock.Mock(side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                install_main._ask("Ready?")


class TestTheDemoIsNotLoadBearing(unittest.TestCase):

    def test_a_demo_that_blows_up_does_not_stop_the_question(self):
        calls = []
        ask = asking(True)
        with orchestrator(calls), \
                mock.patch.object(install_main.statusline, "_demo",
                                  mock.Mock(side_effect=RuntimeError("boom"))):
            _code, out, _err = run_main([], ask=ask, isatty=lambda: True,
                                        which=which_finding(),
                                        run=tmux_answering(returncode=1))
        self.assertIn("could not be drawn", out)
        self.assertEqual(len(ask.asked), 1)
        self.assertEqual(self._mode(calls), "own")

    def _mode(self, calls):
        return [k.get("mode") for name, what, _a, k in calls
                if name == "claude" and what == "install"][0]


if __name__ == "__main__":
    unittest.main()
