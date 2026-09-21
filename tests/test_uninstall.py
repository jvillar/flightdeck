"""`flightdeck uninstall`: the command that takes Flightdeck back off a machine.

This is the one command that DELETES, so read the fixtures before the tests: every
single one of them runs inside a temporary directory that holds its own HOME, its
own `XDG_DATA_HOME`, its own state directory and its own `config.json`
(`sandbox`), and the three installers are replaced by recorders unless the test
says otherwise. Nothing here can reach the home, the state or the code directory
of whoever is running the suite -- the repository checkout in particular, which is
what `config.installed_here()` protects and which has to still be there when the
suite ends.

The tmux half of `uninstall` (the stock options, the keys, the menus) is the bash
command's, shared with `quit`, and is tested in `test_flightdeck_cli.py` against a
tmux server of that file's own.
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

from flightdeck import config, uninstall
from flightdeck.install import BACKUP_MARK, agy, claude, codex


@contextlib.contextmanager
def sandbox(tmp):
    """A throwaway machine: HOME, data, state and config all under `tmp`.

    -> the home directory. The data directory is `<tmp>/share/flightdeck`, which
    is where a test builds a fake `current/` when it wants the code-removal path.
    """
    tmp = Path(tmp)
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home),
           "XDG_DATA_HOME": str(tmp / "share"),
           "FLIGHTDECK_STATE_DIR": str(tmp / "state"),
           "FLIGHTDECK_CONFIG": str(tmp / "config" / "config.json"),
           "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
           "XDG_STATE_HOME": str(tmp / "xdg-state"),
           # Set here rather than left to the suite's defaults, which are not
           # there when this file is run on its own: nothing in this module
           # talks to tmux, and a socket nobody listens on is what keeps it
           # that way if something one day does.
           "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket"}
    with mock.patch.dict(os.environ, env):
        yield home


@contextlib.contextmanager
def fake_agents(claude_raises=None, codex_raises=None, agy_raises=None):
    """The three installers replaced by recorders. -> the list of who was called.

    What this module decides is an ORDER, a set of removals and an exit code; the
    writing into `~/.claude`, `~/.codex` and `~/.gemini` is tested where that code
    lives. Replacing them also means no test of this file can reach a real
    settings file: the three modules read their paths at import time, so a
    temporary HOME set afterwards would NOT move them.
    """
    called = []

    def recorder(name, raises):
        def one():
            called.append(name)
            if raises is not None:
                raise raises
            return []
        return one

    with mock.patch.object(claude, "uninstall", recorder("claude", claude_raises)), \
            mock.patch.object(codex, "uninstall", recorder("codex", codex_raises)), \
            mock.patch.object(agy, "uninstall", recorder("agy", agy_raises)):
        yield called


def run(argv=(), **kwargs):
    """`uninstall.main(argv)` with its output captured. -> (exit code, output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = uninstall.main(list(argv), **kwargs)
    return code, out.getvalue()


def code_at(path):
    """Pretend the code running right now lives at `path`."""
    return mock.patch.object(config, "code_dir", return_value=Path(path))


def a_code_tree(data):
    """A fake installed code directory: `<data>/current`, `previous` and `bin`.

    -> the `current` path. It is built with real files so the removal is a real
    removal, and it lives in the test's own temporary directory.
    """
    data = Path(data)
    for name in ("current", "previous"):
        (data / name / "bin").mkdir(parents=True, exist_ok=True)
        (data / name / "bin" / "flightdeck").write_text("#!/bin/sh\n")
        (data / name / "VERSION").write_text("0.1.0\n")
    (data / "bin").mkdir(parents=True, exist_ok=True)
    (data / "bin" / "fzf").write_text("binary")
    return data / "current"


class TestTheAgents(unittest.TestCase):
    """The three installers, in order, and one refusal does not stop the rest."""

    def test_all_three_are_asked_in_order(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents() as called:
                code, _out = run()
        self.assertEqual(called, ["claude", "codex", "agy"])
        self.assertEqual(code, 0)

    def test_a_tool_that_refuses_is_one_line_and_the_others_still_run(self):
        for boom in (config.ConfigError("invalid JSON in x"),
                     ValueError("not a mode"),
                     OSError("read-only file system")):
            with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
                with fake_agents(codex_raises=boom) as called:
                    code, out = run()
            self.assertEqual(called, ["claude", "codex", "agy"], boom)
            self.assertEqual(code, 1, boom)
            self.assertIn("✗ codex: %s" % boom, out)

    def test_it_is_never_a_traceback(self):
        # The exceptions the three installers raise are caught by kind, so a new
        # one would come out of here as a crash. This is the guard for the day
        # somebody adds a fourth.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents(agy_raises=OSError("gone")):
                code, out = run()
        self.assertEqual(code, 1)
        self.assertIn("✗ agy:", out)


class TestTheLinks(unittest.TestCase):
    """`~/.local/bin/flightdeck` and `fld`, and only when they are ours."""

    def _bin(self, home):
        path = Path(home) / ".local" / "bin"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_a_link_into_our_code_goes(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            local = self._bin(home)
            for name in ("flightdeck", "fld"):
                (local / name).symlink_to(current / "bin" / "flightdeck")
            with fake_agents(), code_at(current):
                code, out = run()
            self.assertFalse((local / "flightdeck").is_symlink())
            self.assertFalse((local / "fld").is_symlink())
        self.assertEqual(code, 0)
        self.assertIn("removed the link", out)

    def test_somebody_elses_fld_is_left_exactly_where_it_is(self):
        # `fld` is a short name and the installer only links it when it is free:
        # what says a link is ours is where it LANDS, never what it is called.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            local = self._bin(home)
            theirs = Path(tmp) / "someone-elses-fld"
            theirs.write_text("#!/bin/sh\n")
            (local / "fld").symlink_to(theirs)
            with fake_agents(), code_at(current):
                _code, out = run()
            self.assertTrue((local / "fld").is_symlink())
            self.assertTrue(theirs.exists())
        self.assertIn("left alone", out)

    def test_a_plain_file_of_theirs_with_our_name_is_left_alone_too(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            local = self._bin(home)
            (local / "flightdeck").write_text("#!/bin/sh\necho theirs\n")
            with fake_agents(), code_at(current):
                _code, out = run()
            self.assertTrue((local / "flightdeck").exists())
        self.assertIn("left alone", out)

    def test_a_link_whose_target_has_already_gone_is_still_ours(self):
        # The code directory may be removed by hand before the command is run;
        # the dangling link is still ours and still breaks `command -v`.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            current = Path(tmp) / "share" / "flightdeck" / "current"
            local = self._bin(home)
            (local / "flightdeck").symlink_to(current / "bin" / "flightdeck")
            with fake_agents(), code_at(current):
                _code, _out = run()
            self.assertFalse((local / "flightdeck").is_symlink())

    def test_with_no_links_it_says_so_and_fails_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents():
                code, out = run()
        self.assertEqual(code, 0)
        self.assertIn("no link", out)


class TestTheShellRcLine(unittest.TestCase):
    """The `# flightdeck` line `install.sh` adds, and nothing else in the file."""

    RC = ('# my prompt\nexport EDITOR=vim\n'
          'export PATH="$HOME/.local/bin:$PATH"  # flightdeck\n'
          'alias ll="ls -l"\n')

    def _rc(self, home, name=".zshrc", text=None):
        path = Path(home) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.RC if text is None else text)
        return path

    def test_only_the_marked_line_goes_and_the_rest_is_byte_equal(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home)
            with fake_agents():
                code, out = run()
            self.assertEqual(rc.read_text(),
                             '# my prompt\nexport EDITOR=vim\nalias ll="ls -l"\n')
        self.assertEqual(code, 0)
        self.assertIn(".zshrc", out)

    def test_it_leaves_a_timestamped_backup_of_the_file_it_rewrote(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home)
            before = rc.read_text()
            with fake_agents():
                run()
            copies = list(rc.parent.glob(rc.name + BACKUP_MARK + "*"))
            self.assertEqual(len(copies), 1, copies)
            self.assertEqual(copies[0].read_text(), before)

    def test_every_rc_file_is_looked_at_bash_and_fish_included(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            files = [self._rc(home, name) for name in
                     (".zshrc", ".bashrc", ".bash_profile", ".config/fish/config.fish")]
            with fake_agents():
                run()
            for path in files:
                self.assertNotIn("# flightdeck", path.read_text(), path)

    def test_a_file_with_nothing_of_ours_is_not_written_at_all(self):
        # Not even a backup: rewriting a file byte for byte still changes its
        # mtime, and a `.bak-flightdeck-` beside an untouched rc file is a
        # puzzle for whoever finds it.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home, text="export EDITOR=vim\n")
            mtime = rc.stat().st_mtime_ns
            with fake_agents():
                run()
            self.assertEqual(rc.read_text(), "export EDITOR=vim\n")
            self.assertEqual(rc.stat().st_mtime_ns, mtime)
            self.assertEqual(list(rc.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_the_line_endings_of_the_file_are_left_as_they_were(self):
        # A CRLF rc file (WSL2, an editor from Windows) must not come back with
        # its endings rewritten, and a file with no trailing newline must not
        # grow one: the only difference is the lines that carried the mark.
        text = 'a=1\r\nexport PATH="x"  # flightdeck\r\nb=2'
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home, text=text)
            with fake_agents():
                run()
            # Read as BYTES: `read_text` translates the endings on the way in,
            # so it would report a file rewritten to LF as if it were intact.
            self.assertEqual(rc.read_bytes(), b"a=1\r\nb=2")

    def test_a_line_of_theirs_that_merely_mentions_us_stays(self):
        # The mark is the END of our line, not a word anywhere in the file.
        # Somebody's own `# flightdeck notes` heading, or an alias with our name
        # in its comment, is theirs -- and losing it would only be discoverable
        # by diffing a backup they do not know is there.
        theirs = ('# flightdeck notes to self\n'
                  "alias fd='flightdeck'  # flightdeck shortcut\n"
                  'export PATH="$HOME/.local/bin:$PATH"  # flightdeck\n'
                  'echo "# flightdeck" >> /tmp/x\n')
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home, text=theirs)
            with fake_agents():
                run()
            self.assertEqual(rc.read_text(),
                             '# flightdeck notes to self\n'
                             "alias fd='flightdeck'  # flightdeck shortcut\n"
                             'echo "# flightdeck" >> /tmp/x\n')

    def test_a_file_with_only_such_lines_is_not_written_at_all(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home, text="# flightdeck notes to self\n")
            mtime = rc.stat().st_mtime_ns
            with fake_agents():
                run()
            self.assertEqual(rc.stat().st_mtime_ns, mtime)
            self.assertEqual(list(rc.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_trailing_whitespace_after_the_mark_is_still_our_line(self):
        # An editor that strips nothing, a copy-paste: the line is ours.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            rc = self._rc(home, text='export PATH="x"  # flightdeck   \nkeep=1\n')
            with fake_agents():
                run()
            self.assertEqual(rc.read_text(), "keep=1\n")

    def test_it_prints_the_line_it_took_out(self):
        # The spec says uninstall lists what it removed. A file name alone
        # leaves the user diffing a backup to find out what changed in a file
        # they own.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            self._rc(home)
            with fake_agents():
                _code, out = run()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"  # flightdeck', out)

    def test_it_says_which_file_it_changed(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            self._rc(home, ".bashrc")
            with fake_agents():
                _code, out = run()
        self.assertIn(".bashrc", out)
        self.assertIn("PATH line", out)


class TestPureRcPruning(unittest.TestCase):
    """`without_our_lines` on its own: the rule, with no file in sight."""

    def test_it_takes_every_marked_line_and_reports_them(self):
        text = "a\nb  # flightdeck\nc\nd # flightdeck\n"
        kept, gone = uninstall.without_our_lines(text)
        self.assertEqual(kept, "a\nc\n")
        self.assertEqual(gone, ["b  # flightdeck", "d # flightdeck"])

    def test_the_mark_has_to_END_the_line(self):
        text = ("# flightdeck is great\n"
                "alias x=y  # flightdeck helper\n"
                "export PATH=z  # flightdeck\n")
        kept, gone = uninstall.without_our_lines(text)
        self.assertEqual(kept, "# flightdeck is great\nalias x=y  # flightdeck helper\n")
        self.assertEqual(gone, ["export PATH=z  # flightdeck"])

    def test_with_no_mark_nothing_moves(self):
        self.assertEqual(uninstall.without_our_lines("a\nb\n"), ("a\nb\n", []))

    def test_an_empty_file_is_an_empty_file(self):
        self.assertEqual(uninstall.without_our_lines(""), ("", []))


class TestTheCodeDirectory(unittest.TestCase):
    """R8: the code goes only when this Flightdeck IS the installed one."""

    def test_current_previous_and_the_downloaded_fzf_all_go(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            data = Path(tmp) / "share" / "flightdeck"
            current = a_code_tree(data)
            with fake_agents(), code_at(current):
                code, out = run()
            self.assertFalse(current.exists())
            self.assertFalse((data / "previous").exists())
            self.assertFalse((data / "bin").exists())
        self.assertEqual(code, 0)
        self.assertIn("removed the code", out)

    def test_a_source_checkout_is_never_deleted(self):
        # The guard that matters most in this file: a clone somebody runs from
        # is not Flightdeck's to remove, and `uninstall` says so instead of
        # silently doing nothing.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            checkout = Path(tmp) / "checkout"
            (checkout / "bin").mkdir(parents=True)
            (checkout / "bin" / "flightdeck").write_text("#!/bin/sh\n")
            data = Path(tmp) / "share" / "flightdeck"
            a_code_tree(data)
            with fake_agents(), code_at(checkout):
                code, out = run()
            self.assertTrue((checkout / "bin" / "flightdeck").exists())
            # and not a finger laid on what happens to be in the data directory
            self.assertTrue((data / "current").exists())
            self.assertTrue((data / "previous").exists())
        self.assertEqual(code, 0)
        self.assertIn("source checkout", out)

    def test_the_repository_this_suite_runs_from_is_a_source_checkout(self):
        # Belt and braces, and a real assertion about this machine: with no
        # patching at all, `config.installed_here()` is false here, so no run of
        # this suite can ever reach the checkout's own files.
        self.assertFalse(config.installed_here())

    def test_it_is_the_last_thing_the_command_does(self):
        # Everything else has to be printed before the directory holding this
        # very code disappears from under the process.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as home:
            (home / ".zshrc").write_text('export PATH="x"  # flightdeck\n')
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            with fake_agents(), code_at(current):
                _code, out = run()
            lines = out.splitlines()
            code_line = max(i for i, line in enumerate(lines) if "the code" in line)
            for needle in (".zshrc", "link"):
                self.assertLess(max(i for i, line in enumerate(lines)
                                    if needle in line), code_line, needle)

    def test_a_directory_it_cannot_remove_is_one_line_and_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            with fake_agents(), code_at(current), \
                    mock.patch.object(uninstall.shutil, "rmtree",
                                      side_effect=OSError("busy")):
                code, out = run()
        self.assertEqual(code, 1)
        self.assertIn("✗", out)
        self.assertIn("busy", out)


class TestTheStateAndTheConfig(unittest.TestCase):
    """Kept by default; `--purge` takes them and says so."""

    def _state(self, tmp):
        """A state directory and a config file with something in each."""
        state = Path(tmp) / "state"
        (state / "sessions").mkdir(parents=True)
        (state / "sessions" / "abc.json").write_text("{}")
        (state / "delegates").mkdir(parents=True)
        (state / "delegates" / "statusline").write_text("sh mine.sh\n")
        cfg = Path(tmp) / "config" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"menu_port": 42707}')
        return state, cfg

    def test_by_default_both_survive_and_it_says_where_they_are(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            state, cfg = self._state(tmp)
            with fake_agents():
                code, out = run()
            self.assertTrue((state / "delegates" / "statusline").exists())
            self.assertTrue(cfg.exists())
        self.assertEqual(code, 0)
        self.assertIn("--purge", out)

    def test_purge_removes_the_state_and_the_config(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            state, cfg = self._state(tmp)
            with fake_agents():
                code, out = run(["--purge"])
            self.assertFalse(state.exists())
            self.assertFalse(cfg.exists())
        self.assertEqual(code, 0)
        self.assertIn("removed the state", out)
        self.assertIn("config", out)

    def test_purge_with_nothing_there_says_so_and_fails_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents():
                code, out = run(["--purge"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out.lower())

    def test_the_purge_happens_before_the_code_goes(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            self._state(tmp)
            current = a_code_tree(Path(tmp) / "share" / "flightdeck")
            with fake_agents(), code_at(current):
                _code, out = run(["--purge"])
            lines = out.splitlines()
            self.assertLess(max(i for i, line in enumerate(lines) if "state" in line),
                            max(i for i, line in enumerate(lines) if "the code" in line))


class TestTheWords(unittest.TestCase):

    def test_an_unknown_flag_is_the_usage_on_stderr_and_exit_2(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with contextlib.redirect_stderr(err), fake_agents() as called:
                code = uninstall.main(["--everything"])
        self.assertEqual(code, 2)
        self.assertEqual(called, [], "it started removing things anyway")
        self.assertIn("usage: flightdeck uninstall", err.getvalue())

    def test_it_names_the_version_and_the_code_it_is_removing(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents():
                _code, out = run()
        self.assertIn(config.version(), out)

    def test_it_says_the_work_sessions_were_never_touched(self):
        # The promise `quit` makes and this command has to make too: the agents
        # running in tmux carry on. Somebody uninstalling mid-afternoon needs to
        # read that before they go looking.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp):
            with fake_agents():
                _code, out = run()
        self.assertIn("work", out.lower())


class TestARealUninstall(unittest.TestCase):
    """The smoke test: a real install and a real uninstall in a temporary HOME.

    Nothing is patched here -- the bash command and the three installers do
    exactly what they do on a machine -- so everything they can reach is
    throwaway: HOME (and with it `~/.claude`, `~/.codex`, `~/.gemini` and
    `~/.local/bin`), the data, state and config directories, and a tmux socket
    with no server behind it. On this machine `claude`, `codex` and `agy` ARE on
    PATH, so files really do appear under that home, which is the point.

    The code directory is the repository checkout, so `uninstall` reports it as
    one and removes nothing of it -- asserted, because it is the guard that
    keeps this test from deleting the code it is testing.
    """

    CMD = config.code_dir() / "bin" / "flightdeck"

    def _env(self, tmp):
        home = Path(tmp) / "home"
        home.mkdir()
        env = dict(os.environ)
        env.update({"HOME": str(home),
                    "XDG_DATA_HOME": str(Path(tmp) / "share"),
                    # The other two XDG paths follow the temporary home as well:
                    # every Flightdeck path falls back to one of them, and
                    # `quit` sources `$XDG_CONFIG_HOME/tmux/tmux.conf`. Inert
                    # here only because the socket is dead -- which is not a
                    # reason to leave a real path reachable.
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_STATE_HOME": str(home / ".local" / "state"),
                    "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
                    "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
                    "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket"})
        return home, env

    def _run(self, args, env, tmp):
        return subprocess.run([str(self.CMD)] + list(args), capture_output=True,
                              text=True, env=env, timeout=180, cwd=tmp)

    # The places a Flightdeck install can reach inside a home. The whole tree is
    # deliberately NOT walked: with HOME pointing here, this python writes its
    # bytecode cache under it too (`~/Library/Caches` on macOS), and those files
    # carry the module sources -- including the string "session_hook.py", which
    # would make the assertion below pass or fail for a reason that has nothing
    # to do with what was installed.
    SCAN = (".claude", ".codex", ".gemini", ".local", ".config",
            ".zshrc", ".bashrc", ".bash_profile")

    def _snapshot(self, home):
        """The files in those places, backups aside. -> {relative path: bytes}."""
        found = {}
        for name in self.SCAN:
            root = home / name
            if root.is_file():
                paths = [root]
            elif root.is_dir():
                paths = sorted(root.rglob("*"))
            else:
                continue
            for path in paths:
                if path.is_file() and BACKUP_MARK not in path.name:
                    found[str(path.relative_to(home))] = path.read_bytes()
        return found

    def test_install_then_uninstall_puts_the_home_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, env = self._env(tmp)
            # Something of theirs in each place, so "byte equal afterwards" is a
            # claim about a file with content and not about an empty one.
            (home / ".zshrc").write_text("export EDITOR=vim\n")
            before = self._snapshot(home)

            installed = self._run(["install", "--yes"], env, tmp)
            settings = home / ".claude" / "settings.json"
            self.assertTrue(settings.exists(), installed.stdout + installed.stderr)
            self.assertIn("session_hook.py", settings.read_text())

            removed = self._run(["uninstall"], env, tmp)
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            self.assertEqual(removed.stderr, "")
            self.assertIn("source checkout", removed.stdout)

            after = self._snapshot(home)
            # Every file that was there before is back byte for byte...
            for name, content in before.items():
                self.assertEqual(after.get(name), content, name)
            # ...and nothing Flightdeck registered is left anywhere under it.
            for name, content in after.items():
                if BACKUP_MARK in name:
                    continue
                text = content.decode("utf-8", "replace")
                self.assertNotIn("session_hook.py", text, name)
                self.assertNotIn("context_tee.py", text, name)
                self.assertNotIn("codex_notify.py", text, name)
            # The backups of the files it rewrote are there to be read.
            self.assertTrue(list((home / ".claude").glob("*" + BACKUP_MARK + "*")))

    def test_the_state_is_kept_and_purge_takes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _home, env = self._env(tmp)
            state = Path(tmp) / "state"
            self._run(["install", "--yes"], env, tmp)
            first = self._run(["uninstall"], env, tmp)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue(Path(env["FLIGHTDECK_CONFIG"]).exists())
            self.assertTrue(state.exists())

            second = self._run(["uninstall", "--purge"], env, tmp)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse(state.exists())
            self.assertFalse(Path(env["FLIGHTDECK_CONFIG"]).exists())

    def test_the_repository_is_untouched_by_all_of_that(self):
        # The one assertion this whole file exists to make.
        self.assertTrue((config.code_dir() / "bin" / "flightdeck").is_file())
        self.assertTrue((config.code_dir() / "flightdeck" / "uninstall.py").is_file())


if __name__ == "__main__":
    unittest.main()
