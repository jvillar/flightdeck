"""`flightdeck.tools`: what varies per tool (claude / codex / agy), together.

The rest of Flightdeck knows nothing about tools: it asks here with the card's
`tool`. A missing or unknown `tool` means claude, which is the common case and
the legacy one (every card from before the multi-tool phase carries no field).
"""
import datetime
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from flightdeck import tools as ft


class TestMark(unittest.TestCase):
    def test_claude_carries_no_mark_it_is_the_common_case(self):
        self.assertIsNone(ft.mark_of("claude"))
        self.assertIsNone(ft.mark_of(None))
        self.assertIsNone(ft.mark_of(""))

    def test_codex_and_agy_are_marked(self):
        self.assertEqual(ft.mark_of("codex"), "codex")
        self.assertEqual(ft.mark_of("agy"), "agy")

    def test_an_unknown_tool_is_shown_as_it_is(self):
        # A card from a future tool cannot be left invisible.
        self.assertEqual(ft.mark_of("gemini"), "gemini")


class TestGlyph(unittest.TestCase):
    """NEW here, not ported: the identity table used to live in the picker
    (`_GLIFO`), and the status line needs it without importing the menu. The ANSI
    row formatting stays in the picker, and so does its test."""

    def test_a_known_tool_gives_its_glyph_and_colour(self):
        self.assertEqual(ft.glyph("claude"), ("✳", 173))
        self.assertEqual(ft.glyph("codex"), ("⬡", 36))
        self.assertEqual(ft.glyph("agy"), ("✦", 69))

    def test_an_unknown_or_absent_tool_has_no_glyph(self):
        # A row with no tool inside is a bare shell: it gets a blank, not
        # claude's glyph. The caller resolves the claude default itself.
        self.assertIsNone(ft.glyph("gemini"))
        self.assertIsNone(ft.glyph(None))
        self.assertIsNone(ft.glyph(""))


class TestResume(unittest.TestCase):
    def test_the_resume_argv_per_tool(self):
        self.assertEqual(ft.resume_argv("claude", "abc-1"), ["claude", "--resume", "abc-1"])
        self.assertEqual(ft.resume_argv(None, "abc-1"), ["claude", "--resume", "abc-1"])
        self.assertEqual(ft.resume_argv("codex", "019ffd5b-x"), ["codex", "resume", "019ffd5b-x"])
        # `--conversation=<id>`, glued: it is LITERALLY the command agy prints on
        # its way out ("Resume with -c (or command below): agy
        # --conversation=<id>", measured on 1.1.27). The form with
        # a space was never tried against the binary.
        self.assertEqual(ft.resume_argv("agy", "38777a1e-x"), ["agy", "--conversation=38777a1e-x"])

    def test_a_tool_we_cannot_resume_returns_none(self):
        # Better to type nothing than to invent a subcommand.
        self.assertIsNone(ft.resume_argv("gemini", "x"))


class TestCapabilities(unittest.TestCase):
    def test_only_claude_has_fork_and_flags(self):
        """Ctrl-F (--fork-session) and the Ctrl-L catalogue belong to claude: on
        rows of other tools those keys warn and do nothing."""
        self.assertTrue(ft.supports_fork("claude"))
        self.assertTrue(ft.supports_fork(None))
        self.assertFalse(ft.supports_fork("codex"))
        self.assertFalse(ft.supports_fork("agy"))


class TestCtxPctCodex(unittest.TestCase):
    """Codex's 🧠 comes from its rollout: the LAST `token_count` carries
    `last_token_usage` (the context that travelled in the last turn) and
    `model_context_window`. pct = (input+output of the last turn) / window."""

    def _rollout(self, folder, sid, events):
        f = Path(folder) / "2026" / "09" / "07" / ("rollout-2026-09-07T01-00-00-%s.jsonl" % sid)
        f.parent.mkdir(parents=True)
        lines = [json.dumps({"type": "session_meta", "payload": {"session_id": sid, "cwd": "/x"}})]
        for e in events:
            lines.append(json.dumps(e))
        f.write_text("\n".join(lines) + "\n")
        return f

    def _tc(self, inp, out, window):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": inp, "output_tokens": out},
            "model_context_window": window}}}

    def test_pct_of_the_last_token_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "abc-1", [self._tc(10000, 2000, 100000),
                                         {"type": "response_item", "payload": {"x": "padding " * 50}},
                                         self._tc(50000, 5000, 100000)])
            self.assertEqual(ft.ctx_pct_codex("abc-1", sessions_dir=tmp), 55)

    def test_without_a_rollout_or_without_a_token_count_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ft.ctx_pct_codex("does-not-exist", sessions_dir=tmp))
            self._rollout(tmp, "empty-1", [{"type": "response_item", "payload": {}}])
            self.assertIsNone(ft.ctx_pct_codex("empty-1", sessions_dir=tmp))

    def test_a_token_count_without_a_window_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "odd-1", [{"type": "event_msg", "payload": {"type": "token_count", "info": {}}}])
            self.assertIsNone(ft.ctx_pct_codex("odd-1", sessions_dir=tmp))


class TestCodexWorking(unittest.TestCase):
    def _card(self, sid, ago_s):
        upd = (datetime.datetime.now().astimezone()
               - datetime.timedelta(seconds=ago_s)).isoformat(timespec="seconds")
        return {"session_id": sid, "updated_at": upd}

    def _rollout(self, folder, sid, mtime_ago_s):
        f = Path(folder) / "2026" / "09" / "07" / ("rollout-x-%s.jsonl" % sid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}\n")
        t = time.time() - mtime_ago_s
        os.utime(f, (t, t))

    def test_a_rollout_written_after_the_stop_is_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "cx-1", 2)          # written 2 s ago
            card = self._card("cx-1", 60)          # the Stop was a minute ago
            self.assertTrue(ft.codex_working(card, sessions_dir=tmp))

    def test_a_rollout_untouched_since_the_stop_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "cx-2", 60)
            card = self._card("cx-2", 30)          # Stop AFTER the last write
            self.assertFalse(ft.codex_working(card, sessions_dir=tmp))

    def test_without_a_rollout_or_with_an_odd_card_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(ft.codex_working(self._card("nothing", 5), sessions_dir=tmp))
            self._rollout(tmp, "cx-3", 1)
            self.assertFalse(ft.codex_working({"session_id": "cx-3"}, sessions_dir=tmp))


class TestHandoverCodex(unittest.TestCase):
    def test_inheritable_flags_codex(self):
        argv = "codex --model frontier-x -p profile --search fix the lint".split()
        self.assertEqual(ft.inheritable_flags_codex(argv),
                         ["--model", "frontier-x", "-p", "profile", "--search"])

    def test_the_image_and_the_prompt_are_dropped(self):
        argv = "codex -i /tmp/f.png --model gpt-5.6 make me a logo".split()
        self.assertEqual(ft.inheritable_flags_codex(argv), ["--model", "gpt-5.6"])

    def test_handover_command(self):
        self.assertEqual(ft.handover_command("codex", "exp 2", ["--model", "gpt-5.6"]),
                         "codex --model gpt-5.6")
        self.assertIsNone(ft.handover_command("claude", "x", []))

    def test_is_argv_of(self):
        self.assertTrue(ft.is_argv_of("codex", ["/opt/homebrew/bin/codex", "--model", "x"]))
        self.assertFalse(ft.is_argv_of("codex", ["zsh", "-c", "something"]))
        self.assertTrue(ft.is_argv_of("claude", ["claude", "-n", "t"]))


class TestPayloadAgy(unittest.TestCase):
    def test_normalises_camel_case_to_claudes_keys(self):
        p = {"conversationId": "38777a1e-8cb4", "workspacePaths": ["/Users/j/proj", "/other"],
             "transcriptPath": "/Users/j/.gemini/antigravity-cli/transcript.jsonl",
             "modelName": "auto", "terminationReason": "model_stop", "fullyIdle": True}
        n = ft.payload_for("agy", p)
        self.assertEqual(n["session_id"], "38777a1e-8cb4")
        self.assertEqual(n["cwd"], "/Users/j/proj")
        self.assertEqual(n["transcript_path"], "/Users/j/.gemini/antigravity-cli/transcript.jsonl")
        self.assertEqual(n["terminationReason"], "model_stop")   # the rest, untouched
        self.assertEqual(p.get("session_id"), None)              # the original is not mutated

    def test_strips_the_file_uri_from_the_workspace(self):
        n = ft.payload_for("agy", {"workspacePaths": ["file:///Users/j/proj"]})
        self.assertEqual(n["cwd"], "/Users/j/proj")

    def test_other_tools_and_odd_payloads_pass_through(self):
        p = {"session_id": "x", "cwd": "/a"}
        self.assertIs(ft.payload_for("claude", p), p)
        self.assertIs(ft.payload_for("codex", p), p)
        self.assertEqual(ft.payload_for("agy", {"workspacePaths": "not-a-list"}).get("cwd"), None)
        self.assertEqual(ft.payload_for("agy", None), None)


class TestFlagsAgy(unittest.TestCase):
    def test_inherits_the_session_ones_and_drops_the_conversation_ones(self):
        argv = ["agy", "--conversation", "abc", "--mode", "plan", "--dangerously-skip-permissions",
                "--model", "gemini-3", "-c", "--effort", "high"]
        self.assertEqual(ft.inheritable_flags_agy(argv),
                         ["--mode", "plan", "--dangerously-skip-permissions",
                          "--model", "gemini-3", "--effort", "high"])

    def test_the_go_forms_single_dash_and_equals(self):
        self.assertEqual(ft.inheritable_flags_agy(["agy", "-mode", "plan", "-model=gemini-3", "--effort=low"]),
                         ["--mode", "plan", "--model=gemini-3", "--effort=low"])

    def test_the_non_interactive_flags_and_the_prompt_are_dropped(self):
        # -p/--print/--prompt/-i carry THAT agy's prompt: neither the flag nor its value
        self.assertEqual(ft.inheritable_flags_agy(["agy", "-p", "say hi", "--model", "m"]), ["--model", "m"])
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--prompt-interactive", "hi", "--sandbox"]), ["--sandbox"])
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--output-format", "json", "--json-schema", "{}"]), [])
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--new-project", "--log-file", "/tmp/x.log"]), [])

    def test_the_double_dash_cuts_the_sweep(self):
        # In Go, `--` (and a bare `-`) ends the flag section: what comes behind
        # is THAT agy's prompt, even when it looks like a flag.
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--sandbox", "--", "--model", "m"]),
                         ["--sandbox"])
        self.assertEqual(ft.inheritable_flags_agy(["agy", "-", "--model", "m"]), [])

    def test_a_flag_with_a_value_does_not_eat_another_flag(self):
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--model", "--sandbox"]), ["--model", "--sandbox"])

    def test_the_prompt_cuts_the_sweep_and_add_dir_is_truncated(self):
        # A loose token ends the flag section (like Go's `flag` package), so
        # anything typed behind the prompt is not inherited.
        self.assertEqual(ft.inheritable_flags_agy(["agy", "fix this", "--add-dir", "/a", "/b"]),
                         [])
        # And with no prompt in front, a variadic flag is truncated to its first
        # value: the second one is already a positional.
        self.assertEqual(ft.inheritable_flags_agy(["agy", "--add-dir", "/a", "/b"]),
                         ["--add-dir", "/a"])
        self.assertEqual(ft.inheritable_flags_agy(["agy"]), [])
        self.assertEqual(ft.inheritable_flags_agy([]), [])

    def test_a_word_from_the_prompt_is_not_inherited_as_a_flag(self):
        # `ps` flattens the line: without the cut, the `-foo` of a prompt like
        # "fix the -foo bug" would be born as a flag and the new agy would not
        # even start ("flag provided but not defined: -foo").
        self.assertEqual(
            ft.inheritable_flags_agy(["agy", "-i", "fix", "the", "-foo", "bug", "--model", "x"]),
            [])

    def test_dispatch_per_tool(self):
        self.assertEqual(ft.inheritable_flags_for("agy", ["agy", "--sandbox"]), ["--sandbox"])
        self.assertEqual(ft.inheritable_flags_for("agy", ["zsh", "-c", "--sandbox"]), [])   # recycled pid
        self.assertEqual(ft.inheritable_flags_for("codex", ["codex", "--model", "g"]), ["--model", "g"])
        self.assertEqual(ft.inheritable_flags_for("claude", ["claude", "--model", "g"]), [])
        self.assertEqual(ft.inheritable_flags_for(None, ["claude"]), [])


class TestHandoverAgy(unittest.TestCase):
    def test_is_argv_of_agy(self):
        self.assertTrue(ft.is_argv_of("agy", ["/Users/j/.local/bin/agy", "--sandbox"]))
        self.assertFalse(ft.is_argv_of("agy", ["codex"]))
        self.assertFalse(ft.is_argv_of("agy", []))

    def test_handover_command_agy_without_a_title_and_with_quoting(self):
        self.assertEqual(ft.handover_command("agy", "pricing 2", ["--mode", "plan", "--model", "a b"]),
                         "agy --mode plan --model 'a b'")
        self.assertEqual(ft.handover_command("agy", "x", []), "agy")
        self.assertIsNone(ft.handover_command("claude", "x", []))

    def test_where_agy_keeps_its_things(self):
        # `flightdeck.history` imports it to list agy's conversations.
        self.assertEqual(str(ft.AGY_DIR), str(Path.home()) + "/.gemini/antigravity-cli")


if __name__ == "__main__":
    unittest.main()
