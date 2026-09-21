"""Migrating off the pre-release cockpit Flightdeck was ported from, which is
what `flightdeck install` does before it installs anything.

Every fixture here is a temporary directory standing in for that cockpit's
checkout, with the two delegate files it keeps under `state/`. The real one is
LIVE while this runs: nothing in this file, and nothing in the module it tests,
ever writes inside that root -- there is a test for exactly that, which compares
the tree byte for byte before and after.

The settings, `config.toml` and `hooks.json` fixtures copy the shapes measured
on a real machine, entries of the user's own included: what has to survive a
migration is precisely those.
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
from flightdeck.install import BACKUP_MARK, agy, claude, codex, migrate


def old_cockpit(tmp, statusline=None, notify=None):
    """A stand-in for `…/session-manager`, with its `state/` delegates. -> its path.

    `statusline` is the text of `state/statusline-delegate` (one command line,
    the same format as Flightdeck's `delegates/statusline`) and `notify` the
    argv list in `state/codex-notify-delegate` (JSON, the same format as
    Flightdeck's `delegates/codex-notify`).
    """
    root = Path(tmp) / "old" / "session-manager"
    (root / "state").mkdir(parents=True, exist_ok=True)
    # A file of the cockpit's own, so the "we never write in there" test has
    # something to compare besides the delegates.
    (root / "session-hook.py").write_text("# the old state hook\n")
    if statusline is not None:
        (root / "state" / "statusline-delegate").write_text(statusline)
    if notify is not None:
        (root / "state" / "codex-notify-delegate").write_text(json.dumps(notify))
    return root


def tree(root):
    """Every file under `root` as {relative path: bytes}."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def run(fn, *args):
    """Call fn capturing what it prints -> (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


def old_hook(root, event, tool=None, quoted=True):
    """One command line of the old cockpit's state hook."""
    path = "'%s'" % (root / "session-hook.py") if quoted else str(root / "session-hook.py")
    return "python3 %s %s%s" % (path, event, " --tool %s" % tool if tool else "")


# ── the fixtures, as measured on a real machine ──────────────────────────────

def old_claude_settings(root):
    """`~/.claude/settings.json` with the old cockpit in it AND a guard of theirs."""
    entry = lambda event: {"hooks": [{"type": "command",
                                      "command": old_hook(root, event, quoted=False),
                                      "timeout": 5}]}
    return {
        "model": "opus",
        "permissions": {"allow": ["Bash(git:*)"]},
        "env": {"EDITOR": "vim"},
        "hooks": {
            "SessionStart": [entry("SessionStart")],
            "UserPromptSubmit": [entry("UserPromptSubmit")],
            "PostToolUse": [entry("PostToolUse")],
            "Stop": [entry("Stop")],
            "Notification": [entry("Notification")],
            "SessionEnd": [entry("SessionEnd")],
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command",
                            "command": "python3 /home/u/.claude/hooks/bash-guard.py",
                            "timeout": 5,
                            "statusMessage": "Static safety check..."}]},
                {"matcher": "AskUserQuestion|ExitPlanMode",
                 "hooks": [{"type": "command",
                            "command": old_hook(root, "PreToolUse", quoted=False),
                            "timeout": 5}]},
            ],
        },
        "statusLine": {"type": "command",
                       "command": "python3 %s" % (root / "context-tee.py")},
    }


OLD_STATUS_LINE = "sh /home/u/.claude/statusline-command.sh\n"
OLD_NOTIFY = ["/home/u/.codex/computer-use/Codex Computer Use.app/Contents/"
              "SharedSupport/notify", "--json"]


def old_codex_toml(root):
    return ('notify = ["python3", "%s"]\n'
            'model = "gpt-5.6"\n'
            '\n'
            '[tui]\n'
            'notifications = true\n' % (root / "codex-notify-tee.py"))


def old_codex_hooks(root):
    return {"hooks": {
        "PreToolUse": [{"matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": "python3 '/home/u/.codex/hooks/bash-guard.py'",
                                   "timeout": 5}]}],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command",
                        "command": "sh '/home/u/.codex/hooks/session-turn-warn.sh'",
                        "timeout": 3}]},
            {"hooks": [{"type": "command",
                        "command": old_hook(root, "UserPromptSubmit", "codex"),
                        "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command",
                             "command": old_hook(root, "Stop", "codex"),
                             "timeout": 5}]}],
    }}


def old_agy_hooks(root):
    def handler(event):
        return {"type": "command",
                "command": "exec " + old_hook(root, event, "agy"),
                "timeout": 5}
    return {"session-cockpit": {"enabled": True,
                                "PreInvocation": [handler("PreInvocation")],
                                "PostToolUse": [{"matcher": "*",
                                                 "hooks": [handler("PostToolUse")]}],
                                "Stop": [handler("Stop")]},
            "lint": {"enabled": True,
                     "Stop": [{"type": "command", "command": "./lint.sh"}]}}


@contextlib.contextmanager
def sandbox(tmp, claude_settings=None, codex_toml=None, codex_hooks=None,
            agy_hooks=None):
    """Point every file the migration reads or writes at throwaways.

    -> a dict of the paths. Flightdeck's own state directory and config file are
    throwaways too, so the delegates it copies into are never the real ones.
    """
    paths = {"claude": Path(tmp) / "home" / ".claude" / "settings.json",
             "codex_toml": Path(tmp) / "home" / ".codex" / "config.toml",
             "codex_hooks": Path(tmp) / "home" / ".codex" / "hooks.json",
             "agy_hooks": Path(tmp) / "home" / ".gemini" / "config" / "hooks.json"}
    for key, content in (("claude", claude_settings), ("codex_toml", codex_toml),
                         ("codex_hooks", codex_hooks), ("agy_hooks", agy_hooks)):
        if content is None:
            continue
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        paths[key].write_text(content if isinstance(content, str)
                              else json.dumps(content, indent=2) + "\n")
    env = {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state"),
           "FLIGHTDECK_CONFIG": str(Path(tmp) / "config.json")}
    with mock.patch.object(claude, "SETTINGS_PATH", paths["claude"]), \
            mock.patch.object(codex, "CONFIG_TOML", paths["codex_toml"]), \
            mock.patch.object(codex, "HOOKS_JSON", paths["codex_hooks"]), \
            mock.patch.object(codex, "OLD_PATH", Path(tmp) / "home" / ".codex"
                              / "hooks" / "hooks.json"), \
            mock.patch.object(agy, "HOOKS_JSON", paths["agy_hooks"]), \
            mock.patch.object(agy, "SETTINGS_PATH", Path(tmp) / "home" / ".gemini"
                              / "antigravity-cli" / "settings.json"), \
            mock.patch.dict(os.environ, env):
        yield paths


class TestOldRoot(unittest.TestCase):
    """The old checkout's directory is read off the command that names it."""

    def test_an_unquoted_path(self):
        self.assertEqual(
            migrate.old_root("python3 /home/u/dev/session-manager/session-hook.py Stop"),
            "/home/u/dev/session-manager")

    def test_a_quoted_path(self):
        self.assertEqual(
            migrate.old_root("exec python3 '/home/u/session-manager/session-hook.py' "
                             "Stop --tool agy"),
            "/home/u/session-manager")

    def test_a_quoted_path_with_a_space_in_it(self):
        self.assertEqual(
            migrate.old_root("python3 '/home/u/my stuff/session-manager/context-tee.py'"),
            "/home/u/my stuff/session-manager")

    def test_a_command_that_is_not_the_old_cockpits(self):
        self.assertEqual(migrate.old_root("python3 /elsewhere/session_hook.py Stop"), "")
        self.assertEqual(migrate.old_root(""), "")
        self.assertEqual(migrate.old_root(None), "")


class TestWithoutTheOldClaudeHooks(unittest.TestCase):
    """Pure: the entries go, everything else stays."""

    def test_ours_go_and_the_users_own_guard_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            settings, events = migrate.without_old_claude_hooks(
                old_claude_settings(root))
        self.assertEqual(sorted(events),
                         sorted(["SessionStart", "UserPromptSubmit", "PostToolUse",
                                 "Stop", "Notification", "SessionEnd", "PreToolUse"]))
        # the Bash guard is the only PreToolUse entry left, untouched
        left = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["matcher"], "Bash")
        self.assertEqual(left[0]["hooks"][0]["statusMessage"],
                         "Static safety check...")
        # an event whose list is left empty goes with them
        self.assertNotIn("Stop", settings["hooks"])
        # and nothing else in the file is touched
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["env"], {"EDITOR": "vim"})

    def test_a_file_with_nothing_of_the_old_cockpit_in_it_is_unchanged(self):
        before = {"hooks": {"Stop": [{"hooks": [{"command": "./mine.sh"}]}]}}
        settings, events = migrate.without_old_claude_hooks(json.loads(json.dumps(before)))
        self.assertEqual(events, [])
        self.assertEqual(settings, before)

    def test_it_does_not_mutate_what_it_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            given = old_claude_settings(old_cockpit(tmp))
            before = json.dumps(given, sort_keys=True)
            migrate.without_old_claude_hooks(given)
            self.assertEqual(json.dumps(given, sort_keys=True), before)


class TestMigrateClaude(unittest.TestCase):

    def test_the_hooks_go_the_line_comes_back_and_the_delegate_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE)
            with sandbox(tmp, claude_settings=old_claude_settings(root)) as paths:
                changed, text = run(migrate.migrate_claude)
                written = json.loads(paths["claude"].read_text())
                # the status line they really had is back in settings.json...
                self.assertEqual(written["statusLine"]["command"],
                                 OLD_STATUS_LINE.strip())
                # ...and saved where Flightdeck's tee will look for it
                self.assertEqual(claude.delegate_file().read_text(), OLD_STATUS_LINE)
                self.assertTrue(changed)
                self.assertIn("session-manager", text)
            # no hook of the old cockpit is left anywhere in the file
            self.assertNotIn("session-manager", json.dumps(written))

    def test_the_backup_holds_the_file_as_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE)
            before = old_claude_settings(root)
            with sandbox(tmp, claude_settings=before) as paths:
                run(migrate.migrate_claude)
                backups = list(paths["claude"].parent.glob("*" + BACKUP_MARK + "*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(json.loads(backups[0].read_text()), before)

    def test_the_installer_then_wraps_the_ORIGINAL_line_and_not_the_old_tee(self):
        """The whole point of migrating before installing.

        The other way round, Flightdeck's tee would be put in front of the OLD
        tee and save THAT as the delegate: a tee calling a tee, with the user's
        real status line buried underneath both.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE)
            with sandbox(tmp, claude_settings=old_claude_settings(root)) as paths:
                run(migrate.migrate_claude)
                run(claude.install)
                written = json.loads(paths["claude"].read_text())
                self.assertIn(str(claude.TEE), written["statusLine"]["command"])
                self.assertEqual(claude.saved_delegate(), OLD_STATUS_LINE.strip())

    def test_ours_is_never_overwritten_by_theirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE)
            with sandbox(tmp, claude_settings=old_claude_settings(root)):
                claude.delegate_file().parent.mkdir(parents=True, exist_ok=True)
                claude.delegate_file().write_text("sh already-ours.sh\n")
                run(migrate.migrate_claude)
                self.assertEqual(claude.delegate_file().read_text(),
                                 "sh already-ours.sh\n")

    def test_with_no_old_delegate_the_status_line_setting_goes_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)          # no statusline-delegate at all
            with sandbox(tmp, claude_settings=old_claude_settings(root)) as paths:
                run(migrate.migrate_claude)
                self.assertNotIn("statusLine", json.loads(paths["claude"].read_text()))

    def test_a_second_run_changes_nothing_and_leaves_no_second_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE)
            with sandbox(tmp, claude_settings=old_claude_settings(root)) as paths:
                run(migrate.migrate_claude)
                snapshot = paths["claude"].read_text()
                backups = len(list(paths["claude"].parent.glob("*" + BACKUP_MARK + "*")))
                changed, _ = run(migrate.migrate_claude)
                self.assertEqual(changed, [])
                self.assertEqual(paths["claude"].read_text(), snapshot)
                self.assertEqual(
                    len(list(paths["claude"].parent.glob("*" + BACKUP_MARK + "*"))),
                    backups)

    def test_with_no_settings_file_there_is_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sandbox(tmp) as paths:
                changed, _ = run(migrate.migrate_claude)
                self.assertEqual(changed, [])
                self.assertFalse(paths["claude"].exists())


class TestMigrateCodex(unittest.TestCase):

    def test_the_notify_comes_back_and_its_delegate_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                changed, _ = run(migrate.migrate_codex)
                written = paths["codex_toml"].read_text()
                self.assertIn(json.dumps(OLD_NOTIFY), written)
                self.assertNotIn("session-manager", written)
                # the rest of the file is byte for byte as it was
                self.assertIn('model = "gpt-5.6"\n\n[tui]\nnotifications = true\n',
                              written)
                self.assertEqual(json.loads(codex.delegate_file().read_text()),
                                 OLD_NOTIFY)
                self.assertTrue(changed)

    def test_with_no_old_delegate_the_notify_line_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)          # no codex-notify-delegate
            with sandbox(tmp, codex_toml=old_codex_toml(root)) as paths:
                run(migrate.migrate_codex)
                self.assertEqual(paths["codex_toml"].read_text(),
                                 'model = "gpt-5.6"\n\n[tui]\nnotifications = true\n')

    def test_the_hooks_go_and_the_users_own_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                run(migrate.migrate_codex)
                left = json.loads(paths["codex_hooks"].read_text())["hooks"]
                self.assertNotIn("session-manager", json.dumps(left))
                self.assertEqual(len(left["PreToolUse"]), 1)
                self.assertIn("bash-guard", json.dumps(left["PreToolUse"]))
                # theirs was the FIRST of the two UserPromptSubmit entries
                self.assertEqual(len(left["UserPromptSubmit"]), 1)
                self.assertIn("session-turn-warn", json.dumps(left["UserPromptSubmit"]))
                # and an event left with nothing in it goes with ours
                self.assertNotIn("Stop", left)

    def test_the_installer_then_wraps_the_ORIGINAL_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                run(migrate.migrate_codex)
                run(codex.install)
                self.assertIn(str(codex.TEE), paths["codex_toml"].read_text())
                self.assertEqual(json.loads(codex.delegate_file().read_text()),
                                 OLD_NOTIFY)

    def test_each_file_is_backed_up_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                run(migrate.migrate_codex)
                for key in ("codex_toml", "codex_hooks"):
                    found = list(paths[key].parent.glob(
                        paths[key].name + BACKUP_MARK + "*"))
                    self.assertEqual(len(found), 1, key)

    def test_a_second_run_changes_nothing_and_leaves_no_second_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                run(migrate.migrate_codex)
                snapshot = (paths["codex_toml"].read_text(),
                            paths["codex_hooks"].read_text())
                backups = len(list(paths["codex_toml"].parent.glob("*" + BACKUP_MARK + "*")))
                changed, _ = run(migrate.migrate_codex)
                self.assertEqual(changed, [])
                self.assertEqual((paths["codex_toml"].read_text(),
                                  paths["codex_hooks"].read_text()), snapshot)
                self.assertEqual(
                    len(list(paths["codex_toml"].parent.glob("*" + BACKUP_MARK + "*"))),
                    backups)

    def test_with_no_codex_at_all_there_is_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sandbox(tmp) as paths:
                changed, _ = run(migrate.migrate_codex)
                self.assertEqual(changed, [])
                self.assertFalse(paths["codex_toml"].exists())


class TestMigrateAgy(unittest.TestCase):

    def test_the_old_key_goes_and_the_users_own_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            with sandbox(tmp, agy_hooks=old_agy_hooks(root)) as paths:
                changed, _ = run(migrate.migrate_agy)
                left = json.loads(paths["agy_hooks"].read_text())
                self.assertEqual(list(left), ["lint"])
                self.assertTrue(changed)

    def test_it_is_the_command_that_decides_and_not_the_name(self):
        # The old cockpit's key is called `session-cockpit`, but a key is
        # the old cockpit's because of what it RUNS. Anybody's named hook
        # calling that checkout goes; a `session-cockpit` of somebody else's,
        # calling something else entirely, stays.
        with tempfile.TemporaryDirectory() as tmp:
            hooks = {"session-cockpit": {"enabled": True,
                                         "Stop": [{"type": "command",
                                                   "command": "./not-the-old-one.sh"}]}}
            with sandbox(tmp, agy_hooks=hooks) as paths:
                changed, _ = run(migrate.migrate_agy)
                self.assertEqual(changed, [])
                self.assertEqual(json.loads(paths["agy_hooks"].read_text()), hooks)

    def test_a_file_left_with_nothing_in_it_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            only_ours = {"session-cockpit": old_agy_hooks(root)["session-cockpit"]}
            with sandbox(tmp, agy_hooks=only_ours) as paths:
                run(migrate.migrate_agy)
                self.assertFalse(paths["agy_hooks"].exists())
                backups = list(paths["agy_hooks"].parent.glob(
                    "hooks.json" + BACKUP_MARK + "*"))
                self.assertEqual(len(backups), 1)

    def test_the_installer_then_registers_ours_beside_theirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            with sandbox(tmp, agy_hooks=old_agy_hooks(root)) as paths:
                run(migrate.migrate_agy)
                run(agy.install)
                written = json.loads(paths["agy_hooks"].read_text())
                self.assertEqual(sorted(written), sorted(["lint", agy.NAME]))
                self.assertNotIn("session-manager", json.dumps(written))

    def test_a_second_run_changes_nothing_and_leaves_no_second_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            with sandbox(tmp, agy_hooks=old_agy_hooks(root)) as paths:
                run(migrate.migrate_agy)
                snapshot = paths["agy_hooks"].read_text()
                backups = len(list(paths["agy_hooks"].parent.glob("*" + BACKUP_MARK + "*")))
                changed, _ = run(migrate.migrate_agy)
                self.assertEqual(changed, [])
                self.assertEqual(paths["agy_hooks"].read_text(), snapshot)
                self.assertEqual(
                    len(list(paths["agy_hooks"].parent.glob("*" + BACKUP_MARK + "*"))),
                    backups)

    def test_with_no_agy_at_all_there_is_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sandbox(tmp) as paths:
                changed, _ = run(migrate.migrate_agy)
                self.assertEqual(changed, [])
                self.assertFalse(paths["agy_hooks"].exists())


class TestTheOldCheckoutIsNeverWrittenTo(unittest.TestCase):
    """The one rule with no exceptions: that directory is READ, never touched.

    It is a live cockpit and it goes on working until its owner switches it off
    by hand. Two files inside it are read -- the two delegates -- and nothing
    else, ever.
    """

    def test_the_whole_tree_is_byte_for_byte_the_same_afterwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            before = tree(root)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root),
                         agy_hooks=old_agy_hooks(root)):
                run(migrate.migrate_claude)
                run(migrate.migrate_codex)
                run(migrate.migrate_agy)
            self.assertEqual(tree(root), before)


class TestARefusalIsOneLineAndNoWrite(unittest.TestCase):
    """An unparseable file of theirs is refused, exactly as the installers do."""

    def test_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sandbox(tmp, claude_settings="{ not json") as paths:
                with self.assertRaises(config.ConfigError):
                    run(migrate.migrate_claude)
                self.assertEqual(paths["claude"].read_text(), "{ not json")

    def test_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, notify=OLD_NOTIFY)
            with sandbox(tmp, codex_toml=old_codex_toml(root),
                         codex_hooks="{ not json") as paths:
                with self.assertRaises(config.ConfigError):
                    run(migrate.migrate_codex)
                self.assertEqual(paths["codex_hooks"].read_text(), "{ not json")
                # and config.toml is untouched: nothing is written at all
                self.assertEqual(paths["codex_toml"].read_text(), old_codex_toml(root))

    def test_agy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sandbox(tmp, agy_hooks="{ not json") as paths:
                with self.assertRaises(config.ConfigError):
                    run(migrate.migrate_agy)
                self.assertEqual(paths["agy_hooks"].read_text(), "{ not json")


class TestAnEmptyOldDelegate(unittest.TestCase):
    """The old cockpit writes an EMPTY delegate for someone who had no line."""

    def test_nothing_is_copied_and_the_setting_simply_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline="")
            with sandbox(tmp, claude_settings=old_claude_settings(root)) as paths:
                _changed, text = run(migrate.migrate_claude)
                self.assertNotIn("statusLine", json.loads(paths["claude"].read_text()))
                self.assertFalse(claude.delegate_file().exists())
                self.assertNotIn("status line saved", text)


class TestTheWholeInstallOnTheAuthorsMachine(unittest.TestCase):
    """`flightdeck install` end to end where the old cockpit is still registered.

    The one test that proves the ORDER is right rather than just plausible: real
    installers, real migration, one run, and afterwards not a single command in
    any of the four files names the old checkout -- while the delegates hold the
    commands it was wrapping and the user's own hooks are all still there.
    """

    def test_it_migrates_and_installs_in_one_pass(self):
        from flightdeck.install import __main__ as install_main

        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            before = tree(root)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root),
                         agy_hooks=old_agy_hooks(root)) as paths:
                code, _text = run(lambda: install_main.main(
                    ["--yes"],
                    which=lambda cmd: "/usr/bin/%s" % cmd if cmd in
                    ("claude", "codex", "agy") else None,
                    run=lambda argv, **kw: _NoServer()))
                written = {name: path.read_text() for name, path in paths.items()}

                # nothing anywhere still calls the old checkout
                for name, text in written.items():
                    self.assertNotIn("session-manager", text, name)
                # what it was wrapping is saved where Flightdeck's tees look
                self.assertEqual(claude.saved_delegate(), OLD_STATUS_LINE.strip())
                self.assertEqual(json.loads(codex.delegate_file().read_text()),
                                 OLD_NOTIFY)
                # ours is registered in all three
                self.assertIn(str(claude.HOOK), written["claude"])
                self.assertIn(str(codex.TEE), written["codex_toml"])
                self.assertIn(agy.NAME, json.loads(written["agy_hooks"]))
                # and every hook of the user's own is untouched
                self.assertIn("bash-guard", written["claude"])
                self.assertIn("session-turn-warn", written["codex_hooks"])
                self.assertIn("lint", json.loads(written["agy_hooks"]))
                self.assertIsInstance(code, int)
            # the old checkout is exactly as it was
            self.assertEqual(tree(root), before)


class _NoServer:
    """What a tmux command answers on a socket nothing is listening on."""
    returncode = 1
    stdout = ""


class TestAnUnusableOldNotifyDelegate(unittest.TestCase):
    """Anything that is not a non-empty argv list reads as "there was none".

    Writing it back would put a `notify` in codex's config that it chokes on at
    the end of every turn -- the same rule `codex._saved_notify` follows.
    """

    def test_the_line_goes_and_nothing_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)
            (root / "state" / "codex-notify-delegate").write_text('"not an argv"')
            with sandbox(tmp, codex_toml=old_codex_toml(root)) as paths:
                run(migrate.migrate_codex)
                self.assertNotIn("notify", paths["codex_toml"].read_text())
                self.assertFalse(codex.delegate_file().exists())


class TestTheParallelInstallAndThenTheMigration(unittest.TestCase):
    """`install --keep-legacy` first, the migrating `install` second.

    The first run installs BESIDE the old cockpit, so Flightdeck's tees wrap the
    old cockpit's tees and save THOSE as their delegates. That is what wrapping
    means and it is not a bug in that run -- every test here pins it on purpose.

    What would be a bug is leaving them there. Both status line branches of the
    migration key on the old tee being the command REGISTERED, and after the
    first run it is not: ours is, with theirs behind it. Left alone, Flightdeck's
    tee would call the retired cockpit's tee for ever -- still writing to the old
    state directory, still firing its own handover notices -- and `uninstall`
    would one day hand that tee back as the user's status line.
    """

    def _both_runs(self):
        """`--keep-legacy`, then the real thing. -> what the first run saved.

        The orchestrator drives both, because the flag is what is under test and
        the argument that skips the migration is the whole of it.
        """
        from flightdeck.install import __main__ as install_main

        def install(argv):
            return run(lambda: install_main.main(
                argv,
                which=lambda cmd: "/usr/bin/%s" % cmd if cmd in
                ("claude", "codex", "agy") else None,
                run=lambda argv, **kw: _NoServer()))

        install(["--yes", "--keep-legacy"])
        wrapped = (claude.saved_delegate(), codex._saved_notify())
        install(["--yes"])
        return wrapped

    def test_the_old_tees_are_what_the_first_run_wraps(self):
        # The defect state, pinned: this is what the second run has to repair.
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)):
                statusline, notify = self._both_runs()
        self.assertIn("context-tee.py", statusline)
        self.assertIn("codex-notify-tee.py", json.dumps(notify))

    def test_and_the_second_run_puts_the_users_own_back_in_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            before = tree(root)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                self._both_runs()
                # the delegates hold the line and the notify the user really had
                self.assertEqual(claude.saved_delegate(), OLD_STATUS_LINE.strip())
                self.assertEqual(codex._saved_notify(), OLD_NOTIFY)
                # ours is still the registered command in both
                written = {name: path.read_text() for name, path in paths.items()
                           if path.exists()}
                self.assertIn(str(claude.TEE),
                              json.loads(written["claude"])["statusLine"]["command"])
                self.assertIn(str(codex.TEE), written["codex_toml"])
                # and nothing anywhere names the old checkout any more
                for name, text in written.items():
                    self.assertNotIn("session-manager", text, name)
            self.assertEqual(tree(root), before)

    def test_a_third_run_changes_nothing_and_leaves_no_extra_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root),
                         codex_hooks=old_codex_hooks(root)) as paths:
                self._both_runs()
                snapshot = {name: path.read_text() for name, path in paths.items()
                            if path.exists()}
                delegates = (claude.saved_delegate(), codex._saved_notify())
                backups = {name: len(list(path.parent.glob("*" + BACKUP_MARK + "*")))
                           for name, path in paths.items() if path.exists()}
                changed, _ = run(migrate.migrate_claude)
                self.assertEqual(changed, [])
                changed, _ = run(migrate.migrate_codex)
                self.assertEqual(changed, [])
                self.assertEqual({name: path.read_text()
                                  for name, path in paths.items() if path.exists()},
                                 snapshot)
                self.assertEqual((claude.saved_delegate(), codex._saved_notify()),
                                 delegates)
                self.assertEqual(
                    {name: len(list(path.parent.glob("*" + BACKUP_MARK + "*")))
                     for name, path in paths.items() if path.exists()},
                    backups)

    def test_a_delegate_holding_the_users_own_line_is_never_touched(self):
        # The new branch is for one thing only: a delegate of ours that holds an
        # OLD TEE. Anything else in there is the user's, saved from a live file.
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp, statusline=OLD_STATUS_LINE, notify=OLD_NOTIFY)
            with sandbox(tmp, claude_settings=old_claude_settings(root),
                         codex_toml=old_codex_toml(root)):
                claude.delegate_file().parent.mkdir(parents=True, exist_ok=True)
                claude.delegate_file().write_text("sh mine.sh\n")
                codex.delegate_file().write_text(json.dumps(["mine", "--json"]))
                run(migrate.migrate_claude)
                run(migrate.migrate_codex)
                self.assertEqual(claude.delegate_file().read_text(), "sh mine.sh\n")
                self.assertEqual(codex._saved_notify(), ["mine", "--json"])

    def test_with_no_old_status_line_delegate_ours_is_left_empty(self):
        # The user had no status line before either cockpit. An EMPTY delegate is
        # how the tee and `uninstall` already spell that.
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)              # no state/statusline-delegate
            with sandbox(tmp, claude_settings=old_claude_settings(root)):
                run(claude.install)
                self.assertIn("context-tee.py", claude.saved_delegate())
                changed, _ = run(migrate.migrate_claude)
                self.assertEqual(claude.delegate_file().read_text(), "")
                self.assertTrue(any("delegate" in line for line in changed))

    def test_with_no_old_notify_delegate_ours_is_removed(self):
        # No notify before either cockpit: the tee forwards to nobody, which is
        # what a missing delegate already means to it.
        with tempfile.TemporaryDirectory() as tmp:
            root = old_cockpit(tmp)              # no state/codex-notify-delegate
            with sandbox(tmp, codex_toml=old_codex_toml(root)):
                run(codex.install)
                self.assertIn("codex-notify-tee.py", json.dumps(codex._saved_notify()))
                changed, _ = run(migrate.migrate_codex)
                self.assertFalse(codex.delegate_file().exists())
                self.assertTrue(any("delegate" in line for line in changed))


if __name__ == "__main__":
    unittest.main()
