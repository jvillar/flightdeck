"""Discovery of recent sessions (the history) of Claude Code.

Reads the transcripts in ~/.claude/projects/<project>/<uuid>.jsonl and pulls
out of each session the minimum needed to list it and bring it back: id, working
directory, project, readable title and last activity. Defensive by design: a
corrupt or odd transcript must not break the list, only contribute less data.

codex and agy join the same list (`list_recent_sessions`), each from its own
files: a rollout for codex, an opaque conversation file plus two JSON caches for
agy.
"""
import json
import os
import re
from pathlib import Path

from flightdeck import config
# The id ends up inside a `send-keys` (it is typed into a shell), so an odd stem
# never gets typed. The regex is a shared primitive and lives in `common`: the
# picker and the tee filter with the same one.
from flightdeck.common import SAFE_ID
# Where each tool keeps its own things. Per-tool knowledge lives in one module,
# so these are imported rather than declared again here.
from flightdeck.tools import AGY_DIR, CODEX_SESSIONS, without_file_uri

# We only parse the lines that carry one of these texts: it avoids spending time
# deserialising huge lines (attachments) we do not care about.
_NEEDLES = ('"cwd"', '"customTitle"', '"aiTitle"')

HOME_PROJECTS = Path.home() / ".claude" / "projects"


def _iter_relevant(path):
    """Yields JSON objects only for the lines that bring something we care about."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if not any(n in line for n in _NEEDLES):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def session_meta(jsonl_path):
    """Metadata of a session out of its .jsonl transcript."""
    jsonl_path = Path(jsonl_path)
    cwd = None
    custom_title = None
    ai_title = None
    for obj in _iter_relevant(jsonl_path):
        if cwd is None and obj.get("cwd"):
            cwd = obj["cwd"]
        t = obj.get("type")
        if t == "custom-title" and obj.get("customTitle"):
            custom_title = obj["customTitle"]  # the last one wins
        elif t == "ai-title" and obj.get("aiTitle"):
            ai_title = obj["aiTitle"]          # the last one wins
    try:
        last_activity = os.path.getmtime(jsonl_path)
    except OSError:
        last_activity = 0.0
    project = Path(cwd).name if cwd else jsonl_path.stem
    return {
        "session_id": jsonl_path.stem,
        "cwd": cwd,
        "project": project,
        "title": custom_title or ai_title,
        "last_activity": float(last_activity),
    }


def title_for_session(session_id, projects_dir=None):
    """The title a HUMAN gave (/rename) to a live session, or None.

    AI titles are ignored on purpose: in the green row they were confusing --
    "Inconsistent subagent terminal" reads like an error, not like a title. In
    the grey history the AI one IS shown (session_meta), because there it
    identifies sessions that were never named.

    It looks the transcript up by id across every project (the id is unique, so
    there is no need to rebuild the mangled folder name).
    """
    if not session_id or not SAFE_ID.match(session_id):
        return None
    root = Path(projects_dir) if projects_dir else HOME_PROJECTS
    jsonl = next(root.glob("*/%s.jsonl" % session_id), None)
    if jsonl is None:
        return None
    custom = None
    for obj in _iter_relevant(jsonl):
        if obj.get("type") == "custom-title" and obj.get("customTitle"):
            custom = obj["customTitle"]  # the last one wins
    return custom


# The transcripts of a `claude --print` (a script, not a conversation of the
# user's) carry this on every row; the interactive ones carry "cli". A wrapper
# that runs `claude --print` on every Bash command of every session fires them
# constantly, and its transcripts were the newest in the history: 147 of the
# 150 grey rows on one machine.
_RE_SCRIPT = re.compile(rb'"entrypoint"\s*:\s*"sdk')
# The first row of a --print is a `queue-operation` holding the WHOLE PROMPT
# (such a wrapper's is over 19 KB) and the `entrypoint` does not arrive
# until the `user` row, at ~24 KB: with 8 KB it could not be seen (measured).
# 64 KB is plenty and is still one read.
_HEADER_BYTES = 65536
# How many transcripts we get to LOOK AT (their header) while gathering `limit`
# good ones: with thousands of recent --print, without a cap we would read them
# all before painting the list.
MAX_INSPECTED = 3000


def is_script_transcript(jsonl_path):
    """Is that transcript from a `claude --print` (entrypoint sdk-*)? It looks
    only at the first bytes, without parsing JSON. When in doubt, no (it is
    listed)."""
    try:
        with open(jsonl_path, "rb") as fh:
            return _RE_SCRIPT.search(fh.read(_HEADER_BYTES)) is not None
    except OSError:
        return False


# Ids of transcripts we already know to be scripts: one id per line. A
# transcript never changes its nature, and with thousands of --print looking at
# every header on every list cost ~2 s (measured); with the cache, only the new
# ones.
SCRIPT_CACHE_NAME = "script-transcripts.txt"
# Metadata already pulled out, per transcript: {id: {"mtime", "size", "meta"}}.
# A finished transcript does not change; reading them whole (megabytes) for the
# cwd and the title cost ~1 s per list with 150 conversations (measured). Live
# ones change mtime/size and are parsed again (and they are not in the history
# anyway).
META_CACHE_NAME = "history-meta.json"


def _script_cache():
    """Both caches are resolved when they are USED, not when this module is
    imported: the state directory depends on the environment (`FLIGHTDECK_STATE_DIR`,
    `XDG_STATE_HOME`), and a test that moves it must not be reading a path
    frozen at import time."""
    return config.cache_dir() / SCRIPT_CACHE_NAME


def _meta_cache():
    return config.cache_dir() / META_CACHE_NAME


def _read_cache(path):
    try:
        return set(Path(path).read_text(encoding="utf-8", errors="replace").split())
    except Exception:
        return set()


def _extend_cache(path, ids):
    if not ids:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join("%s\n" % i for i in sorted(ids)))
    except Exception:
        pass


def _read_meta_cache(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_meta_cache(path, cache):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _cached_meta(jsonl, st, cache, touched, read=None):
    """The metadata of `jsonl` (through `read`, `session_meta` by default),
    taken from the cache when mtime and size have not changed. `read` may return
    None (the rollout of a `codex exec`): the caller decides what to do with
    it."""
    key = jsonl.stem
    ent = cache.get(key)
    if (isinstance(ent, dict) and ent.get("mtime") == st.st_mtime
            and ent.get("size") == st.st_size and isinstance(ent.get("meta"), dict)):
        return ent["meta"]
    meta = (read or session_meta)(jsonl)
    if meta is not None:
        cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "meta": meta}
        touched.append(True)
    return meta


def session_meta_codex(jsonl_path):
    """Metadata of a CODEX session out of its rollout, or None when it is not
    history: the first useful line is a `session_meta` with `session_id`, `cwd`
    and `source` -- and `source == "exec"` is a non-interactive `codex exec`
    (the equivalent of `claude --print`), which is not listed. Any odd file
    gives None and the caller treats it as a script (it is not read again)."""
    jsonl_path = Path(jsonl_path)
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"session_meta"' not in line:
                    continue
                payload = json.loads(line).get("payload") or {}
                if payload.get("source") == "exec":
                    return None
                sid = payload.get("session_id") or payload.get("id")
                if not sid:
                    return None
                cwd = payload.get("cwd")
                return {"session_id": sid, "cwd": cwd,
                        "project": Path(cwd).name if cwd else jsonl_path.stem,
                        # codex leaves no readable title in the rollout (session
                        # names live in its internal state): the grey row shows
                        # project + the "codex" mark.
                        "title": None,
                        "last_activity": float(os.path.getmtime(jsonl_path)),
                        "tool": "codex"}
    except Exception:
        pass
    return None


# What is shown of an agy conversation: the Preview is the whole last message
# and only a few words fit in a grey row.
TITLE_AGY_MAX = 60


def _agy_text(v):
    """A field of agy's caches as clean text (anything that is not a str does
    not exist: those JSON files are written by another program and cannot be
    allowed to bring the list down)."""
    return v.strip() if isinstance(v, str) else ""


def _agy_metadata(agy_dir):
    """(summaries by id, folder by id) read from agy's two caches. Any failure
    -> empty dicts: an odd file cannot bring the list down."""
    base = Path(agy_dir) / "cache"
    try:
        convs = json.loads((base / "conversation_metadata.json").read_text(
            encoding="utf-8")).get("conversations") or {}
    except Exception:
        convs = {}
    try:
        last = json.loads((base / "last_conversations.json").read_text(encoding="utf-8"))
        folder_of = {sid: path for path, sid in last.items() if isinstance(sid, str)}
    except Exception:
        folder_of = {}
    return convs, folder_of


def session_meta_agy(pb_path):
    """Metadata of an AGY conversation out of its file (opaque: it only gives
    the id -- its name -- and the date -- its mtime); what is readable, title
    and folder, lives in the JSON caches next to it. None only if the file
    vanished.

    With no entry in `conversation_metadata.json` the row shows up JUST THE
    SAME, with the little that is known. It used to return None there, on the
    idea that this cache lagged behind and the row would show up on the next
    pass; measured with agy 1.1.27, **that cache is not written** -- neither
    when the turn ends nor on the way out -- so requiring it left every new
    conversation out of the history, forever. `last_conversations.json` IS
    written on the way out, and it gives the folder of the LAST conversation of
    each one.
    """
    pb_path = Path(pb_path)
    try:
        mtime = float(os.path.getmtime(pb_path))
    except OSError:
        return None
    convs, folder_of = _agy_metadata(pb_path.parent.parent)
    ent = convs.get(pb_path.stem) if isinstance(convs, dict) else None
    summary = {}
    if isinstance(ent, dict) and isinstance(ent.get("summary"), dict):
        summary = ent["summary"]
    uris = summary.get("WorkspaceURIs")   # null when agy does not know the folder
    cwd = None
    if isinstance(uris, list) and uris and isinstance(uris[0], str) and uris[0]:
        cwd = without_file_uri(uris[0])
    cwd = cwd or folder_of.get(pb_path.stem)
    title = _agy_text(summary.get("Title")) or _agy_text(summary.get("Preview")) or None
    if title and len(title) > TITLE_AGY_MAX:
        title = title[:TITLE_AGY_MAX] + "…"
    return {"session_id": pb_path.stem, "cwd": cwd,
            "project": Path(cwd).name if cwd else "no folder",
            "title": title, "last_activity": mtime, "tool": "agy"}


def _excluded(stem, tool, exclude_ids):
    """Is this file one of the sessions that is already ALIVE?

    `exclude_ids` holds the ids of the live sessions, so the history does not
    list a conversation that is on screen in its own green row -- pressing Enter
    on the grey copy would start a SECOND agent on the same conversation.

    claude names its transcript after the id, so the stem is the answer. A codex
    rollout is `rollout-<timestamp>-<uuid>` and its id is that trailing uuid, so
    comparing stems never matched and every live codex was also listed as
    history (carried over from the source, where the codex origin arrived after
    this filter). The separator is required, so two conversations whose ids end
    alike cannot silence each other.
    """
    if stem in exclude_ids:
        return True
    if tool != "codex":
        return False
    return any(sid and stem.endswith("-" + sid) for sid in exclude_ids)


def list_recent_sessions(projects_dir=None, limit=None, exclude_ids=frozenset(),
                         max_inspected=MAX_INSPECTED, cache_path=None,
                         meta_cache_path=None, codex_dir=None, agy_dir=None):
    """History sessions, newest first.

    It ranks by modification date (cheap: one stat per file, without opening
    them) and only parses the contents of the `limit` newest REAL ones: script
    transcripts (`is_script_transcript`) are skipped by looking at their header,
    and it keeps going down until it has gathered `limit` -- with a cap of
    `max_inspected` files looked at. With thousands of transcripts, opening them
    all before showing the list hung the start-up.

    - projects_dir: root of the history (~/.claude/projects by default).
    - limit: how many to return at most (None = all of them).
    - exclude_ids: session_id to skip (e.g. the ones already alive).
    - codex_dir / agy_dir: each tool's root (its own one under HOME by default,
      and only when `projects_dir` is left alone too: with a test root the real
      HOME is not looked at).
    """
    root = Path(projects_dir) if projects_dir else HOME_PROJECTS
    # With a TEST projects_dir and no explicit codex_dir, the real ~/.codex is
    # not touched: the long-standing tests know nothing about codex and must not
    # see it.
    if codex_dir is not None:
        codex = Path(codex_dir)
    else:
        codex = CODEX_SESSIONS if projects_dir is None else root / "no-codex"
    if agy_dir is not None:
        agy = Path(agy_dir)
    else:
        agy = AGY_DIR if projects_dir is None else root / "no-agy"
    candidates = []
    # Three sources, one single ranking by date: claude's transcripts, codex's
    # rollouts and agy's conversations compete for the same 150 grey rows.
    # agy carries TWO extensions: since 1.1.27 a conversation is a `<uuid>.db`
    # (SQLite, measured) and before that it was a `<uuid>.pb` (protobuf). The
    # old ones are still in the folder, so both are looked at; with only
    # `*.pb` the history saw NO new conversation.
    sources = [(root, "claude", ("*/*.jsonl",)),
               (codex, "codex", ("*/*/*/rollout-*.jsonl",)),
               (agy / "conversations", "agy", ("*.db", "*.pb"))]
    for base, tool, patterns in sources:
        if not base.exists():
            continue
        for pattern in patterns:
            for jsonl in base.glob(pattern):
                sid = jsonl.stem
                if not SAFE_ID.match(sid) or _excluded(sid, tool, exclude_ids):
                    continue
                try:
                    st = jsonl.stat()
                except OSError:
                    continue
                candidates.append((st.st_mtime, jsonl, st, tool))
    candidates.sort(key=lambda c: c[0], reverse=True)
    cache_path = _script_cache() if cache_path is None else cache_path
    meta_cache_path = _meta_cache() if meta_cache_path is None else meta_cache_path
    known = _read_cache(cache_path)
    meta_cache = _read_meta_cache(meta_cache_path)
    new, touched = set(), []
    out = []
    for _mtime, jsonl, st, tool in candidates[:max_inspected]:
        if limit is not None and len(out) >= limit:
            break
        if jsonl.stem in known:
            continue
        if tool in ("codex", "agy"):
            meta = _cached_meta(jsonl, st, meta_cache, touched,
                                read=session_meta_codex if tool == "codex"
                                else session_meta_agy)
            if meta is None:
                # The rollout of a `codex exec` never changes its nature: it
                # goes into the scripts cache, which is FOREVER (nobody prunes
                # it), so it is not opened again. In agy, on the other hand, a
                # None only means the file vanished between the `stat` and the
                # read: there is nothing to veto (and with no metadata the row
                # shows up all the same -- see `session_meta_agy`).
                if tool == "codex":
                    new.add(jsonl.stem)
            else:
                out.append(meta)
            continue
        if is_script_transcript(jsonl):
            new.add(jsonl.stem)
            continue
        try:
            out.append(_cached_meta(jsonl, st, meta_cache, touched))
        except Exception:
            continue
    _extend_cache(cache_path, new)
    if touched:
        _save_meta_cache(meta_cache_path, meta_cache)
    return out
