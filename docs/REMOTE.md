# From a phone, or another machine

There is no mobile version and nothing separate to configure: it is the same
menu. Everything lives inside tmux on the machine where the work is, so all a
phone has to do is open a terminal on it.

1. SSH to the machine from your phone (Termius, Blink, whichever client you
   use). A private network such as [Tailscale](https://tailscale.com) makes the
   machine reachable from anywhere without opening a port to the internet, but
   any SSH route does: a local network, a VPN, a jump host.
2. Type `flightdeck`.
3. Navigate with the on-screen keyboard: typing filters the list, Enter goes in.
   `F12` and `prefix + d` work too, if your SSH client sends them.
4. When you let go — or the connection drops — everything carries on running on
   the machine. Connect again, type `flightdeck`, and you are where you were.

The rest of this page is what is worth setting up once, and one thing worth
knowing.

- [F12 on a phone keyboard](#f12-on-a-phone-keyboard)
- [Making the command findable over SSH](#making-the-command-findable-over-ssh)
- [macOS: Full Disk Access for remote login](#macos-full-disk-access-for-remote-login)
- [The menu is per window, which is the point](#the-menu-is-per-window-which-is-the-point)

## F12 on a phone keyboard

F12 is the switch between the menu and the session you were in, and a phone
keyboard rarely has one. Two ways round it:

- **Map a key to F12 in your SSH client.** Termius has a customisable key row
  above the keyboard: add F12 to it and the key works exactly as it does on a
  desktop. Most clients have something equivalent.
- **Use `prefix + j` instead.** It exists for this: it takes you to the menu
  from any session, on any keyboard. Remember that it **only goes one way** —
  from inside the menu it does nothing — so you come back by choosing a session
  with Enter, like any other time. `prefix` is tmux's own leading key, `Ctrl-b`
  out of the box.

The same applies on a desktop wherever something else has already taken F12: the
terminal inside an editor, usually. `prefix + j` is the answer there too, and
[`LIMITATIONS.md`](LIMITATIONS.md#three-keys-stop-doing-what-they-used-to) says
how to give F12 back to whatever wants it.

## Making the command findable over SSH

When you SSH in and get a prompt, your shell reads its usual startup file and
everything is found. The trouble starts when a command arrives **without an
interactive shell** — `ssh mymachine flightdeck` from a script, or the "run this
on connect" feature some phone clients have. Then the startup file is never
read, and the command comes back as `flightdeck: command not found`.

The reason is where the installer puts its line. When `~/.local/bin` was not
already on your PATH, it adds

```sh
export PATH="$HOME/.local/bin:$PATH"  # flightdeck
```

to `~/.zshrc` for zsh (`~/.bashrc` or `~/.bash_profile` for bash, and
`~/.config/fish/config.fish` for fish, in fish's own spelling), and zsh only
reads `.zshrc` for **interactive** shells. A command run over SSH without one
never sees it.

The fix is one line in `~/.zshenv`, which zsh reads for **every** invocation,
interactive or not:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

If the tools themselves are somewhere unusual — Homebrew on an Apple Silicon Mac
puts them in `/opt/homebrew/bin`, which is not on the default PATH — add that
directory in the same line, because a non-interactive shell will not find `tmux`,
`fzf` or `python3` either:

```sh
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
```

For bash, the equivalent is to point `BASH_ENV` at a small file that does the
same, or simply to make your client open a login shell instead of running a bare
command. For fish, `~/.config/fish/config.fish` is read by every fish, including
a non-interactive one, so the line the installer already put there is enough:

```fish
set -gx PATH "$HOME/.local/bin" $PATH
```

If you only ever type `flightdeck` at a prompt you have got yourself, none of
this is needed.

## macOS: Full Disk Access for remote login

This one is a macOS privacy rule, not anything Flightdeck does, and it is worth
knowing about because the symptom is baffling.

macOS protects `~/Documents`, `~/Desktop` and `~/Downloads` per **application**,
not per user. Your terminal app has been granted access (you clicked "OK" once,
long ago), but a process that arrives over SSH did not come from that app: it
came from the remote-login service. So over SSH, reading a project inside
`~/Documents` fails with `Operation not permitted` — as the very same user who
owns the file, which is what makes it look like a broken installation rather
than a permission.

For Flightdeck that shows up as an agent you start over SSH failing to read the
very project it is supposed to be working on, if that project lives under one of
those three folders.

One fix is to grant Full Disk Access to the remote-login helper:

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Press **+**, then `⌘⇧G` in the file picker and type
   `/usr/libexec/sshd-keygen-wrapper`, which is the helper the remote-login
   service runs.
3. Turn it on, and turn **Remote Login** off and on again in **General →
   Sharing** so existing connections pick it up.

**Know what that grant costs before you make it.** It is not scoped to
Flightdeck: it gives **every** SSH session on the machine access to
`~/Documents`, `~/Desktop` and `~/Downloads`, whoever opened it and whatever they
run there. The narrower alternative is to need no grant at all — keep the
projects you work on, and the agent's own folder (`~/.claude` and its
equivalents, which are outside the protected three by default), out of those
three directories. A project under `~/code` or `~/src` is reachable over SSH with
no privacy exception of any kind.

The exact wording of those panes moves between macOS releases; the item you are
looking for is always the SSH helper, under Full Disk Access.

A related finding, on macOS 26: **plain SSH rather than mosh** is the safer
choice for exactly this reason. The mosh server is a separate program that does
not inherit the SSH helper's access, and a session that cannot read the folder it
works in is worse than one that reconnects slightly less gracefully.

## The menu is per window, which is the point

Each connection that arrives while the main menu is already being looked at gets
a menu of its own, with its own cursor and its own search box. The **work**
sessions are shared, so the session you left running on your desktop is the one
you open on your phone — same screen, same conversation, and typing in one is
typing in the other. That is what makes this work at all, and
[`GUIDE.md`](GUIDE.md#several-windows-at-once) has the details.
