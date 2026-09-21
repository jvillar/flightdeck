"""Tests for flightdeck.config: XDG paths, config.json and FLIGHTDECK_* overrides."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from flightdeck import config


class TestPaths(unittest.TestCase):
    def test_state_dir_prefers_env_then_xdg_then_home(self):
        with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": "/tmp/fd-state"}, clear=False):
            self.assertEqual(config.state_dir(), Path("/tmp/fd-state"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg"}, clear=True):
            self.assertEqual(config.state_dir(), Path("/tmp/xdg/flightdeck"))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            Path, "home", return_value=Path("/h")
        ):
            self.assertEqual(config.state_dir(), Path("/h/.local/state/flightdeck"))

    def test_config_path_prefers_env_then_xdg_then_home(self):
        with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": "/tmp/fd/my.json"}, clear=False):
            self.assertEqual(config.config_path(), Path("/tmp/fd/my.json"))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdgc"}, clear=True):
            self.assertEqual(config.config_path(), Path("/tmp/xdgc/flightdeck/config.json"))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            Path, "home", return_value=Path("/h")
        ):
            self.assertEqual(config.config_path(), Path("/h/.config/flightdeck/config.json"))

    def test_subdirs_hang_from_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": d}, clear=False):
                self.assertEqual(config.sessions_dir(), Path(d) / "sessions")
                self.assertEqual(config.cache_dir(), Path(d) / "cache")
                self.assertEqual(config.delegates_dir(), Path(d) / "delegates")

    def test_asking_for_a_path_never_creates_it(self):
        # Asking where something lives is a read. `load_sessions` and the status
        # bar ask on every repaint, and a machine where Flightdeck was never
        # installed must not end up with a state directory built by a bar that
        # found nothing to paint.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "never-created"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": str(state)},
                                 clear=False):
                config.sessions_dir()
                config.cache_dir()
                config.delegates_dir()
                self.assertFalse(state.exists())

    def test_ensure_dir_creates_it_and_hands_the_path_back(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": d}, clear=False):
                path = config.ensure_dir(config.sessions_dir())
                self.assertEqual(path, Path(d) / "sessions")
                self.assertTrue(path.is_dir())

    def test_ensure_dir_survives_a_directory_it_cannot_create(self):
        # A read-only or full disk must not raise out of it: the hooks that call
        # it have their own error handling for the write that follows.
        with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": "/tmp/fd-state"}, clear=False):
            with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
                self.assertEqual(config.ensure_dir(config.sessions_dir()),
                                 Path("/tmp/fd-state/sessions"))

    def test_empty_env_var_counts_as_unset(self):
        # `FLIGHTDECK_STATE_DIR= flightdeck` used to mean Path(""), i.e. the
        # working directory; an empty variable is an unset variable here.
        with mock.patch.dict(
            os.environ, {"FLIGHTDECK_STATE_DIR": "", "XDG_STATE_HOME": ""}, clear=True
        ), mock.patch.object(Path, "home", return_value=Path("/h")):
            self.assertEqual(config.state_dir(), Path("/h/.local/state/flightdeck"))
        with mock.patch.dict(
            os.environ, {"FLIGHTDECK_CONFIG": "", "XDG_CONFIG_HOME": ""}, clear=True
        ), mock.patch.object(Path, "home", return_value=Path("/h")):
            self.assertEqual(config.config_path(), Path("/h/.config/flightdeck/config.json"))

    def test_data_dir_is_xdg_then_home_and_takes_no_flightdeck_variable(self):
        # Where the INSTALLED code lives (`current`, `previous`) and a downloaded
        # fzf (`bin`). There is deliberately no `FLIGHTDECK_DATA_DIR`: four
        # variables configure Flightdeck and no more, so a test moves it with
        # the XDG one like any other program would.
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdgd"}, clear=True):
            self.assertEqual(config.data_dir(), Path("/tmp/xdgd/flightdeck"))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            Path, "home", return_value=Path("/h")
        ):
            self.assertEqual(config.data_dir(), Path("/h/.local/share/flightdeck"))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}, clear=True), \
                mock.patch.object(Path, "home", return_value=Path("/h")):
            self.assertEqual(config.data_dir(), Path("/h/.local/share/flightdeck"))
        with mock.patch.dict(os.environ, {"FLIGHTDECK_DATA_DIR": "/tmp/nope",
                                          "XDG_DATA_HOME": "/tmp/xdgd"}, clear=True):
            self.assertEqual(config.data_dir(), Path("/tmp/xdgd/flightdeck"))

    def test_asking_for_the_data_dir_never_creates_it_either(self):
        with tempfile.TemporaryDirectory() as d:
            share = Path(d) / "never-created"
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(share)}, clear=False):
                config.data_dir()
                self.assertFalse(share.exists())

    def test_code_dir_is_the_repo_root(self):
        self.assertEqual(config.code_dir(), Path(config.__file__).resolve().parent.parent)
        self.assertTrue((config.code_dir() / "flightdeck" / "config.py").is_file())


class TestTmuxSocket(unittest.TestCase):
    def test_env_or_none(self):
        with mock.patch.dict(os.environ, {"FLIGHTDECK_TMUX_SOCKET": "fd-test"}, clear=False):
            self.assertEqual(config.tmux_socket(), "fd-test")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.tmux_socket())
        with mock.patch.dict(os.environ, {"FLIGHTDECK_TMUX_SOCKET": ""}, clear=True):
            self.assertIsNone(config.tmux_socket())


class TestLoad(unittest.TestCase):
    def test_missing_file_gives_defaults_without_error(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {"FLIGHTDECK_CONFIG": d + "/none.json"}
        ):
            cfg = config.load()
        self.assertEqual(cfg["menu_port"], 42707)
        self.assertEqual(cfg["pins"], [])
        self.assertIsNone(config.load.error)

    def test_defaults_carry_every_documented_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {"FLIGHTDECK_CONFIG": d + "/none.json", "HOME": "/home/tester"}, clear=True
        ):
            cfg = config.load()
        self.assertEqual(cfg["context_warn_pct"], 80)
        self.assertEqual(cfg["context_rearm_pct"], 75)
        self.assertEqual(cfg["context_show_pct"], 50)
        self.assertEqual(cfg["history_limit"], 150)
        self.assertEqual(cfg["notice_ms"], 5000)
        self.assertEqual(cfg["statusline"],
                         {"claude": "own", "agy": "own", "codex": "items",
                          "lines": 3, "account": True})
        self.assertEqual(cfg["projects_dir"], "~")

    def test_the_line_ships_complete(self):
        # Both keys are OPT-OUTS: Flightdeck's line is the reference line,
        # complete -- three lines and the signed-in account on the first of
        # them. They are pinned here and not only in the renderer's tests
        # because they are what a fresh install paints, and `account` in
        # particular decides whether somebody's e-mail address is in the
        # screenshots they take.
        self.assertIs(config.DEFAULTS["statusline"]["account"], True)
        self.assertEqual(config.DEFAULTS["statusline"]["lines"], 3)

    def test_file_overrides_defaults_and_keeps_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(
                json.dumps({"menu_port": 43000, "projects_dir": "~/code", "extra": 1}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"FLIGHTDECK_CONFIG": str(path), "HOME": "/home/tester"}, clear=True
            ):
                cfg = config.load()
                # Raw from load() (it is what gets written back), expanded from
                # the accessor (it is what a shell gets to cd into).
                self.assertEqual(cfg["projects_dir"], "~/code")
                self.assertEqual(config.projects_dir(), Path("/home/tester/code"))
        self.assertEqual(cfg["menu_port"], 43000)
        self.assertEqual(cfg["extra"], 1)
        self.assertEqual(cfg["history_limit"], 150)
        self.assertIsNone(config.load.error)

    def test_invalid_json_gives_defaults_and_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
        self.assertEqual(cfg["menu_port"], 42707)
        self.assertIsNotNone(config.load.error)
        self.assertIn("config.json", config.load.error)

    def test_json_that_is_not_an_object_gives_defaults_and_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
        self.assertEqual(cfg["menu_port"], 42707)
        self.assertIn("config.json", config.load.error)

    def test_error_is_cleared_by_the_next_good_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.load()
                self.assertIsNotNone(config.load.error)
                path.write_text("{}", encoding="utf-8")
                config.load()
                self.assertIsNone(config.load.error)

    def test_env_projects_dir_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"projects_dir": "~/y"}), encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "FLIGHTDECK_CONFIG": str(path),
                    "FLIGHTDECK_PROJECTS_DIR": "/x",
                    "HOME": "/home/tester",
                },
                clear=True,
            ):
                self.assertEqual(config.projects_dir(), Path("/x"))
                # ...and the file's own value is untouched by the override, so
                # nothing writes the transient /x back into the user's config.
                self.assertEqual(config.load()["projects_dir"], "~/y")

    def test_projects_dir_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"projects_dir": None}), encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"FLIGHTDECK_CONFIG": str(path), "HOME": "/home/tester"}, clear=True
            ):
                self.assertEqual(config.projects_dir(), Path("/home/tester"))

    def test_nested_statusline_keys_are_merged_not_replaced(self):
        # Saying "wrap" for Claude must not leave agy and codex without a mode:
        # every reader of cfg["statusline"] would then hit a KeyError.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"statusline": {"claude": "wrap"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
        self.assertEqual(cfg["statusline"],
                         {"claude": "wrap", "agy": "own", "codex": "items",
                          "lines": 3, "account": True})

    def test_load_never_hands_out_the_defaults_containers(self):
        # Without a config file the returned dict used to share `pins` and
        # `statusline` with DEFAULTS: `flightdeck pin add` appending to the list
        # it got back would have poisoned the defaults for the whole process.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {"FLIGHTDECK_CONFIG": d + "/none.json"}, clear=False
        ):
            cfg = config.load()
        self.assertIsNot(cfg["pins"], config.DEFAULTS["pins"])
        self.assertIsNot(cfg["statusline"], config.DEFAULTS["statusline"])
        cfg["pins"].append({"name": "later"})
        cfg["statusline"]["codex"] = "off"
        self.assertEqual(config.DEFAULTS["pins"], [])
        self.assertEqual(
            config.DEFAULTS["statusline"],
            {"claude": "own", "agy": "own", "codex": "items",
             "lines": 3, "account": True},
        )

    def test_load_never_mutates_defaults_through_file_values(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(
                json.dumps({"pins": [{"name": "accounts"}], "statusline": {"claude": "stack"}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
                cfg["pins"].append({"name": "later"})
                cfg["statusline"]["codex"] = "off"
        self.assertEqual(config.DEFAULTS["pins"], [])
        self.assertEqual(
            config.DEFAULTS["statusline"],
            {"claude": "own", "agy": "own", "codex": "items",
             "lines": 3, "account": True},
        )

    def test_undecodable_bytes_give_defaults_and_error(self):
        # A config.json saved in some other encoding raises UnicodeDecodeError,
        # which is a ValueError and not an OSError: a separate arm of the read.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_bytes(b'{"projects_dir": "\xff\xfe"}')
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
        self.assertEqual(cfg["menu_port"], 42707)
        self.assertIn("config.json", config.load.error)

    def test_unreadable_file_gives_defaults_and_error(self):
        with tempfile.TemporaryDirectory() as d:
            # A directory where the file should be: read_text raises OSError, not
            # FileNotFoundError, and load() still has to return something usable.
            path = Path(d) / "config.json"
            path.mkdir()
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                cfg = config.load()
        self.assertEqual(cfg["menu_port"], 42707)
        self.assertIn("config.json", config.load.error)


class TestLoadRaw(unittest.TestCase):
    def test_missing_file_is_an_empty_dict(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {"FLIGHTDECK_CONFIG": d + "/none.json"}, clear=False
        ):
            self.assertEqual(config.load_raw(), {})

    def test_returns_the_file_verbatim_without_defaults_or_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"projects_dir": "~/code"}), encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "FLIGHTDECK_CONFIG": str(path),
                    "FLIGHTDECK_PROJECTS_DIR": "/x",
                    "HOME": "/home/tester",
                },
                clear=True,
            ):
                self.assertEqual(config.load_raw(), {"projects_dir": "~/code"})

    def test_invalid_or_non_object_file_is_an_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            for content in ("{", "[1, 2]", '"a string"'):
                path.write_text(content, encoding="utf-8")
                with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                    self.assertEqual(config.load_raw(), {}, content)

    def test_error_attribute_tracks_the_last_call(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                # A missing file is not a fault: there is simply no config yet.
                config.load_raw()
                self.assertIsNone(config.load_raw.error)

                path.write_text("{", encoding="utf-8")
                config.load_raw()
                self.assertIn("config.json", config.load_raw.error)

                path.write_text('{"menu_port": 1}', encoding="utf-8")
                config.load_raw()
                self.assertIsNone(config.load_raw.error)

    def test_does_not_touch_the_load_error(self):
        # load.error describes the last load(), which is what doctor reports.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.load()
                before = config.load.error
                config.load_raw()
                self.assertEqual(config.load.error, before)
                self.assertIsNotNone(before)


class TestUpdate(unittest.TestCase):
    def test_writes_only_the_change_never_the_defaults(self):
        # The finding this replaces: a read-modify-write through load()/save()
        # baked every default and an expanded /Users/<name> into the user's
        # file, so a synced config stopped following the defaults for good.
        pins = [{"name": "accounts", "command": "cswap tui"}]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            with mock.patch.dict(
                os.environ,
                {
                    "FLIGHTDECK_CONFIG": str(path),
                    "FLIGHTDECK_PROJECTS_DIR": "/x",
                    "HOME": "/home/tester",
                },
                clear=True,
            ):
                returned = config.update({"pins": pins})
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(config.load()["pins"], pins)
        self.assertEqual(on_disk, {"pins": pins})
        self.assertEqual(returned, {"pins": pins})
        self.assertNotIn("projects_dir", on_disk)
        self.assertNotIn("menu_port", on_disk)

    def test_refuses_an_unparseable_config_and_leaves_it_byte_identical(self):
        # A `pin add` on a config with one stray comma must not replace every
        # other setting the user has with the one pin.
        broken = b'{"menu_port": 43000,,\n  "mine": "kept"}'
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_bytes(broken)
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                with self.assertRaises(config.ConfigError) as caught:
                    config.update({"pins": []})
            self.assertEqual(path.read_bytes(), broken)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["config.json"])
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("fix the file first", str(caught.exception))

    def test_refuses_a_config_that_is_not_a_json_object(self):
        array = b'[{"name": "accounts"}]'
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_bytes(array)
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                with self.assertRaises(config.ConfigError):
                    config.update({"pins": []})
            self.assertEqual(path.read_bytes(), array)

    def test_refuses_a_config_it_cannot_read(self):
        # Same guard, other arm: a directory where the file should be. Reading
        # fails, so what is there is unknown, so it must not be overwritten.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.mkdir()
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                with self.assertRaises(config.ConfigError):
                    config.update({"pins": []})
            self.assertTrue(path.is_dir())

    def test_keeps_the_keys_it_was_not_given(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(
                json.dumps({"menu_port": 43000, "mine": "kept"}), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.update({"statusline": {"claude": "wrap"}})
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                cfg = config.load()
        self.assertEqual(
            on_disk, {"menu_port": 43000, "mine": "kept", "statusline": {"claude": "wrap"}}
        )
        # The merge still fills the tools the user did not name.
        self.assertEqual(cfg["statusline"],
                         {"claude": "wrap", "agy": "own", "codex": "items",
                          "lines": 3, "account": True})

    def test_a_dict_value_merges_instead_of_replacing_what_the_file_holds(self):
        # Measured before it was fixed: `raw.update({"statusline": {...}})`
        # REPLACED the whole section, so `flightdeck statusline --mode own`
        # silently threw away the modes the user had chosen for the other tools.
        # `load()` then handed those tools the default again and their status
        # line changed behind their back.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"statusline": {"agy": "wrap", "codex": "items"}}),
                            encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.update({"statusline": {"claude": "stack"}})
                on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["statusline"],
                         {"agy": "wrap", "codex": "items", "claude": "stack"})

    def test_a_dict_replacing_a_value_that_is_not_one_still_wins(self):
        # Only two dicts merge. A section the user wrote as something else is
        # not something to merge into: it is replaced, which is also the only
        # way a mistyped value can ever be corrected.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"statusline": "own"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.update({"statusline": {"claude": "own"}})
                on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["statusline"], {"claude": "own"})


class TestSave(unittest.TestCase):
    def test_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.save({"menu_port": 1})
                self.assertEqual(config.load()["menu_port"], 1)
            leftovers = [p.name for p in Path(d).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_creates_the_missing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deep" / "nest" / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                config.save({"pins": [{"name": "accounts", "command": "cswap tui"}]})
                cfg = config.load()
        self.assertEqual(cfg["pins"], [{"name": "accounts", "command": "cswap tui"}])

    def test_failed_save_leaves_no_tmp_behind(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}, clear=False):
                with self.assertRaises(TypeError):
                    config.save({"bad": object()})
            names = [p.name for p in Path(d).iterdir()]
        self.assertEqual([n for n in names if n.endswith(".tmp")], [])


class TestCli(unittest.TestCase):
    """`python3 -m flightdeck.config <key>`: how the bash command reads config."""

    def _run(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = config.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_prints_a_scalar_without_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self._run(["menu_port"], {"FLIGHTDECK_CONFIG": d + "/none.json"})
        self.assertEqual(code, 0)
        self.assertEqual(out, "42707\n")

    def test_prints_the_expanded_projects_dir(self):
        # The caller is a shell about to cd there: `~` and `~/code` are not
        # directories, so this one key prints the effective path, not the raw.
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self._run(
                ["projects_dir"], {"FLIGHTDECK_CONFIG": d + "/none.json", "HOME": "/home/tester"}
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "/home/tester\n")

            path = Path(d) / "config.json"
            path.write_text(json.dumps({"projects_dir": "~/code"}), encoding="utf-8")
            code, out, _ = self._run(
                ["projects_dir"], {"FLIGHTDECK_CONFIG": str(path), "HOME": "/home/tester"}
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "/home/tester/code\n")

            code, out, _ = self._run(
                ["projects_dir"],
                {
                    "FLIGHTDECK_CONFIG": str(path),
                    "FLIGHTDECK_PROJECTS_DIR": "/x",
                    "HOME": "/home/tester",
                },
            )
        self.assertEqual(code, 0)
        self.assertEqual(out, "/x\n")

    def test_prints_json_for_lists_and_dicts(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self._run(["statusline"], {"FLIGHTDECK_CONFIG": d + "/none.json"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out),
                         {"claude": "own", "agy": "own", "codex": "items",
                          "lines": 3, "account": True})

    def test_unknown_key_prints_nothing_and_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self._run(["nope"], {"FLIGHTDECK_CONFIG": d + "/none.json"})
        self.assertEqual(code, 1)
        self.assertEqual(out, "")

    def test_wrong_number_of_arguments_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self._run([], {"FLIGHTDECK_CONFIG": d + "/none.json"})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage", err)

    def test_broken_config_still_prints_the_default(self):
        # The bash command asks for a port on every menu entry; a typo in
        # config.json cannot be allowed to leave it with an empty answer.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{", encoding="utf-8")
            code, out, _ = self._run(["menu_port"], {"FLIGHTDECK_CONFIG": str(path)})
        self.assertEqual(code, 0)
        self.assertEqual(out, "42707\n")


if __name__ == "__main__":
    unittest.main()
