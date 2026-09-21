"""Is the claude that just stopped waiting for YOU, or for its TIMER?

A session with `/loop` (or one that schedules itself with `ScheduleWakeup`) ends
turn after turn, and for Flightdeck "the turn ended" meant "waiting for you" --
so `⏳ WAITING FOR YOU` kept turning up on a session that was waiting for
nobody. The only signal readable from outside is in the session's TRANSCRIPT
(Claude Code 2.1.233):

- Every round of a loop leaves a `system` row with `subtype:
  "scheduled_task_fire"` ("Running scheduled task") and right after it the `user`
  row of the prompt, `isMeta: true`, `promptSource: "system"`, with `parentUuid`
  = that system row's uuid. A typed prompt carries `promptSource: "typed"`.
- Scheduling a round is a tool: `ScheduleWakeup` (`stop: true` stops it),
  `CronCreate`, `CronDelete`. They show up as `tool_use` with their input.

(The `UserPromptSubmit` hook's payload has a `source` field with exactly this
information, but in the binary it is compiled OUT for external users -- "only set
for Anthropic-internal sessions while the field is trialed" -- and session crons
live in memory, not on disk. Hence the transcript.)

It is read BACKWARDS, only as far back as the start of the turn that has just
finished, with a byte cap: a long turn cannot hang the hook. This is transcript
STRUCTURE, not tokens (the same class of dependency `flightdeck.history` has on
titles): if Claude Code changes those rows this degrades to "waiting for you" --
what it did before -- never to anything worse.
"""
import json
import re

# How much transcript is read backwards at most while looking for the start of
# the turn. A turn with many tools (or images) can take megabytes; past this the
# answer is "I don't know" (which is "not a loop").
MAX_BYTES = 8 * 1024 * 1024
CHUNK = 64 * 1024

# Tools that schedule (or unschedule) a future round.
_SCHEDULERS = ("ScheduleWakeup", "CronCreate", "CronDelete")

# Cheap filters before parsing JSON: only the lines that can matter are decoded
# (the tool_result ones with enormous outputs are skipped without opening them).
_RE_USER = re.compile(rb'"type"\s*:\s*"user"')
_RE_ASSISTANT = re.compile(rb'"type"\s*:\s*"assistant"')
_RE_SYSTEM = re.compile(rb'"type"\s*:\s*"system"')
_RE_TOOL_RESULT = re.compile(rb'"type"\s*:\s*"tool_result"')
_RE_SCHEDULE = re.compile(rb'"name"\s*:\s*"(?:ScheduleWakeup|CronCreate|CronDelete)"')


def lines_backwards(path, max_bytes=MAX_BYTES, chunk=CHUNK):
    """The file's lines from the LAST to the first, reading in chunks.

    It stops on reaching the beginning or on going past `max_bytes` bytes; in the
    second case the last (split) line is not handed over. Any I/O error ends the
    walk silently: the caller treats "there is no more" as "I don't know".
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            read = 0
            rest = b""
            while pos > 0 and read < max_bytes:
                n = min(chunk, pos, max_bytes - read)   # the cap holds to the byte
                pos -= n
                f.seek(pos)
                data = f.read(n) + rest
                read += n
                parts = data.split(b"\n")
                rest = parts[0]
                for p in reversed(parts[1:]):
                    if p.strip():
                        yield p
            if pos == 0 and rest.strip():
                yield rest
    except Exception:
        return


def _parse(raw):
    try:
        row = json.loads(raw)
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def _blocks(row):
    content = (row.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _is_start(row):
    """Is this `user` row the PROMPT a turn started with?

    These are not: the `tool_result` ones (they travel as user), the rows that
    loading a skill injects (user + isMeta, with `sourceToolUseID` and without
    `promptSource`) nor a subagent's (`isSidechain`). With `promptSource` present
    -- typed, queued, system... -- it is a prompt submission, whoever sent it;
    without it (old transcripts) `isMeta` decides: non-meta = typed.
    """
    if row.get("type") != "user" or row.get("isSidechain"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
    elif not isinstance(content, str):
        return False
    if row.get("promptSource"):
        return True
    return not row.get("isMeta") and not row.get("sourceToolUseID")


def _schedule_of(row):
    """"pending" / "stopped" according to the LAST scheduling tool in this
    assistant row, or None when there is none."""
    if row.get("type") != "assistant" or row.get("isSidechain"):
        return None
    for b in reversed(_blocks(row)):
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        name = b.get("name")
        if name == "ScheduleWakeup":
            tool_input = b.get("input") if isinstance(b.get("input"), dict) else {}
            return "stopped" if tool_input.get("stop") is True else "pending"
        if name == "CronCreate":
            return "pending"
        if name == "CronDelete":
            return "stopped"
    return None


def _is_noise(row):
    """Rows that do not count towards knowing who the start's parent is."""
    return row.get("type") not in ("user", "assistant", "system")


def analyze_turn(path, max_bytes=MAX_BYTES, chunk=CHUNK):
    """What happened in the turn that has just finished (the transcript's last).

    Returns {"start": "fire" | "prompt" | None,
             "schedule": "pending" | "stopped" | None,
             "complete": bool}
    - `start`: "fire" when a timer started the turn (a `scheduled_task_fire` row
      + its `user` with `promptSource: system`); "prompt" when a prompt of any
      other origin started it (typed, or injected by the machine WITHOUT a timer:
      the notice of a background task, another agent...); None when it was not
      found within the cap.
    - `schedule`: the LAST tool of the turn that schedules a round:
      `ScheduleWakeup` without stop / `CronCreate` = "pending"; `ScheduleWakeup`
      with `stop: true` / `CronDelete` = "stopped".
    - `complete`: the start was reached (otherwise `start` is None).
    """
    res = {"start": None, "schedule": None, "complete": False}
    if not path:
        return res
    parent_wanted = None   # uuid of the start's parent, when the start is "system"
    for raw in lines_backwards(path, max_bytes=max_bytes, chunk=chunk):
        if parent_wanted is not None:
            # We already have the start (a prompt from the machine): it is a loop
            # round only if its parent is the scheduled_task_fire row.
            if not _RE_SYSTEM.search(raw) and not _RE_USER.search(raw) \
                    and not _RE_ASSISTANT.search(raw):
                continue
            row = _parse(raw)
            if row is None or _is_noise(row):
                continue
            is_fire = (row.get("type") == "system"
                       and row.get("subtype") == "scheduled_task_fire"
                       and row.get("uuid") == parent_wanted)
            res["start"] = "fire" if is_fire else "prompt"
            res["complete"] = True
            return res
        if _RE_ASSISTANT.search(raw):
            if res["schedule"] is None and _RE_SCHEDULE.search(raw):
                row = _parse(raw)
                if row is not None:
                    res["schedule"] = _schedule_of(row)
            continue
        if not _RE_USER.search(raw):
            continue
        if _RE_TOOL_RESULT.search(raw) and b'"promptSource"' not in raw:
            continue   # a tool_result: no need even to open it
        row = _parse(raw)
        if row is None or not _is_start(row):
            continue
        if row.get("promptSource") == "system" and row.get("parentUuid"):
            parent_wanted = row["parentUuid"]
            continue
        res["start"] = "prompt"
        res["complete"] = True
        return res
    if parent_wanted is not None:
        # The file (or the cap) ran out without seeing the parent: not a known round.
        res["start"] = "prompt"
        res["complete"] = True
    return res


def is_loop(analysis):
    """Is the session left LOOPING (waiting for its timer) after this turn?

    The last thing scheduled in the turn wins: pending = yes, stopped = no.
    With nothing scheduled in the turn, it is a loop when a timer started the
    turn (one more round of the loop). When in doubt (empty analysis, no start
    found), NO: it stays on "waiting for you", which is what it did before.
    """
    if not analysis:
        return False
    schedule = analysis.get("schedule")
    if schedule == "pending":
        return True
    if schedule == "stopped":
        return False
    return analysis.get("start") == "fire"
