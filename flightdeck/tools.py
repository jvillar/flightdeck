"""What varies per tool (claude / codex / agy), all in one place.

Flightdeck was born for Claude Code and in 2026-09 it learnt Codex (phase 1) and
agy / Antigravity (phase 2). The golden rule: the rest of the code does NOT know
about tools -- it asks here with the card's `tool` (missing = claude, the legacy
one). An unknown tool degrades to "it is shown but not operated": a visible
mark, no resume, no fork.
"""
import json
import shlex
from pathlib import Path

# The argv that resumes a closed conversation, per tool. Verified:
# `codex resume <id|name>` (0.145's help) and, for agy, the command the TOOL
# ITSELF prints on its way out (measured with 1.1.27):
#     Resume with -c (or command below):
#     agy --conversation=1c48fbba-01a3-4969-a86e-6d31a026dd22
# The value goes GLUED with `=` for that reason: it is the form agy vouches for.
# The form with a space (`--conversation <ID>`) looked reasonable -- they are Go
# flags -- but it was never tried against the binary, and Enter on a grey row is
# an every-day key. The id passes through SAFE_ID in the picker first.
_RESUME = {
    "claude": lambda sid: ["claude", "--resume", sid],
    "codex": lambda sid: ["codex", "resume", sid],
    "agy": lambda sid: ["agy", "--conversation=" + sid],
}


def _tool(tool):
    return tool or "claude"


def mark_of(tool):
    """The tool mark in the row's annotation, or None for claude (the common
    case carries no noise). A future tool is shown as it is: better an odd word
    in the row than a session disguised as claude."""
    t = _tool(tool)
    return None if t == "claude" else t


def resume_argv(tool, session_id):
    """The argv that resumes that conversation, or None when we do not know how."""
    f = _RESUME.get(_tool(tool))
    return f(session_id) if f else None


# The tool's glyph and its 256-colour number: one cell each, so no column moves.
# Thin marks in the brand's colour rather than the real logos, which would need a
# Nerd Font that phone terminals do not carry. 173 ~= the Anthropic clay
# (#D97757), 36 ~= the OpenAI green (#10A37F), 69 ~= the Gemini blue. The WORD
# stays in the row's annotation (`· codex`): a glyph cannot be typed into a
# fuzzy filter.
GLYPHS = {"claude": ("✳", 173), "codex": ("⬡", 36), "agy": ("✦", 69)}


def glyph(tool):
    """That tool's (glyph, colour number), or None when there is no glyph for it.

    Unlike everywhere else in this module, a missing `tool` is NOT claude here:
    the caller is painting a row, and a row with no tool inside (a bare shell) has
    to get a blank, not claude's ✳. Whoever wants the claude default resolves it
    before asking (`entry.get("tool") or "claude"`), which is also what keeps an
    unknown tool out of the brand colours instead of borrowing one.
    """
    return GLYPHS.get(tool)


# Where agy keeps its own things (conversations and history). `flightdeck.history`
# imports it for agy's grey rows, like CODEX_SESSIONS.
AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"


def without_file_uri(path):
    """agy's workspace arrives sometimes as a URI (`file:///Users/...`).

    Public because the history reads the same field out of agy's own metadata
    cache: measured, the hook payload carried a bare path while the
    `.db` carried the URI form, so both readers have to cope with both.
    """
    return path[len("file://"):] if path.startswith("file://") else path


def payload_for(tool, payload):
    """A hook's payload, with the keys Flightdeck expects (claude's). agy's hooks
    speak camelCase (`conversationId`, `workspacePaths`, `transcriptPath` --
    documentation embedded in the 1.1.27 binary): those are translated without
    touching the rest. Other tools, as they come."""
    if _tool(tool) != "agy" or not isinstance(payload, dict):
        return payload
    out = dict(payload)   # the original belongs to the caller: it is not mutated
    sid = payload.get("conversationId")
    if isinstance(sid, str) and sid:
        out["session_id"] = sid
    ws = payload.get("workspacePaths")
    if isinstance(ws, list) and ws and isinstance(ws[0], str) and ws[0]:
        out["cwd"] = without_file_uri(ws[0])
    tp = payload.get("transcriptPath")
    if isinstance(tp, str) and tp:
        out["transcript_path"] = tp
    return out


# Where codex's rollouts live (one transcript per session). The file NAME
# contains the session uuid: finding it is a glob, not opening anything.
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
# How much rollout is read backwards looking for the last token_count: they come
# every few turns, so a little is enough; the cap avoids paying for rollouts of
# several megabytes.
CTX_MAX_BYTES = 512 * 1024


def ctx_pct_codex(session_id, sessions_dir=None):
    """The % of context a codex session has used, or None when it is not known.

    It comes from the LAST `token_count` event of its rollout (read BACKWARDS
    with `flightdeck.turn`'s reader): `last_token_usage` is what travelled in the
    last turn -- input + output ~= the current size of the context -- against
    `model_context_window`. Measured on a real rollout:
    71,538 + 4,804 out of 258,400 = 30%.
    """
    if not session_id:
        return None
    base = Path(sessions_dir) if sessions_dir else CODEX_SESSIONS
    try:
        rollout = next(base.glob("*/*/*/rollout-*%s.jsonl" % session_id), None)
    except Exception:
        return None
    if rollout is None:
        return None
    from flightdeck.turn import lines_backwards
    for raw in lines_backwards(rollout, max_bytes=CTX_MAX_BYTES):
        if b'"token_count"' not in raw:
            continue
        try:
            info = (json.loads(raw).get("payload") or {}).get("info") or {}
            window = info["model_context_window"]
            last = info.get("last_token_usage") or {}
            used = last.get("input_tokens", 0) + last.get("output_tokens", 0)
            if not window or not used:
                return None
            return min(100, round(100.0 * used / window))
        except Exception:
            return None
    return None


def rollout_of(session_id, sessions_dir=None):
    """The path of that codex session's rollout, or None (the file name contains
    the uuid: it is a glob)."""
    if not session_id:
        return None
    base = Path(sessions_dir) if sessions_dir else CODEX_SESSIONS
    try:
        return next(base.glob("*/*/*/rollout-*%s.jsonl" % session_id), None)
    except Exception:
        return None


# Margin between the rollout's mtime and the card's updated_at before we say
# "working": the notify's Stop and the last write to the rollout happen almost
# together (same end of turn); only a clearly LATER write is a new turn in
# flight.
ROLLOUT_MARGIN_S = 3.0


def codex_working(card, now=None, sessions_dir=None):
    """Is that codex in the middle of a turn RIGHT NOW? Codex writes its rollout
    in streaming during the turn (measured), and its file hooks do not fire yet
    (0.153: it validates and trusts them, but does not invoke them -- measured):
    the only signal of a turn in flight is that the rollout has moved
    AFTER the last end of turn we know about (the notify's Stop,
    `updated_at`)."""
    import datetime
    rollout = rollout_of(card.get("session_id"), sessions_dir=sessions_dir)
    if rollout is None:
        return False
    try:
        mtime = rollout.stat().st_mtime
        upd = datetime.datetime.fromisoformat(card["updated_at"]).timestamp()
    except Exception:
        return False
    return mtime > upd + ROLLOUT_MARGIN_S


# Flag inheritance for the CODEX handover (0.153's help). Same contract as
# claude's table in `flightdeck.handover`: `ps` flattens the argv, so every token
# that is neither a flag nor the value of a flag-with-value is DROPPED (it would
# be the new codex's prompt). `--image` points at files of THAT prompt: out.
FLAGS_WITH_VALUE_CODEX = {
    "-c", "--config", "--enable", "--disable", "--remote",
    "--remote-auth-token-env", "-m", "--model", "--local-provider",
    "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir",
    "-a", "--ask-for-approval", "-i", "--image",
}
NON_INHERITABLE_CODEX = {"-i", "--image", "--"}


def inheritable_flags_codex(argv):
    """The flags of a live codex its handover can inherit (same mechanics as
    `flightdeck.handover.inheritable_flags`, with codex's tables)."""
    out = []
    i = 1
    while i < len(argv):
        token = argv[i]
        if not token.startswith("-"):
            i += 1          # subcommand or the rest of the prompt: out
            continue
        with_value = token in FLAGS_WITH_VALUE_CODEX
        value = None
        if with_value and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            value = argv[i + 1]
        if token in NON_INHERITABLE_CODEX:
            i += 2 if value is not None else 1
            continue
        out.append(token)
        if value is not None:
            out.append(value)
            i += 1
        i += 1
    return out


# Flag inheritance for the AGY handover (1.1.27's help). These are Go
# flags: `--x v`, `-x v`, `--x=v` and `-x=v` all work, so the token is normalised
# before it is looked up in the tables.
FLAGS_WITH_VALUE_AGY = {
    "--add-dir", "--agent", "--conversation", "--effort", "-i",
    "--prompt-interactive", "--input-format", "--json-schema", "--log-file",
    "--mode", "--model", "--output-format", "-p", "--print", "--prompt",
    "--print-timeout", "--project",
}
# Out: whatever picks WHICH conversation (-c/--continue/--conversation), that
# agy's prompt (-p/--print/--prompt/-i) and its non-interactive family, whatever
# would create new things on every handover (--new-project) and that process's
# log. The bare `--` is not here because it is not a flag to cross out: it cuts
# the sweep (below).
NON_INHERITABLE_AGY = {
    "-c", "--continue", "--conversation", "-i", "--prompt-interactive",
    "-p", "--print", "--prompt", "--input-format", "--output-format",
    "--json-schema", "--print-timeout", "--disable-slash-commands",
    "--new-project", "--log-file",
}


def _go_flag(token):
    """(canonical name `--x` or `-x`, glued value or None) of a Go token."""
    name, _, glued = token.partition("=")
    clean = name.lstrip("-")
    canon = ("--" if len(clean) > 1 else "-") + clean
    return canon, (glued if "=" in token else None)


def inheritable_flags_agy(argv):
    """The flags of a live agy its handover can inherit (agy's tables and its Go
    forms). It stops at the FIRST loose token: from there on it is prompt, not
    flags."""
    out = []
    i = 1
    while i < len(argv):
        token = argv[i]
        # The cut. In the FLATTENED line `ps` gives back, a word of the prompt is
        # indistinguishable from a positional argument, and Go's `flag` stops
        # parsing at the first positional (and at bare `--`/`-`, which are the
        # same case). We stop there: losing the flags typed BEHIND the prompt is
        # better than inheriting a word of the prompt as a flag -- out of "fix
        # the -foo bug" a `--foo` would come, and the new agy would not even
        # start ("flag provided but not defined: -foo").
        if not token.startswith("-") or not token.lstrip("-"):
            break
        canon, glued = _go_flag(token)
        with_value = canon in FLAGS_WITH_VALUE_AGY and glued is None
        value = None
        if with_value and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            value = argv[i + 1]
        if canon in NON_INHERITABLE_AGY:
            i += 2 if value is not None else 1
            continue
        out.append(canon if glued is None else "%s=%s" % (canon, glued))
        if value is not None:
            out.append(value)
            i += 1
        i += 1
    return out


def inheritable_flags_for(tool, argv):
    """The inheritable flags of a live process, per tool, recycled-pid guard
    included. claude does not come through here (`flightdeck.handover` has its
    own table and its own guard)."""
    t = _tool(tool)
    if t == "codex":
        return inheritable_flags_codex(argv) if is_argv_of("codex", argv) else []
    if t == "agy":
        return inheritable_flags_agy(argv) if is_argv_of("agy", argv) else []
    return []


def handover_command(tool, title, flags):
    """The line the handover types for the one being BORN. claude carries the
    title (`-n`); neither codex nor agy has a title flag -- the numbered title
    lives in the notice and in the card, not inside the tool."""
    t = _tool(tool)
    if t in ("codex", "agy"):
        return " ".join([t] + [shlex.quote(f) for f in flags])
    return None   # claude: `flightdeck.handover.launch_command` builds it (title included)


def is_argv_of(tool, argv):
    """Is that argv from the tool the card claims? (recycled-pid guard, like
    `is_claude_argv`)."""
    if not argv:
        return False
    base = Path(str(argv[0])).name
    t = _tool(tool)
    if t == "codex":
        return "codex" in base or base.startswith("node")
    if t == "agy":
        return base.startswith("agy")
    return base.startswith("claude") or base[:1].isdigit()


def supports_fork(tool):
    """Only claude has `--fork-session`, so Ctrl-F's copy exists for claude
    alone: on the rest that key warns and does nothing. (Ctrl-L is a different
    matter: every tool has a flag catalogue, `picker._CATALOGUE_BY_TOOL`.)"""
    return _tool(tool) == "claude"
