"""The installer for codex: the `notify` tee and `~/.codex/hooks.json`.

Nothing here touches the real `~/.codex`: `CONFIG_TOML`, `HOOKS_JSON` and
`OLD_PATH` are pointed at temporary files and `FLIGHTDECK_STATE_DIR` at a
temporary directory, so the saved notify delegate is a throwaway too.
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
from flightdeck.install import BACKUP_MARK, codex


@contextlib.contextmanager
def codex_home(tmp, config_toml=None, hooks_json=None, old_hooks=None):
    """Point the three codex paths and the state directory at throwaways.

    Each argument, when given, is the text that file starts with. -> the three
    paths, in the order (config.toml, hooks.json, the old plugins path).
    """
    root = Path(tmp) / "codex"
    paths = {"CONFIG_TOML": root / "config.toml",
             "HOOKS_JSON": root / "hooks.json",
             "OLD_PATH": root / "hooks" / "hooks.json"}
    for name, text in (("CONFIG_TOML", config_toml), ("HOOKS_JSON", hooks_json),
                       ("OLD_PATH", old_hooks)):
        if text is not None:
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            paths[name].write_text(text)
    with contextlib.ExitStack() as stack:
        for name, path in paths.items():
            stack.enter_context(mock.patch.object(codex, name, path))
        stack.enter_context(mock.patch.dict(
            os.environ, {"FLIGHTDECK_STATE_DIR": str(Path(tmp) / "state")}))
        yield paths["CONFIG_TOML"], paths["HOOKS_JSON"], paths["OLD_PATH"]


def run(fn, *args):
    """Call fn capturing what it prints -> (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


class TestRegisterNotify(unittest.TestCase):

    def test_it_wraps_the_existing_notify_and_returns_it_as_the_delegate(self):
        text = 'x = 1\nnotify = ["/Apps/Sky", "turn-ended"]\nmodel = "gpt"\n'
        new, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky", "turn-ended"])
        self.assertIn('notify = ["python3", "%s"]' % codex.TEE, new)
        self.assertNotIn("/Apps/Sky", new)
        self.assertIn('model = "gpt"', new)

    def test_with_no_previous_notify_it_adds_one_and_no_delegate(self):
        new, delegate = codex.register_notify('model = "gpt"\n')
        self.assertIsNone(delegate)
        self.assertIn("notify = ", new)

    def test_the_blank_line_under_the_notify_survives(self):
        # Found on a real config.toml: the trailing `\s*` of the pattern ate the
        # line break and the blank line with it, so the table under it ended up
        # glued to the notify line and the uninstall gave back a file one line
        # short of the one it was handed.
        text = 'notify = ["/Apps/Sky"]\n\n[tui]\nnotifications = true\n'
        new, _ = codex.register_notify(text)
        self.assertIn("]\n\n[tui]", new)
        self.assertEqual(codex.restore_notify(new, ["/Apps/Sky"])[0], text)

    def test_idempotent_it_does_not_overwrite_the_delegate(self):
        new, delegate = codex.register_notify('x = 1\nnotify = ["/Apps/Sky"]\n')
        again, delegate2 = codex.register_notify(new)
        self.assertEqual(again, new)
        # returning the tee here would overwrite the saved Sky command
        self.assertIsNone(delegate2)

    def test_a_trailing_comment_is_recognised_and_kept(self):
        # It did not match, so a SECOND `notify` key was prepended: two keys,
        # and a codex that will not start. The comment is the user's note about
        # that line, so it stays where they put it, whitespace and all -- which
        # is also what makes the round trip byte-equal.
        text = 'notify = ["/Apps/Sky"]  # my notifier\nmodel = "gpt"\n'
        new, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky"])
        # one KEY, not two: the tee's own file name carries the word as well
        self.assertEqual(len([l for l in new.split("\n") if l.startswith("notify =")]), 1)
        self.assertIn('notify = ["python3", "%s"]  # my notifier' % codex.TEE, new)
        self.assertEqual(codex.restore_notify(new, ["/Apps/Sky"])[0], text)

    def test_a_hash_inside_the_value_is_not_a_comment(self):
        text = 'notify = ["/Apps/Sky", "a#b"]\n'
        _, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky", "a#b"])

    def test_an_indented_key_keeps_its_indentation(self):
        new, delegate = codex.register_notify('  notify = ["/Apps/Sky"]\n')
        self.assertEqual(delegate, ["/Apps/Sky"])
        self.assertTrue(new.startswith("  notify = "), repr(new))

    def test_a_notify_it_cannot_read_is_refused_and_nothing_is_written(self):
        # Both of these used to end in TWO `notify` keys -- a duplicate key,
        # which codex refuses -- and with the user's own command neither saved
        # nor reported. Refusing is the only safe answer: the text comes back
        # untouched and the message says what to do.
        for text in ('notify = [\n  "/Apps/Sky",\n]\nmodel = "gpt"\n',
                     'notify = "/Apps/Sky"\n',
                     'notify = ["/Apps/Sky"\n'):
            with self.assertRaises(config.ConfigError) as caught:
                codex.register_notify(text)
            self.assertIn("notify", str(caught.exception))
            self.assertIn("nothing was written", str(caught.exception))

    def test_a_notify_inside_another_table_is_not_codexs_own(self):
        # After a table header, `notify = ...` is THAT table's key. Replacing it
        # would break a setting of theirs and leave the tee uninstalled.
        text = '[some_table]\nnotify = ["/Apps/Sky"]\n'
        new, delegate = codex.register_notify(text)
        self.assertIsNone(delegate)
        self.assertIn('[some_table]\nnotify = ["/Apps/Sky"]\n', new)   # untouched
        self.assertTrue(new.startswith("notify = "), repr(new))        # ours on top

    def test_a_bracket_inside_a_multi_line_value_is_not_a_table_header(self):
        # The preamble used to end at the first line beginning with `[`. That
        # line need not be a table header: here it is one element of a value,
        # and taking it for a header put the user's notify out of reach, so
        # ours was prepended and the file had two of them.
        text = 'foo = [\n  [1, 2],\n]\nnotify = ["/Apps/Sky"]\n'
        new, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky"])
        self.assertEqual(len([l for l in new.split("\n") if l.startswith("notify =")]), 1)
        self.assertIn("foo = [\n  [1, 2],\n]\n", new)     # their value, untouched

    def test_a_section_inside_a_multi_line_string_is_not_a_table_header(self):
        text = 'doc = """\n[section]\n"""\nnotify = ["/Apps/Sky"]\n'
        new, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky"])
        self.assertEqual(len([l for l in new.split("\n") if l.startswith("notify =")]), 1)
        self.assertIn('doc = """\n[section]\n"""\n', new)

    def test_a_notify_written_inside_a_string_is_not_the_key(self):
        # Same scan, the other way round: a line that LOOKS like the key but is
        # somebody's prose. Rewriting it would corrupt their text and leave the
        # real key -- and the tee -- alone.
        text = 'doc = """\nnotify = ["not-a-key"]\n"""\nnotify = ["/Apps/Sky"]\n'
        new, delegate = codex.register_notify(text)
        self.assertEqual(delegate, ["/Apps/Sky"])
        self.assertIn('notify = ["not-a-key"]', new)      # the prose is untouched
        self.assertNotIn('["python3", "%s"]\n"""' % codex.TEE, new)

    def test_a_unicode_separator_inside_a_string_does_not_end_a_line(self):
        # TOML ends a line at `\n` (and `\r\n`) and nowhere else. `splitlines`
        # also breaks on U+2028, U+2029, U+0085, \v and \f, so one of those
        # inside a string value -- a paragraph separator pasted into somebody's
        # prompt -- cut the scan at a line that is not there. What followed the
        # phantom break began with `[`, so it read as a table header, the
        # preamble ended early and the real `notify` was out of reach: ours got
        # prepended and codex was handed a file with two `notify` keys, which is
        # a codex that will not start.
        for sep in (" ", " ", "", "\v", "\f"):
            with self.subTest(sep=repr(sep)):
                text = 'title = "a%s[tui]"\nnotify = ["/Apps/Sky"]\n' % sep
                new, delegate = codex.register_notify(text)
                self.assertEqual(delegate, ["/Apps/Sky"])
                self.assertEqual(
                    len([l for l in new.split("\n") if l.startswith("notify =")]), 1)
                self.assertIn('title = "a%s[tui]"\n' % sep, new)   # theirs, untouched

    def test_the_state_of_a_notify_key(self):
        ours, _ = codex.register_notify("")
        self.assertEqual(codex.notify_state(ours), "ours")
        self.assertEqual(codex.notify_state('notify = ["/Apps/Sky"]\n'), "custom")
        self.assertEqual(codex.notify_state('notify = [\n"/Apps/Sky",\n]\n'), "unreadable")
        self.assertEqual(codex.notify_state('model = "gpt"\n'), "none")


class TestRegisterStatusLine(unittest.TestCase):
    """codex has no external status line command: it has a list of its own items.

    So the "line" Flightdeck gives it is the nearest thing -- its items in the
    order of our own line -- written into `[tui]` of config.toml. Every other
    line of that file has to come out byte for byte: it is the user's whole
    codex configuration.
    """

    OURS = 'status_line = %s' % json.dumps(codex.STATUS_LINE_ITEMS)

    def test_with_no_tui_table_it_adds_one_at_the_end(self):
        text = 'model = "gpt"\nnotify = ["x"]\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertTrue(new.startswith(text))          # nothing above is touched
        self.assertIn("[tui]", new)
        self.assertIn(self.OURS, new)
        self.assertIn("status_line_use_colors = true", new)

    def test_into_an_empty_file_it_writes_just_the_table(self):
        new, changed = codex.register_status_line("")
        self.assertTrue(changed)
        self.assertEqual(new, "[tui]\n" + self.OURS + "\nstatus_line_use_colors = true\n")

    def test_a_tui_table_without_a_status_line_gets_ours_and_keeps_its_keys(self):
        text = '[tui]\nnotifications = true\n\n[other]\nk = 1\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertIn("notifications = true", new)
        self.assertIn("[other]\nk = 1\n", new)
        self.assertIn(self.OURS, new)
        # ours goes INSIDE [tui], not into the table that follows it
        self.assertLess(new.index(self.OURS), new.index("[other]"))

    def test_a_status_line_of_their_own_is_left_exactly_as_it_is(self):
        text = '[tui]\nstatus_line = ["app-name", "thread-id"]\n'
        self.assertEqual(codex.register_status_line(text), (text, False))

    def test_force_replaces_theirs(self):
        text = '[tui]\nstatus_line = ["app-name"]\nnotifications = true\n'
        new, changed = codex.register_status_line(text, force=True)
        self.assertTrue(changed)
        self.assertNotIn("app-name", new)
        self.assertIn(self.OURS, new)
        self.assertIn("notifications = true", new)

    def test_ours_already_there_changes_nothing(self):
        text = '[tui]\n' + self.OURS + '\nstatus_line_use_colors = true\n'
        self.assertEqual(codex.register_status_line(text), (text, False))
        self.assertEqual(codex.register_status_line(text, force=True), (text, False))

    def test_ours_without_the_colours_gets_them_and_nothing_else(self):
        text = '[tui]\n' + self.OURS + '\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertEqual(new, text + "status_line_use_colors = true\n")

    def test_their_colour_choice_is_never_flipped(self):
        # `status_line_use_colors = false` is a decision, not a gap. And a
        # second copy of the key is not a preference: it is a TOML parse error.
        text = '[tui]\nstatus_line_use_colors = false\n'
        new, _ = codex.register_status_line(text)
        self.assertEqual(new.count("status_line_use_colors"), 1)
        self.assertIn("= false", new)

    def test_an_array_spanning_lines_counts_as_theirs(self):
        # Where it ends cannot be read off one line, so it is not something to
        # replace: not even with force.
        text = '[tui]\nstatus_line = [\n  "app-name",\n]\n'
        self.assertEqual(codex.register_status_line(text), (text, False))
        self.assertEqual(codex.register_status_line(text, force=True), (text, False))

    def test_a_dotted_key_outside_the_table_counts_as_theirs(self):
        # `tui.status_line = [...]` before any table header is the same key. A
        # second one inside `[tui]` would make codex refuse the whole file.
        text = 'tui.status_line = ["app-name"]\n[other]\nk = 1\n'
        self.assertEqual(codex.register_status_line(text), (text, False))
        self.assertEqual(codex.register_status_line(text, force=True), (text, False))

    def test_a_bare_tui_header_as_the_last_line_is_not_glued(self):
        # Measured: with no newline after `[tui]` at the end of the file, our
        # first key landed on the header line (`[tui]status_line = [...]`),
        # which codex refuses to parse -- and the damage did not heal itself,
        # because the header was no longer recognised on the way out.
        text = 'model = "gpt"\n[tui]'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertIn("[tui]\n" + self.OURS, new)
        self.assertEqual(new.count("[tui]"), 1)
        # and the table they wrote comes back, with the newline it was missing
        self.assertEqual(codex.restore_status_line(new), ('model = "gpt"\n[tui]\n', True))

    def test_an_empty_table_of_their_own_is_not_taken_away(self):
        # A `[tui]` they wrote and left empty is theirs. Only the table we
        # appended ourselves goes when it ends up empty again.
        text = '[tui]\n\n[other]\nk = 1\n'
        new, _ = codex.register_status_line(text)
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_spaces_inside_the_brackets_are_the_same_table(self):
        # `[ tui ]` is valid TOML. Unrecognised, it got a SECOND `[tui]` table
        # appended -- a duplicate table definition, which is the very thing the
        # module reads line by line to avoid.
        text = '[ tui ]\nnotifications = true\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertNotIn("[tui]", new)                 # no second table
        self.assertIn("[ tui ]\n" + self.OURS, new)    # ours inside theirs
        self.assertEqual(codex.status_line_state(new), "ours")
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_crlf_line_endings_are_the_same_table_too(self):
        # Measured while checking the two above: a config.toml with Windows line
        # endings (a WSL2 file edited from Windows) missed the header as well
        # and got a second `[tui]`. Our own lines go in with `\n`; TOML does not
        # mind the mixture, and a duplicate table is a codex that will not start.
        text = 'model = "gpt"\r\n[tui]\r\nnotifications = true\r\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertEqual(new.count("[tui]"), 1)
        self.assertIn("notifications = true", new)
        self.assertEqual(codex.status_line_state(new), "ours")
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_a_comment_after_the_header_is_still_the_tui_table(self):
        text = '[tui]  # mine\nnotifications = true\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertEqual(new.count("[tui]"), 1)           # no second table
        self.assertIn("[tui]  # mine\n" + self.OURS, new)
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_a_tui_header_inside_a_string_is_not_the_table(self):
        # A `[tui]` in somebody's prose is not a table: editing "inside" it
        # would write our keys into their string.
        text = 'doc = """\n[tui]\n"""\nmodel = "gpt"\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertIn(text, new)                          # their file, whole
        self.assertTrue(new.rstrip().endswith("status_line_use_colors = true"))
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_a_table_after_a_multi_line_value_still_ends_the_block(self):
        text = '[tui]\nfoo = [\n  [1, 2],\n]\n[other]\nk = 1\n'
        new, _ = codex.register_status_line(text)
        # ours goes inside [tui], their value and the table after it stay put
        self.assertLess(new.index(self.OURS), new.index("foo = ["))
        self.assertIn("[other]\nk = 1\n", new)
        self.assertEqual(codex.restore_status_line(new), (text, True))

    def test_a_sub_table_of_tui_is_not_the_tui_table(self):
        text = '[tui.colors]\nstatus_line = ["app-name"]\n'
        new, changed = codex.register_status_line(text)
        self.assertTrue(changed)
        self.assertIn("app-name", new)                 # theirs, under [tui.colors]
        self.assertIn("[tui]\n" + self.OURS, new)      # ours, in a table of its own

    def test_comments_and_spacing_survive(self):
        text = '# my codex\n[tui]\n# I like this one\nnotifications = true\n'
        new, _ = codex.register_status_line(text)
        for line in text.split("\n"):
            self.assertIn(line, new)

    def test_the_items_are_the_documented_ones_in_order(self):
        self.assertEqual(codex.STATUS_LINE_ITEMS,
                         ["model-with-reasoning", "project-name", "git-branch",
                          "context-used", "five-hour-limit", "weekly-limit",
                          "estimated-thread-cost"])


class TestStatusLineState(unittest.TestCase):

    def test_none_ours_and_custom(self):
        ours = '[tui]\nstatus_line = %s\n' % json.dumps(codex.STATUS_LINE_ITEMS)
        self.assertEqual(codex.status_line_state('model = "gpt"\n'), "none")
        self.assertEqual(codex.status_line_state('[tui]\nnotifications = true\n'), "none")
        self.assertEqual(codex.status_line_state(ours), "ours")
        self.assertEqual(codex.status_line_state('[tui]\nstatus_line = ["app-name"]\n'),
                         "custom")


class TestRestoreStatusLine(unittest.TestCase):

    def test_ours_comes_out_line_and_all(self):
        text = 'model = "gpt"\n'
        with_ours, _ = codex.register_status_line(text)
        self.assertEqual(codex.restore_status_line(with_ours), (text, True))

    def test_a_table_that_was_already_there_keeps_its_own_keys_and_header(self):
        text = '[tui]\nnotifications = true\n'
        with_ours, _ = codex.register_status_line(text)
        self.assertEqual(codex.restore_status_line(with_ours), (text, True))

    def test_a_status_line_of_their_own_is_never_removed(self):
        text = '[tui]\nstatus_line = ["app-name"]\nstatus_line_use_colors = true\n'
        self.assertEqual(codex.restore_status_line(text), (text, False))

    def test_a_colour_line_they_changed_is_left_behind(self):
        text = '[tui]\nstatus_line = %s\nstatus_line_use_colors = false\n' % \
            json.dumps(codex.STATUS_LINE_ITEMS)
        new, removed = codex.restore_status_line(text)
        self.assertTrue(removed)
        self.assertEqual(new, "[tui]\nstatus_line_use_colors = false\n")

    def test_with_nothing_of_ours_it_is_a_no_op(self):
        for text in ("", 'model = "gpt"\n', "[tui]\nnotifications = true\n"):
            self.assertEqual(codex.restore_status_line(text), (text, False))


class TestHooksWithFlightdeck(unittest.TestCase):

    def test_it_adds_the_events_without_touching_anything_foreign(self):
        foreign = {"hooks": [{"type": "command", "command": "other.sh"}]}
        d = codex.hooks_with_flightdeck(
            {"hooks": {"SessionStart": [foreign]}, "another_key": 1})
        self.assertEqual(d["another_key"], 1)
        start = d["hooks"]["SessionStart"]
        self.assertEqual(start[0], foreign)
        # `command` is a shell STRING (official docs), with the timeout in seconds
        self.assertIsInstance(start[1]["hooks"][0]["command"], str)
        self.assertIn("--tool codex", start[1]["hooks"][0]["command"])
        self.assertEqual(start[1]["hooks"][0]["timeout"], 5)
        self.assertIn("Stop", d["hooks"])
        for event in codex.EVENTS:
            self.assertTrue(any(codex.is_ours(e) for e in d["hooks"][event]), event)

    def test_reinstalling_does_not_duplicate(self):
        once = codex.hooks_with_flightdeck({})
        twice = codex.hooks_with_flightdeck(once)
        self.assertEqual(once, twice)


class TestPruningWhatWeDoNotUnderstand(unittest.TestCase):
    """The shared pruning rule over a hooks.json that is not shaped like one.

    `_without_entries` is called by `uninstall` and by the migration, and what
    it is given comes off a file people edit by hand. A `"hooks"` that is a
    number or a list used to reach `dict(...)` and raise `TypeError`, which is
    not one of the three exceptions `flightdeck install` catches: the whole
    command went down with a traceback over somebody's typo.
    """

    def test_a_hooks_key_that_is_not_a_mapping_is_left_exactly_as_it_was(self):
        for odd in (5, [{"x": 1}], "hooks", None):
            data = {"hooks": odd, "other": 1}
            out, removed = codex._without_entries(data, lambda entry: True)
            self.assertEqual(removed, [], odd)
            self.assertEqual(out, {"hooks": odd, "other": 1}, odd)

    def test_the_same_through_the_caller_uninstall_uses(self):
        out, removed = codex.hooks_without_flightdeck({"hooks": 5})
        self.assertEqual((out, removed), ({"hooks": 5}, []))

    def test_an_empty_hooks_key_it_did_not_empty_is_kept(self):
        # An empty `hooks` mapping is THEIRS: the rule removes a key it emptied
        # itself, not one that arrived empty.
        data = {"hooks": {}, "other": 1}
        out, removed = codex._without_entries(data, lambda entry: True)
        self.assertEqual((out, removed), ({"hooks": {}, "other": 1}, []))


class TestTheDelegateIsTheFileTheTeeReads(unittest.TestCase):
    """The installer saves the notify where the tee looks for it.

    Two modules, one file name: if they disagree, the user's own notify (their
    desktop notification, say) is never called again and nothing says so.
    """

    def test_both_sides_name_the_same_file(self):
        from flightdeck.hooks import codex_notify
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FLIGHTDECK_STATE_DIR": tmp}):
                self.assertEqual(codex.delegate_file(),
                                 codex_notify._delegate_file())


class TestInstall(unittest.TestCase):

    def test_it_writes_both_files_and_saves_the_previous_notify(self):
        toml = 'notify = ["/Apps/Sky", "turn-ended"]\nmodel = "gpt"\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, hooks, _):
                run(codex.install)
                self.assertIn(str(codex.TEE), config_toml.read_text())
                self.assertEqual(json.loads(codex.delegate_file().read_text()),
                                 ["/Apps/Sky", "turn-ended"])
                registered = json.loads(hooks.read_text())["hooks"]
                for event in codex.EVENTS:
                    self.assertTrue(any(codex.is_ours(e) for e in registered[event]),
                                    event)
                backups = list(config_toml.parent.glob("config.toml" + BACKUP_MARK + "*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), toml)

    def test_a_second_pass_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='model = "gpt"\n') as (toml, hooks, _):
                run(codex.install)
                snapshot = (toml.read_text(), hooks.read_text())
                _, text = run(codex.install)
                self.assertEqual((toml.read_text(), hooks.read_text()), snapshot)
                self.assertIn("already installed", text)

    def test_the_old_plugins_path_is_retired_when_it_is_ours(self):
        # An older cockpit's install went to `hooks/hooks.json`, which is the
        # PLUGINS path: codex validated and trusted it but never fired anything.
        # Leaving it there would be a second, dead installation.
        with tempfile.TemporaryDirectory() as tmp:
            ours = json.dumps({"hooks": codex.hooks_with_flightdeck({})["hooks"]})
            with codex_home(tmp, old_hooks=ours) as (_, _2, old_path):
                run(codex.install)
                self.assertFalse(old_path.exists())
                # deleting a file is the one thing here that cannot be undone,
                # so it is backed up first like every other write
                backups = list(old_path.parent.glob("hooks.json" + BACKUP_MARK + "*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), ours)

    def test_a_file_that_is_not_a_hooks_file_at_all_is_never_touched(self):
        # The gate used to be "every event has one of ours", which is vacuously
        # TRUE for a file with no `hooks` key: a plugins file was unlinked.
        with tempfile.TemporaryDirectory() as tmp:
            foreign = json.dumps({"plugins": [{"name": "someone-elses"}]})
            with codex_home(tmp, old_hooks=foreign) as (_, _2, old_path):
                run(codex.install)
                self.assertEqual(old_path.read_text(), foreign)
                self.assertEqual(list(old_path.parent.glob("*" + BACKUP_MARK + "*")), [])

    def test_an_old_file_mixing_ours_with_someone_elses_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixed = json.dumps({"hooks": {
                "Stop": [codex.hook_entry("Stop"),
                         {"hooks": [{"type": "command", "command": "their.sh"}]}]}})
            with codex_home(tmp, old_hooks=mixed) as (_, _2, old_path):
                run(codex.install)
                self.assertEqual(old_path.read_text(), mixed)

    def test_someone_elses_old_hooks_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            foreign = json.dumps({"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "their.sh"}]}]}})
            with codex_home(tmp, old_hooks=foreign) as (_, _2, old_path):
                run(codex.install)
                self.assertEqual(old_path.read_text(), foreign)


class TestInstallWritesTheItems(unittest.TestCase):

    def test_one_write_and_one_backup_for_both_changes(self):
        toml = 'model = "gpt"\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                changed, text = run(codex.install)
                written = config_toml.read_text()
                backups = list(config_toml.parent.glob("config.toml" + BACKUP_MARK + "*"))
                self.assertEqual(len(backups), 1)               # not one per change
                self.assertEqual(backups[0].read_text(), toml)
        self.assertIn(str(codex.TEE), written)                  # the notify tee
        self.assertIn(json.dumps(codex.STATUS_LINE_ITEMS), written)
        self.assertTrue(any("status line" in line for line in changed), changed)

    def test_a_status_line_of_their_own_is_left_alone_and_said_so(self):
        toml = '[tui]\nstatus_line = ["app-name"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                _, text = run(codex.install)
                self.assertIn('status_line = ["app-name"]', config_toml.read_text())
                self.assertNotIn("model-with-reasoning", config_toml.read_text())
        self.assertIn("--force", text)          # how to get ours in anyway

    def test_force_says_so_when_it_cannot_reach_the_value(self):
        toml = '[tui]\nstatus_line = [\n  "app-name",\n]\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                _, text = run(codex.install, True)
                self.assertIn('status_line = [\n  "app-name",\n]', config_toml.read_text())
                self.assertIn(str(config_toml), text)   # where to change it by hand
        self.assertIn("one line", text)

    def test_force_takes_theirs_over(self):
        toml = '[tui]\nstatus_line = ["app-name"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                run(codex.install, True)
                self.assertIn("model-with-reasoning", config_toml.read_text())

    def test_a_notify_it_cannot_read_stops_the_install_with_nothing_written(self):
        toml = 'notify = [\n  "/Apps/Sky",\n]\nmodel = "gpt"\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, hooks, _):
                with self.assertRaises(config.ConfigError) as caught:
                    codex.install()
                self.assertEqual(config_toml.read_text(), toml)   # byte for byte
                self.assertFalse(hooks.exists())                  # and no half install
                self.assertEqual(list(config_toml.parent.glob("*" + BACKUP_MARK + "*")), [])
                self.assertFalse(codex.delegate_file().exists())
        self.assertIn(str(config_toml), str(caught.exception))
        self.assertEqual(len(str(caught.exception).split("\n")), 1)

    def test_a_second_pass_leaves_the_items_exactly_as_they_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='model = "gpt"\n') as (config_toml, _, _2):
                run(codex.install)
                snapshot = config_toml.read_text()
                run(codex.install)
                self.assertEqual(config_toml.read_text(), snapshot)


class TestStatus(unittest.TestCase):

    def test_it_says_whose_the_status_line_items_are(self):
        cases = [('model = "gpt"\n', "none"),
                 ('[tui]\nstatus_line = ["app-name"]\n', "custom")]
        for toml, expected in cases:
            with tempfile.TemporaryDirectory() as tmp:
                with codex_home(tmp, config_toml=toml):
                    _, text = run(codex.status)
            self.assertIn("status line items: %s" % expected, text)
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='model = "gpt"\n'):
                run(codex.install)
                _, text = run(codex.status)
        self.assertIn("status line items: ours", text)

    def test_it_says_when_a_notify_is_there_and_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='notify = [\n  "/Apps/Sky",\n]\n'):
                _, text = run(codex.status)
        self.assertIn("cannot read", text)
        self.assertIn("notify", text)

    def test_installed_it_names_the_tee_the_delegate_and_the_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='notify = ["/Apps/Sky"]\n'):
                run(codex.install)
                _, text = run(codex.status)
        self.assertIn("notify → Flightdeck's tee: yes", text)
        self.assertIn("saved delegate: yes", text)
        for event in codex.EVENTS:
            self.assertIn(event, text)

    def test_with_nothing_installed_it_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp):
                _, text = run(codex.status)
        self.assertIn("notify → Flightdeck's tee: no", text)
        self.assertIn("saved delegate: no", text)
        self.assertIn("missing or unreadable", text)


class TestUninstall(unittest.TestCase):

    FOREIGN_HOOKS = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "their-hook.sh"}]}]}}

    def test_a_round_trip_leaves_both_foreign_files_byte_equal(self):
        toml = 'x = 1\nnotify = ["/Apps/Sky", "turn-ended"]\nmodel = "gpt"\n'
        hooks_text = json.dumps(self.FOREIGN_HOOKS, indent=2) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml,
                            hooks_json=hooks_text) as (config_toml, hooks, _):
                run(codex.install)
                self.assertNotEqual(config_toml.read_text(), toml)
                removed, _ = run(codex.uninstall)
                self.assertEqual(config_toml.read_text(), toml)
                self.assertEqual(hooks.read_text(), hooks_text)
                self.assertTrue(removed)

    def test_the_items_come_out_too_and_the_file_is_byte_equal(self):
        toml = '# mine\n[tui]\nnotifications = true\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                run(codex.install)
                self.assertIn("model-with-reasoning", config_toml.read_text())
                removed, _ = run(codex.uninstall)
                self.assertEqual(config_toml.read_text(), toml)
        self.assertTrue(any("status line" in line for line in removed), removed)

    def test_a_status_line_of_their_own_survives_the_uninstall(self):
        toml = 'notify = ["/Apps/Sky"]\n[tui]\nstatus_line = ["app-name"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml) as (config_toml, _, _2):
                run(codex.install)
                run(codex.uninstall)
                self.assertEqual(config_toml.read_text(), toml)

    def test_a_notify_that_was_only_ours_is_removed_line_and_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml='model = "gpt"\n') as (toml, _, _2):
                run(codex.install)
                run(codex.uninstall)
                self.assertEqual(toml.read_text(), 'model = "gpt"\n')

    def test_a_hooks_file_that_was_only_ours_goes_away(self):
        # We created it; emptied of our events it holds nothing at all, and a
        # `{}` left behind would look like a configuration the user made.
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp) as (_, hooks, _2):
                run(codex.install)
                self.assertTrue(hooks.exists())
                run(codex.uninstall)
                self.assertFalse(hooks.exists())

    def test_without_flightdeck_it_is_a_no_op(self):
        toml = 'model = "gpt"\n'
        hooks_text = json.dumps(self.FOREIGN_HOOKS, indent=2) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml,
                            hooks_json=hooks_text) as (config_toml, hooks, _):
                removed, text = run(codex.uninstall)
                self.assertEqual(removed, [])
                self.assertEqual(config_toml.read_text(), toml)
                self.assertEqual(hooks.read_text(), hooks_text)
                self.assertIn("Nothing to remove", text)
                self.assertEqual(list(config_toml.parent.glob("*" + BACKUP_MARK + "*")), [])


class TestAnUnreadableConfigToml(unittest.TestCase):
    """A `config.toml` that is not UTF-8 is a one-line refusal, not a traceback.

    It happens: a WSL2 file edited from a Windows editor. The claude and agy
    readers already turn the same case into a `ConfigError`; this one used to
    let a `UnicodeDecodeError` out of `status()` and `install()`.
    """

    NOT_UTF8 = b'model = "gpt"\nnotify = ["\xff\xfe"]\n'

    @contextlib.contextmanager
    def _codex(self, tmp):
        with codex_home(tmp) as (config_toml, hooks, old):
            config_toml.parent.mkdir(parents=True, exist_ok=True)
            config_toml.write_bytes(self.NOT_UTF8)
            yield config_toml, hooks, old

    def test_install_refuses_in_one_line_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._codex(tmp) as (config_toml, hooks, _old):
                with self.assertRaises(config.ConfigError) as caught:
                    run(codex.install)
                self.assertIn(str(config_toml), str(caught.exception))
                self.assertEqual(config_toml.read_bytes(), self.NOT_UTF8)
                self.assertFalse(hooks.exists())

    def test_status_says_it_instead_of_blowing_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._codex(tmp) as (config_toml, _hooks, _old):
                _, text = run(codex.status)
                self.assertIn("cannot read", text)
                self.assertIn(str(config_toml), text)

    def test_the_doctor_probe_reports_it_as_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._codex(tmp) as (config_toml, _hooks, _old):
                state = codex.installed_state()
        self.assertIn(str(config_toml), state["error"])


class TestAnUnreadableHooksJsonIsRefused(unittest.TestCase):
    """Read as empty it would be REBUILT, dropping the user's own entries."""

    def test_install_refuses_and_leaves_both_files_alone(self):
        toml = 'model = "gpt"\n'
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=toml,
                            hooks_json="{ not json") as (config_toml, hooks, _):
                with self.assertRaises(config.ConfigError) as caught:
                    run(codex.install)
                self.assertIn(str(hooks), str(caught.exception))
                self.assertEqual(hooks.read_text(), "{ not json")
                # and config.toml is untouched too: the notify is written first,
                # so the refusal has to come BEFORE anything is written at all.
                self.assertEqual(config_toml.read_text(), toml)
                self.assertEqual(list(config_toml.parent.glob("*" + BACKUP_MARK + "*")),
                                 [])


class TestOurOwnTeeAfterTheCodeMoved(unittest.TestCase):
    """A `notify` naming our tee at a path that is no longer here is OURS.

    Read as somebody else's it was saved as the delegate, and the tee then
    called the old tee, which called the delegate: a tee calling a tee, and the
    user's real notify buried one level further down on every code move.
    """

    def _stale(self):
        return 'notify = ["python3", "/old/place/flightdeck/hooks/%s"]\n' % codex.TEE.name

    def test_the_state_is_stale_and_not_custom(self):
        self.assertEqual(codex.notify_state(self._stale()), "stale")

    def test_install_rewrites_the_path_and_saves_no_delegate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=self._stale()) as (config_toml, _h, _o):
                run(codex.install)
                self.assertIn(str(codex.TEE), config_toml.read_text())
                self.assertNotIn("/old/place", config_toml.read_text())
                self.assertFalse(codex.delegate_file().exists())

    def test_the_delegate_the_user_already_had_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with codex_home(tmp, config_toml=self._stale()) as (_toml, _h, _o):
                codex.delegate_file().parent.mkdir(parents=True, exist_ok=True)
                codex.delegate_file().write_text('["/Apps/Sky"]')
                run(codex.install)
                self.assertEqual(json.loads(codex.delegate_file().read_text()),
                                 ["/Apps/Sky"])

    def test_register_notify_reports_no_delegate_to_save(self):
        new, delegate = codex.register_notify(self._stale())
        self.assertIsNone(delegate)
        self.assertIn(str(codex.TEE), new)


if __name__ == "__main__":
    unittest.main()
