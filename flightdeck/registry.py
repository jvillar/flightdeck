"""Claude Code's OWN session registry, and parked conversations.

Claude Code records every live process in `~/.claude/sessions/<pid>.json`: pid,
`sessionId`, `cwd`, `kind` ("interactive" / "bg"), the tmux destination when
there is one (`tmux: "retail_specs:@12.%15"`), the name, and -- what we care
about -- the link between a PARKED conversation and its job: the interactive one
in the pane carries `parkedJobId` (a SHORT id, 8 characters) and the job carries
`kind: "bg"` and `jobId`. Next to those there are `<pid>.<hash>.key` files that
are not JSON and are ignored.

A conversation is parked with `/background` (`/bg`) or with the ← ARROW pressed
on an empty box (with claude working or idle; measured in 2.1.234).
The thread carries on in a daemon job -- outside tmux, with no TMUX_PANE in its
environment, so its session card has no pane -- and the original claude stays in
the pane, parked: it watches the work (or an empty conversation), but it is no
longer the one working, and its card goes stale (seen live: an "attention" from
09:38 sitting there all day). With this registry Flightdeck shows the JOB's
state on the pane's row, and does not list the job separately as "outside tmux".

That job can also be OPENED in a pane of its own: Claude Code 2.1.26x+ does it
with `claude attach <short id>`, and there the only entry is the job's -- the
viewer registers nothing at all. `common._attach_rows` links those two through
the process table and, to find the job, reads `job_id` from here: the same short
id the viewer was given on its command line.

Read only. This is Claude Code's state, not ours.
"""
import json
from pathlib import Path

REG_DIR = Path.home() / ".claude" / "sessions"


def _text(v):
    return v if isinstance(v, str) and v else None


def _ms(v):
    """An epoch in MILLISECONDS from the registry, or None for anything else.

    `bool` is excluded by hand because in python `True` IS an int: a
    `"startedAt": true` would otherwise come through as the epoch 1, and a row
    built from it would claim to be from 1970.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def read_registry(reg_dir=None):
    """The registry entries, normalised. Any odd file is skipped.

    -> [{"pid", "session_id", "cwd", "kind", "tmux", "parked_job", "job_id",
         "name", "status", "waiting_for", "started_at", "status_updated_at",
         "updated_at"}]
    """
    base = REG_DIR if reg_dir is None else Path(reg_dir)
    out = []
    try:
        files = sorted(base.glob("*.json"))
    except Exception:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("pid"), int):
            continue
        out.append({
            "pid": d["pid"],
            "session_id": _text(d.get("sessionId")),
            "cwd": _text(d.get("cwd")),
            "kind": _text(d.get("kind")),
            "tmux": _text(d.get("tmux")),
            "parked_job": _text(d.get("parkedJobId")),
            "job_id": _text(d.get("jobId")),
            "name": _text(d.get("name")),
            # The state Claude Code records about itself, and the one the status
            # bar and the menu use so they do not announce false waits: "busy"
            # (a turn in flight OR delegated agents/tasks active), "idle",
            # "waiting" (a dialog is open; `waiting_for` says which) or "shell"
            # (idle but with a background Bash running). Enum from the 2.1.234
            # binary: ["busy","shell","idle","waiting"]. It is updated a few ms
            # AFTER the Stop hooks run (measured: 8 ms), so it is good for
            # whoever reads later, not for the hook while it is hot.
            "status": _text(d.get("status")),
            "waiting_for": _text(d.get("waitingFor")),
            # The three clocks, in epoch MILLISECONDS (the card's own dates are
            # ISO text; these are Claude Code's and arrive as numbers). They are
            # what lets `common` build a row for a claude that predates
            # Flightdeck's hooks: `updated_at` is that row's age, and
            # `started_at` next to `status_updated_at` says whether an `idle`
            # has ever moved from the one it was born with -- a session just
            # opened rather than one waiting for you.
            "started_at": _ms(d.get("startedAt")),
            "status_updated_at": _ms(d.get("statusUpdatedAt")),
            "updated_at": _ms(d.get("updatedAt")),
        })
    return out


def status_by_pid(registry):
    """{pid: status} for the registry entries that carry a status."""
    return {e["pid"]: e["status"] for e in (registry or [])
            if e.get("pid") is not None and e.get("status")}


def parked_map(cards, registry, all_cards=None):
    """{session_id of the pane's card: session_id of its job's card}.

    A card is parked when the registry says, BY ITS PID (which is what the hook
    records with getppid), that the process has a `parkedJobId`; and the link
    only counts when there is a card whose session_id STARTS WITH that short id
    (with no card for the job there is no state to show: the row is left as it
    is). The job is looked for among `all_cards` when they are passed; if not,
    among `cards`.
    """
    if not cards or not registry:
        return {}
    by_pid = {e["pid"]: e for e in registry if e.get("pid") is not None}
    ids = [c.get("session_id") for c in (all_cards if all_cards is not None else cards)
           if isinstance(c.get("session_id"), str)]
    out = {}
    for c in cards:
        pid = c.get("pid")
        if not isinstance(pid, int) or pid not in by_pid:
            continue
        short = by_pid[pid].get("parked_job")
        if not short:
            continue
        job = next((s for s in ids if s.startswith(short) and s != c.get("session_id")), None)
        if job and isinstance(c.get("session_id"), str):
            out[c["session_id"]] = job
    return out
