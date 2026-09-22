"""Tests for flightdeck.pins: the preset catalogue, the config edits and the CLI.

Nothing here touches a real `config.json` or a real PATH: every test that reads
the config points `FLIGHTDECK_CONFIG` at a temporary file, and detection is
always driven by a fake `which`, never by what happens to be installed on the
machine running the suite.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from flightdeck import picker, pins

# A `which` that finds nothing and one that finds a fixed set: detection must
# never depend on the machine the suite runs on.
NOTHING = lambda cmd: None  # noqa: E731 - a one-line stub reads better inline


def which_finding(*commands):
    """A fake `shutil.which` that only knows `commands`."""
    known = set(commands)
    return lambda cmd: ("/usr/bin/%s" % cmd) if cmd in known else None


@contextlib.contextmanager
def temp_config(contents=None):
    """A throwaway `config.json`, and the environment pointing at it.

    `contents` None leaves the file MISSING, which is the normal state of a
    Flightdeck that has never been configured.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        if contents is not None:
            path.write_text(contents if isinstance(contents, str)
                            else json.dumps(contents), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLIGHTDECK_CONFIG": str(path)}):
            yield path


def run_cli(*argv):
    """`pins.main(argv)` with its output captured: (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = pins.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class TestThePresetCatalogue(unittest.TestCase):
    def test_the_seven_presets(self):
        # The catalogue is the contract: seven presets, by name and in order.
        self.assertEqual(list(pins.PRESETS),
                         ["top", "htop", "btop", "cswap", "lazygit", "lazydocker", "k9s"])

    def test_every_preset_is_complete(self):
        for name, preset in pins.PRESETS.items():
            with self.subTest(preset=name):
                for key in ("label", "command", "session", "hint"):
                    self.assertTrue(preset.get(key), "%s has no %s" % (name, key))
                self.assertIn("darwin", preset["hint"])
                self.assertIn("linux", preset["hint"])

    def test_labels_are_short_and_lead_with_a_glyph(self):
        # The label is what the menu row paints; a long one pushes the row past
        # a phone-width terminal, which is where this menu is half read.
        for name, preset in pins.PRESETS.items():
            with self.subTest(preset=name):
                label = preset["label"]
                self.assertLess(len(label), 16, label)
                self.assertFalse(label[0].isascii(), label)

    def test_no_preset_claims_a_reserved_session(self):
        # A preset whose session were `flightdeck` would be unpinnable: `add`
        # refuses reserved names, so the catalogue would ship a broken entry.
        for name, preset in pins.PRESETS.items():
            with self.subTest(preset=name):
                self.assertFalse(picker.is_reserved(preset["session"]))

    def test_cswap_only_opens_its_tui(self):
        # The auto-rotation of accounts stays vetoed: the preset is
        # a window on the switcher, never a command that switches by itself.
        self.assertEqual(pins.PRESETS["cswap"]["command"], "cswap tui")

    def test_the_hint_falls_back_to_linux_on_an_unknown_platform(self):
        # WSL2 reports "linux"; anything else (a BSD, say) gets the same advice
        # rather than an empty line where the install command should be.
        self.assertEqual(pins.hint_for("htop", platform="darwin"), "brew install htop")
        self.assertEqual(pins.hint_for("htop", platform="freebsd13"),
                         pins.PRESETS["htop"]["hint"]["linux"])

    def test_the_hint_of_something_that_is_not_a_preset_is_empty(self):
        self.assertEqual(pins.hint_for("my-notes"), "")


class TestDetected(unittest.TestCase):
    def test_only_what_the_which_finds(self):
        self.assertEqual(pins.detected(which=which_finding("htop", "k9s")),
                         ["htop", "k9s"])

    def test_nothing_installed_is_an_empty_list(self):
        self.assertEqual(pins.detected(which=NOTHING), [])

    def test_it_looks_up_the_first_word_of_the_command(self):
        # `cswap tui` is detected by `cswap`: handing the whole command line to
        # `which` would answer None for every preset that takes a subcommand.
        seen = []

        def which(cmd):
            seen.append(cmd)
            return "/usr/local/bin/cswap" if cmd == "cswap" else None

        self.assertEqual(pins.detected(which=which), ["cswap"])
        self.assertNotIn("cswap tui", seen)

    def test_the_order_is_the_catalogue_order(self):
        every = pins.detected(which=lambda cmd: "/bin/" + cmd)
        self.assertEqual(every, list(pins.PRESETS))


class TestAdd(unittest.TestCase):
    def test_a_preset_lands_with_its_label_command_and_session(self):
        cfg = pins.add({"pins": []}, name="htop")
        self.assertEqual(cfg["pins"], [{"name": "htop",
                                        "label": pins.PRESETS["htop"]["label"],
                                        "session": "htop", "command": "htop"}])

    def test_the_keys_are_the_ones_the_menu_reads(self):
        # `picker._pin_entry` reads exactly these four; a pin written with any
        # other spelling would be a row that does nothing on Enter.
        pin = pins.add({"pins": []}, name="cswap")["pins"][0]
        self.assertEqual(sorted(pin), ["command", "label", "name", "session"])
        entry = picker._pin_entry(pin)
        self.assertEqual(entry["kind"], "pin")
        self.assertEqual(entry["target"], pin["session"])
        self.assertEqual(entry["command"], "cswap tui")

    def test_a_custom_entry_with_label_and_command(self):
        cfg = pins.add({"pins": []}, label="📝 notes", command="vim ~/notes.md")
        self.assertEqual(cfg["pins"], [{"name": "notes", "label": "📝 notes",
                                        "session": "notes",
                                        "command": "vim ~/notes.md"}])

    def test_a_custom_entry_can_name_its_own_session(self):
        cfg = pins.add({"pins": []}, label="📝 notes", command="vim n.md",
                       session="scratch")
        self.assertEqual(cfg["pins"][0]["session"], "scratch")
        self.assertEqual(cfg["pins"][0]["name"], "scratch")

    def test_an_explicit_name_wins_over_the_derived_one(self):
        cfg = pins.add({"pins": []}, name="jot", label="📝 notes", command="vim n.md")
        self.assertEqual(cfg["pins"][0]["name"], "jot")
        self.assertEqual(cfg["pins"][0]["session"], "jot")

    def test_a_preset_can_be_overridden(self):
        cfg = pins.add({"pins": []}, name="htop", session="mon", label="📊 mon")
        self.assertEqual(cfg["pins"][0], {"name": "htop", "label": "📊 mon",
                                          "session": "mon", "command": "htop"})

    def test_the_session_is_sanitised_the_way_tmux_would(self):
        # tmux replaces "." and ":" silently (common.tmux_safe_name), so a pin
        # written with them would point at a session that never exists.
        cfg = pins.add({"pins": []}, label="site", command="vim",
                       session="example.com:1")
        self.assertEqual(cfg["pins"][0]["session"], "example_com_1")

    def test_the_original_cfg_is_left_alone(self):
        before = {"menu_port": 43000, "pins": []}
        after = pins.add(before, name="htop")
        self.assertEqual(before["pins"], [])
        self.assertEqual(after["menu_port"], 43000)
        self.assertIsNot(after["pins"], before["pins"])

    def test_a_cfg_without_a_pins_key_grows_one(self):
        self.assertEqual(len(pins.add({}, name="htop")["pins"]), 1)

    def test_an_unknown_preset_is_refused(self):
        with self.assertRaises(pins.PinError) as caught:
            pins.add({"pins": []}, name="nope")
        self.assertIn('"nope" is not a preset', str(caught.exception))
        self.assertIn("flightdeck pin list", str(caught.exception))

    def test_a_duplicate_name_is_refused(self):
        cfg = pins.add({"pins": []}, name="htop")
        with self.assertRaises(pins.PinError) as caught:
            pins.add(cfg, name="htop")
        self.assertIn('a pin named "htop" already exists', str(caught.exception))

    def test_a_pin_without_a_command_is_refused(self):
        # A row that types nothing on Enter is worse than no row at all.
        with self.assertRaises(pins.PinError) as caught:
            pins.add({"pins": []}, label="📝 notes", command="   ")
        self.assertIn("command", str(caught.exception))

    def test_a_pin_that_cannot_be_named_is_refused(self):
        with self.assertRaises(pins.PinError) as caught:
            pins.add({"pins": []}, label="📝", command="vim")
        self.assertIn("name", str(caught.exception))

    def test_the_menus_own_session_is_reserved(self):
        with self.assertRaises(pins.PinError) as caught:
            pins.add({"pins": []}, label="x", command="top", session="flightdeck")
        self.assertIn('session "flightdeck" is reserved', str(caught.exception))

    def test_a_per_window_menu_session_is_reserved(self):
        # `flightdeck-3` is one of the menus the bash command creates when the
        # main one already has a client; a pin there would be killed by `quit`.
        with self.assertRaises(pins.PinError) as caught:
            pins.add({"pins": []}, label="x", command="top", session="flightdeck-3")
        self.assertIn('session "flightdeck-3" is reserved', str(caught.exception))

    def test_another_pins_session_is_refused(self):
        # The NAME here is free, so what refuses the pin is the session alone:
        # two rows switching to one tmux session would fight over what runs in
        # it. The message names the pin that already has it.
        cfg = pins.add({"pins": []}, name="htop")
        with self.assertRaises(pins.PinError) as caught:
            pins.add(cfg, name="monitor", label="📊 monitor", command="btop",
                     session="htop")
        self.assertIn('session "htop"', str(caught.exception))
        self.assertIn('the pin "htop"', str(caught.exception))

    def test_a_session_that_only_tmux_would_spell_the_same_still_collides(self):
        # The pin already in the file has no `session`, so its name is the
        # session -- and tmux writes `a.b` as `a_b`. Both have to be compared in
        # the spelling tmux ends up with, or the two pins would share a session.
        cfg = {"pins": [{"name": "a.b", "command": "top"}]}
        with self.assertRaises(pins.PinError) as caught:
            pins.add(cfg, name="other", label="x", command="top", session="a.b")
        self.assertIn('the pin "a.b"', str(caught.exception))

    def test_a_pins_list_that_is_not_a_list_is_refused_not_overwritten(self):
        # Same rule as config.update: a file that cannot be understood is not
        # replaced, because the user can still fix it.
        with self.assertRaises(pins.PinError):
            pins.add({"pins": "htop"}, name="htop")


class TestRemove(unittest.TestCase):
    def test_it_removes_the_named_pin_and_leaves_the_rest(self):
        cfg = pins.add(pins.add({"pins": []}, name="htop"), name="k9s")
        after = pins.remove(cfg, "htop")
        self.assertEqual([p["name"] for p in after["pins"]], ["k9s"])

    def test_removing_something_that_is_not_pinned_is_refused(self):
        with self.assertRaises(pins.PinError) as caught:
            pins.remove({"pins": []}, "htop")
        self.assertIn('no pin named "htop"', str(caught.exception))

    def test_the_original_cfg_is_left_alone(self):
        before = pins.add({"pins": []}, name="htop")
        after = pins.remove(before, "htop")
        self.assertEqual(len(before["pins"]), 1)
        self.assertEqual(after["pins"], [])


class TestListRows(unittest.TestCase):
    def test_presets_first_then_the_custom_pins(self):
        cfg = pins.add({"pins": []}, label="📝 notes", command="vim n.md")
        names = [row[0] for row in pins.list_rows(cfg, which=NOTHING)]
        self.assertEqual(names, list(pins.PRESETS) + ["notes"])

    def test_installed_and_pinned_are_independent(self):
        cfg = pins.add({"pins": []}, name="k9s")
        rows = {row[0]: row for row in pins.list_rows(cfg, which=which_finding("htop"))}
        self.assertEqual(rows["htop"][2:], (True, False))
        self.assertEqual(rows["k9s"][2:], (False, True))
        self.assertEqual(rows["btop"][2:], (False, False))

    def test_a_pinned_preset_shows_the_label_the_menu_paints(self):
        # The user may have pinned it with a label of their own; the list has to
        # agree with the row they see in the menu.
        cfg = pins.add({"pins": []}, name="htop", label="📊 mon")
        rows = {row[0]: row for row in pins.list_rows(cfg, which=NOTHING)}
        self.assertEqual(rows["htop"][1], "📊 mon")
        self.assertEqual(rows["btop"][1], pins.PRESETS["btop"]["label"])

    def test_a_custom_pin_is_detected_by_its_own_command(self):
        cfg = pins.add({"pins": []}, label="📝 notes", command="vim ~/n.md")
        rows = {row[0]: row for row in pins.list_rows(cfg, which=which_finding("vim"))}
        self.assertEqual(rows["notes"][2:], (True, True))

    def test_a_hand_edited_pin_that_makes_no_sense_is_skipped(self):
        # config.json is edited by hand: a pin that is not an object, or has no
        # name, must not take the listing down.
        cfg = {"pins": ["htop", {"command": "top"}, {"name": "ok", "command": "top"}]}
        names = [row[0] for row in pins.list_rows(cfg, which=NOTHING)]
        self.assertEqual(names, list(pins.PRESETS) + ["ok"])


class TestTheListingColumns(unittest.TestCase):
    """The listing lines up, which plain `%-16s` padding does not do.

    Asserted on the widths themselves rather than on the printed lines: a test
    that measured the output with the same function it is checking would agree
    with any answer, right or wrong.
    """

    def test_an_emoji_takes_two_columns_and_one_character(self):
        self.assertEqual(pins._display_width("htop"), 4)
        self.assertEqual(pins._display_width("📊 htop"), 7)
        self.assertEqual(pins._display_width("🐳 lazydocker"), 13)
        # Not every glyph is wide: these two are one cell, like a letter.
        self.assertEqual(pins._display_width("☸ k9s"), 5)
        self.assertEqual(pins._display_width("⚙ accounts"), 10)

    def test_the_variation_selector_adds_nothing(self):
        # `⚙️` is the gear plus U+FE0F asking for the emoji shape: one glyph.
        self.assertEqual(pins._display_width("⚙️"), pins._display_width("⚙"))

    def test_pad_fills_the_column_in_columns_not_characters(self):
        for label in ("📊 htop", "☸ k9s", "top"):
            with self.subTest(label=label):
                self.assertEqual(pins._display_width(pins._pad(label, 17)), 17)

    def test_pad_always_leaves_a_space_after_something_too_long(self):
        # A custom pin can be named anything; the columns after it shift right
        # rather than running into it.
        self.assertEqual(pins._pad("a-name-longer-than-the-column", 13),
                         "a-name-longer-than-the-column ")


class TestCli(unittest.TestCase):
    def test_list_prints_one_line_per_preset_with_its_state(self):
        # The fake `which` says the opposite of this machine on purpose (k9s is
        # not installed on the developer's, htop usually is): if the listing
        # ever stopped going through it, both assertions would fail.
        with temp_config({"pins": [{"name": "htop", "label": "📊 htop",
                                    "session": "htop", "command": "htop"}]}):
            with mock.patch.object(pins.shutil, "which", which_finding("k9s")):
                rc, out, err = run_cli("list")
        self.assertEqual(rc, 0, err)
        lines = {line.split()[0]: line for line in out.splitlines() if line[:1].isalnum()}
        self.assertEqual(sorted(lines), sorted(pins.PRESETS))
        self.assertIn("installed", lines["k9s"])
        self.assertNotIn("not installed", lines["k9s"])
        self.assertNotIn("pinned", lines["k9s"])
        self.assertIn("not installed", lines["htop"])
        self.assertIn("pinned", lines["htop"])

    def test_list_puts_the_install_hint_under_what_is_missing(self):
        with temp_config(), mock.patch.object(pins.shutil, "which", NOTHING):
            with mock.patch.object(pins.sys, "platform", "darwin"):
                rc, out, _ = run_cli("list")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        htop = lines.index([line for line in lines if line.startswith("htop")][0])
        self.assertEqual(lines[htop + 1].strip(), "brew install htop")

    def test_list_is_what_the_bare_command_does(self):
        with temp_config(), mock.patch.object(pins.shutil, "which", NOTHING):
            bare = run_cli()
            listed = run_cli("list")
        self.assertEqual(bare, listed)

    def test_add_writes_only_the_pins_key(self):
        # It goes through config.update: a `pin add` must not stamp every
        # default and an expanded projects_dir into the user's file.
        with temp_config() as path:
            rc, out, err = run_cli("add", "htop")
            self.assertEqual(rc, 0, err)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(written), ["pins"])
        self.assertEqual(written["pins"][0]["name"], "htop")
        self.assertIn("htop", out)

    def test_add_of_a_missing_tool_says_how_to_install_it(self):
        """Pinning is allowed with the tool absent, and then it has to say so.

        Otherwise the row is pinned, Enter opens a session, the command is not
        found and the pane dies -- with nothing anywhere to say why. `pin list`
        printed the hint already; `pin add` is where the user actually is.
        """
        with temp_config(), mock.patch.object(pins.shutil, "which", NOTHING), \
                mock.patch.object(pins.sys, "platform", "darwin"):
            rc, out, err = run_cli("add", "htop")
        self.assertEqual(rc, 0, err)
        self.assertIn("brew install htop", out)

    def test_add_of_an_installed_tool_says_nothing_about_installing(self):
        with temp_config(), mock.patch.object(pins.shutil, "which",
                                              which_finding("htop")):
            rc, out, err = run_cli("add", "htop")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("brew install", out)

    def test_a_name_that_is_not_a_preset_is_not_called_an_unknown_preset(self):
        """`pin add --name x` never asked for a preset, so "unknown preset x"
        answered a question the user did not ask. What they are missing is
        `--command`, and the message has to be the one that says so."""
        with temp_config() as path:
            rc, out, err = run_cli("add", "--name", "scratch")
            self.assertFalse(path.exists(), "nothing should have been written")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn('"scratch" is not a preset', err)
        self.assertIn("--command", err)

    def test_add_keeps_the_settings_that_were_already_there(self):
        with temp_config({"menu_port": 43000}) as path:
            self.assertEqual(run_cli("add", "htop")[0], 0)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["menu_port"], 43000)
        self.assertEqual(sorted(written), ["menu_port", "pins"])

    def test_add_of_a_custom_entry_takes_its_flags(self):
        with temp_config() as path:
            rc, _, err = run_cli("add", "--label", "📝 notes", "--command",
                                 "vim ~/n.md", "--session", "scratch")
            self.assertEqual(rc, 0, err)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["pins"], [{"name": "scratch", "label": "📝 notes",
                                            "session": "scratch",
                                            "command": "vim ~/n.md"}])

    def test_the_flags_also_take_an_equals_sign(self):
        with temp_config() as path:
            rc, _, err = run_cli("add", "--label=📝 notes", "--command=vim n.md")
            self.assertEqual(rc, 0, err)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["pins"][0]["name"], "notes")

    def test_remove_takes_the_pin_out_of_the_file(self):
        with temp_config() as path:
            self.assertEqual(run_cli("add", "htop")[0], 0)
            rc, out, err = run_cli("remove", "htop")
            self.assertEqual(rc, 0, err)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["pins"], [])
        self.assertIn("htop", out)

    def test_an_unknown_preset_is_one_line_on_stderr_and_exit_1(self):
        with temp_config() as path:
            rc, out, err = run_cli("add", "nope")
            self.assertFalse(path.exists(), "nothing should have been written")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn('"nope" is not a preset', err)

    def test_removing_something_that_is_not_pinned_is_one_line_and_exit_1(self):
        with temp_config():
            rc, _, err = run_cli("remove", "htop")
        self.assertEqual(rc, 1)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn('no pin named "htop"', err)

    def test_a_config_it_cannot_parse_is_one_line_and_exit_1(self):
        # config.update raises ConfigError rather than replacing a file it
        # cannot read; the CLI turns it into a sentence, never a traceback.
        with temp_config("{ this is not json") as path:
            rc, out, err = run_cli("add", "htop")
            untouched = path.read_text(encoding="utf-8")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("cannot write config:", err)
        self.assertEqual(untouched, "{ this is not json")

    def test_a_config_it_cannot_parse_still_lists(self):
        # Reading degrades (config.load falls back to the defaults), so the
        # catalogue is still printed -- with a word about the file.
        with temp_config("{ this is not json"), mock.patch.object(
                pins.shutil, "which", NOTHING):
            rc, out, err = run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("htop", out)
        self.assertIn("config", err.lower())

    def test_an_unknown_subcommand_says_the_usage_and_exits_2(self):
        rc, out, err = run_cli("frobnicate")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("usage:", err)

    def test_an_unknown_option_is_refused(self):
        with temp_config() as path:
            rc, _, err = run_cli("add", "--colour", "red")
            self.assertFalse(path.exists())
        self.assertEqual(rc, 2)
        self.assertIn("--colour", err)

    def test_remove_needs_a_name(self):
        rc, _, err = run_cli("remove")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class TestTheSwitcherHint(unittest.TestCase):
    """`cswap` is the `claude-swap` package, and the hint has to say how to get it.

    The port took it for a personal tool with nothing to install; a colleague
    on a fresh machine then found no accounts row and no way to get one.
    """

    def test_the_hint_installs_claude_swap_on_both_platforms(self):
        for platform in ("darwin", "linux"):
            hint = pins.hint_for("cswap", platform)
            self.assertIn("claude-swap", hint, platform)
            self.assertIn("pipx install claude-swap", hint, platform)
            self.assertIn("github.com/realiti4/claude-swap", hint, platform)


if __name__ == "__main__":
    unittest.main()
