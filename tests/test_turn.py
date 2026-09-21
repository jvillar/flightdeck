"""`flightdeck.turn`: read the end of the transcript and decide whether the
session is left LOOPING (waiting for its timer) or WAITING FOR YOU.

The transcripts here are synthetic but they copy the real rows of 2.1.233 (seen
live): a round of `/loop` leaves a `system` row with
`subtype: scheduled_task_fire` and right after it the `user` row of the prompt,
`isMeta: true` and `promptSource: "system"`, with `parentUuid` = the system
row's uuid. A typed prompt carries `promptSource: "typed"` (and
`origin: {kind: human}`).

The JSON rows below are DATA copied from Claude Code 2.1.233: keys and values
are reproduced as they were captured, not translated.
"""
import json
import tempfile
import unittest
from pathlib import Path

from flightdeck import turn as ft


def _row(**kw):
    return json.dumps(kw, ensure_ascii=False)


def _typed(text, uuid="u1"):
    return _row(type="user", uuid=uuid, parentUuid="p0", promptSource="typed",
                origin={"kind": "human"}, message={"role": "user", "content": text})


def _fire(text, uuid_sys="s1", uuid="u2"):
    return [
        _row(type="system", subtype="scheduled_task_fire", uuid=uuid_sys,
             content="Running scheduled task (Aug 17 7:04pm)", isMeta=False),
        _row(type="user", uuid=uuid, parentUuid=uuid_sys, isMeta=True,
             promptSource="system", queuePriority="later",
             message={"role": "user", "content": text}),
    ]


def _tool_use(name, inp, uuid="a1"):
    return _row(type="assistant", uuid=uuid, message={
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t-" + uuid, "name": name, "input": inp}]})


def _tool_result(text="ok", uuid="r1"):
    return _row(type="user", uuid=uuid, message={
        "role": "user",
        "content": [{"tool_use_id": "t", "type": "tool_result", "content": text}]})


def _text(text, uuid="a2"):
    return _row(type="assistant", uuid=uuid, message={
        "role": "assistant", "content": [{"type": "text", "text": text}]})


def _skill_meta(uuid="m1"):
    # What loading a skill injects: user + isMeta, WITHOUT promptSource, with sourceToolUseID.
    return _row(type="user", uuid=uuid, isMeta=True, sourceToolUseID="toolu_x",
                message={"role": "user", "content": [{"type": "text", "text": "Approach this as..."}]})


def _noise():
    return [_row(type="attachment", attachment={"type": "hook_success"}),
            _row(type="queue-operation", operation="dequeue"),
            _row(type="system", subtype="turn_duration"),
            _row(type="custom-title", customTitle="x")]


class _WithTranscript(unittest.TestCase):
    def _write(self, rows):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / "s.jsonl"
        p.write_text("\n".join(rows) + "\n")
        return str(p)


class TestTurnStart(_WithTranscript):
    def test_a_typed_prompt_is_a_prompt_start(self):
        p = self._write([_typed("hello"), _tool_use("Bash", {"command": "ls"}),
                         _tool_result(), _text("done")] + _noise())
        a = ft.analyze_turn(p)
        self.assertEqual(a["start"], "prompt")
        self.assertIsNone(a["schedule"])
        self.assertFalse(ft.is_loop(a))

    def test_a_loop_round_is_a_fire_start(self):
        p = self._write([_typed("an older turn", uuid="u0"), _text("its old answer", uuid="a0")]
                        + _fire("On-call check") +
                        [_tool_use("Bash", {"command": "curl"}), _tool_result(),
                         _text("nothing to report")] + _noise())
        a = ft.analyze_turn(p)
        self.assertEqual(a["start"], "fire")
        self.assertTrue(ft.is_loop(a))

    def test_a_system_meta_user_without_its_fire_row_is_not_a_round(self):
        # A turn injected by the machine that is NOT a timer (the notice of a
        # background task, a message from another agent): promptSource "system"
        # but without the scheduled_task_fire row as its parent. That is NOT a
        # loop: claude has finished and really is waiting for you.
        p = self._write([_text("before", uuid="a0"),
                         _row(type="user", uuid="u9", parentUuid="a0", isMeta=True,
                              promptSource="system",
                              message={"role": "user", "content": "task done"}),
                         _text("right")])
        a = ft.analyze_turn(p)
        self.assertEqual(a["start"], "prompt")
        self.assertFalse(ft.is_loop(a))

    def test_a_skills_meta_row_mid_turn_is_not_the_start(self):
        p = self._write([_typed("write me a report"), _tool_use("Skill", {"skill": "x"}),
                         _tool_result("Launching skill"), _skill_meta(),
                         _tool_use("Bash", {"command": "ls"}), _tool_result(), _text("ok")])
        a = ft.analyze_turn(p)
        self.assertEqual(a["start"], "prompt")

    def test_tool_results_are_not_a_start(self):
        p = self._write(_fire("round") + [_tool_use("Bash", {"command": "x"}),
                                          _tool_result("enormous output " * 1000)])
        self.assertEqual(ft.analyze_turn(p)["start"], "fire")

    def test_sidechain_rows_are_ignored(self):
        # A subagent writing into the same file (isSidechain) counts neither as
        # a start nor as a schedule.
        side = _row(type="user", uuid="sc", isSidechain=True, promptSource="typed",
                    message={"role": "user", "content": "the subagent's prompt"})
        side_tool = _row(type="assistant", uuid="sa", isSidechain=True, message={
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "ScheduleWakeup", "input": {"delaySeconds": 60}}]})
        p = self._write(_fire("round") + [side, side_tool, _text("end")])
        a = ft.analyze_turn(p)
        self.assertEqual(a["start"], "fire")
        self.assertIsNone(a["schedule"])

    def test_without_promptSource_isMeta_decides(self):
        # Transcripts from versions without promptSource: a non-meta user is a typed prompt.
        p = self._write([_row(type="user", uuid="u1",
                              message={"role": "user", "content": "hello"}),
                         _text("hello")])
        self.assertEqual(ft.analyze_turn(p)["start"], "prompt")


class TestScheduleWithinTheTurn(_WithTranscript):
    def test_a_pending_ScheduleWakeup_loops_even_if_it_started_typed(self):
        # The first turn of a `/loop` with no interval: the user types, the
        # model schedules itself a wake-up and finishes. That is already a loop.
        p = self._write([_typed("/loop watch X"),
                         _tool_use("ScheduleWakeup", {"delaySeconds": 900, "prompt": "watch X",
                                                      "reason": "x", "noop": True}),
                         _tool_result(), _text("see you later")])
        a = ft.analyze_turn(p)
        self.assertEqual((a["start"], a["schedule"]), ("prompt", "pending"))
        self.assertTrue(ft.is_loop(a))

    def test_ScheduleWakeup_stop_breaks_the_loop_even_on_a_round(self):
        p = self._write(_fire("round") + [_tool_use("ScheduleWakeup", {"stop": True}),
                                          _tool_result(), _text("loop finished")])
        a = ft.analyze_turn(p)
        self.assertEqual((a["start"], a["schedule"]), ("fire", "stopped"))
        self.assertFalse(ft.is_loop(a))

    def test_CronCreate_loops_and_CronDelete_stops_it(self):
        p = self._write([_typed("/loop 10m check"),
                         _tool_use("CronCreate", {"cron": "*/10 * * * *", "prompt": "check"}),
                         _tool_result(), _text("scheduled")])
        self.assertTrue(ft.is_loop(ft.analyze_turn(p)))
        p = self._write(_fire("round") + [_tool_use("CronDelete", {"id": "abc"}),
                                          _tool_result(), _text("loop stopped")])
        self.assertFalse(ft.is_loop(ft.analyze_turn(p)))

    def test_the_last_schedule_of_the_turn_wins(self):
        p = self._write([_typed("x"),
                         _tool_use("ScheduleWakeup", {"delaySeconds": 60}, uuid="a1"),
                         _tool_result(uuid="r1"),
                         _tool_use("ScheduleWakeup", {"stop": True}, uuid="a3"),
                         _tool_result(uuid="r3"), _text("end")])
        self.assertEqual(ft.analyze_turn(p)["schedule"], "stopped")

    def test_a_schedule_from_an_earlier_turn_does_not_count(self):
        p = self._write([_typed("/loop x", uuid="u0"),
                         _tool_use("ScheduleWakeup", {"delaySeconds": 60}, uuid="a0"),
                         _tool_result(uuid="r0"), _text("ok", uuid="t0"),
                         _typed("wait, a question", uuid="u1"), _text("an answer", uuid="t1")])
        a = ft.analyze_turn(p)
        self.assertEqual((a["start"], a["schedule"]), ("prompt", None))
        self.assertFalse(ft.is_loop(a))


class TestRobustness(_WithTranscript):
    def test_a_missing_empty_or_garbage_file(self):
        for path in (None, "", "/no/such/file.jsonl"):
            a = ft.analyze_turn(path)
            self.assertIsNone(a["start"], path)
            self.assertFalse(ft.is_loop(a))
        p = self._write(["this is not json", "{not this either", ""])
        self.assertFalse(ft.is_loop(ft.analyze_turn(p)))
        self.assertFalse(ft.is_loop(None))
        self.assertFalse(ft.is_loop({}))

    def test_the_start_is_found_even_across_a_chunk_boundary(self):
        # It is read backwards in chunks: a row split between two chunks has to
        # be put back together. A small chunk on purpose, to force it.
        rows = _fire("round") + [_tool_use("Bash", {"command": "x" * 500}), _tool_result("y" * 700),
                                 _text("z" * 300)]
        p = self._write(rows)
        for chunk in (64, 100, 333, 1024):
            with self.subTest(chunk=chunk):
                a = ft.analyze_turn(p, chunk=chunk)
                self.assertEqual(a["start"], "fire")
                self.assertTrue(a["complete"])

    def test_with_a_read_cap_the_start_is_not_invented(self):
        rows = _fire("round") + [_tool_result("x" * 5000)] * 5 + [_text("end")]
        p = self._write(rows)
        a = ft.analyze_turn(p, max_bytes=2000)
        self.assertIsNone(a["start"])
        self.assertFalse(a["complete"])
        self.assertFalse(ft.is_loop(a))

    def test_a_giant_line_does_not_blow_up(self):
        p = self._write(_fire("round") + [_tool_result("A" * 300000), _text("end")])
        self.assertEqual(ft.analyze_turn(p)["start"], "fire")


if __name__ == "__main__":
    unittest.main()
