# The three tools

Flightdeck understands three coding agents: **Claude Code**, **Codex** (OpenAI's)
and **Antigravity** (Google's, whose command is `agy`). This page says what it
sees of each one, how, and — just as important — what it does not see.

Anything else still works inside a Flightdeck session, because a session is a
shell: you open one with Ctrl-N and run whatever you like in it. You simply do
not get a state on its row.

What Flightdeck needs from a tool is four things, and no tool gives all four in
the same way:

1. **A way of being told what the tool is doing**, so the row can say working,
   waiting for you, asking you. This is what its hooks are for — the small
   programs an agent runs when something happens.
2. **A way of reading how full the conversation is**, which is the `🧠`.
3. **A list of past conversations**, which is the grey history.
4. **A command that resumes one**, which is what Enter types for you.

The three sections below are laid out the same way: the states, the `🧠`, the
history, the handover, what installing writes, and the limits — stated plainly,
because a limit you know about is a feature and a limit you discover is a bug.

Two things are shared by all three. The grey history holds **150 rows in total**,
not 150 per tool: the newest conversations win, whichever tool they belong to.
And to find them Flightdeck looks at the 3,000 most recently touched files, so a
conversation older than that does not appear even if the list has room.

- [Claude Code](#claude-code)
- [Codex](#codex)
- [Antigravity (`agy`)](#antigravity-agy)
- [Side by side](#side-by-side)

## Claude Code

The tool Flightdeck was built for, and the only one that gives every signal.

### What the row can say, and where it comes from

| The row says | Comes from |
|---|---|
| `● working` | the session starting, a prompt being submitted, or a tool being used |
| `○ open` | a `working` whose last event is still the one that started it: opened or resumed, and you have not said anything to it yet |
| `⏳ WAITING FOR YOU` | the end of the turn, crossed with Claude Code's own record of what it is doing: if that says busy, the row stays `● working` |
| `❓ asking you` | a multiple-choice question or a plan waiting for your approval. An exact signal: Flightdeck registers itself only for those two, by name |
| `⚠ needs attention` | the notification Claude Code sends when it is blocked on a permission |
| `🔁 looping` | the end of a round of a self-scheduled loop, worked out by reading the end of the conversation transcript |
| `· background` | the conversation in that pane was sent to the background; the state shown is the background job's |
| the row goes back to `shell` | the session ending |

That crossing is what makes waiting mean waiting. The end-of-turn event fires at
the end of **every** turn, including ones that leave agents or delegated tasks
running which wake the model up a second later. Claude Code keeps its own note of
whether it is busy, and Flightdeck reads that note **every time it builds the
list**: if it says busy, the row says `● working` and the status bar does not
count it. The floating notice is held back separately, for a second and a half,
and then only sent if the session is still waiting — see
[`GUIDE.md`](GUIDE.md#notices-how-you-find-out-that-something-wants-you).

### The 🧠

From Claude Code's status line — the line painted under its pane. Flightdeck
reads the context percentage as it goes past, in all three status line modes: its
own line, your line wrapped, or both stacked. See
[`INSTALL.md`](INSTALL.md#the-status-line).

The number is noted on every repaint, so it goes stale when an agent is deep in a
long turn and repaints nothing. After 15 minutes without a fresh note the `🧠`
disappears from the row: no number means not known.

### The grey history

Conversation transcripts in `~/.claude/projects/`. The row shows the project's
folder, the age, and the title — the one you set with `/rename`, or the one the
model gave the conversation itself.

Transcripts left by `claude --print` (the non-interactive, one-answer mode) are
**skipped**. They are not conversations you would ever want to resume, and on a
machine where some tool runs `claude --print` in the background there are
thousands of them, which used to crowd the real conversations out of the list
entirely.

| Key | What it types |
|---|---|
| **Enter** | `claude --resume <id>` in that work's folder, in a session named after the project |
| **Ctrl-F** | `claude --resume <id> --fork-session` — a copy with a new id, in a session named `<project>-fork`. **Claude Code is the only tool that can do this** |
| **Ctrl-L** | the same resume command plus the flags you pick, with the whole line editable first |

Ctrl-L's list for Claude Code: `--dangerously-skip-permissions`, `--model fable`,
`--model opus`, `--model sonnet`, `--permission-mode plan`.

### The handover

`prefix + n` types `/exit`, waits for the pane to be a shell again, and types
`claude -n '<title> <N+1>'` followed by the flags the closed agent was running
with. Claude Code is the only one of the three that can be given a conversation
title on its command line, which is why the numbered title survives inside the
conversation and not only in Flightdeck.

Two details of the inherited flags. A flag that takes several values — `--add-dir
/a /b`, say — comes back **truncated to its first value**, because by the time
Flightdeck reads the running command line, a second value is indistinguishable
from a stray word of a prompt, and slipping a stray word through would make it
the new agent's first message. And a flag Flightdeck has never heard of that
demands a value is the one hole left: the line is built with the title **first**
precisely so that such a flag fails loudly instead of silently swallowing the
title.

### What installing writes

`~/.claude/settings.json`: seven hook events and the status line setting, with a
timestamped backup first. Details in [`INSTALL.md`](INSTALL.md#flightdeck-install).

### Known limits

- **A form dismissed with `Esc` can leave the row saying `❓ asking you`.**
  Answering a question *is* using that tool, and that is what tells Flightdeck
  you have answered; dismissing it runs nothing. The row corrects itself the next
  time you type in that session.
- **A turn you cut short with `Esc` leaves the row at `● working`**, and the
  handover will refuse there. Close the agent yourself and start the next one by
  hand.
- **`🔁 looping` is worked out, not announced.** Claude Code knows which of you
  started each turn but does not pass that to its hooks in the public versions,
  so Flightdeck reads the end of the transcript instead. If a future version
  changes what it writes there, this degrades to `⏳ WAITING FOR YOU` — what it
  said before the state existed — and never to something worse.
  See [`LIMITATIONS.md`](LIMITATIONS.md#looping-is-worked-out-not-announced).
- **A conversation sent to the background** (`/background`, or the `←` arrow
  pressed with an empty box) keeps running outside tmux, and what is left in your
  pane only watches. Flightdeck shows the background job's state on the pane's
  row, marked `· background`, and the handover there closes the pane with
  `Ctrl-C` rather than `/exit` — in a parked session `/exit` opens the agents
  view instead, and typing text in that view creates a *new* background task.
  The background conversation survives the handover, and the closing notice tells
  you its job id. A pane where you *opened* one of those conversations — Claude
  Code leaves a viewer of it there — is painted as that conversation, with the
  same `· background` mark, rather than as an empty `shell`; the handover refuses
  on it, because how that viewer closes has never been measured, and tells you to
  press `←` in any claude or close it yourself.
  See [`LIMITATIONS.md`](LIMITATIONS.md#conversations-sent-to-the-background).
- **After a first install, restart the sessions you already have open.** Hooks
  are registered when a session starts, so the ones already running never fire
  them. Their rows are still shown — Flightdeck reads Claude Code's own register
  of open sessions for the name, the state and the age — but nothing about them
  updates until they are restarted: no floating notices, no list redrawing itself
  when one of them starts waiting for you, and no `🧠` unless Flightdeck's status
  line is already running there.

## Codex

A citizen of the menu since September 2026: a green row marked `· codex` with a
⬡ in front of it, a state, a `🧠`, floating notices, grey history rows you can
resume, and the handover.

### What the row can say, and where it comes from

| The row says | Comes from |
|---|---|
| `⏳ WAITING FOR YOU` | its `notify` command, which is codex's own end-of-turn signal |
| `● working` | its transcript having been written to more than 3 seconds after that last end of turn. Codex writes as it goes, so a moving file means a turn is under way |
| `⚠ needs attention` | its permission-request hook. It is registered, and **nobody has yet seen it fire** |
| the row goes back to `shell` | codex's process being gone |

**The annotation appears after the first turn.** Codex does not announce itself
when it opens (the session-start hook does not fire in the version this was
measured against), so until you ask it for something its tmux session is on the
list — it is a tmux session like any other — with nothing said about what is
inside it. For the same reason there is no `○ open` for codex.

Flightdeck chains itself in **front of** whatever `notify` command you already
had, and passes the notification on untouched, so anything you had set up to
receive codex's notifications carries on receiving them.

**No `❓ asking you` and no `🔁 looping`**: those are signals Claude Code gives
and codex does not.

### The 🧠

Read straight out of codex's own transcript: the last token count it recorded,
against the model's context window. It is the one case with no staleness window,
because the number is in a file that codex keeps writing rather than in a note
somebody has to refresh.

### The grey history

Codex's own session transcripts in `~/.codex/sessions`. Codex leaves **no
readable title** in them, so the row is identified by its project folder, its age
and the `· codex` mark. Non-interactive runs (`codex exec`) are skipped, exactly
as `claude --print` transcripts are.

| Key | What it types |
|---|---|
| **Enter** | `codex resume <id>` |
| **Ctrl-F** | nothing. Codex cannot copy a conversation from its command line, so Flightdeck says so: `Ctrl-F copies Claude Code conversations only: codex and agy have no fork from the command line` |
| **Ctrl-L** | `codex resume <id>` plus the flags you pick, with the line editable first |

Ctrl-L's list for codex: `--dangerously-bypass-approvals-and-sandbox`,
`--ask-for-approval never`, `--sandbox workspace-write`, `--search`.

### The handover

`prefix + n` types `/exit`, waits for the shell, and relaunches `codex` with the
flags it was running with. Codex has no flag for naming a conversation, so the
numbered title lives in the notice (`🔄 handover → «api 3»`) and in Flightdeck's
own row, not inside codex.

### What installing writes

`~/.codex/config.toml` (the notify command, and codex's built-in status line
items) and `~/.codex/hooks.json` (six hook events), each with a timestamped
backup. Details in [`INSTALL.md`](INSTALL.md#flightdeck-install).

### Known limits

- **Codex asks you to trust the hooks, once.** The next codex you start after
  installing will say "N hooks are new or changed": the answer is "Trust all and
  continue", and those are these. New hooks are skipped in the very session where
  you trust them, so they start working from the session after that.
- **Closing with `/exit` sends no end-of-session event.** The hook is registered
  and does fire on other ways out, but not on that one, so after an `/exit` the
  annotation disappears when Flightdeck notices the process is gone, and the
  leftover record is cleared an hour later.
- **"Working" is inferred from a file's timestamp**, not announced. A turn that
  writes nothing for more than 3 seconds and then stops looks the same as one
  that has finished — in practice codex streams its transcript continuously, so
  this holds, but it is an inference and not a signal.
- **The status line is codex's own text.** Codex has no way of running an
  external command for its status line, only a list of built-in items, so
  Flightdeck arranges its items in the order of its own line and turns the
  colours on. There is no context bar and no tool mark there, and there is
  nothing more to be had.
- **Codex sometimes sends a second notification from a sub-thread**, which
  produces a second record with the same process id. It is harmless: the green
  row groups by tmux session.

## Antigravity (`agy`)

**Antigravity** is Google's command-line coding tool; its command is called
`agy`. Its sessions are citizens of the menu with a ✦ in front of them and `·
agy` in the annotation, with three absences worth knowing before you rely on it.

### What the row can say, and where it comes from

| The row says | Comes from |
|---|---|
| `● working` | the start of a turn ("before calling the model"). Also from a tool being used — **that one is registered but has never been seen to fire**, because the turn it was measured on used no tools |
| `⏳ WAITING FOR YOU` | the end of a turn |
| the row goes back to `shell` | agy's process being gone |

**The annotation appears after the first turn**, as with codex: agy has no
session-start event at all, so an agy you have just opened and not spoken to has
a green tmux session with nothing said about what is inside it. A handover on
that pane will tell you there is nothing to hand over until you ask it something.

**No `❓ asking you`, no `🔁 looping` and no "asking for a permission."** The only
event that would come close to a permission is the one that runs *before* a tool,
and Flightdeck deliberately does not register it: agy's own documentation says it
expects an allow-or-deny answer from whatever is listening there, and Flightdeck
is a cockpit that watches, not one that decides. An agy stopped waiting for you
to approve something therefore shows as `● working` until you answer it.

### The 🧠

From agy's status line, which turned out to publish the same shape of
information as Claude Code's, context percentage included. That is the only
source: agy's transcript carries no token counts at all, its conversation file is
an opaque database, and agy does not show a percentage in its own built-in line
either.

One thing follows from how agy repaints: it repaints **on events, not on a
timer** — measured as zero repaints in thirty seconds of sitting idle. So an agy
left alone stops refreshing its note and its `🧠` disappears after 15 minutes,
the same rule that hides a stale percentage for Claude Code. It comes back on the
next turn.

### The grey history

agy's conversation files in `~/.gemini/antigravity-cli/conversations/`. Two
honest warnings:

- **Usually no title.** Flightdeck reads the title from the metadata cache agy
  keeps beside those files, and the version this was measured against (1.1.27)
  **does not write that cache** — not when a turn ends and not on the way out. So
  in practice the row is identified by its folder and its date, and it will start
  showing titles by itself the day agy starts writing them.
- **Only the last conversation of each folder knows its folder.** agy records one
  folder per conversation only for the most recent one in it; the earlier ones
  show as `no folder`, and resuming one opens it wherever the menu happens to be.

| Key | What it types |
|---|---|
| **Enter** | `agy --conversation=<id>`, with the `=` attached — the form agy itself prints on its way out |
| **Ctrl-F** | nothing. agy cannot copy a conversation from its command line, and the refusal is the same sentence as codex's above |
| **Ctrl-L** | `agy --conversation=<id>` plus the flags you pick, with the line editable first |

Ctrl-L's list for agy: `--dangerously-skip-permissions`, `--mode plan`,
`--effort high`.

### The handover

`prefix + n` types `/exit` — one Enter is enough, because typing `/exit` opens
agy's command autocomplete and the same Enter both selects and runs it — waits
for the shell, and relaunches `agy` with the flags it was running with. Like
codex, agy has no flag for naming a conversation, so the numbered title lives in
the notice and in Flightdeck.

There is one quirk in what agy can inherit. Its flags are Go-style, so `--x v`,
`-x v`, `--x=v` and `-x=v` all mean the same thing and Flightdeck normalises
them. But the sweep **stops at the first word that is not a flag**: Go's own
parser stops there too, and in a flattened command line a word of your prompt is
indistinguishable from anything else. Out of `fix the -foo bug` you would
otherwise get a `--foo` and the new agy would not start at all. Losing the flags
typed behind a prompt is the better trade.

### What installing writes

`~/.gemini/config/hooks.json` (one named key with three hook events — your own
hooks beside it are untouched) and `~/.gemini/antigravity-cli/settings.json` (the
status line), each with a timestamped backup. agy does **not** ask you to trust
the hooks; it does ask you to trust the workspace the first time, which is its
own behaviour and nothing to do with Flightdeck. Details in
[`INSTALL.md`](INSTALL.md#flightdeck-install).

### Known limits

Beyond the missing states above, four things are honestly untested rather than
broken. They are listed because you may be the first to hit them.

- **The tool event has never been seen to fire.** It is registered, and the turn
  it was measured on used no tools. If it does not fire, an agy using tools shows
  as `● working` from the start of the turn anyway — the state is right, it is
  just refreshed less often.
- **`agy --conversation=<id>` has never actually been run** from the menu. It was
  copied from what agy prints when it exits.
- **The handover has never been run end to end against a live agy.** The closing
  half was measured (the `/exit`, one Enter, the pane back to a shell); the
  relaunching half is the same code codex uses, which was measured whole.
- **agy's end-of-turn event carries an "is it fully idle" field that nobody
  reads.** Every end of turn becomes `⏳ WAITING FOR YOU`. The one that was
  measured did say fully idle, but the existence of that field suggests agy can
  stop without being altogether stopped, which would show as a session waiting
  for you while it carries on. If you see that, it is a known gap and not a
  mystery.

## Side by side

| | Claude Code | Codex | Antigravity (`agy`) |
|---|---|---|---|
| Waiting for you / working | yes | yes | yes |
| `○ open` (opened, not yet spoken to) | yes | no (the row starts at the first turn) | no (the row starts at the first turn) |
| `❓ asking you`, `🔁 looping` | yes | no signal | no signal |
| `⚠ needs attention` (a permission) | yes | its hook is registered, never seen to fire | no signal |
| Context gauge (`🧠`) | from its status line | from its own transcript | from its status line |
| Row appears | as soon as the session starts | after the first turn | after the first turn |
| Row disappears | on the session-end event | when the process is gone | when the process is gone |
| Past conversations in the menu | yes, with titles | yes, no titles (folder and date) | yes, usually no titles (folder and date) |
| Enter on a grey row types | `claude --resume <id>` | `codex resume <id>` | `agy --conversation=<id>` |
| Copy a conversation (Ctrl-F) | yes | not offered by the tool | not offered by the tool |
| Pick flags (Ctrl-L) | yes | yes | yes |
| Handover (`prefix + n`) | yes, and the new conversation carries the numbered title | yes, but it cannot be given a title, so the number lives in Flightdeck | yes, same |
