"""`flightdeck update`: fetch a release, put it in place, reinstall, restart.

The shape of it, and why each step is the way it is:

1. **Refuse a source checkout** (`config.installed_here`). This command
   manages `<data>/current` and nothing else; a clone somebody runs from is
   updated with `git pull`. It is the first thing checked, before a byte is
   downloaded.
2. **Download** the tarball with `urllib` into a temporary file inside `<data>`,
   which is the same filesystem as `current` -- that is what makes the rename at
   the end a rename and not a copy.
3. **Extract** into a temporary directory next to it, after deciding whether the
   archive is allowed to be extracted at all: no absolute names, nothing climbing
   out with `..`, no link pointing outside, nothing that is not a file, a
   directory or a link (`unsafe_members`). Python 3.9 has no `filter=` for
   `tarfile.extractall`, and "extract first, check later" is not a check. The
   tree has to be ONE top-level directory holding `bin/flightdeck` and `VERSION`,
   and the command comes out executable whatever mode the archive carried.
4. **Compare versions** and stop when they are equal: nothing is swapped, and
   nothing is reinstalled either.
5. **Swap**: `previous` out, `current` -> `previous`, the new tree -> `current`.
   Two renames inside one directory, so `current` is never half a Flightdeck. The
   NAME is what the agents' hooks and the `~/.local/bin` link point at, which is
   why an update replaces what is behind it instead of installing somewhere new.
   If the second rename fails, the old code goes back where it was.
6. **Reinstall by running the NEW command as a subprocess**
   (`<data>/current/bin/flightdeck install --yes`) and never in this process: the
   interpreter running has the OLD modules imported and would write the old
   paths back into the agents' settings.
7. **Restart the menus**, but only when a tmux server is up: the options and
   bindings live inside the server and `restart` has nothing to say to a machine
   where there is none.

Then the old and new versions and the changelog URL, which is the one thing a
person actually wants out of an update.
"""

import contextlib
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from flightdeck import common, config

USAGE = "usage: flightdeck update [--version vX.Y.Z]\n"

REPO = "https://github.com/jvillar/flightdeck"
LATEST_URL = REPO + "/releases/latest/download/flightdeck.tar.gz"
TAGGED_URL = REPO + "/releases/download/%s/flightdeck.tar.gz"
CHANGELOG_URL = REPO + "/blob/%s/CHANGELOG.md"

# A release tag, once the leading `v` has been put back: digits first, then the
# characters a version can carry. It ends up in a URL PATH, so anything with a
# slash or a space in it is refused rather than escaped -- there is no such
# release, and a request built out of it would be asking for somebody else's.
TAG = re.compile(r"^v[0-9][0-9A-Za-z.+_-]*$")

# Seconds to wait on the download. Long enough for a slow line and a big
# release, short enough that a hung connection is not a hung terminal.
TIMEOUT = 120

# What the two files an archive must carry are called.
REQUIRED = ("bin/flightdeck", "VERSION")


class UpdateError(Exception):
    """Something about the release itself: it could not be fetched, read or put
    in place. One line for the user, never a traceback."""


# ── where the release comes from ─────────────────────────────────────────────

def normalise_tag(version):
    """`1.2.3` or `v1.2.3` -> `v1.2.3`; anything that is not a version -> None.

    The `v` is optional because both spellings are what people type, and the
    result is always the tag as GitHub has it.
    """
    text = (version or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    tag = "v" + text
    return tag if TAG.match(tag) else None


def tarball_url(tag=None):
    """The release to fetch: the environment, else that tag, else the latest.

    `FLIGHTDECK_TARBALL_URL` is the seam the tests use (a `file://` URL) and the
    way to try a release candidate that is not on GitHub. It wins over
    everything, including `--version`: it names a specific file, and there is
    nothing left to pick.
    """
    override = os.environ.get("FLIGHTDECK_TARBALL_URL")
    if override:
        return override
    return TAGGED_URL % tag if tag else LATEST_URL


def changelog_url(tag=None):
    """Where to read what changed: that tag's file, or main's."""
    return CHANGELOG_URL % (tag or "main")


# ── the archive ──────────────────────────────────────────────────────────────

def _inside(path):
    """Is this archive-relative path inside the archive's own root?"""
    if posixpath.isabs(path) or path.startswith("/"):
        return False
    normalised = posixpath.normpath(path)
    return normalised != ".." and not normalised.startswith("../")


def unsafe_members(members):
    """The members of an archive that must not be extracted. -> their names

    Decided BEFORE anything is written, because there is no undoing half an
    extraction. Three families, and each of them is a real way a tarball takes
    over a machine: a name that is absolute or climbs out with `..` writes
    wherever it likes; a symlink or a hard link pointing outside turns the next
    write into a write somewhere else; and anything that is neither a file, a
    directory nor a link (a device node, a fifo) has no business in a directory
    holding source code.

    A link target is refused outright when it is absolute or carries a `..`
    ANYWHERE, rather than being worked out to see where it lands. That is
    `install.sh`'s rule, now here too, and the reason is a CHAIN: `a -> ..`,
    `a/b -> ..`, `a/b/c -> ..` each lands inside on its own, and followed one
    after another they walk one level out per hop. Where a chain of links ends
    is only knowable by resolving it, and an archive of ours holds files and
    directories and nothing else -- so the day one legitimately needs a link,
    this is the line that says so, loudly, at release-testing time.
    """
    bad = []
    for member in members:
        name = member.name
        if not _inside(name):
            bad.append(name)
            continue
        if member.issym() or member.islnk():
            target = member.linkname or ""
            if posixpath.isabs(target) or ".." in target.split("/"):
                bad.append(name)
            continue
        if not (member.isfile() or member.isdir()):
            bad.append(name)
    return bad


def top_directory(members):
    """The one directory everything in the archive lives under, or None.

    `flightdeck.tar.gz` holds exactly one top-level directory, and that is what
    makes "extract, then rename the result into place" a safe pair of steps:
    what is renamed is one directory, whatever it happens to be called.
    """
    tops = set()
    for member in members:
        parts = [part for part in posixpath.normpath(member.name).split("/")
                 if part not in ("", ".")]
        if parts:
            tops.add(parts[0])
    return tops.pop() if len(tops) == 1 else None


def download(url, destination):
    """Fetch `url` into `destination`. Raises `UpdateError` and nothing else."""
    try:
        with contextlib.closing(urllib.request.urlopen(url, timeout=TIMEOUT)) as response:
            with open(str(destination), "wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # ValueError is what urllib raises for a URL whose scheme it does not
        # know, which is what a mistyped `FLIGHTDECK_TARBALL_URL` looks like.
        raise UpdateError("could not download %s: %s" % (url, exc))


def extract(archive, into):
    """Unpack `archive` inside `into`. -> the one directory it held.

    Raises `UpdateError` for an archive that is not a release of ours: unsafe
    members, more or fewer than one top-level directory, or a tree missing the
    command or the VERSION.
    """
    try:
        with tarfile.open(str(archive), "r:*") as tar:
            members = tar.getmembers()
            bad = unsafe_members(members)
            if bad:
                raise UpdateError(
                    "the release holds entries that would write outside the "
                    "directory, so nothing was extracted: %s"
                    % ", ".join(bad[:3]))
            top = top_directory(members)
            if top is None:
                raise UpdateError("the release must hold exactly one directory "
                                  "with Flightdeck inside it")
            tar.extractall(str(into))
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError("could not read the release: %s" % exc)

    root = Path(into) / top
    for needed in REQUIRED:
        if not (root / needed).exists():
            raise UpdateError("the release has no %s in it: this is not a "
                              "Flightdeck release" % needed)
    command = root / "bin" / "flightdeck"
    try:
        # Whatever mode the archive carried: a release packed with a strange
        # umask would otherwise leave the user with a command their shell
        # refuses to run, and no way to tell why.
        os.chmod(str(command), os.stat(str(command)).st_mode | 0o111)
    except OSError as exc:
        raise UpdateError("could not make %s executable: %s" % (command, exc))
    return root


def read_version(root):
    """The VERSION of an extracted tree ("unknown" when it cannot be read)."""
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except (OSError, ValueError):
        return "unknown"


def swap(data, staged):
    """Put `staged` in place as `<data>/current`, keeping the old as `previous`.

    Two renames inside one directory, so there is no moment where `current` is
    half a Flightdeck. If the second one fails -- a full disk, a permission --
    the old code is renamed back and the machine is left exactly as it was: the
    hooks and the link point at `current`, and leaving that name empty would
    break the cockpit rather than merely fail to update it.
    """
    data = Path(data)
    current = data / "current"
    previous = data / "previous"
    try:
        if previous.exists():
            shutil.rmtree(str(previous))
    except OSError as exc:
        raise UpdateError("could not clear %s: %s" % (previous, exc))

    moved = False
    try:
        if current.exists():
            os.rename(str(current), str(previous))
            moved = True
        os.rename(str(staged), str(current))
    except OSError as exc:
        if moved and not current.exists():
            try:
                os.rename(str(previous), str(current))
            except OSError:
                pass   # Best effort: the message below is what the user acts on.
        raise UpdateError("could not put the new code in place (%s); the "
                          "Flightdeck you had is untouched" % exc)


# ── the command ──────────────────────────────────────────────────────────────

def _parse(argv):
    """-> (the tag or None, an error message or None)."""
    tag = None
    words = list(argv)
    while words:
        word = words.pop(0)
        if word == "--version":
            if not words:
                return None, "--version needs a release, like --version v1.2.3"
            value = words.pop(0)
        elif word.startswith("--version="):
            value = word.split("=", 1)[1]
        else:
            return None, "unknown option %s" % word
        tag = normalise_tag(value)
        if tag is None:
            return None, "%r is not a version; they look like v1.2.3" % value
    return tag, None


def _shown(version):
    """A version as it is written for a person: `0.9.9` -> `v0.9.9`."""
    return "v" + version if version[:1].isdigit() else version


def _tmux_alive(run):
    """Is there a tmux server to restart the menus in?

    The keys, the bar and the menus live inside the server, not in a file: with
    no server there is nothing to restart and `restart` would only print that it
    made a menu nobody asked for.
    """
    try:
        done = run(common.tmux_bin() + ["has-session"], capture_output=True,
                   text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def main(argv=None, run=None):
    """`flightdeck update [--version vX.Y.Z]`. -> the exit code.

    0 when the new code is in and the reinstall was happy (and when there was
    nothing new to fetch), 1 when the release could not be fetched, read or put
    in place, or when the reinstall refused. 2 when the words do not parse.

    `run` is injected so the tests can watch what the new command is asked to do
    without anything being installed or restarted.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    run = run or subprocess.run
    tag, problem = _parse(argv)
    if problem:
        sys.stderr.write("flightdeck update: %s\n" % problem)
        sys.stderr.write(USAGE)
        return 2

    old_version = config.version()
    print("flightdeck %s — update" % old_version)
    if not config.installed_here():
        # R8, and the first thing, before anything is fetched.
        print("this Flightdeck runs from a source checkout (%s): update it with "
              "git pull" % config.code_dir())
        return 1

    data = config.data_dir()
    url = tarball_url(tag)
    print("  from: %s" % url)

    archive = None
    staging = None
    try:
        handle, archive = tempfile.mkstemp(prefix=".release-", suffix=".tar.gz",
                                           dir=str(data))
        os.close(handle)
        download(url, archive)
        # Both temporaries live in `<data>`, the filesystem `current` is on, so
        # the rename that finishes the job cannot turn into a copy across
        # devices -- and a copy is exactly what would leave a half-written
        # `current` behind if the disk filled up.
        staging = tempfile.mkdtemp(prefix=".unpack-", dir=str(data))
        root = extract(archive, staging)
        new_version = read_version(root)
        if new_version == old_version:
            print("already at %s — nothing to do" % _shown(old_version))
            return 0
        print("  new: %s (you have %s)" % (_shown(new_version), _shown(old_version)))
        swap(data, root)
    except (UpdateError, OSError) as exc:
        # `UpdateError` is everything this module decides is wrong with a
        # release; the bare `OSError` is for the two temporary files themselves
        # (a full or read-only `<data>`), which is a one-line refusal too and
        # never a traceback on top of a cockpit that still works.
        print("✗ %s" % exc)
        return 1
    finally:
        for leftover in (archive, staging):
            if not leftover:
                continue
            try:
                if os.path.isdir(leftover):
                    shutil.rmtree(leftover)
                elif os.path.exists(leftover):
                    os.unlink(leftover)
            except OSError:
                pass   # A leftover in the data directory is litter, not a failure.

    command = data / "current" / "bin" / "flightdeck"
    print("\nReinstalling with the new code")
    # The NEW command, as a subprocess: this interpreter is holding the modules
    # of the Flightdeck that has just been replaced.
    broken = run([str(command), "install", "--yes"]).returncode != 0

    if _tmux_alive(run):
        run([str(command), "restart"])
    else:
        print("no tmux server running — `flightdeck` opens the menu with the "
              "new code")

    print("\n✓ updated %s → %s" % (_shown(old_version), _shown(new_version)))
    print("  changelog: %s" % changelog_url(tag))
    if broken:
        print("  the reinstall reported a problem — the new code is in place, "
              "so run `flightdeck install` again once it is fixed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
