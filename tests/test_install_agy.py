"""The installer for agy (Antigravity): a NAMED hook in its shared hooks.json,
and the status line command in its own settings.json.

Nothing here touches the real `~/.gemini/config/hooks.json` or
`~/.gemini/antigravity-cli/settings.json`: both are live files of the user's
(other hooks of theirs can sit next to ours in the first; the second holds their
colour scheme and their trusted workspaces). Flightdeck's own config.json and
state directory are temporary too, so a test never writes a mode into the
config of whoever is running the suite.
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
from flightdeck.install import BACKUP_MARK, agy

# agy's settings.json as measured, minus the status line.
MEASURED_SETTINGS = {"colorScheme": "tokyo night",
                     "trustedWorkspaces": ["/home/user/projects/demo"]}


@contextlib.contextmanager
def agy_files(hooks=None, settings=None):
    """Point every file the installer writes at throwaway paths.

    -> (hooks.json path, settings.json path). `install()` now writes three
    files (agy's hooks.json, agy's settings.json and Flightdeck's config.json)
    and none of them may be the real one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        hooks_path = Path(tmp) / "config" / "hooks.json"
        settings_path = Path(tmp) / "antigravity-cli" / "settings.json"
        for path, content in ((hooks_path, hooks), (settings_path, settings)):
            if content is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        env = {"FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json"),
               "FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}
        with mock.patch.object(agy, "HOOKS_JSON", hooks_path), \
                mock.patch.object(agy, "SETTINGS_PATH", settings_path), \
                mock.patch.dict(os.environ, env):
            yield hooks_path, settings_path


@contextlib.contextmanager
def hooks_json_at(content=None):
    """`agy_files` for the tests that only care about hooks.json. -> its path."""
    with agy_files(hooks=content) as (hooks_path, _settings):
        yield hooks_path


def run(fn, *args, **kwargs):
    """Call fn swallowing what it prints. -> (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def installation_pointing_at(path):
    """Our key with its three events, but calling ANOTHER session_hook.py (same
    flat/grouped split as the good one)."""
    d = agy.hooks_with_flightdeck({})
    for event in agy.EVENTS:
        handler = {"type": "command",
                   "command": "exec python3 '%s' %s --tool agy" % (path, event),
                   "timeout": 5}
        d[agy.NAME][event] = ([{"matcher": "*", "hooks": [handler]}]
                              if event in agy._GROUPED else [handler])
    return d


class TestHooksWithFlightdeck(unittest.TestCase):

    def test_it_adds_our_named_hook_without_touching_the_others(self):
        foreign = {"PreToolUse": [{"matcher": "run_command",
                                   "hooks": [{"command": "./lint.sh"}]}]}
        d = agy.hooks_with_flightdeck({"lint": foreign})
        self.assertEqual(d["lint"], foreign)
        ours = d[agy.NAME]
        self.assertTrue(ours["enabled"])
        # PreInvocation and Stop are FLAT lists of handlers; PostToolUse is
        # grouped under a matcher
        self.assertEqual(ours["PreInvocation"], [agy.handler("PreInvocation")])
        self.assertEqual(ours["Stop"], [agy.handler("Stop")])
        self.assertEqual(ours["PostToolUse"],
                         [{"matcher": "*", "hooks": [agy.handler("PostToolUse")]}])
        # its answer must carry a `decision`: not ours to make
        self.assertNotIn("PreToolUse", ours)

    def test_the_handler_is_exec_python_with_an_absolute_path_and_a_timeout(self):
        handler = agy.handler("Stop")
        self.assertEqual(handler["type"], "command")
        self.assertTrue(handler["command"].startswith("exec python3 '"))
        self.assertIn(str(agy.HOOK), handler["command"])
        self.assertIn(str(Path("flightdeck") / "hooks" / "session_hook.py"),
                      handler["command"])
        self.assertTrue(handler["command"].endswith(" Stop --tool agy"))
        self.assertEqual(handler["timeout"], 5)

    def test_reinstalling_does_not_duplicate_and_overwrites_only_ours(self):
        once = agy.hooks_with_flightdeck({})
        stale = dict(once)
        stale[agy.NAME] = {"enabled": False, "Stop": []}
        self.assertEqual(agy.hooks_with_flightdeck(stale), once)
        self.assertEqual(agy.hooks_with_flightdeck(once), once)

    def test_an_odd_input_degrades_to_an_empty_dict(self):
        self.assertIn(agy.NAME, agy.hooks_with_flightdeck(None))
        self.assertIn(agy.NAME, agy.hooks_with_flightdeck(["a list"]))

    def test_it_does_not_mutate_the_dict_it_is_given(self):
        given = {"lint": {"enabled": True}}
        agy.hooks_with_flightdeck(given)
        self.assertEqual(given, {"lint": {"enabled": True}})


class TestInstall(unittest.TestCase):

    def test_with_no_file_it_creates_it_with_its_directory_and_no_backup(self):
        with hooks_json_at() as path:
            _, text = run(agy.install)
            self.assertEqual(json.loads(path.read_text()),
                             agy.hooks_with_flightdeck({}))
            self.assertIn("registered", text)
            self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_foreign_hooks_are_kept_and_a_backup_is_left(self):
        before = {"lint": {"enabled": True,
                           "Stop": [{"type": "command", "command": "./lint.sh"}]}}
        with hooks_json_at(json.dumps(before)) as path:
            run(agy.install)
            written = json.loads(path.read_text())
            self.assertEqual(written["lint"], before["lint"])
            self.assertIn(agy.NAME, written)
            backups = list(path.parent.glob("hooks.json" + BACKUP_MARK + "*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), before)

    def test_a_second_pass_neither_rewrites_nor_backs_up_again(self):
        with hooks_json_at() as path:
            run(agy.install)
            snapshot = path.read_text()
            _, text = run(agy.install)
            self.assertEqual(path.read_text(), snapshot)
            self.assertIn("already installed", text)
            self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_an_unreadable_file_is_refused_and_left_alone(self):
        # It holds the OTHER named hooks the user has. Read as empty it would be
        # rebuilt with nothing but our key in it, and theirs would survive only
        # in the timestamped backup. The doctor already tells them to fix it by
        # hand, so the installer refuses in one line and writes nothing.
        with hooks_json_at("{ this is not json") as path:
            with self.assertRaises(config.ConfigError) as caught:
                run(agy.install)
            self.assertIn(str(path), str(caught.exception))
            self.assertEqual(path.read_text(), "{ this is not json")
            self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])


class TestStatus(unittest.TestCase):

    def test_with_no_file_it_says_so_with_the_path(self):
        with hooks_json_at() as path:
            _, text = run(agy.status)
            self.assertIn("missing or unreadable", text)
            self.assertIn(str(path), text)

    def test_installed_it_lists_the_events(self):
        with hooks_json_at():
            run(agy.install)
            _, text = run(agy.status)
            for event in agy.EVENTS:
                self.assertIn(event, text)
            self.assertNotIn("DISABLED", text)
            self.assertNotIn("STALE", text)

    def test_stale_pointing_elsewhere_does_not_count_as_installed(self):
        # The code moved (or someone edited the command by hand): the key still
        # has its three events, but every hook would fail in silence.
        elsewhere = installation_pointing_at("/somewhere/else/session_hook.py")
        with hooks_json_at(json.dumps(elsewhere)):
            _, text = run(agy.status)
            self.assertIn("STALE", text)
            self.assertIn("with Flightdeck in: none", text)
            for event in agy.EVENTS:
                self.assertIn(event, text)   # WHICH ones are stale is said

    def test_a_partial_stale_separates_the_good_from_the_bad(self):
        d = installation_pointing_at("/somewhere/else/session_hook.py")
        d[agy.NAME]["Stop"] = [agy.handler("Stop")]
        with hooks_json_at(json.dumps(d)):
            _, text = run(agy.status)
            self.assertIn("with Flightdeck in: Stop (STALE", text)
            self.assertIn("PreInvocation, PostToolUse", text)

    def test_foreign_hooks_but_not_ours_says_it_is_missing(self):
        with hooks_json_at(json.dumps({"lint": {"enabled": True}})):
            _, text = run(agy.status)
            self.assertIn("no Flightdeck in it", text)

    def test_disabled_by_hand_is_announced(self):
        d = agy.hooks_with_flightdeck({})
        d[agy.NAME]["enabled"] = False
        with hooks_json_at(json.dumps(d)):
            _, text = run(agy.status)
            self.assertIn("DISABLED", text)


class TestUninstall(unittest.TestCase):

    def test_a_round_trip_leaves_a_foreign_config_byte_equal(self):
        before = json.dumps(
            {"lint": {"enabled": True,
                      "Stop": [{"type": "command", "command": "./lint.sh"}]}},
            indent=2) + "\n"
        with hooks_json_at(before) as path:
            run(agy.install)
            self.assertNotEqual(path.read_text(), before)
            removed, _ = run(agy.uninstall)
            self.assertEqual(path.read_text(), before)
            self.assertIn(agy.NAME, removed)

    def test_a_file_that_was_only_ours_goes_away(self):
        with hooks_json_at() as path:
            run(agy.install)
            run(agy.uninstall)
            self.assertFalse(path.exists())

    def test_without_flightdeck_it_is_a_no_op(self):
        before = json.dumps({"lint": {"enabled": True}}, indent=2) + "\n"
        with hooks_json_at(before) as path:
            removed, text = run(agy.uninstall)
            self.assertEqual(removed, [])
            self.assertEqual(path.read_text(), before)
            self.assertIn("Nothing to remove", text)
            self.assertEqual(list(path.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_a_stale_key_is_removed_too(self):
        # It is still our key, pointing at code that is being taken away: left
        # behind, agy would run a hook that no longer exists on every turn.
        elsewhere = installation_pointing_at("/somewhere/else/session_hook.py")
        with hooks_json_at(json.dumps(elsewhere)) as path:
            removed, _ = run(agy.uninstall)
            self.assertEqual(removed, [agy.NAME])
            self.assertFalse(path.exists())

    def test_with_no_file_it_is_a_no_op(self):
        with hooks_json_at() as path:
            removed, _ = run(agy.uninstall)
            self.assertEqual(removed, [])
            self.assertFalse(path.exists())


# ── the status line ──────────────────────────────────────────────────────────
# Measured: `/statusline <command>` writes
# `{"type": "command", "command": "<sh string>"}` under a `statusLine` key of
# agy's own settings.json -- Claude Code's exact shape -- and
# `/statusline delete` removes the key whole. `stack_with_default` is a key of
# that same object (agy's changelog and its struct tags).
class TestRegisterStatusLine(unittest.TestCase):

    def test_it_writes_the_command_pointing_at_our_hook(self):
        with agy_files():
            settings, previous = agy.register_status_line({}, "own")
        line = settings["statusLine"]
        self.assertEqual(line["type"], "command")
        self.assertTrue(line["command"].startswith("exec python3 '"))
        self.assertIn(str(agy.STATUS_LINE), line["command"])
        self.assertIn(str(Path("flightdeck") / "hooks" / "agy_statusline.py"),
                      line["command"])
        self.assertEqual(previous, "")

    def test_stack_is_the_one_mode_that_asks_agy_to_stack_its_own_line(self):
        with agy_files():
            stacked, _ = agy.register_status_line({}, "stack")
            self.assertIs(stacked["statusLine"]["stack_with_default"], True)
            for mode in ("own", "wrap"):
                # Written as false it would vanish on agy's next save anyway
                # (the field is `omitempty`), and a key that comes and goes on
                # its own reads as somebody having edited the file.
                plain, _ = agy.register_status_line({}, mode)
                self.assertNotIn("stack_with_default", plain["statusLine"])

    def test_a_previous_command_is_saved_and_given_back(self):
        with agy_files() as (_hooks, _settings):
            before = dict(MEASURED_SETTINGS,
                          statusLine={"type": "command", "command": "sh mine.sh"})
            settings, previous = agy.register_status_line(before, "wrap")
            self.assertEqual(previous, "sh mine.sh")
            self.assertEqual(agy.saved_delegate(), "sh mine.sh")
            self.assertIn(str(agy.STATUS_LINE), settings["statusLine"]["command"])

    def test_a_bare_string_counts_as_a_command_too(self):
        with agy_files():
            _settings, previous = agy.register_status_line(
                {"statusLine": "sh mine.sh"}, "own")
            self.assertEqual(previous, "sh mine.sh")
            self.assertEqual(agy.saved_delegate(), "sh mine.sh")

    def test_the_other_settings_are_left_alone(self):
        with agy_files():
            settings, _ = agy.register_status_line(dict(MEASURED_SETTINGS), "own")
        self.assertEqual(settings["colorScheme"], "tokyo night")
        self.assertEqual(settings["trustedWorkspaces"],
                         MEASURED_SETTINGS["trustedWorkspaces"])

    def test_registering_twice_does_not_delegate_to_ourselves(self):
        # The loop that would follow (our line calling our line, for ever) is
        # the one mistake this function exists to avoid.
        with agy_files():
            once, _ = agy.register_status_line(dict(MEASURED_SETTINGS), "own")
            twice, previous = agy.register_status_line(once, "own")
            self.assertEqual(previous, "")
            self.assertEqual(twice["statusLine"]["command"],
                             once["statusLine"]["command"])
            self.assertEqual(agy.saved_delegate(), "")


class TestInstallStatusLine(unittest.TestCase):

    def test_install_registers_it_and_backs_the_file_up(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            _, text = run(agy.install)
            written = json.loads(settings.read_text())
            self.assertIn(str(agy.STATUS_LINE), written["statusLine"]["command"])
            self.assertEqual(written["colorScheme"], "tokyo night")
            self.assertIn("status line", text)
            backups = list(settings.parent.glob("settings.json" + BACKUP_MARK + "*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), MEASURED_SETTINGS)

    def test_a_second_pass_writes_nothing_at_all(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (hooks, settings):
            run(agy.install)
            snapshots = (hooks.read_text(), settings.read_text())
            changed, text = run(agy.install)
            self.assertEqual((hooks.read_text(), settings.read_text()), snapshots)
            self.assertEqual(changed, [])
            self.assertIn("already", text)

    def test_with_no_settings_file_it_creates_one_without_a_backup(self):
        with agy_files() as (_hooks, settings):
            run(agy.install)
            self.assertIn("statusLine", json.loads(settings.read_text()))
            self.assertEqual(list(settings.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_the_default_mode_keeps_a_line_the_user_already_had(self):
        before = dict(MEASURED_SETTINGS,
                      statusLine={"type": "command", "command": "sh mine.sh"})
        with agy_files(settings=json.dumps(before)):
            run(agy.install)
            self.assertEqual(agy.configured_mode(), "wrap")
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)):
            run(agy.install)
            self.assertEqual(agy.configured_mode(), "own")

    def test_a_mode_asked_for_wins_and_a_chosen_one_is_not_undone(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)):
            run(agy.install, "stack")
            self.assertEqual(agy.configured_mode(), "stack")
            run(agy.install)     # `flightdeck update` must not change it back
            self.assertEqual(agy.configured_mode(), "stack")

    def test_an_unknown_mode_is_refused_before_anything_is_written(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)):
            with self.assertRaises(ValueError):
                agy.set_mode("sideways")

    def test_a_settings_file_it_cannot_parse_is_never_overwritten(self):
        # It holds the user's trusted workspaces: a stray comma is something
        # they can fix, a file replaced by ours is not. The hooks still go in.
        with agy_files(settings="{ not json") as (hooks, settings):
            _, text = run(agy.install)
            self.assertEqual(settings.read_text(), "{ not json")
            self.assertIn(agy.NAME, json.loads(hooks.read_text()))
            self.assertIn("status line", text.lower())

    def test_uninstall_restores_the_command_that_was_there(self):
        before = dict(MEASURED_SETTINGS,
                      statusLine={"type": "command", "command": "sh mine.sh"})
        with agy_files(settings=json.dumps(before)) as (_hooks, settings):
            run(agy.install)
            removed, _ = run(agy.uninstall)
            written = json.loads(settings.read_text())
            self.assertEqual(written["statusLine"],
                             {"type": "command", "command": "sh mine.sh"})
            self.assertEqual(written["colorScheme"], "tokyo night")
            self.assertTrue(any("status line" in r for r in removed))

    def test_uninstall_with_nothing_before_us_removes_the_key(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install)
            run(agy.uninstall)
            self.assertEqual(json.loads(settings.read_text()), MEASURED_SETTINGS)

    def test_uninstall_leaves_a_status_line_that_is_not_ours_alone(self):
        before = dict(MEASURED_SETTINGS,
                      statusLine={"type": "command", "command": "sh theirs.sh"})
        with agy_files(settings=json.dumps(before)) as (_hooks, settings):
            run(agy.uninstall)
            self.assertEqual(json.loads(settings.read_text())["statusLine"],
                             before["statusLine"])


class TestSetModeKeepsBothFilesInStep(unittest.TestCase):
    """For agy the mode lives in TWO files: Flightdeck's config.json and, for
    `stack`, a key beside our command in agy's own settings. Written in only one
    of them, both halves of the mistake are silent -- a `stack` that paints like
    `own`, or an `own` with agy still stacking its default line underneath.
    """

    def test_own_to_stack_turns_the_flag_on(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install, "own")
            self.assertNotIn("stack_with_default",
                             json.loads(settings.read_text())["statusLine"])
            run(agy.set_mode, "stack")
            self.assertEqual(agy.configured_mode(), "stack")
            self.assertIs(json.loads(settings.read_text())
                          ["statusLine"]["stack_with_default"], True)

    def test_stack_to_own_turns_it_off_again(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install, "stack")
            run(agy.set_mode, "own")
            self.assertEqual(agy.configured_mode(), "own")
            self.assertNotIn("stack_with_default",
                             json.loads(settings.read_text())["statusLine"])
            # And our command is still the one agy runs: the mode change is not
            # a reinstall and must not disturb anything else.
            self.assertIn(str(agy.STATUS_LINE),
                          json.loads(settings.read_text())["statusLine"]["command"])

    def test_the_settings_are_backed_up_before_the_flag_moves(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install, "own")
            run(agy.set_mode, "stack")
            backups = sorted(settings.parent.glob("settings.json" + BACKUP_MARK + "*"))
            self.assertTrue(backups)
            # By CONTENT and not by count: the backup's name is stamped to the
            # second, so an install and a mode change in the same second share
            # one file. What matters is that the copy is of what was there
            # before the flag moved.
            self.assertNotIn("stack_with_default",
                             json.loads(backups[-1].read_text()).get("statusLine", {}))

    def test_a_mode_that_changes_nothing_in_agy_writes_nothing(self):
        for first, second in (("own", "wrap"), ("stack", "stack")):
            with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
                run(agy.install, first)
                snapshot = settings.read_text()
                run(agy.set_mode, second)
                self.assertEqual(settings.read_text(), snapshot, (first, second))

    def test_with_our_line_not_registered_it_writes_the_config_and_says_so(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            _, text = run(agy.set_mode, "stack")
            self.assertEqual(agy.configured_mode(), "stack")
            self.assertEqual(json.loads(settings.read_text()), MEASURED_SETTINGS)
            self.assertIn("flightdeck install", text)

    def test_a_status_line_of_theirs_is_not_given_keys_of_ours(self):
        before = dict(MEASURED_SETTINGS,
                      statusLine={"type": "command", "command": "sh theirs.sh"})
        with agy_files(settings=json.dumps(before)) as (_h, settings):
            run(agy.set_mode, "stack")
            self.assertEqual(json.loads(settings.read_text()), before)

    def test_a_settings_file_it_cannot_parse_still_leaves_the_mode_written(self):
        # The config value is what every reader of the mode consults; agy's
        # settings are the copy that follows it, and a file we refuse to touch
        # must not cost the user the setting they asked for.
        with agy_files(settings="{ not json") as (_h, settings):
            _, text = run(agy.set_mode, "wrap")
            self.assertEqual(agy.configured_mode(), "wrap")
            self.assertEqual(settings.read_text(), "{ not json")
            self.assertIn("left as it was", text)

    def test_an_unknown_mode_writes_neither_file(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install, "own")
            snapshot = settings.read_text()
            with self.assertRaises(ValueError):
                agy.set_mode("sideways")
            self.assertEqual(agy.configured_mode(), "own")
            self.assertEqual(settings.read_text(), snapshot)


class TestRestore(unittest.TestCase):

    def test_it_puts_their_command_back_and_keeps_the_saved_copy(self):
        before = dict(MEASURED_SETTINGS,
                      statusLine={"type": "command", "command": "sh mine.sh"})
        with agy_files(settings=json.dumps(before)) as (hooks, settings):
            run(agy.install)
            previous, text = run(agy.restore)
            self.assertEqual(previous, "sh mine.sh")
            self.assertEqual(json.loads(settings.read_text())["statusLine"],
                             {"type": "command", "command": "sh mine.sh"})
            self.assertEqual(agy.saved_delegate(), "sh mine.sh")
            self.assertIn("restored", text)
            # The state hook is not the status line: it stays.
            self.assertIn(agy.NAME, json.loads(hooks.read_text()))

    def test_with_nothing_to_go_back_to_it_says_so_and_changes_nothing(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            run(agy.install)
            snapshot = settings.read_text()
            previous, text = run(agy.restore)
            self.assertEqual(previous, "")
            self.assertEqual(settings.read_text(), snapshot)
            self.assertIn("no status line command before", text)

    def test_without_ours_registered_there_is_nothing_to_restore(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            previous, text = run(agy.restore)
            self.assertEqual(previous, "")
            self.assertEqual(json.loads(settings.read_text()), MEASURED_SETTINGS)
            self.assertIn("nothing to restore", text)


class TestStatusLineRegistered(unittest.TestCase):

    def test_yes_once_installed_and_no_before(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)):
            self.assertFalse(agy.status_line_registered())
            run(agy.install)
            self.assertTrue(agy.status_line_registered())

    def test_a_file_it_cannot_read_refuses_to_answer(self):
        # "I cannot tell" is not "no": a report that says no where it means that
        # is a report that misleads.
        with agy_files(settings="{ not json"):
            with self.assertRaises(config.ConfigError):
                agy.status_line_registered()


class TestStatusReportsTheLine(unittest.TestCase):

    def test_it_says_the_command_the_delegate_and_the_mode(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)):
            run(agy.install)
            _, text = run(agy.status)
            self.assertIn("statusLine", text)
            self.assertIn(str(agy.STATUS_LINE), text)
            self.assertIn("statusline.agy", text)
            self.assertIn(agy.configured_mode(), text)

    def test_with_nothing_installed_it_says_so_without_blowing_up(self):
        with agy_files():
            _, text = run(agy.status)
            self.assertIn("statusLine", text)
            self.assertIn("—", text)

    def test_the_default_mode_is_the_configs(self):
        self.assertEqual(agy.MODES, ("own", "wrap", "stack"))
        self.assertIn(config.DEFAULTS["statusline"]["agy"], agy.MODES)


class TestAConfigThatCannotBeWrittenLeavesAgyAlone(unittest.TestCase):
    """The mode goes into OUR config.json before agy's settings are touched.

    Parked item 3 of the port: agy's installer wrote agy's settings first, so a
    `config.json` that could not be parsed (and therefore not updated) left
    agy's file carrying `stack_with_default` for a mode the config still did not
    know about. The claude installer already writes the mode first for exactly
    this reason.
    """

    def _with_broken_config(self, settings=None):
        return agy_files(settings=settings)

    def test_agys_settings_are_untouched_when_the_config_is_unparseable(self):
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (_h, settings):
            config.config_path().parent.mkdir(parents=True, exist_ok=True)
            config.config_path().write_text("{ not json")
            before = settings.read_text()
            _, text = run(agy.install)
            self.assertEqual(settings.read_text(), before)
            self.assertIn("status line untouched", text)

    def test_the_hooks_still_go_in(self):
        # Half an installation, but the RIGHT half: the state hook is what the
        # menu reads, it does not depend on the mode, and the person can fix
        # their config.json and run this again.
        with agy_files(settings=json.dumps(MEASURED_SETTINGS)) as (hooks, _s):
            config.config_path().parent.mkdir(parents=True, exist_ok=True)
            config.config_path().write_text("{ not json")
            run(agy.install)
            self.assertIn(agy.NAME, json.loads(hooks.read_text()))


if __name__ == "__main__":
    unittest.main()
