"""The installer for Claude Code: the state hooks and the status line tee.

Nothing here reads or writes the real `~/.claude/settings.json`: every test
points `SETTINGS_PATH` at a temporary file and `FLIGHTDECK_STATE_DIR` at a
temporary directory, so the delegate -- the only copy of the user's own status
line command -- is a throwaway too.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import config
from flightdeck.install import BACKUP_MARK, claude


@contextlib.contextmanager
def settings_at(tmp, content=None):
    """Point `SETTINGS_PATH` at a throwaway file (optionally with content)."""
    path = Path(tmp) / "claude" / "settings.json"
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str)
                        else json.dumps(content, indent=2) + "\n")
    with mock.patch.object(claude, "SETTINGS_PATH", path):
        yield path


@contextlib.contextmanager
def state_at(tmp, delegate=None, config_json=None):
    """Point the state directory AND `config.json` at throwaways. -> the delegate's path.

    `delegate` writes that text into the delegate file first, which is how a
    test says "the user's status line was already saved here"; `config_json` is
    the dict the config file starts with.

    `FLIGHTDECK_CONFIG` is set here and not only where the mode is being tested:
    `install()` writes the chosen mode, so a test that forgot it would write
    into the config of whoever is running the suite.
    """
    env = {"FLIGHTDECK_STATE_DIR": str(tmp),
           "FLIGHTDECK_CONFIG": str(Path(tmp) / "config" / "config.json")}
    with mock.patch.dict(os.environ, env):
        if config_json is not None:
            path = config.config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(config_json, indent=2) + "\n")
        path = claude.delegate_file()
        if delegate is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(delegate)
        yield path


def written_config():
    """Exactly what the throwaway `config.json` holds (`{}` when there is none)."""
    return config.load_raw()


def run(fn, *args):
    """Call fn capturing what it prints -> (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


class TestCurrentStatusline(unittest.TestCase):

    def test_the_normal_shape(self):
        settings = {"statusLine": {"type": "command", "command": "  sh mine.sh "}}
        self.assertEqual(claude.current_statusline(settings), "sh mine.sh")

    def test_a_bare_string_counts_too(self):
        self.assertEqual(claude.current_statusline({"statusLine": "sh mine.sh"}),
                         "sh mine.sh")

    def test_no_status_line(self):
        self.assertEqual(claude.current_statusline({}), "")
        self.assertEqual(claude.current_statusline({"statusLine": None}), "")


class TestSavedDelegate(unittest.TestCase):
    """Reading the delegate can never take the installer down."""

    def test_with_no_file_it_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp):
            self.assertEqual(claude.saved_delegate(), "")

    def test_an_unreadable_file_degrades_to_empty(self):
        # The state directory is disposable and anything can end up in there (a
        # core dump, a half-copied file). A delegate that is not text has to
        # read as "there is none", not as a traceback on top of the installer --
        # which is exactly when the user is trying to fix something.
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp, "") as path:
            path.write_bytes(b"\xff\xfe\x00binary")
            self.assertEqual(claude.saved_delegate(), "")


class TestRegisterStatusline(unittest.TestCase):

    def test_it_saves_the_usual_one_and_registers_the_tee(self):
        settings = {"statusLine": {"type": "command", "command": "sh mine.sh"}}
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp) as path:
                out, saved = claude.register_statusline(settings)
                self.assertIs(out, settings)
                self.assertEqual(saved, "sh mine.sh")
                self.assertEqual(path.read_text().strip(), "sh mine.sh")
        # quoted: Claude Code runs this through a shell, and one space in the
        # install path would leave the user with no status line at all
        self.assertEqual(settings["statusLine"]["command"],
                         "python3 '%s'" % claude.TEE)

    def test_with_the_tee_already_there_it_touches_nothing(self):
        settings = {"statusLine": {"type": "command",
                                   "command": "python3 '%s'" % claude.TEE}}
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, "sh mine.sh\n") as path:
                _, saved = claude.register_statusline(settings)
                self.assertEqual(saved, "")
                # neither the delegate nor the settings are rewritten (a delegate
                # overwritten with the tee itself would call itself for ever)
                self.assertEqual(path.read_text().strip(), "sh mine.sh")
        self.assertEqual(settings["statusLine"]["command"],
                         "python3 '%s'" % claude.TEE)

    def test_a_tee_of_ours_at_a_path_that_has_gone_is_repointed(self):
        """Ours by file name, somewhere else by path: the command comes back here.

        A tee that names a code directory which is no longer there paints
        nothing at all, so the user loses the status line they had. The delegate
        is NOT rewritten: it holds their own command, saved the first time
        round, and it is still the right one.
        """
        settings = {"statusLine": {"type": "command",
                                   "command": "python3 '/elsewhere/old/flightdeck"
                                              "/hooks/context_tee.py'"}}
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, "sh mine.sh\n") as path:
                _, saved = claude.register_statusline(settings)
                # nothing of theirs was saved: our old tee is not a status line
                self.assertEqual(saved, "")
                self.assertEqual(path.read_text().strip(), "sh mine.sh")
        self.assertEqual(settings["statusLine"]["command"],
                         "python3 '%s'" % claude.TEE)

    def test_settings_without_a_status_line_does_not_erase_the_saved_delegate(self):
        # A real case: restoring an old backup of settings leaves statusLine
        # unset; overwriting the delegate with nothing would lose the user's
        # status line for good.
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, "sh mine.sh\n") as path:
                (out, saved), text = run(claude.register_statusline, {})
                self.assertIn("context_tee.py", out["statusLine"]["command"])
                self.assertEqual(saved, "")
                self.assertEqual(path.read_text().strip(), "sh mine.sh")
                self.assertIn("keeping", text)

    def test_with_neither_it_writes_an_empty_delegate(self):
        settings = {}
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp) as path:
                _, saved = claude.register_statusline(settings)
                self.assertEqual(saved, "")
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text().strip(), "")
        self.assertIn("context_tee.py", settings["statusLine"]["command"])


class TestRegisterHooks(unittest.TestCase):
    """Which entries end up in `settings["hooks"]` -- the real file untouched.

    The delicate one is `PreToolUse`: it is the only one with a **matcher** (by
    tool name) and it is where a user's own guards live, which must never be
    touched.
    """

    # What a user's PreToolUse list looks like before we arrive.
    GUARD = {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "python3 ~/bash-guard.py"}]}

    def _ours_in_pretool(self, settings):
        return [e for e in settings["hooks"].get("PreToolUse", [])
                if claude.has_our_hook([e])]

    def test_it_registers_the_six_plus_pretooluse_with_its_matcher(self):
        settings = claude.register_hooks({})
        for event in claude.EVENTS:
            self.assertEqual(len([e for e in settings["hooks"][event]
                                  if claude.has_our_hook([e])]), 1, event)
        ours = self._ours_in_pretool(settings)
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["matcher"], claude.PRETOOL_MATCHER)
        self.assertIn("AskUserQuestion", claude.PRETOOL_MATCHER)
        self.assertIn("ExitPlanMode", claude.PRETOOL_MATCHER)

    def test_the_users_own_pretooluse_hooks_are_untouched(self):
        settings = claude.register_hooks({"hooks": {"PreToolUse": [dict(self.GUARD)]}})
        self.assertIn(self.GUARD, settings["hooks"]["PreToolUse"])
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)  # theirs and ours

    def test_it_is_idempotent(self):
        settings = claude.register_hooks({"hooks": {"PreToolUse": [dict(self.GUARD)]}})
        before = json.dumps(settings, sort_keys=True)
        claude.register_hooks(settings)
        self.assertEqual(json.dumps(settings, sort_keys=True), before)

    def test_a_missing_matcher_is_fixed_in_place_not_duplicated(self):
        """If the list of tools that ask ever changes, the installer has to
        bring it up to date -- not leave the old one and not add a second.

        And it guards the worst case: our entry WITHOUT a matcher would fire on
        EVERY tool of EVERY session.
        """
        settings = claude.register_hooks({})
        del self._ours_in_pretool(settings)[0]["matcher"]
        claude.register_hooks(settings)
        ours = self._ours_in_pretool(settings)
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["matcher"], claude.PRETOOL_MATCHER)

    def test_a_different_matcher_is_brought_up_to_date_too(self):
        """The sibling of the one above, and not the same case: there the
        matcher is MISSING, here it exists but has gone stale (a shorter list of
        tools). It is the real case the day `PRETOOL_MATCHER` grows, and without
        this test a `not in` check instead of the comparison would pass the
        suite leaving the matcher stale for ever.
        """
        settings = claude.register_hooks({})
        self._ours_in_pretool(settings)[0]["matcher"] = "AskUserQuestion"
        claude.register_hooks(settings)
        ours = self._ours_in_pretool(settings)
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["matcher"], claude.PRETOOL_MATCHER)

    def test_the_six_carry_no_matcher(self):
        """A matcher on `Stop` or `SessionStart` means nothing: there is no tool
        to match there, and putting one might stop the hook firing at all."""
        settings = claude.register_hooks({})
        for event in claude.EVENTS:
            for entry in settings["hooks"][event]:
                self.assertNotIn("matcher", entry, event)

    def test_the_command_carries_the_event_and_its_timeout(self):
        settings = claude.register_hooks({})
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertTrue(hook["command"].endswith(" PreToolUse"), hook["command"])
        self.assertIn(str(Path("flightdeck") / "hooks" / "session_hook.py"),
                      hook["command"])
        # the path is QUOTED, as codex's and agy's are: a space in the install
        # path would silently break every hook
        self.assertEqual(hook["command"], "python3 '%s' PreToolUse" % claude.HOOK)
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], 5)


class TestStaleEntriesAreRepointed(unittest.TestCase):
    """Entries of ours naming ANOTHER code directory are brought back here.

    They are ours by file name, so they were never duplicated; but the path they
    carry runs nothing, so every hook fails in silence and the menu goes on
    painting a session that stopped moving hours ago. The doctor already calls
    that `✗` and tells the user to run `flightdeck install` — this is what makes
    that sentence true. It is what codex's installer has always done
    (`hooks_with_flightdeck`, `register_notify`'s "moved"); Claude Code was the
    odd one out, and it is the tool everybody has.

    It is not a corner case: it is the ordinary path from a checkout to a
    released build (install from the checkout, then through `install.sh` into
    `<data>/current`) and anyone's who moves `XDG_DATA_HOME`.
    """

    OLD = "/elsewhere/old-copy/flightdeck/flightdeck/hooks"

    # A user's own guard, on the event ours shares with it.
    GUARD = {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "python3 ~/bash-guard.py"}]}

    def _old_entry(self, event, matcher=None):
        entry = {"hooks": [{"type": "command", "timeout": 5,
                            "command": "python3 '%s/session_hook.py' %s"
                                       % (self.OLD, event)}]}
        return {"matcher": matcher, **entry} if matcher else entry

    def _settings(self):
        """The whole of a past installation, pointing somewhere else."""
        hooks = {event: [self._old_entry(event)] for event in claude.EVENTS}
        hooks["PreToolUse"] = [dict(self.GUARD),
                               self._old_entry("PreToolUse", claude.PRETOOL_MATCHER)]
        return {"model": "opus", "hooks": hooks,
                "statusLine": {"type": "command",
                               "command": "python3 '%s/context_tee.py'" % self.OLD}}

    def test_register_hooks_drops_the_old_entry_and_puts_a_fresh_one_in(self):
        settings = claude.register_hooks(self._settings())
        for event in claude.EVENTS + ["PreToolUse"]:
            ours = [e for e in settings["hooks"][event] if claude.has_our_hook([e])]
            self.assertEqual(len(ours), 1, event)   # replaced, never doubled
            self.assertIn(str(claude.HOOK), ours[0]["hooks"][0]["command"], event)
            self.assertNotIn(self.OLD, json.dumps(settings["hooks"][event]), event)
        # theirs is not ours to move
        self.assertIn(self.GUARD, settings["hooks"]["PreToolUse"])

    def test_install_repoints_every_entry_and_the_tee(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self._settings()) as path, \
                    state_at(tmp + "/state", "sh mine.sh\n") as delegate:
                before = delegate.read_bytes()
                changed, text = run(claude.install)
                written = json.loads(path.read_text())
                state = claude.installed_state(written)
                self.assertEqual(state["stale"], [])
                self.assertEqual(state["missing"], [])
                self.assertEqual(sorted(state["live"]),
                                 sorted(claude.EVENTS + ["PreToolUse"]))
                self.assertEqual(state["status_line"], "ours")
                self.assertEqual(state["matcher"], claude.PRETOOL_MATCHER)
                # the delegate holds THEIR command and our old tee is not one:
                # it must come through the repair byte for byte
                self.assertEqual(delegate.read_bytes(), before)
                self.assertEqual(written["model"], "opus")
                self.assertIn("repointed", " ".join(changed))
                self.assertIn("repointed", text)

    def test_a_second_pass_writes_nothing_and_backs_nothing_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self._settings()) as path, \
                    state_at(tmp + "/state", "sh mine.sh\n"):
                run(claude.install)
                snapshot = path.read_text()
                backups = len(list(path.parent.glob("*" + BACKUP_MARK + "*")))
                changed, text = run(claude.install)
                self.assertEqual(changed, [])
                self.assertEqual(path.read_text(), snapshot)
                self.assertIn("Nothing to do", text)
                self.assertEqual(len(list(path.parent.glob("*" + BACKUP_MARK + "*"))),
                                 backups)

    def test_the_doctors_fix_line_now_tells_the_truth(self):
        """The doctor says a stale hook is fixed by `flightdeck install`."""
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self._settings()), \
                    state_at(tmp + "/state", "sh mine.sh\n"):
                self.assertTrue(claude.installed_state()["stale"])
                run(claude.install)
                self.assertEqual(claude.installed_state()["stale"], [])


class TestLostDelegateWarning(unittest.TestCase):
    """The warning is about a LOST file, not about an empty one.

    An empty delegate is what the installer writes for somebody who had no
    status line of their own (`register_statusline`), which is most people on
    their first run. Warning about it meant an alarm on every `install`, every
    `update` and every `status`, telling them a line they never had was gone.
    The file having DISAPPEARED is the real loss: it is the only copy of their
    command anywhere, and it is what `uninstall` puts back.
    """

    TEE = {"statusLine": {"type": "command", "command": "python3 /x/context_tee.py"}}

    def test_it_shouts_when_the_tee_is_in_and_the_file_has_gone(self):
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp) as delegate:
            _, text = run(claude.warn_lost_delegate, self.TEE)
            self.assertFalse(delegate.exists())
            self.assertIn(str(delegate), text)
        self.assertIn("⚠", text)

    def test_an_empty_delegate_is_what_the_installer_writes_so_it_is_quiet(self):
        """`register_statusline` writes an empty file for a user with no status
        line at all: warning about it is warning about nothing."""
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp, ""):
            _, text = run(claude.warn_lost_delegate, self.TEE)
        self.assertEqual(text, "")

    def test_a_blank_delegate_is_quiet_too(self):
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp, "\n"):
            _, text = run(claude.warn_lost_delegate, self.TEE)
        self.assertEqual(text, "")

    def test_a_fresh_install_for_someone_with_no_status_line_says_nothing(self):
        """The whole point: this is most people's first run."""
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"model": "opus"}), state_at(tmp + "/state"):
                _, text = run(claude.install)
        self.assertNotIn("⚠", text)

    def test_the_consequence_is_worded_for_the_mode_in_force(self):
        """Three modes, three different things lost -- and `own` loses nothing
        today, only what `uninstall` would have put back."""
        said = {}
        for mode in claude.MODES:
            with tempfile.TemporaryDirectory() as tmp:
                with state_at(tmp, config_json={"statusline": {"claude": mode}}):
                    _, said[mode] = run(claude.warn_lost_delegate, self.TEE)
        self.assertIn("minimal line", said["wrap"])       # the tee's own Ctx NN%
        self.assertNotIn("minimal line", said["stack"])   # ours is still painted
        self.assertNotIn("minimal line", said["own"])
        self.assertIn("uninstall", said["own"])
        self.assertEqual(len({text for text in said.values()}), 3)

    def test_it_stays_quiet_when_there_is_a_delegate(self):
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp, "sh mine.sh\n"):
            _, text = run(claude.warn_lost_delegate, self.TEE)
        self.assertEqual(text, "")

    def test_it_stays_quiet_when_the_tee_is_not_even_installed(self):
        with tempfile.TemporaryDirectory() as tmp, state_at(tmp):
            _, text = run(claude.warn_lost_delegate,
                          {"statusLine": {"command": "sh mine.sh"}})
        self.assertEqual(text, "")


class TestTheDelegateIsTheFileTheTeeReads(unittest.TestCase):
    """The installer saves the status line where the tee looks for it.

    Two modules, one file name. If they ever disagree the tee finds no delegate
    and the user's status line goes silently missing, which is the one failure
    this whole delegate dance exists to prevent.
    """

    def test_both_sides_name_the_same_file(self):
        from flightdeck.hooks import context_tee
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}):
                self.assertEqual(claude.delegate_file(),
                                 context_tee._delegate_file())


class TestInstall(unittest.TestCase):

    def _install(self, tmp, settings):
        with settings_at(tmp, settings) as path, state_at(tmp + "/state"):
            result, text = run(claude.install)
            return result, text, path

    def test_it_registers_the_hooks_and_the_tee_and_leaves_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = {"statusLine": {"type": "command", "command": "sh mine.sh"},
                      "model": "opus"}
            changed, text, path = self._install(tmp, before)
            written = json.loads(path.read_text())
            self.assertEqual(written["model"], "opus")
            for event in claude.EVENTS + ["PreToolUse"]:
                self.assertTrue(claude.has_our_hook(written["hooks"][event]), event)
            self.assertIn("context_tee.py", written["statusLine"]["command"])
            self.assertTrue(changed)
            self.assertIn("backup", text)
            backups = list(path.parent.glob("*" + BACKUP_MARK + "*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), before)

    def test_a_second_pass_writes_nothing_and_backs_nothing_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"model": "opus"}) as path, state_at(tmp + "/s"):
                run(claude.install)
                snapshot = path.read_text()
                backups = len(list(path.parent.glob("*" + BACKUP_MARK + "*")))
                changed, text = run(claude.install)
                self.assertEqual(changed, [])
                self.assertEqual(path.read_text(), snapshot)
                self.assertIn("Nothing to do", text)
                # no SECOND backup: the one from the first pass is all there is
                self.assertEqual(len(list(path.parent.glob("*" + BACKUP_MARK + "*"))),
                                 backups)

    def test_a_stale_matcher_reaches_the_FILE_not_just_the_dict(self):
        """The one change `install()` can make without adding anything.

        `register_hooks` fixes the matcher in the dict it is handed; if the
        condition that turns that into a write ever stops firing, the matcher is
        corrected in memory, "Nothing to do" is printed, and the file on disk
        keeps the stale matcher for ever. Both ways of going stale are covered:
        a matcher that is MISSING (fires on every tool) and one that is merely
        out of date.
        """
        for label, break_it in (
                ("missing", lambda entry: entry.pop("matcher")),
                ("different", lambda entry: entry.update(matcher="AskUserQuestion"))):
            with self.subTest(matcher=label), tempfile.TemporaryDirectory() as tmp:
                with settings_at(tmp, {"model": "opus"}) as path, state_at(tmp + "/s"):
                    run(claude.install)
                    settings = json.loads(path.read_text())
                    break_it(claude.our_entry(settings["hooks"]["PreToolUse"]))
                    path.write_text(json.dumps(settings, indent=2) + "\n")

                    changed, _ = run(claude.install)

                    self.assertIn("matcher", " ".join(changed))
                    on_disk = claude.our_entry(
                        json.loads(path.read_text())["hooks"]["PreToolUse"])
                    self.assertEqual(on_disk["matcher"], claude.PRETOOL_MATCHER)

    def test_with_no_settings_file_it_creates_one(self):
        # `flightdeck install` runs on machines where Claude Code has never been
        # started: a missing settings.json is a normal state, not a failure.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp) as path, state_at(tmp + "/s"):
                run(claude.install)
                self.assertTrue(claude.has_our_hook(
                    json.loads(path.read_text())["hooks"]["Stop"]))

    def test_a_broken_settings_file_is_never_overwritten(self):
        # It holds the user's whole Claude Code configuration (permissions,
        # environment, model). A stray comma is something they can still fix by
        # hand; installing on top of it would not be.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, "{ not json at all") as path, state_at(tmp + "/s"):
                with self.assertRaises(config.ConfigError):
                    claude.install()
                self.assertEqual(path.read_text(), "{ not json at all")


class TestStatus(unittest.TestCase):

    def test_it_lists_the_events_the_status_line_and_the_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": {"type": "command",
                                                  "command": "sh mine.sh"}}), \
                    state_at(tmp + "/s"):
                run(claude.install)
                _, text = run(claude.status)
        for event in claude.EVENTS + ["PreToolUse"]:
            self.assertIn(event, text)
        self.assertIn(claude.PRETOOL_MATCHER, text)   # which tools ours watches
        self.assertIn("context_tee.py", text)
        self.assertIn("sh mine.sh", text)             # the delegate, named

    def test_with_nothing_installed_it_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {}), state_at(tmp + "/s"):
                _, text = run(claude.status)
        self.assertIn("—", text)


class TestUninstall(unittest.TestCase):
    """Takes out only what we put in, and puts the status line back."""

    FOREIGN = {
        "hooks": {"PreToolUse": [{"matcher": "Bash",
                                  "hooks": [{"type": "command",
                                             "command": "python3 ~/guard.py"}]}]},
        "statusLine": {"type": "command", "command": "sh mine.sh"},
        "model": "opus",
    }

    def test_a_round_trip_leaves_a_foreign_config_byte_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.FOREIGN) as path, state_at(tmp + "/s"):
                before = path.read_text()
                run(claude.install)
                self.assertNotEqual(path.read_text(), before)
                removed, _ = run(claude.uninstall)
                self.assertEqual(path.read_text(), before)
                self.assertTrue(removed)

    def test_the_users_own_hooks_survive_on_the_same_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.FOREIGN) as path, state_at(tmp + "/s"):
                run(claude.install)
                run(claude.uninstall)
                left = json.loads(path.read_text())["hooks"]["PreToolUse"]
                self.assertEqual(left, self.FOREIGN["hooks"]["PreToolUse"])

    def test_it_restores_the_status_line_from_the_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.FOREIGN) as path, state_at(tmp + "/s"):
                run(claude.install)
                removed, text = run(claude.uninstall)
                self.assertEqual(json.loads(path.read_text())["statusLine"],
                                 {"type": "command", "command": "sh mine.sh"})
                self.assertIn("sh mine.sh", " ".join(removed))
                self.assertIn("sh mine.sh", text)

    def test_with_no_delegate_the_status_line_key_goes_away(self):
        # Nothing was there before us, so nothing is what goes back: leaving the
        # tee's command behind would point Claude Code at code being removed.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"model": "opus"}) as path, state_at(tmp + "/s"):
                run(claude.install)
                run(claude.uninstall)
                self.assertEqual(json.loads(path.read_text()), {"model": "opus"})

    def test_without_flightdeck_it_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.FOREIGN) as path, state_at(tmp + "/s"):
                before = path.read_text()
                removed, text = run(claude.uninstall)
                self.assertEqual(removed, [])
                self.assertEqual(path.read_text(), before)
                self.assertIn("Nothing to remove", text)
                self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_with_no_settings_file_it_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp) as path, state_at(tmp + "/s"):
                removed, _ = run(claude.uninstall)
                self.assertEqual(removed, [])
                self.assertFalse(path.exists())


class TestThePruningRuleKeepsWhatItDidNotEmpty(unittest.TestCase):
    """`_without_entries`, the rule `uninstall` and the migration share.

    An event list left empty, and a `hooks` key left empty, were ours to create
    and go with the entries -- but only when this pass is what emptied them. A
    `"hooks": {}` that was already in the file is the user's, and dropping it
    silently changes a file we were asked not to touch.
    """

    def test_an_empty_hooks_key_it_did_not_empty_is_kept(self):
        settings = {"hooks": {}, "model": "opus"}
        out, removed = claude._without_entries(settings, lambda entry: True)
        self.assertEqual(removed, [])
        self.assertEqual(out, {"hooks": {}, "model": "opus"})

    def test_an_event_holding_nothing_of_ours_comes_back_untouched(self):
        theirs = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                 "command": "theirs.sh"}]}]}}
        out, removed = claude._without_entries(theirs, lambda entry: False)
        self.assertEqual(removed, [])
        self.assertEqual(out, theirs)

    def test_and_a_key_this_pass_did_empty_still_goes(self):
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                   "command": "ours.py"}]}]}}
        out, removed = claude._without_entries(settings, lambda entry: True)
        self.assertEqual(removed, ["Stop"])
        self.assertEqual(out, {})


class TestTheModeInstallChooses(unittest.TestCase):
    """Which line the user ends up with when they never said."""

    def _install(self, tmp, settings, mode=None, config_json=None):
        with settings_at(tmp, settings), state_at(tmp + "/s", config_json=config_json):
            _, text = run(claude.install, mode)
            return written_config(), text

    def test_with_no_status_line_of_their_own_the_default_is_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            written, text = self._install(tmp, {"model": "opus"})
        self.assertEqual(written["statusline"]["claude"], "own")
        self.assertIn("own", text)

    def test_with_a_status_line_of_their_own_the_default_keeps_theirs(self):
        # The user does not lose the line they have unless they choose
        # ours. `wrap` is the tee passing their command through untouched.
        with tempfile.TemporaryDirectory() as tmp:
            written, _ = self._install(
                tmp, {"statusLine": {"type": "command", "command": "sh mine.sh"}})
        self.assertEqual(written["statusline"]["claude"], "wrap")

    def test_an_asked_for_mode_wins_over_both_defaults(self):
        for settings in ({}, {"statusLine": "sh mine.sh"}):
            with tempfile.TemporaryDirectory() as tmp:
                written, _ = self._install(tmp, settings, mode="stack")
            self.assertEqual(written["statusline"]["claude"], "stack", settings)

    def test_a_mode_already_chosen_is_never_overwritten_by_a_reinstall(self):
        # `flightdeck update` runs install again. Choosing `own` and then being
        # put back on `wrap` by an update is the bug this pins.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}), state_at(tmp + "/s"):
                run(claude.install, "own")
                run(claude.install)
                self.assertEqual(written_config()["statusline"]["claude"], "own")

    def test_a_mode_in_the_config_file_survives_a_fresh_install(self):
        # Uninstalling does not clear config.json, so the choice made last time
        # is still in there and is still theirs.
        with tempfile.TemporaryDirectory() as tmp:
            written, _ = self._install(tmp, {"statusLine": "sh mine.sh"},
                                       config_json={"statusline": {"claude": "own"}})
        self.assertEqual(written["statusline"]["claude"], "own")

    def test_the_second_pass_writes_nothing_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"model": "opus"}) as path, state_at(tmp + "/s"):
                run(claude.install)
                config_before = config.config_path().read_text()
                settings_before = path.read_text()
                changed, text = run(claude.install)
                self.assertEqual(changed, [])
                self.assertIn("Nothing to do", text)
                self.assertEqual(config.config_path().read_text(), config_before)
                self.assertEqual(path.read_text(), settings_before)

    def test_the_mode_alone_is_written_even_with_the_hooks_all_in_place(self):
        # An installation made before modes existed: settings.json needs
        # nothing, config.json still has no mode, and "nothing to do" would
        # leave the tee reading a key that is not there.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"model": "opus"}) as path, state_at(tmp + "/s"):
                run(claude.install)
                config.config_path().unlink()
                for stale in path.parent.glob("*" + BACKUP_MARK + "*"):
                    stale.unlink()
                before = path.read_text()
                changed, _ = run(claude.install)
                self.assertEqual(written_config()["statusline"]["claude"], "own")
                self.assertEqual(path.read_text(), before)   # settings untouched
                self.assertTrue(changed)
                self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])


    def test_a_broken_config_stops_the_install_before_settings_is_touched(self):
        # The mode is written first for this reason: `config.update` refuses a
        # file it cannot parse, and someone with a stray comma in config.json
        # must not end up with our tee painting the default line over the one
        # they had. They fix the typo and run it again.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}) as path, \
                    state_at(tmp + "/s"):
                config.config_path().parent.mkdir(parents=True, exist_ok=True)
                config.config_path().write_text('{"menu_port": 43000,,}')
                before = path.read_text()
                with self.assertRaises(config.ConfigError):
                    claude.install()
                self.assertEqual(path.read_text(), before)


class TestSetMode(unittest.TestCase):

    def test_it_writes_the_key_and_gives_back_the_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp):
                self.assertEqual(claude.set_mode("stack"), "stack")
                self.assertEqual(written_config(), {"statusline": {"claude": "stack"}})

    def test_it_leaves_the_other_tools_modes_alone(self):
        # `config.update` replaces top-level keys; the whole `statusline`
        # section going in as `{"claude": ...}` would wipe agy's and codex's.
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, config_json={"statusline": {"agy": "wrap",
                                                          "codex": "items"},
                                            "menu_port": 43000}):
                claude.set_mode("own")
                written = written_config()
        self.assertEqual(written["statusline"],
                         {"agy": "wrap", "codex": "items", "claude": "own"})
        self.assertEqual(written["menu_port"], 43000)

    def test_a_mode_that_does_not_exist_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp):
                with self.assertRaises(ValueError) as caught:
                    claude.set_mode("fancy")
                self.assertFalse(config.config_path().exists())
        for word in claude.MODES:
            self.assertIn(word, str(caught.exception))

    def test_a_broken_config_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp):
                path = config.config_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"menu_port": 43000,,}')
                with self.assertRaises(config.ConfigError):
                    claude.set_mode("own")
                self.assertEqual(path.read_text(), '{"menu_port": 43000,,}')


class TestConfiguredMode(unittest.TestCase):

    def test_with_no_config_file_it_is_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp):
                self.assertEqual(claude.configured_mode(),
                                 config.DEFAULTS["statusline"]["claude"])

    def test_a_mode_spelled_wrong_degrades_to_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, config_json={"statusline": {"claude": "fancy"}}):
                self.assertEqual(claude.configured_mode(),
                                 config.DEFAULTS["statusline"]["claude"])

    def test_it_reads_what_the_file_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            with state_at(tmp, config_json={"statusline": {"claude": "stack"}}):
                self.assertEqual(claude.configured_mode(), "stack")


class TestTheModeIsTheKeyTheTeeReads(unittest.TestCase):
    """The installer writes it and the tee reads it: one key, one spelling.

    Two modules name `statusline.claude` from opposite ends. If they ever drift
    apart -- a rename on one side, a fourth mode on the other -- `--mode stack`
    would report success and the bar would keep painting something else, with
    nothing anywhere to say why.
    """

    def test_what_set_mode_writes_is_what_the_tee_paints_by(self):
        from flightdeck.hooks import context_tee
        for mode in claude.MODES:
            with tempfile.TemporaryDirectory() as tmp:
                with state_at(tmp):
                    claude.set_mode(mode)
                    self.assertEqual(context_tee.statusline_mode(), mode)

    def test_both_sides_know_the_same_modes(self):
        from flightdeck.hooks import context_tee
        self.assertEqual(tuple(claude.MODES), tuple(context_tee.MODES))
        self.assertEqual(sorted(claude.MODE_HELP), sorted(claude.MODES))


class TestRestore(unittest.TestCase):
    """`flightdeck statusline --restore`: their own line back in settings.json."""

    def test_a_round_trip_puts_the_command_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = {"statusLine": {"type": "command", "command": "sh mine.sh"},
                      "model": "opus"}
            with settings_at(tmp, before) as path, state_at(tmp + "/s"):
                run(claude.install)
                restored, text = run(claude.restore)
                written = json.loads(path.read_text())
        self.assertEqual(restored, "sh mine.sh")
        self.assertEqual(written["statusLine"],
                         {"type": "command", "command": "sh mine.sh"})
        self.assertEqual(written["model"], "opus")
        self.assertIn("sh mine.sh", text)
        # The hooks are not part of the status line and stay registered.
        self.assertTrue(claude.has_our_hook(written["hooks"]["Stop"]))

    def test_the_delegate_is_kept_so_reinstalling_finds_it_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}), \
                    state_at(tmp + "/s") as delegate:
                run(claude.install)
                run(claude.restore)
                self.assertEqual(delegate.read_text().strip(), "sh mine.sh")
                # and installing again wraps the very same line
                run(claude.install)
                self.assertEqual(claude.saved_delegate(), "sh mine.sh")

    def test_it_leaves_a_backup_of_the_file_it_rewrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}) as path, \
                    state_at(tmp + "/s"):
                run(claude.install)
                for stale in path.parent.glob("*" + BACKUP_MARK + "*"):
                    stale.unlink()
                run(claude.restore)
                self.assertEqual(len(list(path.parent.glob("*" + BACKUP_MARK + "*"))), 1)

    def test_it_does_not_touch_the_mode(self):
        # Deliberate: the tee is no longer the command, so the mode is dormant
        # and there is nothing to record. Reinstalling honours the same choice.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}), state_at(tmp + "/s"):
                run(claude.install, "own")
                run(claude.restore)
                self.assertEqual(written_config()["statusline"]["claude"], "own")

    def test_with_nothing_saved_it_says_so_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {}) as path, state_at(tmp + "/s"):
                run(claude.install)          # no status line of theirs to save
                before = path.read_text()
                restored, text = run(claude.restore)
                self.assertEqual(path.read_text(), before)
        self.assertEqual(restored, "")
        self.assertIn("uninstall", text)     # what DOES take ours out

    def test_with_the_tee_not_registered_it_says_so_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}) as path, \
                    state_at(tmp + "/s", delegate="sh other.sh\n"):
                before = path.read_text()
                restored, text = run(claude.restore)
                self.assertEqual(path.read_text(), before)
        self.assertEqual(restored, "")
        self.assertIn("not registered", text)

    def test_a_broken_settings_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, "{ not json at all") as path, \
                    state_at(tmp + "/s", delegate="sh mine.sh\n"):
                with self.assertRaises(config.ConfigError):
                    claude.restore()
                self.assertEqual(path.read_text(), "{ not json at all")


class TestStatusSaysWhichLine(unittest.TestCase):

    def test_it_names_the_mode_and_what_it_means(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": "sh mine.sh"}), state_at(tmp + "/s"):
                run(claude.install, "stack")
                _, text = run(claude.status)
        self.assertIn("stack", text)
        self.assertIn(claude.MODE_HELP["stack"], text)

    def test_with_no_config_file_it_names_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {}), state_at(tmp + "/s"):
                _, text = run(claude.status)
        self.assertIn(config.DEFAULTS["statusline"]["claude"], text)


class TestTheStatusLineObjectIsKeptWhole(unittest.TestCase):
    """Keys of theirs inside `statusLine` survive install, restore and uninstall.

    The setting carries more than `command`: the documented `padding`, which
    people set to 0 to take the left margin off. The delegate saves the command
    alone, so an object replaced wholesale loses the rest of it to the backup
    file and nobody notices until they wonder where their margin went.
    """

    PADDED = {"statusLine": {"type": "command", "command": "sh mine.sh",
                             "padding": 0},
              "model": "opus"}

    def test_install_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.PADDED) as path, state_at(tmp + "/s"):
                run(claude.install)
                line = json.loads(path.read_text())["statusLine"]
        self.assertEqual(line["padding"], 0)
        self.assertIn("context_tee.py", line["command"])

    def test_restore_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.PADDED) as path, state_at(tmp + "/s"):
                run(claude.install)
                run(claude.restore)
                line = json.loads(path.read_text())["statusLine"]
        self.assertEqual(line, {"type": "command", "command": "sh mine.sh",
                                "padding": 0})

    def test_uninstall_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, self.PADDED) as path, state_at(tmp + "/s"):
                before = path.read_text()
                run(claude.install)
                run(claude.uninstall)
                self.assertEqual(path.read_text(), before)

    def test_with_no_delegate_only_the_command_goes(self):
        # Someone who had `padding` and no command at all: uninstall takes out
        # the two keys that are ours to decide and leaves the object with what
        # is left, instead of deleting a setting we never wrote.
        with tempfile.TemporaryDirectory() as tmp:
            with settings_at(tmp, {"statusLine": {"padding": 0}}) as path, \
                    state_at(tmp + "/s"):
                run(claude.install)
                run(claude.uninstall)
                self.assertEqual(json.loads(path.read_text()),
                                 {"statusLine": {"padding": 0}})


if __name__ == "__main__":
    unittest.main()
