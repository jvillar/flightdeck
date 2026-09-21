#!/bin/sh
# Flightdeck's installer, the one behind the line in the README:
#
#   curl -fsSL https://raw.githubusercontent.com/jvillar/flightdeck/main/install.sh | sh
#
# It gets a machine ready and then hands over: it checks the system and the
# dependencies, offers to download an fzf when the one here is too old, fetches
# the release into `~/.local/share/flightdeck/current`, links the command into
# `~/.local/bin` and puts one marked line in the shell's rc file -- and then
# `exec`s `flightdeck install`, which is what sets the agents up and has the
# doctor say whether it worked.
#
# Three things about the shape of it, and none of them is style:
#
# * **Everything is a function and `main "$@"` is the LAST line.**
#   Piped into `sh`, a script is executed as it arrives: a half-downloaded one
#   must define some functions and do nothing, never run half an installation.
# * **Every question is read from `/dev/tty`**, and this shell's own stdin is
#   never redirected. Piped into `sh`, stdin IS the script, so a `read` there
#   would eat the rest of it.
# * **POSIX sh**, so it runs under the `dash` that is `/bin/sh` on Debian and
#   Ubuntu: no arrays, no `[[`, no `read -p`, no `${var//…}`, no `pipefail`.
#   `shellcheck -s sh install.sh` is the validator.
#
# It never runs a package manager and never asks for sudo. What is
# missing is PRINTED, with the command for this machine, and that is all.
set -eu

# ── what this script knows ───────────────────────────────────────────────────

REPO_URL="https://github.com/jvillar/flightdeck"
LATEST_URL="$REPO_URL/releases/latest/download/flightdeck.tar.gz"

# The floors and the recommendations, the same numbers `flightdeck doctor` uses:
# tmux 3.2 is where `new-session -e` and `display-message -d` arrive and 3.4
# where notices can be printed literally; fzf 0.36 is `--listen` (the menu
# reloading while it is open) and 0.71 is `--id-nth` (the cursor staying on the
# same session when the list is reordered); 3.9 is the python every module is
# written against.
TMUX_MIN="3.2"
TMUX_GOOD="3.4"
FZF_MIN="0.36"
FZF_GOOD="0.71"
PYTHON_MIN="3.9"

# The fzf release this script offers to download. PINNED, and not
# "releases/latest", because the asset's name carries the version and so does
# the checksums file: with `latest` there is no way to ask for a file without
# already knowing which one.
#
# Checked against https://github.com/junegunn/fzf/releases on 2026-09-17: the
# current release is v0.74.4, its assets are named `fzf-0.74.4-darwin_arm64.tar.gz`,
# `fzf-0.74.4-darwin_amd64.tar.gz`, `fzf-0.74.4-linux_amd64.tar.gz` and
# `fzf-0.74.4-linux_arm64.tar.gz`, and the checksums live in
# `fzf_0.74.4_checksums.txt` as lines of `<sha256>  <asset>`.
FZF_VERSION=0.74.4

# The two packages Flightdeck needs from a package manager. They travel together
# on purpose: somebody installing tmux by hand today is the same person whose
# fzf will be too old tomorrow, and one line is easier to act on than two.
PACKAGES="tmux fzf"

# The line added to a shell rc file, exactly as it reads here. The `# flightdeck`
# at the END is the mark, and the ONLY thing that tells `flightdeck uninstall`
# which line in somebody's rc file is ours (`uninstall.is_our_line` matches lines
# that END with it, so it can never take a heading or a comment of theirs). The
# two spellings have to agree for ever.
# shellcheck disable=SC2016  # `$HOME` and `$PATH` are for the shell that READS
# this line later, not for this one: expanding them here would bake one machine's
# home into somebody's rc file.
RC_LINE_SH='export PATH="$HOME/.local/bin:$PATH"  # flightdeck'
# shellcheck disable=SC2016  # the same, in fish's spelling.
RC_LINE_FISH='set -gx PATH "$HOME/.local/bin" $PATH  # flightdeck'

# Native Windows is not supported. Single quotes: the backticks in it are part
# of the sentence, not a command to run.
# shellcheck disable=SC2016  # the backticks are part of the sentence.
WSL_MESSAGE='Flightdeck runs inside WSL2: run `wsl --install`, open Ubuntu and run this command there'

USAGE="usage: install.sh [--yes] [--force] [--keep-legacy] [--version vX.Y.Z]"

# What a release is not allowed to hold, as two patterns read off `tar`'s own
# listings before a byte is extracted (see `fetch_code`).
#
# A name that is absolute or climbs out with `..`, used on `tar -tzf` (one name
# per line) and again on each symlink's TARGET.
RE_BAD_NAME='^/|(^|/)\.\.(/|$)'
# A type letter that is not a file, a directory or a symlink, used on column 1
# of `tar -tvzf`. That column is the type in both the bsdtar macOS ships and the
# GNU tar on Linux (`-`, `d`, `l`, `h` for a hard link, `p` for a fifo, `c`/`b`
# for device nodes); every other column differs between them and none is read.
RE_BAD_TYPE='^[^-dl]'

# ── saying things ────────────────────────────────────────────────────────────

say() { printf '%s\n' "$*"; }

warn() { printf 'flightdeck: %s\n' "$*" >&2; }

die() { warn "$*"; exit 1; }

step() { printf '\n%s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    say "$USAGE"
    say ""
    say "  --yes              take the default answer to every question"
    say "                     (so does FLIGHTDECK_YES=1)"
    say "  --force            let the agent setup replace items somebody else"
    say "                     put in codex's status line"
    say "  --keep-legacy      leave an older cockpit's entries in place"
    say "  --version vX.Y.Z   install that release instead of the latest one"
}

# One yes/no question, read from the TERMINAL and never from stdin: piped into
# `sh`, this script IS stdin. Enter means yes.
#
# **A question that cannot be asked takes the prompt's own default, yes**: the
# only thing this ever asks about is downloading fzf's official release,
# checksummed, into a directory only the `flightdeck` command sees and
# `uninstall` removes, and a scripted install that simply works was judged
# worth more than a refusal nobody was there to read. `--yes` (or
# `FLIGHTDECK_YES=1`) still says yes to everything explicitly, and it is what
# CI and `flightdeck update` pass.
ask() {
    if [ "$YES" = 1 ]; then
        return 0
    fi
    if ! ( : </dev/tty ) 2>/dev/null; then
        # Nobody to ask, so the prompt's own default is taken: yes. The one
        # thing this ever downloads is fzf's official release, checksummed, into
        # a directory only the `flightdeck` command sees and `uninstall`
        # removes, and a scripted install that simply works was judged worth
        # more than a refusal nobody was there to read.
        say "$1"
        say "  (no terminal to ask on: taking the default, yes)"
        return 0
    fi
    printf '%s ' "$1"
    _answer=""
    read -r _answer </dev/tty || _answer=""
    case "$_answer" in
        n|N|no|No|NO) return 1 ;;
        *) return 0 ;;
    esac
}

# ── versions ─────────────────────────────────────────────────────────────────

# MAJOR and MINOR of a version string, as two numbers, or NOTHING when there is
# no version in it. `tmux 3.2a` -> `3 2`, `0.72.0 (Homebrew)` -> `0 72`,
# `tmux next-3.5` -> `3 5`. Only those two numbers matter: they are what the
# minimums are written in, and tmux's trailing letter is not a number at all.
version_pair() {
    printf '%s' "${1:-}" | sed -n 's/^[^0-9]*\([0-9][0-9]*\)\.\([0-9][0-9]*\).*$/\1 \2/p'
}

# Is version A at least version B? A version neither side can read is never "at
# least" anything: the callers decide what to do about that, and none of them
# treats it as good news.
version_ge() {
    _a="$(version_pair "${1:-}")"
    _b="$(version_pair "${2:-}")"
    if [ -z "$_a" ] || [ -z "$_b" ]; then
        return 1
    fi
    if [ "${_a% *}" -gt "${_b% *}" ]; then return 0; fi
    if [ "${_a% *}" -lt "${_b% *}" ]; then return 1; fi
    if [ "${_a#* }" -ge "${_b#* }" ]; then return 0; fi
    return 1
}

# `1.2.3` or `v1.2.3` -> `v1.2.3`; anything that is not a version -> nothing.
# The result ends up in a URL PATH, so something carrying a slash or a space is
# REFUSED rather than escaped: there is no such release, and a request built out
# of it would be asking for somebody else's file. The same rule, and the same
# shape, as `update.normalise_tag`.
normalise_tag() {
    printf '%s' "${1:-}" | sed -n 's/^[vV]\{0,1\}\([0-9][0-9A-Za-z.+_-]*\)$/v\1/p'
}

# ── the arguments ────────────────────────────────────────────────────────────

# `--yes`, `--force`, `--keep-legacy` and `--version` are this script's own.
# Only the first three are passed on to `flightdeck install`, which
# would refuse a `--version` it knows nothing about.
parse_args() {
    YES=0
    FORCE=0
    KEEP_LEGACY=0
    TAG=""
    if [ "${FLIGHTDECK_YES:-}" = "1" ]; then
        YES=1
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y) YES=1 ;;
            --force) FORCE=1 ;;
            --keep-legacy) KEEP_LEGACY=1 ;;
            --version)
                shift
                if [ "$#" -eq 0 ]; then
                    warn "--version needs a release, like --version v1.2.3"
                    printf '%s\n' "$USAGE" >&2
                    exit 2
                fi
                set_tag "$1"
                ;;
            --version=*)
                set_tag "${1#--version=}"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                warn "unknown option $1"
                printf '%s\n' "$USAGE" >&2
                exit 2
                ;;
        esac
        shift
    done
}

set_tag() {
    TAG="$(normalise_tag "$1")"
    if [ -z "$TAG" ]; then
        warn "'$1' is not a version; they look like v1.2.3"
        printf '%s\n' "$USAGE" >&2
        exit 2
    fi
}

# ── 1. the system ────────────────────────────────────────────────────────────

# macOS, Linux or WSL2 -> `SYSTEM`. Native Windows is the end of the road:
# Flightdeck is tmux and a POSIX shell, and both live inside WSL2.
detect_system() {
    _uname="$(uname -s 2>/dev/null || echo unknown)"
    # Windows is asked first and by two names: a shell under MSYS or Git Bash
    # reports things that read like a Unix otherwise, and `OS=Windows_NT` is
    # Windows saying so itself.
    case "$_uname" in
        MINGW*|MSYS*|CYGWIN*) die "$WSL_MESSAGE" ;;
    esac
    if [ "${OS:-}" = "Windows_NT" ]; then
        die "$WSL_MESSAGE"
    fi
    case "$_uname" in
        Darwin) SYSTEM=darwin ;;
        *) SYSTEM=linux ;;
    esac
    # WSL2 is a Linux whose `/proc/version` carries "microsoft". It is worth
    # saying out loud because everything -- Claude Code, tmux, Flightdeck --
    # lives in here and not in Windows, which is not obvious from a Windows
    # desktop.
    if [ "$SYSTEM" = "linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then
        SYSTEM=wsl2
    fi
    case "$SYSTEM" in
        darwin) say "macOS" ;;
        wsl2) say "WSL2 — Claude Code, tmux and Flightdeck all live in here, not in Windows" ;;
        *) say "Linux" ;;
    esac
}

# How this machine installs a command-line tool. PRINTED, never run.
pkg_hint() {
    if [ "$SYSTEM" = "darwin" ]; then
        printf 'brew install %s' "$PACKAGES"
    elif have apt-get; then
        printf 'sudo apt install %s' "$PACKAGES"
    elif have dnf; then
        printf 'sudo dnf install %s' "$PACKAGES"
    else
        printf 'install %s with your package manager' "$PACKAGES"
    fi
}

# ── 2. the dependencies ──────────────────────────────────────────────────────

check_deps() {
    if ! have tmux; then
        warn "tmux is not on PATH, and Flightdeck is a cockpit built on tmux."
        die "install it with: $(pkg_hint)"
    fi
    _tmux="$(tmux -V 2>/dev/null || true)"
    if [ -z "$(version_pair "$_tmux")" ]; then
        # The doctor calls this a warning and carries on, so the installer does
        # too: refusing to install on a tmux built from master would make the
        # installer stricter than the check that follows it.
        warn "tmux says '$_tmux', which is not a version I can read — carrying on"
    elif ! version_ge "$_tmux" "$TMUX_MIN"; then
        warn "$_tmux is below the minimum $TMUX_MIN ('new-session -e' and 'display-message -d' arrive there)."
        die "install a newer one with: $(pkg_hint)"
    elif ! version_ge "$_tmux" "$TMUX_GOOD"; then
        say "$_tmux — enough ($TMUX_GOOD and newer print the floating notices literally)"
    else
        say "$_tmux"
    fi

    if ! have python3; then
        warn "python3 is not on PATH. Flightdeck is python $PYTHON_MIN and its standard library, and nothing else."
        die "install python3 $PYTHON_MIN or newer and make sure 'python3' is on PATH"
    fi
    _python="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if ! version_ge "$_python" "$PYTHON_MIN"; then
        die "python3 is ${_python:-not answering}, below the minimum $PYTHON_MIN"
    fi
    say "python3 $_python"

    have curl || die "curl is not on PATH, and it is how the release is fetched"
    have tar || die "tar is not on PATH, and it is how the release is unpacked"

    if have claude; then
        say "claude is on PATH"
    else
        warn "claude is not on PATH — Flightdeck installs all the same, but it is a cockpit with nothing flying in it yet"
    fi
}

# fzf IS the menu, so a machine without one has nothing to install into. When it
# is missing, or older than the recommended 0.71, the official binary is
# offered: pinned, checksummed, and into `<data>/bin`, which the command
# puts on its own PATH -- nothing is written into a system directory and nothing
# is installed behind a package manager's back.
maybe_download_fzf() {
    _fzf=""
    if have fzf; then
        _fzf="$(fzf --version 2>/dev/null || true)"
    fi
    if have fzf && [ -z "$(version_pair "$_fzf")" ]; then
        warn "fzf is on PATH but does not answer '--version' with anything I can read — leaving it alone"
        return 0
    fi
    if version_ge "$_fzf" "$FZF_GOOD"; then
        say "fzf $_fzf"
        return 0
    fi
    if have fzf; then
        say "fzf $_fzf — older than the recommended $FZF_GOOD"
    else
        say "fzf is not installed, and fzf is the menu itself"
    fi
    if ask "Download fzf $FZF_VERSION from its GitHub release to $DATA/bin? [Y/n]"; then
        if download_fzf; then
            return 0
        fi
    fi
    # No fzf was installed: it was declined, or it could not be done. What that
    # means depends entirely on what is already here.
    if ! have fzf; then
        warn "there is no fzf here, and the menu cannot be drawn without one."
        die "install it with: $(pkg_hint) — or re-run with --yes, which takes the download offered above"
    fi
    if version_ge "$_fzf" "$FZF_MIN"; then
        warn "carrying on with fzf $_fzf: the menu will reload while it is open, but the cursor will not stay on the same session when the list is reordered (that is --id-nth, $FZF_GOOD and newer)"
        return 0
    fi
    warn "fzf $_fzf is below the minimum $FZF_MIN: without '--listen' the menu cannot refresh while it is open."
    die "install a newer one with: $(pkg_hint) — or re-run with --yes, which takes the download offered above"
}

# The published fzf binary into `<data>/bin/fzf`. -> 0 when it is there.
#
# Every step checks its own error rather than leaning on `set -e`: this function
# is called from an `if`, and inside a condition the shell IGNORES errexit --
# a failed download would otherwise carry straight on to checksumming a file
# that is not there.
download_fzf() {
    _os="$SYSTEM"
    if [ "$_os" = "wsl2" ]; then
        _os=linux                     # WSL2 runs the Linux build, like any Linux
    fi
    _machine="$(uname -m 2>/dev/null || echo unknown)"
    case "$_machine" in
        x86_64|amd64) _arch=amd64 ;;
        arm64|aarch64) _arch=arm64 ;;
        *)
            warn "there is no published fzf build for $_machine — skipping the download"
            return 1
            ;;
    esac
    if ! have shasum && ! have sha256sum; then
        warn "neither shasum nor sha256sum is here, and an unchecked download is not worth having — skipping it"
        return 1
    fi

    # One base for the archive AND its checksums, so the two can never come from
    # different places. The variable is the tests' seam (a `file://` directory).
    _base="${FLIGHTDECK_FZF_BASE_URL:-https://github.com/junegunn/fzf/releases/download/v$FZF_VERSION}"
    _asset="fzf-$FZF_VERSION-${_os}_${_arch}.tar.gz"
    _sums="fzf_${FZF_VERSION}_checksums.txt"
    _work="$(mktemp -d "${TMPDIR:-/tmp}/flightdeck-fzf.XXXXXX")" || return 1

    say "  fetching $_base/$_asset"
    if ! curl -fsSL -o "$_work/$_asset" "$_base/$_asset"; then
        rm -rf "$_work"
        warn "could not download $_base/$_asset"
        return 1
    fi
    if ! curl -fsSL -o "$_work/$_sums" "$_base/$_sums"; then
        rm -rf "$_work"
        warn "could not download $_base/$_sums, and the archive is not worth having unchecked"
        return 1
    fi
    _want="$(awk -v want="$_asset" '$2 == want { print $1; exit }' "$_work/$_sums" || true)"
    _got="$(sha256_of "$_work/$_asset")"
    if [ -z "$_want" ] || [ "$_want" != "$_got" ]; then
        # A `die` and not a `return 1`: everything else here is "I could not get
        # you an fzf", which the caller can work around. A file that does not
        # match its published checksum is not the file, and carrying on as if
        # nothing had happened is the one thing that must not occur.
        rm -rf "$_work"
        die "the fzf download does not match its published checksum ($_asset) — it has been deleted and nothing was installed"
    fi
    if ! tar -xzf "$_work/$_asset" -C "$_work" || [ ! -f "$_work/fzf" ]; then
        rm -rf "$_work"
        warn "the fzf archive does not hold an fzf"
        return 1
    fi
    mkdir -p "$DATA/bin"
    if ! mv "$_work/fzf" "$DATA/bin/fzf"; then
        rm -rf "$_work"
        warn "could not put the fzf in $DATA/bin"
        return 1
    fi
    chmod +x "$DATA/bin/fzf"
    rm -rf "$_work"
    say "✓ fzf $FZF_VERSION in $DATA/bin (the command puts that directory on its own PATH)"
    return 0
}

# The sha256 of one file, with whichever of the two tools this machine has.
sha256_of() {
    if have shasum; then
        shasum -a 256 "$1" | awk '{ print $1 }'
    elif have sha256sum; then
        sha256sum "$1" | awk '{ print $1 }'
    fi
}

# ── 3. the terminal ──────────────────────────────────────────────────────────

# The menu is made of glyphs (the badges, the brain, the box-drawing). In a
# locale that is not UTF-8 they come out as question marks, which looks like a
# broken installation and is not one.
check_locale() {
    case "$(printf '%s' "${LC_ALL:-}${LANG:-}" | tr '[:upper:]' '[:lower:]')" in
        *utf-8*|*utf8*)
            say "locale: ${LC_ALL:-${LANG:-}}"
            ;;
        *)
            warn "neither LC_ALL nor LANG is UTF-8 (LC_ALL=${LC_ALL:-unset}, LANG=${LANG:-unset}) — the menu's glyphs will not print. Add something like 'export LANG=en_US.UTF-8' to your shell's rc file"
            ;;
    esac
}

# ── 4. the code ──────────────────────────────────────────────────────────────

# The release into `<data>/current`, atomically. `current` is a NAME
# and not a version: the `~/.local/bin` link and the absolute paths in the three
# agents' settings all point inside it, which is why an install -- like an
# update -- replaces what is BEHIND the name instead of putting the new code
# somewhere new.
fetch_code() {
    _url="${FLIGHTDECK_TARBALL_URL:-}"
    if [ -z "$_url" ]; then
        if [ -n "$TAG" ]; then
            _url="$REPO_URL/releases/download/$TAG/flightdeck.tar.gz"
        else
            _url="$LATEST_URL"
        fi
    fi
    say "  from: $_url"
    mkdir -p "$DATA" || die "could not create $DATA"
    # The temporary directory lives INSIDE <data>, on the same filesystem as
    # `current`: that is what makes the two steps at the end renames and not
    # copies, and a copy is what would leave a half-written `current` behind if
    # the disk filled up.
    _work="$(mktemp -d "$DATA/.install-XXXXXX")" || die "could not write in $DATA"
    _archive="$_work/flightdeck.tar.gz"
    if ! curl -fsSL -o "$_archive" "$_url"; then
        rm -rf "$_work"
        die "could not download $_url"
    fi
    # What the archive HOLDS is read before a byte of it is written, because
    # there is no undoing half an extraction. The same three families
    # `update.unsafe_members` refuses:
    #
    #  * a name that is absolute or climbs out with `..`, which writes wherever
    #    it likes;
    #  * a link whose target does the same, which turns the NEXT member into a
    #    write somewhere else entirely (`x -> /home/you/.ssh`, and then
    #    `x/authorized_keys`) -- and neither that name nor that target is
    #    absolute or carries a `..` by itself;
    #  * anything that is not a file, a directory or a symlink -- a hard link, a
    #    fifo, a device node -- which has no business in a directory of source
    #    code.
    #
    # A link target carrying `..` anywhere is refused outright rather than
    # worked out, and `unsafe_members` now does the same: a CHAIN of links that
    # each land inside walks one level out per hop. One place where this is
    # still STRICTER than the python, and deliberately: a hard link is refused
    # with the rest rather than having its target checked. That piece of path
    # arithmetic is not worth writing in shell, and a Flightdeck release holds
    # files and directories and nothing else -- if one ever legitimately needs a
    # link, this is the line that says so, loudly, at release-testing time.
    _names="$_work/names.txt"
    _listing="$_work/listing.txt"
    if ! tar -tzf "$_archive" > "$_names" 2>/dev/null \
        || ! tar -tvzf "$_archive" > "$_listing" 2>/dev/null; then
        rm -rf "$_work"
        die "could not read the release: it is not a tar.gz I can open"
    fi
    if grep -Eq "$RE_BAD_NAME" "$_names"; then
        rm -rf "$_work"
        die "the release holds entries that would write outside the directory, so nothing was extracted"
    fi
    if grep -Eq "$RE_BAD_TYPE" "$_listing"; then
        rm -rf "$_work"
        die "the release holds entries that are not files, directories or symlinks, so nothing was extracted"
    fi
    # A symlink's target, which `tar -tvzf` prints after ` -> ` in both tars.
    if sed -n 's/.* -> //p' "$_listing" | grep -Eq "$RE_BAD_NAME"; then
        rm -rf "$_work"
        die "the release holds a link pointing outside the directory, so nothing was extracted"
    fi
    mkdir -p "$_work/unpack"
    if ! tar -xzf "$_archive" -C "$_work/unpack"; then
        rm -rf "$_work"
        die "could not unpack the release"
    fi
    # Exactly ONE directory inside, which is what makes "extract, then rename
    # the result into place" a safe pair of steps: what is renamed is one
    # directory, whatever it happens to be called.
    set -- "$_work"/unpack/*
    if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
        rm -rf "$_work"
        die "the release must hold exactly one directory with Flightdeck inside it"
    fi
    _root="$1"
    for _needed in bin/flightdeck VERSION; do
        if [ ! -e "$_root/$_needed" ]; then
            rm -rf "$_work"
            die "the release has no $_needed in it: this is not a Flightdeck release"
        fi
    done
    # Whatever mode the archive carried: a release packed with a strange umask
    # would otherwise leave a command the shell refuses to run, and no way to
    # tell why.
    chmod +x "$_root/bin/flightdeck"

    rm -rf "$DATA/previous"
    if [ -d "$DATA/current" ]; then
        mv "$DATA/current" "$DATA/previous"
    fi
    if ! mv "$_root" "$DATA/current"; then
        # Two renames in one directory, so `current` is never half a
        # Flightdeck -- and if the second one fails the old code goes back where
        # it was, because leaving that name empty breaks the cockpit rather than
        # merely failing to install.
        if [ -d "$DATA/previous" ] && [ ! -d "$DATA/current" ]; then
            mv "$DATA/previous" "$DATA/current" || true
        fi
        rm -rf "$_work"
        die "could not put the new code in $DATA/current"
    fi
    rm -rf "$_work"
    say "✓ code in $DATA/current ($(cat "$DATA/current/VERSION" 2>/dev/null || echo "version unknown"))"
}

# ── 5. the command ───────────────────────────────────────────────────────────

# `flightdeck` and its short name into `~/.local/bin`, both pointing inside the
# code. `uninstall` takes out exactly the links that LAND in there, so a `fld`
# of somebody else's survives both this and that.
link_command() {
    _bin="$HOME/.local/bin"
    _target="$DATA/current/bin/flightdeck"
    mkdir -p "$_bin" || die "could not create $_bin"
    ln -sf "$_target" "$_bin/flightdeck" || die "could not link $_bin/flightdeck"
    say "✓ $_bin/flightdeck -> $_target"
    # `fld` is a short name and somebody else's may be sitting there. It is
    # taken only when nothing answers to it, or when what is there is already
    # this very link.
    #
    # The PATH is asked (`have`) and so is the FILE, because the two are not the
    # same question here: `~/.local/bin` is quite likely not on the PATH yet --
    # that is the line being added two steps down -- and a script of theirs
    # sitting in there unreachable is still theirs. Without the second check
    # `ln -sf` would delete it without a word.
    _fld="$_bin/fld"
    if [ "$(readlink "$_fld" 2>/dev/null || true)" != "$_target" ] \
        && { [ -e "$_fld" ] || [ -L "$_fld" ] || have fld; }; then
        say "fld is already taken ($(command -v fld 2>/dev/null || echo "$_fld")) — left alone; the full name is 'flightdeck'"
        return 0
    fi
    ln -sf "$_target" "$_fld" || die "could not link $_fld"
    say "✓ $_fld -> $_target (the short name)"
}

# ONE line in the rc file of the shell the user actually runs, and only when
# `~/.local/bin` is not already on their PATH. Idempotent: a second install
# writes nothing and makes no second backup.
add_path_line() {
    _bin="$HOME/.local/bin"
    case ":${PATH}:" in
        *":$_bin:"*)
            say "$_bin is already on your PATH"
            return 0
            ;;
    esac
    _shell="${SHELL:-}"
    _shell="${_shell##*/}"
    _line="$RC_LINE_SH"
    case "$_shell" in
        zsh) _rc="$HOME/.zshrc" ;;
        bash)
            # A login shell on macOS reads `.bash_profile` and not `.bashrc`,
            # which is the whole difference between a PATH that takes and one
            # that does not.
            if [ "$SYSTEM" = "darwin" ]; then
                _rc="$HOME/.bash_profile"
            else
                _rc="$HOME/.bashrc"
            fi
            ;;
        fish)
            _rc="$HOME/.config/fish/config.fish"
            _line="$RC_LINE_FISH"
            ;;
        *)
            # An rc file we cannot name is not one we may write to.
            say "your shell is '${_shell:-unknown}', so add this to its startup file yourself:"
            say "    $RC_LINE_SH"
            say "  (it puts $_bin on your PATH, which is where the command now lives)"
            return 0
            ;;
    esac
    if [ -f "$_rc" ] && grep -Fq "$_line" "$_rc"; then
        say "the PATH line is already in $_rc"
        return 0
    fi
    mkdir -p "$(dirname "$_rc")" || die "could not create the directory for $_rc"
    _backup=""
    if [ -f "$_rc" ]; then
        _backup="$_rc.bak-flightdeck-$(date +%Y%m%d-%H%M%S)"
        cp -p "$_rc" "$_backup" || die "could not back $_rc up"
    fi
    # An rc file is hand-edited and may not end in a newline; without this the
    # line would be glued onto whatever was last in there.
    if [ -s "$_rc" ] && [ "$(tail -c 1 "$_rc" | wc -l)" -eq 0 ]; then
        printf '\n' >> "$_rc"
    fi
    printf '%s\n' "$_line" >> "$_rc" || die "could not write to $_rc"
    say "✓ added one line to $_rc:"
    say "    $_line"
    if [ -n "$_backup" ]; then
        say "  backup: $_backup"
    fi
    say "  open a new shell (or run 'source $_rc') before typing 'flightdeck'"
}

# ── 6. handing over ──────────────────────────────────────────────────────────

# `flightdeck install` does the rest: the agents' hooks, the status line, the
# migration, the pins, the tmux keys and the doctor. It is `exec`ed, so its exit
# code is this script's.
hand_over() {
    # The rc line has just been written and nobody has sourced it, so
    # `flightdeck` is not yet on any PATH -- and the doctor the child ends
    # with calls "the link is there and the command does not resolve" a ✗. One
    # export, for this process and the child it is about to become.
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    _cmd="$HOME/.local/bin/flightdeck"
    set -- install
    if [ "$FORCE" = 1 ]; then
        set -- "$@" --force
    fi
    if [ "$KEEP_LEGACY" = 1 ]; then
        set -- "$@" --keep-legacy
    fi
    # The child gets the TERMINAL on its stdin so that its own questions --
    # which status line to use -- and the demo that goes with them work when
    # this script was piped into `sh`. Never `exec </dev/tty` for
    # this shell: piped in, its stdin is the script itself. And when there is no
    # terminal, nobody is asked: `--yes` goes with it instead.
    if [ "$YES" != 1 ] && ( : </dev/tty ) 2>/dev/null; then
        say ""
        say "→ $_cmd $*"
        exec "$_cmd" "$@" </dev/tty
    fi
    set -- "$@" --yes
    say ""
    say "→ $_cmd $*"
    exec "$_cmd" "$@"
}

# ── the steps, in order ──────────────────────────────────────────────────────

main() {
    parse_args "$@"
    # Where the code goes, the same path `config.data_dir()` reads.
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}/flightdeck"

    say "Flightdeck — installing"
    step "1/6  this machine"
    detect_system
    step "2/6  what Flightdeck needs"
    check_deps
    maybe_download_fzf
    step "3/6  the terminal"
    check_locale
    step "4/6  the code"
    fetch_code
    step "5/6  the command"
    link_command
    add_path_line
    step "6/6  setting up the agents on this machine"
    hand_over
}

main "$@"
