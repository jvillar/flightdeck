# Using Flightdeck

One screen with every AI coding session you have open, and a few keys that never
change. This page is the whole of it: what you are looking at, what each key
does, and the two things Flightdeck does for you — telling you when a session
wants you, and handing a tired conversation over to a fresh one.

Installing, updating and removing are in [`INSTALL.md`](INSTALL.md), along with
`flightdeck restart`, `flightdeck init` and `flightdeck quit`. What each agent
tells Flightdeck (and what it does not) is in [`TOOLS.md`](TOOLS.md).

- [The menu: what you are looking at](#the-menu-what-you-are-looking-at)
  - [The two lines underneath: the folder, and the rest of the story](#the-two-lines-underneath-the-folder-and-the-rest-of-the-story)
- [The keys](#the-keys)
  - [Enter or Ctrl-F on a grey row: carry on, or consult](#enter-or-ctrl-f-on-a-grey-row-carry-on-or-consult)
  - [Ctrl-L: launch a grey row your way](#ctrl-l-launch-a-grey-row-your-way)
  - [Ctrl-N: a new, empty session](#ctrl-n-a-new-empty-session)
  - [F12 and `prefix + j` are not the same key](#f12-and-prefix--j-are-not-the-same-key)
- [A day of work](#a-day-of-work)
- [The context gauge: how much room a conversation has left](#the-context-gauge-how-much-room-a-conversation-has-left)
- [The handover: passing the baton to a fresh session](#the-handover-passing-the-baton-to-a-fresh-session)
- [Notices: how you find out that something wants you](#notices-how-you-find-out-that-something-wants-you)
- [Session names: they are yours](#session-names-they-are-yours)
- [Several windows at once](#several-windows-at-once)

## The menu: what you are looking at

![The Flightdeck menu](img/menu.png)

Type `flightdeck` in any terminal and the menu fills the screen. Four kinds of
row, each in its own colour:

```
  session>
    9/9
  Ctrl-L: flags · Ctrl-F: copy · Ctrl-N: new · ?: details · F12 · Esc: shell
  ✳ recommender         12s  recommender · ⏳ WAITING FOR YOU      ← green
  ✳ pricing              6s  pricing · ❓ asking you               ← green
  ⬡ api                  1m  api · codex · ● working               ← green
  ✳ example_com          4m  example_com · ● working · 🧠82%       ← green
    sandbox                  shell                                 ← green
  📊 htop                                                          ← pinned
  ◇ ✳ review the PR      8m  outside tmux (● working)              ← dimmed
  ▹ ✳ pricing       18h 40m  Rework the margin calculation         ← grey
  ▹ ⬡ api             2h 05m  — · codex                            ← grey
  ───────────────────────────────────────────────────────────────────
  📁 ~/Documents/work/recommender · main
  ✳ claude · recommender · waiting for you · 12s · id 4f21ba90
```

**The list keeps itself up to date.** When an agent finishes, starts working,
asks you a question or asks for a permission, its row changes in every menu you
have open, without you doing anything — and the cursor stays on the same session
even if the list reorders itself underneath it (that last part needs fzf 0.71 or
newer; on an older one the cursor stays where it is on screen, see
[`LIMITATIONS.md`](LIMITATIONS.md#an-older-tmux-or-fzf-loses-a-feature-each)).
What does *not* trigger a
refresh is ordinary work: a reload for every command an agent runs would be a
list flickering all day.

The middle column is the **age**: how long ago something last happened there
(`12s`, `4m`, `18h 40m`). On a green row it is blank when there is no agent
registered inside — a bare shell has no "last activity" to count.

**Green — live tmux sessions.** One per piece of work, most recently used
first. On the left is the tmux session's name, which is **the one you gave it**;
on the right is what is running inside, which is a different thing. That
annotation is the conversation's **title** (the one you set with `/rename`
inside the agent, or the numbered one a handover left) and, failing that, the
name of the project folder. Then the state:

| Badge | What it means |
|---|---|
| `⏳ WAITING FOR YOU` | it finished its turn and is waiting for you |
| `❓ asking you` | it has opened a **form** and will not carry on until you pick: either a multiple-choice question or a "does this plan look right?" |
| `⚠ needs attention` | it is asking for a permission |
| `● working` | a turn is under way |
| `○ open` | it is sitting there doing nothing: you have just opened or resumed it and have not said anything to it yet |
| `🔁 looping` | it is in a self-scheduled loop: it finished a round and is waiting for **its own timer**, not for you. It does not count as a wait, does not add to the bar and does not notify you |
| `shell` | there is no agent in that session right now, only your terminal |

The hollow circle against the filled one is exactly that difference: **○ = open
and idle**, **● = genuinely working**. Behind the state you may also see
`· background` (the conversation in that pane was sent to the background, and
what you are seeing is the state of the background work — see [conversations
sent to the background](LIMITATIONS.md#conversations-sent-to-the-background))
and, when that conversation has used more than half its memory, the `🧠82%`,
which has [its own section](#the-context-gauge-how-much-room-a-conversation-has-left).

A pane where you **opened** one of those background conversations — Claude Code
puts a viewer of it in your terminal — is painted as that conversation too, with
its name, its state and the same `· background` mark, instead of as an empty
`shell`. The handover does not run on a pane like that: press `←` in any claude
to get back to the conversation, or close the viewer yourself.

In front of every row is a one-character mark for the tool: **✳** Claude Code,
**⬡** Codex, **✦** Antigravity. The word is in the annotation as well
(`· codex`, `· agy`) because you cannot type a glyph into a search box. Claude
Code, the common case, carries no word.

**Sessions that were already open when you installed Flightdeck** get a row too,
and it is worth knowing where it comes from. Flightdeck learns what an agent is
doing from hooks, and hooks are registered when a session starts, so those
sessions never fire them. For Claude Code, rather than show you a bare `shell`,
Flightdeck reads Claude Code's own register of open sessions and paints the row
from that: the name, the state and the age. What that row cannot do is *change*
— no floating notice when it starts waiting for you, no list redrawing itself,
and no `🧠` unless Flightdeck's status line is already running there. Restart
those sessions and everything works normally. Codex and Antigravity have no such
register, so theirs stay as `shell` until you restart them.

**Pinned rows** are the commands you keep open — a process viewer, a git
interface, an account switcher. Enter opens one in its own tmux session;
Flightdeck never counts it as work and never kills it. They live behind the
green rows, and [`INSTALL.md`](INSTALL.md#pinned-rows) says how to add one.

**Dimmed `◇` — agents alive but outside tmux**, a desktop app for example.
These are informative only: a process that already has a terminal of its own
cannot be adopted into tmux. Press Enter on one and it tells you so. The name is
the one the agent registered for itself, trimmed to 40 characters, with the
folder as a fallback.

**Grey `▹` — the history**: the 150 most recent conversations that are no longer
running, newest first, with their titles. These you can **revive** (Enter, you
carry on with that conversation), **copy** (Ctrl-F, you look at it without
touching it) or **launch your way** (Ctrl-L, choosing the flags it starts with).
The number 150 is the `history_limit` setting.

Type to filter: the search runs over everything visible in the row — the name,
the project, the title, the tool. Flightdeck's own sessions (`flightdeck`, the
extra `flightdeck-2`, `flightdeck-3`… and each pin's session) are not listed as
work, because they are the cockpit itself.

### The two lines underneath: the folder, and the rest of the story

A row is one line wide, so it cannot say everything. The two lines at the
bottom of the screen say the rest, about **whichever row the cursor is on**.
Move the cursor and they follow it.

```
📁 ~/Documents/work/recommender · main
✳ claude · recommender · waiting for you · 12s · id 4f21ba90
```

- **The first line is the folder**, shortened with `~` for your home, and after
  it the **git branch** of that folder. The branch part is simply missing when
  the folder is not a git checkout. This is the line that exists because of the
  case that started it: two sessions both called `recommender`, and no way to
  tell from the list that one of them was in the worktree.
- **The second line is who is inside and how it is doing**: the tool, the full
  title of the conversation (the list cuts a long one off at the edge of your
  terminal; here you read all of it), the state in plain words, the age, and
  the first eight characters of the conversation's id — which is what you quote
  in a bug report, or grep for in a transcript.

The other kinds of row say what they have. A **shell** shows its folder and the
word `shell`. A **pinned** row shows the command it runs (`pin: cswap tui`) and
the session it lives in. A **grey** row from the history has no state, so it
does not claim one. A **dimmed** row says `outside tmux`, its name and its state.

**`?` hides the strip, and `?` brings it back.** The one thing you give up for
it: a question mark can no longer be typed into the search box, because that key
now belongs to the strip.

## The keys

| Key | What it does |
|---|---|
| **Enter** on a **green** row | jump into that tmux session |
| **Enter** on a **grey** row | create a new tmux session in that work's folder and **type for you** that tool's resume command, which you watch run: `claude --resume <id>`, `codex resume <id>` or `agy --conversation=<id>`. You carry on with *that same* conversation: whatever you say is added to it |
| **Enter** on a **pinned** row | open that pin's session, creating it if it had died |
| **Ctrl-F** on a **grey Claude Code** row | open a **copy** of that conversation: the same as Enter, but it types `claude --resume <id> --fork-session`. The copy starts with a **new id**, so nothing you say there is written into the original, which stays grey and untouched. The tmux session is called `<project>-fork` so you cannot confuse the copy with the real one. **Claude Code only**: neither Codex nor Antigravity can copy a conversation from its command line. On a green, pinned or dimmed row it says the copy only makes sense on a grey one; on a grey Codex or Antigravity row it says the copy is Claude Code's alone |
| **Ctrl-L** on a **grey** row | the same as Enter, but **you choose how it starts**: first a menu of flags (mark as many as you like with Tab), then the whole command written out for you to edit, and only then does it run. It works with all three tools, and **each one shows its own flags**. On any other row it says so and does nothing |
| **Ctrl-N** | a new, **empty** session: named after whatever you typed in the search box (if you typed nothing, it asks), opening a shell in your projects folder |
| **?** | hide the [two detail lines](#the-two-lines-underneath-the-folder-and-the-rest-of-the-story) at the bottom, and show them again. They are on when the menu opens. While this key belongs to the strip, a question mark cannot be typed into the search box |
| **Esc** | leave the menu loop and stay in an ordinary shell inside the menu's own session (`flightdeck`, or the `flightdeck-2` your second window got). To get the menu back, F12 or `prefix + j`: both restart it right there |
| **F12** | the menu↔session switch: from any session it takes you to the menu, from the menu back to the last session you were in. If the menu did not exist, or you had left it on a shell, it creates or revives it first |
| **prefix + j** | **one way to the menu**, from any session (creating or reviving it exactly as F12 does) |
| **prefix + n** | **the handover**: close the agent in this pane and open a fresh one in its place, with the title numbered up (`pricing 2` → `pricing 3`) |
| **prefix + d** | *detach*: let go of tmux and go back to your own shell. Everything carries on running |

`prefix` is tmux's own leading key, **`Ctrl-b` out of the box** (many people
remap it to `Ctrl-a`; Flightdeck works with whichever you have). So `prefix + n`
means: press `Ctrl-b`, let go, then press `n`.

**Why Ctrl-N and not Enter to create.** Enter *never* creates anything new. It
is a lesson learnt the hard way: with creation on Enter, the fuzzy search ended
up reviving the wrong session. Creating is a separate key, on purpose.

### Enter or Ctrl-F on a grey row: carry on, or consult

An example. Two weeks ago you left `pricing` halfway through the margin
calculation:

- You want to **carry on with it**: **Enter**. The agent resumes that
  conversation and everything you say is added to the same thread. This is the
  normal route, and it is what you want almost every time.
- You only want to **see what it said** — or try another path from that point
  without messing it up: **Ctrl-F**. A session called `pricing-fork` is born
  with a copy: you ask, you read, you try things, and the original stays exactly
  as it was, without one extra turn. The copy is not necessarily throwaway: if
  the new path turns out to be the good one, you stay there and that is that.

One detail when you come back to the menu: the copy also ends up in the grey
history when you close it, with the same project name as the original. You tell
them apart by age (the copy is the more recent one) and by the title, if you set
one with `/rename`.

### Ctrl-L: launch a grey row your way

Enter and Ctrl-F launch the tool **bare**, with no options added. That is
deliberate: what gets typed is always the same and there are no surprises. When
you want *this particular time* to start differently — without being asked for
permission at every step, with a different model, in plan mode — that is
**Ctrl-L**.

Two screens and then it runs. An example from beginning to end. You are in the
menu, you can see the grey row `▹ pricing   18h 40m   Rework the margin
calculation`, and you want to pick it up with Opus and without permission
prompts:

1. **Press Ctrl-L on that row.** A short list of options appears — the ones
   people actually use — with an explanation in brackets where one helps:

   ```
   flags> Tab: pick several · Enter: continue · Esc: cancel
     (none — just edit the command)
     --dangerously-skip-permissions   (no permission prompts)
     --model fable
     --model opus
     --model sonnet
     --permission-mode plan   (start in plan mode)
   ```

   Mark the ones you want with **Tab** (you can mark several) and press Enter.
   In this example you mark `--model opus` and
   `--dangerously-skip-permissions`. What is in brackets is an explanation for
   you: it does **not** travel into the command.

2. **The command appears written out, and you can edit all of it:**

   ```
   command> claude --resume 9f3c1a2b-… --model opus --dangerously-skip-permissions
   ```

   Here you delete, add or change whatever you like — it is an ordinary line of
   text. If today you also want to give it another folder, add
   ` --add-dir ~/other` and that is it. Enter launches it.

3. **It starts exactly as Enter would:** a new tmux session named after the
   project (`pricing`) is born in that work's folder, and the line you just
   approved is typed into it. You watch it run and you are left inside.

Each tool shows its own list. Codex's:

```
--dangerously-bypass-approvals-and-sandbox   (no approvals, no sandbox)
--ask-for-approval never   (never asks; failures go back to the model)
--sandbox workspace-write   (sandbox with write access to the project)
--search   (web search)
```

And Antigravity's:

```
--dangerously-skip-permissions   (no permission prompts)
--mode plan   (start in plan mode)
--effort high
```

Three things worth knowing:

- **What you see is what gets typed**, character for character: the line from
  step 2 is written into the shell exactly as it stands, with nothing added or
  removed — and it does not matter what it is. Even if you delete the `claude`
  and leave it starting with a dash, or write a word that means a key inside
  tmux (`Enter`, `C-c`), it travels as text. If the command has a typo, the one
  that complains is your shell, in its usual words.
- **The copy is not asked for from here.** Do not look for `--fork-session` in
  the list: the copy is **Ctrl-F**, which also christens the session
  `<project>-fork`. Launched from Ctrl-L, a copy would be born with the
  original's name and in the green list you could no longer tell which is the
  real conversation.
- **Esc at either step takes you back to the menu without doing anything** —
  the session is not created and nothing is typed. And if at the first step you
  want none of the options in the list (you came in only to write the command
  yourself), that is the `(none — just edit the command)` row, which is already
  selected when the list opens.

### Ctrl-N: a new, empty session

Type a name in the search box and press Ctrl-N: a session with that name is
born, with a **shell** inside it, in your projects folder (`projects_dir` in the
settings, your home directory unless you change it). If you press Ctrl-N with
nothing typed, it asks for the name.

A shell, not an agent, and that is deliberate: if the agent dies or crashes, the
terminal is still alive and usable — you are not left with a dead pane. It also
means the same menu works for `codex`, for an `npm run dev`, or for anything
else you feel like running.

### F12 and `prefix + j` are not the same key

The difference matters day to day. **F12 goes and comes back**: it is a switch
between the menu and the last session you were in. **`prefix + j` only goes.**
If you use `prefix + j` to reach the menu, you come back by choosing a session
with Enter, like any other time.

`prefix + j` exists for keyboards where F12 is awkward — a phone's, typically —
not as a full replacement. Inside the menu it does nothing at all.

Both of them *guarantee* where they are taking you: if the `flightdeck` session
had been killed, they create it; if you had pressed Esc and left it on a shell,
they restart the menu in that same pane.

## A day of work

1. You open a terminal in the morning and type `flightdeck`. The menu appears.
2. Yesterday you left the pricing work half done. You type `pric`, the grey row
   `▹ pricing  18h 40m  Rework the margin calculation` floats to the top,
   **Enter**. A tmux session called `pricing` is created, in that project's
   folder, and you watch it type `claude --resume 9f3c…` for itself and start up
   where you left off.
3. You give it a long job. While it works, you press **F12** and you are back on
   the menu.
4. You want to start something else: you type `notes` and press **Ctrl-N**. A
   session called `notes` is born with a shell in your projects folder. You `cd`
   wherever you like and launch `claude` yourself (or `codex`, or anything at
   all) with whatever arguments you please.
5. A while later, at the bottom of the screen, `⏳ pricing is waiting for you`
   appears. **F12**, `pricing` is at the top in green with `⏳ WAITING FOR YOU`,
   Enter, you answer it.
6. Its row now reads `🧠82%`. **prefix + n**, and you watch the handover happen.
7. You go for lunch: **prefix + d**. Everything carries on running.
8. From the sofa, on your phone: SSH to the machine, `flightdeck`, and you are
   looking at exactly the same menu. See [`REMOTE.md`](REMOTE.md).

Look again at step 4: Flightdeck creates **shells**, not agents. That is the
whole reason the cockpit works with tools it has never heard of.

## The context gauge: how much room a conversation has left

Every conversation an agent holds has a limited working memory — the
**context**: everything the two of you have said plus every file it has read.
When it fills up the conversation gets clumsy, and the fix is to hand over to a
fresh one. Flightdeck shows you that percentage **before** it catches you out:

```
✳ pricing              12s  pricing 2 · ⏳ WAITING FOR YOU · 🧠82%
```

That reads: the tmux session `pricing`, with the conversation titled `pricing 2`
inside it, which has finished its turn and is waiting for you, and which has
used **82 % of its memory**.

It shows up in three places, each with its own threshold:

| Where | From | What it looks like |
|---|---|---|
| The green row in the menu | 50 % | `· 🧠82%` at the end of the row |
| The tmux status bar, in red | 80 % | `🧠 pricing 82%`, behind the waits, at most **two** of them, fullest first |
| A floating message, once | 80 % | `🧠 pricing at 82% — start thinking about a handover` |

Below 50 % nothing appears on the row: a conversation at 20 % is not news, and
the number would only be in the way. The three numbers are the settings
`context_show_pct`, `context_warn_pct` and `context_rearm_pct`, and you can
change them.

The floating message is a notice, not an alarm: while you stay above the line
**it does not repeat**. It is re-armed when the number drops below 75 % (a
compaction, or a real handover), and then it would warn you again the next time
you climb. That dead band between 75 and 80 is there on purpose: the percentage
bounces around on the boundary, and without it you would get the same message
every time the agent repainted its own line.

**And sometimes there is no number, which is not the same as "it is empty".**
The number is noted down by Flightdeck's status line every time the agent
repaints the line under its pane — that is to say, **per interaction, not per
clock tick**: an agent ten minutes into a long turn repaints nothing and its
note sits still. Flightdeck trusts any note from the **last 15 minutes** and
stops showing the 🧠 after that, because by then the number says nothing about
now. So a session you have just opened, one that is only a shell, or one that
has been idle a while, shows no 🧠 at all: **no number means not known**.

Where the number comes from is different for each tool, and
[`TOOLS.md`](TOOLS.md) says which is which. Codex is the exception to the
15-minute rule: its percentage is read straight out of its own transcript, so it
has no staleness window.

### The line under the pane

That line is Flightdeck's too, if you let it be, and it is three lines:

```
✳ Fable 5.1 · xhigh │ 👤 you@example.com max │ ~/…/claude/flightdeck │ main
Ctx █████████████░░ 88% │ 5h █░░░░░░░░░ 11% ↻06:30 │ 7d ███░░░░░░░ 34% ↻23/09 22:00
Tokens: msg 1.1k │ cache 882.9k │ session 884.0k │ turns 716
```

Who you are, what you have spent, and what it cost. Each line fits itself to
the pane on its own, so a narrow terminal loses the branch and the reset times
rather than a bar — except the third, which has only the turn count to give up
and is left to wrap below about 48 columns rather than have a figure cut in
half. If you would rather your address were not on screen,
`{ "statusline": { "account": false } }` in `config.json` takes that segment
off, and `{ "statusline": { "lines": 2 } }` puts the first two lines back on one
row. [`INSTALL.md`](INSTALL.md#the-status-line) has the rest, including how to
keep the line you already had.

## The handover: passing the baton to a fresh session

When a conversation is running out of memory (that `🧠82%` on the green row and
in the bar), writing more does not fix it: it is time for a **handover**. That
means closing that agent and opening another one, with a clean context, that
carries the work on from where the last one left it.

Two gestures, and the second is a single key:

1. **You, inside the session: get the wrap-up written down.** Ask the agent to
   record what the next one needs to know — in a note in the repository, in your
   issue tracker, wherever you keep such things — and read what it wrote. This
   is **not** automated, on purpose: the new conversation starts empty, and what
   carries across is exactly what somebody decided to write down.
2. **You, one key: `prefix + n`.** From here on you only watch.

What happens in your own pane, in front of you, in about a second:

- `/exit` is typed for you and the old agent closes;
- the pane goes back to your shell (this is why Flightdeck's sessions are shells
  first and agents second);
- the new one's starting line is typed in the same directory:
  `claude -n 'pricing 3'`.

The new one **inherits the flags** the one you are closing was running with —
`--dangerously-skip-permissions` or `--model opus`, say — so you do not lose
your way of working just by handing over. What it does **not** inherit is three
families, all for the same reason (a handover changes conversation without
moving house): which conversation it belonged to (`--resume`, `--continue`,
`--session-id` and their relatives), whatever **creates a new place** to live
(`--worktree`, which opens the agent in a separate copy of the repository, and
`--tmux`, which builds it a tmux session of its own: inheriting those would mean
one more worktree and one more session in the menu *on every handover*), and the
single-answer mode `--print`, which opens no conversation at all.

One thing to take care of before you press: the agent's input box should be
**empty**. What the handover types is added to whatever is already there, so a
half-written message would end up being sent as if it were yours, with the
`/exit` stuck on the end.

Codex and Antigravity are handed over the same way, with one difference:
neither has a flag for naming a conversation, so the numbered title lives in the
notice and in Flightdeck, not inside the tool. See [`TOOLS.md`](TOOLS.md).

### The number is worked out for you

The new title comes from the title of the conversation you are closing — the one
you set with `/rename`, or the one the previous handover left — with the next
number on it:

| Title of the conversation being closed | Title the new one is born with |
|---|---|
| `pricing` | `pricing 2` |
| `pricing 2` | `pricing 3` |
| `v2` | `v2 2` |
| *(none)* | *the tmux session's name* + ` 2` |

Only a number at the **end and separated by a space** counts: `v2` is a name,
not a counter, so it becomes `v2 2` and never `v3`. The same rule applies to the
last row: if your tmux session is called `pricing 2`, an untitled conversation
is born as `pricing 3` and not `pricing 2 2`.

Mind the `v2` row and the last one: what gets numbered is the **conversation's
title**, never the name of the tmux session (that one is yours and nobody
touches it, see [Session names](#session-names-they-are-yours)). In the menu it
looks like this:

```
✳ pricing              12s  pricing 3 · ⏳ WAITING FOR YOU
```

A stopwatch detail: the new title does not exist until the conversation really
starts (the agent writes it down as soon as there is something to save). If you
hand over twice in a row without saying anything to the freshly opened agent,
the second handover finds no title and numbers from the tmux session's name
again.

### The old conversation is not lost

Nothing is deleted. The previous one is left **frozen** in the menu's grey
history, with its title, exactly as you left it:

```
▹ ✳ pricing             3m  pricing 2
```

If tomorrow you need to see what it said, revive it with Enter like any other
grey row — or, if you are only going to consult it and do not want to add
anything, **Ctrl-F**, which opens a copy and leaves the original frozen as it
is.

### When the handover does nothing (and tells you why)

It never leaves your pane half-done in silence: if it cannot go on, it touches
nothing and tells you so with a floating message.

| Message | What is going on |
|---|---|
| `🔄 that agent is still working — wait for it to finish (or close it yourself) and press the handover again` | it is in the middle of a turn, and there the `/exit` would be queued up for it as if it were a message from you |
| `🔄 that agent is still working (background tasks running) — wait for it to finish and press again` | its turn ended, but it has agents or delegated tasks running that will wake it up again in a moment. It is not waiting for you yet |
| `🔄 that agent is waiting for you to answer (a permission) — answer it and press the handover again` | it has a dialog open, and there the Enter would say **yes** to whatever it is asking for |
| `🔄 that agent is asking you something (a form) — answer it and press the handover again` | it has opened a multiple-choice question (or a plan waiting for your nod): there the Enter would **pick an option** for you |
| `🔄 that agent is in a /loop — stop the loop first (the handover would run it over)` | it is `🔁 looping`, waiting for its timer. The `/exit` would go in cleanly, but the loop lives in that agent's memory and the new one would not inherit it: the handover would run it over without a word |
| `🔄 there is no live agent to hand over in this pane` | no agent is running in that pane, or you already closed it |
| `🔄 the handover does not run in «flightdeck» — that session belongs to Flightdeck itself` | the menu and the pinned rows are the cockpit's own, not your work |
| `🔄 the agent did not close in 20 s — I did not touch anything else; close it yourself and press again` | it was asked to `/exit` and it is still there (some dialog open). **Nothing new is started** |
| `🔄 the handover could not type into that pane` / `🔄 the handover cannot find that pane (…)` | the pane went away underneath it |

**A nuance on the first one.** As far as Flightdeck is concerned an agent is
"working" from the moment it starts until it **closes its first turn**, so one
just opened or just resumed used to show up as busy while sitting perfectly
still, and the handover refused. That is fixed: if you have said nothing to it
since it started, the menu paints it `○ open` and the handover **does** hand it
over. One case cannot be told apart: if you **cut a turn short with Esc**, the
session stays marked as working and the handover will refuse. No harm done —
close it yourself with `/exit` and start the next one by hand.

And a case it **does** tell apart, although it looks the same: when an agent's
memory fills up and it starts **compacting** (summarising the conversation so it
can carry on), from the inside that resembles a start-up, but it is in the
middle of a turn. There the menu still says `● working` and the handover
refuses, which is the right answer.

That is the other side of it: if you close the agent yourself, the handover has
nobody left to number and will tell you there is nobody there. Start the new one
yourself, which is exactly the line it was going to type: `claude -n 'pricing
3'` **plus your usual flags** (the ones it would have inherited for you, so
`claude -n 'pricing 3' --dangerously-skip-permissions`).

## Notices: how you find out that something wants you

Two channels, both of them inside tmux. There are no desktop notifications and
no push — [deliberately](LIMITATIONS.md#notices-live-inside-tmux-and-nowhere-else).

- **The status bar** (at the bottom, dark, refreshed every 5 seconds). On the
  left, the name of the session you are in; on the right, in yellow, who is
  waiting for you (up to 3 names), and behind them, in red, the contexts that
  are nearly full:

  ```
   pricing                          ⏳ 2 waiting: retail, example_com
  ```

  If nobody is waiting, the right-hand side is clean — no "0 waiting".

- **A floating message** when it happens: `⏳ pricing is waiting for you`, or
  `❓ pricing is asking you` if what it did was open a form. It is sent to
  **every** tmux client at once (the one on your desktop and the one on your
  phone). It stays up for **5 seconds** and any key clears it earlier — and the
  key still reaches whatever you were doing, it is not swallowed. While it is up
  the screen carries on moving normally.

**"Waiting for you" means waiting for *you*** — to carry on, or to unblock it.
Claude Code counts a turn as finished even when it leaves agents or tasks
working in the background that will wake it up again a second later; that used
to show up as "waiting for you" and then clear itself. Now Flightdeck also reads
the state the agent records for itself: if it is still busy, the row says
`● working`, the bar does not count it and no notice is sent. The "waiting for
you" notice is checked again 1.5 seconds after the turn ends, so it is not sung
if the session has already started moving again. One case that still counts as a
wait: a background shell command (a long job moved to the background) that will
wake the agent when it finishes — today that cannot be told apart from a
permanent `tail -f`.

Both channels count `❓ asking you` the same way: if it is asking you, it is
waiting for you, so that session adds to the `⏳ N waiting` on the bar. A
`🔁 looping` session counts as neither: it is waiting for its timer, so it is not
in that count and it sends no notice. (It can still appear on the right of the
bar in red, because the `🧠` half goes by the percentage alone: a looping session
above 80 % is shown there like any other.) And what is **never** announced is
ordinary work: a message for every tool an agent uses would be a permanent
flicker.

One thing that does **not** move a row: Claude Code sends its own reminder six
seconds after it opens any dialog, and for a multiple-choice form that reminder
arrives with the generic wording of a permission request. A row already saying
`❓ asking you` stays saying it. Without that rule the menu announced "needs
attention" in front of a form, which is a different thing and sends you looking
for a permission that is not there.

If tmux is not running, the notices simply do not happen: silently, with no
error, and without disturbing the agent that triggered them.

## Session names: they are yours

A session's name is **always yours** — starting an agent inside it does not
change it. (An automatic rename existed in an early version and was taken out on
purpose: it trampled the name you had just chosen with Ctrl-N.)

- **Ctrl-N**: the session is called whatever you typed.
- **Renaming later**: `prefix + $` (or `tmux rename-session`); the menu shows the
  new name as soon as you come back.
- **Reviving a grey row**: the session is born named after the project's folder
  — the one automatic christening there is, and it happens when the session is
  created, not when the agent starts. Launching it with flags (Ctrl-L) uses that
  same name: what changes is the command typed, not what the session is called.
  If you copy it with Ctrl-F, `<project>-fork` (and `<project>-fork#2` for the
  second copy): at a glance you know which is the real conversation.
- Two tmux details that apply to any name: it accepts neither `.` nor `:` (they
  are silently turned into `_`, so `example.com` becomes `example_com`), and if
  the name is already taken Flightdeck adds `#2`, `#3`…

**So how do I know what is inside, if the name is mine?** From the **annotation**
on the green row: `mysession   4m  review the checkout flow · ⏳ WAITING FOR
YOU` — the title you gave the conversation with `/rename` (or the project's
folder if it has no title). In other words: **a `/rename` inside the agent does
show up in the menu**; what never happens is the opposite (the tmux name does
not go into the agent).

## Several windows at once

You can have Flightdeck open in two or more terminal windows (or one on your
desktop and one on your phone). What **is** shared and what is not:

- **The menu is NOT shared.** Every `flightdeck` that arrives while the main
  menu is already being looked at by another window gets **a menu of its own**
  (`flightdeck-2`, `flightdeck-3`…), with its own search box and its own
  selection — no two cloned screens moving together. That extra menu dies by
  itself when you close the window or detach; the only permanent one is
  `flightdeck`. F12 and `prefix + j` always take you to **yours**.
- **The terminal's tab says where you are.** The window title is the
  conversation's **title** if it has one (the `/rename`, or the numbered one a
  handover left: `pricing 3`) and, failing that, the tmux session's name
  (`pricing`, `flightdeck`). It changes as you jump, and when the agent closes
  it goes back to the session's name. Only Claude Code publishes a conversation
  title, so a Codex or Antigravity session always shows the session's name.
- **The work sessions and the pinned rows ARE shared**: they are one single
  thing. If you enter the same session from two windows you see the same in
  both, and typing in one is typing in the other — which is exactly what lets
  you carry on from your phone what you left on your desktop. See
  [`REMOTE.md`](REMOTE.md).
