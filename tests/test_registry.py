"""`flightdeck.registry`: Claude Code's own session registry
(`~/.claude/sessions/<pid>.json`) and the pane <-> job link of a PARKED
conversation (sent to the background with `/background` or with the ← arrow).

Real files seen in 2.1.233/2.1.234: the pane's interactive session
carries `kind: "interactive"`, `tmux: "retail_specs:@12.%15"` and
`parkedJobId: "e8053ca3"` (a SHORT id); the job carries `kind: "bg"`,
`jobId: "e8053ca3"` and its full `sessionId` `e8053ca3-2805-...`. Next to them
there are `<pid>.<hash>.key` files that are not JSON.
"""
import json
import tempfile
import unittest
from pathlib import Path

from flightdeck import registry


def _reg(folder, pid, **kw):
    d = {"pid": pid, "sessionId": kw.pop("sessionId", "s-%d" % pid),
         "cwd": "/x", "kind": "interactive", "entrypoint": "cli",
         "status": "busy", "updatedAt": 1787082365087}
    d.update(kw)
    (folder / ("%d.json" % pid)).write_text(json.dumps(d))


class TestReadRegistry(unittest.TestCase):
    def test_reads_the_pid_json_files_and_normalises(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            _reg(c, 57250, sessionId="afbe6f19-627f-444e-902b-a673d1b2a631",
                 tmux="retail_specs:@12.%15", parkedJobId="e8053ca3",
                 name="retail mrp wolf 5")
            _reg(c, 81752, sessionId="e8053ca3-2805-46e7-8c2a-06ea0d526875",
                 kind="bg", jobId="e8053ca3", name="retail mrp wolf 5")
            (c / "57250.abc.key").write_text("not json")
            (c / "junk.json").write_text("{broken")
            reg = registry.read_registry(c)
        by = {e["pid"]: e for e in reg}
        self.assertEqual(set(by), {57250, 81752})
        self.assertEqual(by[57250]["session_id"], "afbe6f19-627f-444e-902b-a673d1b2a631")
        self.assertEqual(by[57250]["kind"], "interactive")
        self.assertEqual(by[57250]["tmux"], "retail_specs:@12.%15")
        self.assertEqual(by[57250]["parked_job"], "e8053ca3")
        self.assertIsNone(by[57250]["job_id"])
        self.assertEqual(by[81752]["kind"], "bg")
        self.assertEqual(by[81752]["job_id"], "e8053ca3")
        self.assertIsNone(by[81752]["parked_job"])
        self.assertEqual(by[81752]["name"], "retail mrp wolf 5")

    def test_missing_or_empty_directory(self):
        self.assertEqual(registry.read_registry("/does/not/exist"), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(registry.read_registry(tmp), [])

    def test_an_entry_without_a_numeric_pid_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            (c / "1.json").write_text(json.dumps({"pid": "x", "sessionId": "s"}))
            (c / "2.json").write_text(json.dumps(["a list"]))
            _reg(c, 3)
            self.assertEqual([e["pid"] for e in registry.read_registry(c)], [3])


class TestParkedMap(unittest.TestCase):
    """{session_id of the pane's card: session_id of its job} for the cards
    whose claude is PARKED according to the registry. It only counts when the
    job has a card (otherwise there is no state to show) and the pane's pid is
    alive."""

    REG = [
        {"pid": 57250, "session_id": "afbe6f19-x", "kind": "interactive",
         "tmux": "retail_specs:@12.%15", "parked_job": "e8053ca3", "job_id": None},
        {"pid": 81752, "session_id": "e8053ca3-2805-x", "kind": "bg",
         "tmux": None, "parked_job": None, "job_id": "e8053ca3"},
        {"pid": 8832, "session_id": "65cfe20d-x", "kind": "interactive",
         "tmux": "recommender:@1.%22", "parked_job": None, "job_id": None},
    ]
    CARDS = [
        {"session_id": "afbe6f19-x", "pid": 57250, "tmux_pane": "%15", "state": "needs_attention"},
        {"session_id": "e8053ca3-2805-x", "pid": 81752, "tmux_pane": None, "state": "looping"},
        {"session_id": "65cfe20d-x", "pid": 8832, "tmux_pane": "%22", "state": "working"},
    ]

    def test_links_a_parked_pane_with_its_job(self):
        self.assertEqual(registry.parked_map(self.CARDS, self.REG),
                         {"afbe6f19-x": "e8053ca3-2805-x"})

    def test_the_short_registry_id_matches_the_long_one_on_the_card(self):
        # parkedJobId is short ("e8053ca3"); the job's card carries the full one.
        reg = [dict(self.REG[0], parked_job="e8053ca3"), self.REG[1]]
        self.assertEqual(registry.parked_map(self.CARDS, reg),
                         {"afbe6f19-x": "e8053ca3-2805-x"})

    def test_without_the_jobs_card_there_is_no_link(self):
        cards = [self.CARDS[0], self.CARDS[2]]
        self.assertEqual(registry.parked_map(cards, self.REG), {})

    def test_without_a_registry_or_without_a_pid_there_is_no_link(self):
        self.assertEqual(registry.parked_map(self.CARDS, []), {})
        cards = [dict(self.CARDS[0], pid=None), self.CARDS[1]]
        self.assertEqual(registry.parked_map(cards, self.REG), {})

    def test_the_registry_pid_decides_not_the_session_id(self):
        # The registry is indexed by pid (that is its file name); the pane's
        # card is matched by pid, which is what the hook records with getppid.
        reg = [dict(self.REG[0], session_id="another-id"), self.REG[1]]
        self.assertEqual(registry.parked_map(self.CARDS, reg),
                         {"afbe6f19-x": "e8053ca3-2805-x"})


class TestStatusByPid(unittest.TestCase):
    REG = [{"pid": 10, "session_id": "a", "kind": "interactive", "status": "busy"},
           {"pid": 11, "session_id": "b", "kind": "bg", "status": "idle"},
           {"pid": 12, "session_id": "c", "kind": "interactive", "status": None}]

    def test_returns_the_registry_status_for_that_pid(self):
        self.assertEqual(registry.status_by_pid(self.REG), {10: "busy", 11: "idle"})
        self.assertEqual(registry.status_by_pid([]), {})

    def test_read_registry_brings_the_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            _reg(c, 5, status="waiting", waitingFor="permission")
            e = registry.read_registry(c)[0]
        self.assertEqual((e["status"], e["waiting_for"]), ("waiting", "permission"))


class TestTheTimestamps(unittest.TestCase):
    """`startedAt`, `statusUpdatedAt` and `updatedAt` are epochs in
    MILLISECONDS. `common` needs the three of them to build a row for a claude
    that predates the hooks: the last one is the row's age, and the first two
    together say whether an `idle` has ever moved from the one it was born with
    (a session just opened, not one waiting for you)."""

    def test_the_three_epochs_come_through_as_integers(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            _reg(c, 7, startedAt=1789565714589, statusUpdatedAt=1789565714635,
                 updatedAt=1789565714635)
            e = registry.read_registry(c)[0]
        self.assertEqual(e["started_at"], 1789565714589)
        self.assertEqual(e["status_updated_at"], 1789565714635)
        self.assertEqual(e["updated_at"], 1789565714635)

    def test_anything_that_is_not_a_number_is_None(self):
        # `True` is an int in python: without excluding it by hand a
        # `"startedAt": true` would sail through as the epoch 1.
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            _reg(c, 8, startedAt="yesterday", statusUpdatedAt=True, updatedAt=None)
            e = registry.read_registry(c)[0]
        self.assertIsNone(e["started_at"])
        self.assertIsNone(e["status_updated_at"])
        self.assertIsNone(e["updated_at"])

    def test_a_missing_one_is_None_and_nothing_blows_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp)
            (c / "9.json").write_text(json.dumps({"pid": 9, "sessionId": "s"}))
            e = registry.read_registry(c)[0]
        self.assertEqual((e["started_at"], e["status_updated_at"], e["updated_at"]),
                         (None, None, None))


if __name__ == "__main__":
    unittest.main()
