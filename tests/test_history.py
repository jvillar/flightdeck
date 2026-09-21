"""`flightdeck.history`: the grey rows, i.e. conversations that are over.

Three sources compete for the same rows: claude's transcripts
(`~/.claude/projects/<project>/<uuid>.jsonl`), codex's rollouts
(`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) and agy's conversations
(`<agy>/conversations/<uuid>.db` and `.pb`).

The JSON shapes below are DATA captured from the real tools (Claude Code
2.1.233, codex 0.153, agy 1.1.27): their keys and their odd values (an empty
`Title`, a null `WorkspaceURIs`) are reproduced as they were measured, not
tidied up.
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from flightdeck import history as fh

FIX = Path(__file__).parent / "fixtures"


class TestSessionMeta(unittest.TestCase):
    def test_a_custom_title_wins_over_an_ai_title(self):
        m = fh.session_meta(FIX / "with_title.jsonl")
        self.assertEqual(m["session_id"], "with_title")
        self.assertEqual(m["cwd"], "/Users/dev/Documents/code/demo")
        self.assertEqual(m["project"], "demo")
        self.assertEqual(m["title"], "My own title")
        self.assertIsInstance(m["last_activity"], float)

    def test_falls_back_to_the_ai_title_when_there_is_no_custom_one(self):
        m = fh.session_meta(FIX / "ai_title_only.jsonl")
        self.assertEqual(m["title"], "Only an AI title")
        self.assertIsNone(m["cwd"])
        # with no cwd, the project comes from the file name as a last resort
        self.assertEqual(m["project"], "ai_title_only")

    def test_a_json_line_that_is_not_an_object_does_not_blow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "odd.jsonl"
            # A valid JSON line that is NOT an object (it contains the needle
            # '"cwd"'): it must be skipped, not raise.
            p.write_text('"cwd"\n{"type":"custom-title","customTitle":"ok"}\n')
            m = fh.session_meta(p)
            self.assertEqual(m["title"], "ok")
            self.assertIsNone(m["cwd"])


class TestListRecent(unittest.TestCase):
    def _write(self, path, sid, mtime):
        path.write_text('{"type":"user","cwd":"/x/%s"}\n' % sid)
        os.utime(path, (mtime, mtime))

    def test_sorted_by_activity_and_excluding_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj1").mkdir()
            (root / "proj2").mkdir()
            self._write(root / "proj1" / "aaa.jsonl", "aaa", 1000.0)
            self._write(root / "proj2" / "bbb.jsonl", "bbb", 2000.0)
            self._write(root / "proj2" / "ccc.jsonl", "ccc", 3000.0)

            res = fh.list_recent_sessions(root)
            ids = [r["session_id"] for r in res]
            self.assertEqual(ids, ["ccc", "bbb", "aaa"])  # newest first

            res2 = fh.list_recent_sessions(root, exclude_ids={"ccc"})
            self.assertEqual([r["session_id"] for r in res2], ["bbb", "aaa"])

            res3 = fh.list_recent_sessions(root, limit=1)
            self.assertEqual([r["session_id"] for r in res3], ["ccc"])

    def test_subagent_files_are_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            self._write(root / "proj" / "real.jsonl", "real", 1000.0)
            # a nested file, <uuid>/subagents/*.jsonl: it must NOT show up
            (root / "proj" / "real" / "subagents").mkdir(parents=True)
            self._write(root / "proj" / "real" / "subagents" / "sub.jsonl", "sub", 2000.0)
            ids = [r["session_id"] for r in fh.list_recent_sessions(root)]
            self.assertEqual(ids, ["real"])

    def test_an_id_with_dangerous_characters_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            self._write(root / "proj" / "ok-123.jsonl", "ok-123", 1000.0)
            bad = root / "proj" / "a;rm -rf.jsonl"
            bad.write_text('{"type":"user","cwd":"/x"}')
            os.utime(bad, (2000.0, 2000.0))
            ids = [r["session_id"] for r in fh.list_recent_sessions(root)]
            self.assertEqual(ids, ["ok-123"])

    def test_limit_takes_only_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            self._write(root / "proj" / "old.jsonl", "old", 1000.0)
            self._write(root / "proj" / "middle.jsonl", "middle", 2000.0)
            self._write(root / "proj" / "new.jsonl", "new", 3000.0)
            ids = [r["session_id"] for r in fh.list_recent_sessions(root, limit=2)]
            self.assertEqual(ids, ["new", "middle"])


class TestScriptTranscriptsAreNotHistory(unittest.TestCase):
    """A `claude --print` (a wrapper may fire one for every Bash command of
    every session) leaves a transcript like anybody else, and they are the
    newest ones: 147 of the 150 grey rows on one machine. Their rows
    carry `"entrypoint":"sdk-cli"` (the interactive ones, "cli"): the first
    bytes are looked at, without parsing, and it keeps going down until it has
    gathered a real `limit`."""

    def _print(self, path, mtime):
        path.write_text('{"type":"user","cwd":"/Users/x","entrypoint":"sdk-cli",'
                        '"promptSource":"sdk","message":{"role":"user","content":"ROLE: guard"}}\n')
        os.utime(path, (mtime, mtime))

    def _real(self, path, sid, mtime, spaced=False):
        row = ('{"type": "user", "cwd": "/x/%s", "entrypoint": "cli"}\n' if spaced
               else '{"type":"user","cwd":"/x/%s","entrypoint":"cli"}\n') % sid
        path.write_text(row)
        os.utime(path, (mtime, mtime))

    def test_is_script_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._print(root / "p.jsonl", 1)
            self._real(root / "r.jsonl", "r", 1)
            self._real(root / "r2.jsonl", "r2", 1, spaced=True)
            (root / "old.jsonl").write_text('{"type":"user","cwd":"/x"}\n')  # no entrypoint
            (root / "empty.jsonl").write_text("")
            self.assertTrue(fh.is_script_transcript(root / "p.jsonl"))
            for f in ("r.jsonl", "r2.jsonl", "old.jsonl", "empty.jsonl", "nosuch.jsonl"):
                self.assertFalse(fh.is_script_transcript(root / f), f)

    def test_they_are_skipped_and_it_keeps_going_down_to_fill_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "home").mkdir(); (root / "proj").mkdir()
            for i in range(20):
                self._print(root / "home" / ("p%02d.jsonl" % i), 5000 + i)   # the newest ones
            self._real(root / "proj" / "aaa.jsonl", "aaa", 1000)
            self._real(root / "proj" / "bbb.jsonl", "bbb", 2000)
            self._real(root / "proj" / "ccc.jsonl", "ccc", 3000)
            res = fh.list_recent_sessions(root, limit=2)
            self.assertEqual([r["session_id"] for r in res], ["ccc", "bbb"])
            res = fh.list_recent_sessions(root)
            self.assertEqual([r["session_id"] for r in res], ["ccc", "bbb", "aaa"])

    def test_the_ones_already_known_are_not_opened_again(self):
        # Cache in a file: the second time round, the script transcripts we
        # already know about are skipped without opening them (with thousands of
        # them, looking at every header on every list cost 2 s).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "home").mkdir(); (root / "proj").mkdir()
            for i in range(10):
                self._print(root / "home" / ("p%02d.jsonl" % i), 5000 + i)
            self._real(root / "proj" / "aaa.jsonl", "aaa", 1000)
            cache = root / "cache.txt"
            with mock.patch.object(fh, "is_script_transcript",
                                   wraps=fh.is_script_transcript) as m:
                fh.list_recent_sessions(root, limit=5, cache_path=cache)
                first = m.call_count
                fh.list_recent_sessions(root, limit=5, cache_path=cache)
                second = m.call_count - first
            self.assertGreaterEqual(first, 10)
            self.assertEqual(second, 1)   # only the good one (aaa) is looked at again
            self.assertEqual(sorted(cache.read_text().split()), ["p%02d" % i for i in range(10)])

    def test_metadata_is_cached_by_mtime_and_size(self):
        # A finished transcript does not change: the second list does not parse
        # it whole again (reading them all cost ~1 s with 150 conversations).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "proj").mkdir()
            self._real(root / "proj" / "aaa.jsonl", "aaa", 1000)
            self._real(root / "proj" / "bbb.jsonl", "bbb", 2000)
            meta_cache = root / "meta.json"
            with mock.patch.object(fh, "session_meta", wraps=fh.session_meta) as m:
                r1 = fh.list_recent_sessions(root, cache_path=root / "c.txt",
                                             meta_cache_path=meta_cache)
                self.assertEqual(m.call_count, 2)
                r2 = fh.list_recent_sessions(root, cache_path=root / "c.txt",
                                             meta_cache_path=meta_cache)
                self.assertEqual(m.call_count, 2)   # not one parse more
            self.assertEqual(r1, r2)
            # If the file changes (mtime or size), it is parsed again.
            self._real(root / "proj" / "aaa.jsonl", "aaa-changed", 3000)
            with mock.patch.object(fh, "session_meta", wraps=fh.session_meta) as m:
                r3 = fh.list_recent_sessions(root, cache_path=root / "c.txt",
                                             meta_cache_path=meta_cache)
                self.assertEqual(m.call_count, 1)
            self.assertEqual([r["session_id"] for r in r3], ["aaa", "bbb"])
            self.assertEqual(r3[0]["cwd"], "/x/aaa-changed")

    def test_a_broken_or_missing_cache_does_not_get_in_the_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "proj").mkdir()
            self._real(root / "proj" / "aaa.jsonl", "aaa", 1000)
            self.assertEqual([r["session_id"] for r in
                              fh.list_recent_sessions(root, cache_path=root / "no" / "cache.txt")],
                             ["aaa"])
            (root / "c.txt").write_bytes(b"\xff\xfe junk")
            (root / "m.json").write_bytes(b"{broken")
            self.assertEqual([r["session_id"] for r in
                              fh.list_recent_sessions(root, cache_path=root / "c.txt",
                                                      meta_cache_path=root / "m.json")], ["aaa"])

    def test_the_inspection_cap_avoids_reading_thousands_of_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "home").mkdir(); (root / "proj").mkdir()
            for i in range(30):
                self._print(root / "home" / ("p%02d.jsonl" % i), 5000 + i)
            self._real(root / "proj" / "aaa.jsonl", "aaa", 1000)
            # With a cap of 10 the good one is never reached: empty list, but fast.
            self.assertEqual(fh.list_recent_sessions(root, limit=5, max_inspected=10), [])
            self.assertEqual([r["session_id"] for r in
                              fh.list_recent_sessions(root, limit=5, max_inspected=100)], ["aaa"])


def _rollout(path, sid, cwd="/x/retail", source="cli", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"timestamp": "2026-09-07T01:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"session_id": sid, "id": sid, "cwd": cwd,
                                            "source": source, "originator": "codex_cli"}})
                    + "\n" + json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestCodexHistory(unittest.TestCase):
    """codex's rollouts (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) join the
    history as grey rows with tool="codex". The ones from `codex exec` (non
    interactive: source=="exec" in their session_meta) are skipped, like the
    `claude --print` ones."""

    def test_meta_of_a_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rollout-2026-09-07T01-00-00-abc123.jsonl"
            _rollout(f, "abc123", cwd="/x/retail")
            m = fh.session_meta_codex(f)
        self.assertEqual(m["session_id"], "abc123")
        self.assertEqual(m["project"], "retail")
        self.assertEqual(m["tool"], "codex")
        self.assertIsNone(m["title"])

    def test_an_exec_is_not_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rollout-x.jsonl"
            _rollout(f, "abc", source="exec")
            self.assertIsNone(fh.session_meta_codex(f))
            (Path(tmp) / "broken.jsonl").write_text("{not json")
            self.assertIsNone(fh.session_meta_codex(Path(tmp) / "broken.jsonl"))

    def test_the_list_mixes_claude_and_codex_by_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude"; (root / "proj").mkdir(parents=True)
            codex = Path(tmp) / "codex"
            row = '{"type":"user","cwd":"/x/aaa","entrypoint":"cli"}\n'
            (root / "proj" / "aaa.jsonl").write_text(row)
            os.utime(root / "proj" / "aaa.jsonl", (2000, 2000))
            _rollout(codex / "2026/09/07" / "rollout-b.jsonl", "bbb-1", mtime=3000)
            _rollout(codex / "2026/09/07" / "rollout-c.jsonl", "ccc-1", mtime=1000)
            _rollout(codex / "2026/09/07" / "rollout-e.jsonl", "eee-1", mtime=4000, source="exec")
            res = fh.list_recent_sessions(root, codex_dir=codex,
                                          cache_path=Path(tmp) / "c.txt",
                                          meta_cache_path=Path(tmp) / "m.json")
        self.assertEqual([(r["session_id"], r.get("tool") or "claude") for r in res],
                         [("bbb-1", "codex"), ("aaa", "claude"), ("ccc-1", "codex")])

    def test_a_live_codex_is_not_also_a_grey_row(self):
        """`exclude_ids` carries the ids of the sessions already alive.

        For claude that is the file's own stem, so the comparison worked. A
        codex rollout is named `rollout-<timestamp>-<uuid>` and its id is the
        UUID INSIDE it, so the stem never matched: a codex you were looking at
        in its green tmux row was listed again underneath as history, and Enter
        on it would have started a second one on the same conversation. Carried
        over from the source, where the codex origin was added after the filter.
        """
        uuid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex"
            _rollout(codex / "2026/09/07" / ("rollout-2026-09-07T01-00-00-%s.jsonl" % uuid),
                     uuid, mtime=3000)
            _rollout(codex / "2026/09/07" / "rollout-2026-09-07T02-00-00-other.jsonl",
                     "other", mtime=1000)
            rows = fh.list_recent_sessions(Path(tmp) / "nothing-here", codex_dir=codex,
                                           exclude_ids={uuid},
                                           cache_path=Path(tmp) / "c.txt",
                                           meta_cache_path=Path(tmp) / "m.json")
        self.assertEqual([r["session_id"] for r in rows], ["other"])

    def test_an_id_that_is_only_part_of_a_word_does_not_exclude_a_rollout(self):
        # The match is on the whole last segment, not on any tail: two different
        # conversations whose uuids end alike must not silence each other.
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex"
            _rollout(codex / "2026/09/07" / "rollout-2026-09-07T01-00-00-abc123.jsonl",
                     "abc123", mtime=3000)
            rows = fh.list_recent_sessions(Path(tmp) / "nothing-here", codex_dir=codex,
                                           exclude_ids={"c123"},
                                           cache_path=Path(tmp) / "c.txt",
                                           meta_cache_path=Path(tmp) / "m.json")
        self.assertEqual([r["session_id"] for r in rows], ["abc123"])

    def test_the_execs_go_to_the_scripts_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex"
            _rollout(codex / "2026/09/07" / "rollout-e.jsonl", "eee-1", source="exec")
            cache = Path(tmp) / "c.txt"
            fh.list_recent_sessions(Path(tmp) / "nothing-here", codex_dir=codex,
                                    cache_path=cache, meta_cache_path=Path(tmp) / "m.json")
            self.assertIn("rollout-e", cache.read_text())


_AGY_UUID = "38777a1e-8cb4-45a0-973e-5d0ccb89c4df"


def _summary(**kw):
    """An agy `summary` with the REAL shape measured (empty Title,
    Preview holding the last message, null WorkspaceURIs)."""
    s = {"ID": _AGY_UUID, "Title": "", "Preview": "exit", "NumSteps": 7,
         "UpdatedAt": "2026-05-21T19:53:39.626445Z", "WorkspaceURIs": None,
         "AppDataDir": "antigravity-cli"}
    s.update(kw)
    return s


class TestAgyHistory(unittest.TestCase):
    """agy's conversations (`~/.gemini/antigravity-cli/conversations/`) join the
    history as grey rows with tool="agy". The file is opaque -- it gives the id
    (its name) and the date (its mtime) -- and what is readable, title and
    folder, lives in the two JSON caches next to it WHEN agy writes them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.empty_claude = self.tmp / "claude"
        (self.empty_claude / "proj").mkdir(parents=True)
        self.agy = self.tmp / "agy"
        (self.agy / "conversations").mkdir(parents=True)
        (self.agy / "cache").mkdir()
        self.pb = self._conversation(_AGY_UUID, ".pb")

    def _conversation(self, uuid, ext):
        """An agy conversation file, in whichever format. Both are opaque: the
        `.db` of 1.1.27 is SQLite and the old `.pb`, protobuf."""
        f = self.agy / "conversations" / (uuid + ext)
        f.write_bytes(b"SQLite format 3\x00opaque" if ext == ".db"
                      else b"\n\x08opaque\x12\x04pb")
        return f

    def _metadata(self, content):
        self._write("conversation_metadata.json", content)

    def _last(self, content):
        self._write("last_conversations.json", content)

    def _write(self, name, content):
        # A str is written AS IT IS: that is how the broken JSONs get tested too.
        text = content if isinstance(content, str) else json.dumps(content)
        (self.agy / "cache" / name).write_text(text, encoding="utf-8")

    def _convs(self, **kw):
        return {"conversations": {_AGY_UUID: {"summary": _summary(**kw),
                                              "is_internal": False,
                                              "last_modified_time": "2026-05-21T21:53:57+02:00"}}}

    def _list(self, **kw):
        # Caches in the temporary directory: the suite cannot write into the
        # real state directory.
        kw.setdefault("projects_dir", self.empty_claude)
        kw.setdefault("cache_path", self.tmp / "c.txt")
        kw.setdefault("meta_cache_path", self.tmp / "m.json")
        return fh.list_recent_sessions(**kw)

    def test_lists_the_conversation_with_the_folder_from_last_conversations(self):
        self._metadata(self._convs(Preview="fix the checkout"))
        self._last({"/Users/j/Documents/floralexperience": _AGY_UUID})
        out = self._list(agy_dir=self.agy)
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m["tool"], "agy")
        self.assertEqual(m["session_id"], _AGY_UUID)
        self.assertEqual(m["cwd"], "/Users/j/Documents/floralexperience")
        self.assertEqual(m["project"], "floralexperience")
        self.assertEqual(m["title"], "fix the checkout")   # Preview when there is no Title
        self.assertAlmostEqual(m["last_activity"], os.path.getmtime(self.pb), delta=1)

    def test_title_beats_preview_and_workspace_uris_beats_last_conversations(self):
        self._metadata(self._convs(Title="Checkout", Preview="fix the checkout",
                                   WorkspaceURIs=["file:///Users/j/other"]))
        self._last({"/Users/j/Documents/floralexperience": _AGY_UUID})
        m = self._list(agy_dir=self.agy)[0]
        self.assertEqual(m["title"], "Checkout")
        self.assertEqual(m["cwd"], "/Users/j/other")
        self.assertEqual(m["project"], "other")

    def test_with_no_known_folder_it_still_shows_up(self):
        self._metadata(self._convs())                 # WorkspaceURIs null
        self._last({"/Users/j/another/project": "some-other-id"})
        m = self._list(agy_dir=self.agy)[0]
        self.assertIsNone(m["cwd"])
        self.assertEqual(m["project"], "no folder")
        self.assertEqual(m["title"], "exit")

    def test_a_new_conversation_is_a_db_and_is_listed_too(self):
        """Measured with agy 1.1.27: a new conversation is saved as
        `<uuid>.db` (SQLite); the `.pb` is the old format and is still in the
        folder. Looking only for `*.pb`, the history saw NO new conversation."""
        self.pb.unlink()
        db = self._conversation(_AGY_UUID, ".db")
        self._last({"/Users/j/Documents/floralexperience": _AGY_UUID})
        out = self._list(agy_dir=self.agy)
        self.assertEqual([r["session_id"] for r in out], [_AGY_UUID])
        self.assertEqual(out[0]["tool"], "agy")
        self.assertEqual(out[0]["cwd"], "/Users/j/Documents/floralexperience")
        self.assertAlmostEqual(out[0]["last_activity"], os.path.getmtime(db), delta=1)

    def test_with_no_metadata_the_row_still_shows_with_id_and_date(self):
        """Measured with agy 1.1.27: `conversation_metadata.json` is NOT
        written -- neither when the turn ends nor on the way out -- so requiring
        it left the conversation out of the history FOREVER, rather than merely
        until the next pass. It shows up with the little that is known."""
        self._metadata({"conversations": {"another-uuid": {"summary": _summary()}}})
        self._last({})
        out = self._list(agy_dir=self.agy)
        self.assertEqual([r["session_id"] for r in out], [_AGY_UUID])
        self.assertIsNone(out[0]["title"])
        self.assertIsNone(out[0]["cwd"])
        self.assertEqual(out[0]["project"], "no folder")
        self.assertAlmostEqual(out[0]["last_activity"], os.path.getmtime(self.pb), delta=1)
        # And NEVER into the scripts cache (the veto there is forever): that is
        # for the rollout of a `codex exec`, which never changes its nature.
        cache = self.tmp / "c.txt"
        self.assertNotIn(_AGY_UUID, cache.read_text() if cache.exists() else "")

    def test_the_metadata_rules_when_agy_does_write_it(self):
        """The old box still works: with `conversation_metadata.json` written,
        title and folder come from there."""
        self._metadata(self._convs(Preview="fix the checkout"))
        self._last({})
        self.assertEqual([(r["session_id"], r["title"]) for r in self._list(agy_dir=self.agy)],
                         [(_AGY_UUID, "fix the checkout")])

    def test_a_long_preview_is_trimmed(self):
        self._metadata(self._convs(Preview="x" * 200))
        self._last({})
        m = self._list(agy_dir=self.agy)[0]
        self.assertEqual(m["title"], "x" * 60 + "…")

    def test_the_claude_tests_do_not_see_the_real_agy(self):
        self._metadata(self._convs())
        self._last({"/Users/j/Documents/floralexperience": _AGY_UUID})
        # With a TEST projects_dir and no agy_dir, AGY_DIR is not looked at.
        with mock.patch.object(fh, "AGY_DIR", self.agy):
            self.assertEqual(self._list(), [])

    def test_the_scripts_cache_and_exclude_ids_apply(self):
        self._metadata(self._convs())
        self._last({})
        self.assertEqual(self._list(agy_dir=self.agy, exclude_ids={_AGY_UUID}), [])
        (self.tmp / "c.txt").write_text(_AGY_UUID + "\n")
        self.assertEqual(self._list(agy_dir=self.agy), [])

    def test_missing_or_broken_caches_do_not_bring_the_list_down(self):
        # Without the two caches, or with both broken, agy's row shows up with
        # what is known of the file and the list stays whole (it also carries a
        # claude conversation): a JSON written by another program cannot wreck it.
        (self.empty_claude / "proj" / "aaa.jsonl").write_text(
            '{"type":"user","cwd":"/x/aaa","entrypoint":"cli"}\n')
        self.assertEqual([r["session_id"] for r in self._list(agy_dir=self.agy)],
                         ["aaa", _AGY_UUID])
        self._metadata("{not json")
        self._last("[1, 2, 3]")
        out = self._list(agy_dir=self.agy)
        self.assertEqual([r["session_id"] for r in out], ["aaa", _AGY_UUID])
        self.assertIsNone(out[1]["cwd"])

    def test_metadata_with_odd_shapes_degrades_to_less_data(self):
        self._metadata({"conversations": {_AGY_UUID: {"summary": "not a dict"}}})
        self._last({"/Users/j/Documents/floralexperience": _AGY_UUID})
        m = self._list(agy_dir=self.agy)[0]
        self.assertIsNone(m["title"])
        self.assertEqual(m["cwd"], "/Users/j/Documents/floralexperience")
        self._metadata(self._convs(Title=7, Preview=None, WorkspaceURIs="not a list"))
        m = fh.session_meta_agy(self.pb)
        self.assertIsNone(m["title"])
        self.assertEqual(m["cwd"], "/Users/j/Documents/floralexperience")

    def test_the_list_mixes_the_three_tools_by_date(self):
        (self.empty_claude / "proj" / "aaa.jsonl").write_text(
            '{"type":"user","cwd":"/x/aaa","entrypoint":"cli"}\n')
        os.utime(self.empty_claude / "proj" / "aaa.jsonl", (2000, 2000))
        codex = self.tmp / "codex"
        _rollout(codex / "2026/09/07" / "rollout-b.jsonl", "bbb-1", mtime=1000)
        self._metadata(self._convs())
        self._last({})
        os.utime(self.pb, (3000, 3000))
        res = self._list(agy_dir=self.agy, codex_dir=codex)
        self.assertEqual([(r["session_id"], r.get("tool") or "claude") for r in res],
                         [(_AGY_UUID, "agy"), ("aaa", "claude"), ("bbb-1", "codex")])


class TestTitleForSession(unittest.TestCase):

    def test_finds_the_transcript_by_id_and_takes_the_last_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            f = root / "proj" / "abc-1.jsonl"
            f.write_text(
                '{"type":"ai-title","aiTitle":"ai title"}\n'
                '{"type":"custom-title","customTitle":"first"}\n'
                '{"type":"custom-title","customTitle":"review redsys"}\n')
            self.assertEqual(fh.title_for_session("abc-1", root), "review redsys")

    def test_the_ai_title_does_not_count(self):
        """Only the human's /rename: an AI title on its own is as good as none."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            (root / "proj" / "abc-2.jsonl").write_text(
                '{"type":"ai-title","aiTitle":"ai title"}\n')
            self.assertIsNone(fh.title_for_session("abc-2", root))

    def test_no_transcript_or_an_odd_id_gives_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            self.assertIsNone(fh.title_for_session("does-not-exist", root))
            self.assertIsNone(fh.title_for_session("a;rm -rf", root))
            self.assertIsNone(fh.title_for_session(None, root))


if __name__ == "__main__":
    unittest.main()
