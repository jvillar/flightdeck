"""`install.sh`: the one-line installer, run for real against throwaway machines.

Every test builds a machine of its own under a temporary directory -- its own
HOME, its own XDG directories, its own PATH -- and runs `sh install.sh` inside
it. Nothing here reaches GitHub (`FLIGHTDECK_TARBALL_URL` and
`FLIGHTDECK_FZF_BASE_URL` point at `file://` archives the tests build), nothing
here reaches a tmux server (`tmux` is a stub that answers `-V` and fails
everything else, the way a tmux with no server answers `has-session`, and
`FLIGHTDECK_TMUX_SOCKET` names a socket nobody listens on), and nothing here can
reach the terminal the suite is being run from: every run is given a session of
its own (`start_new_session=True`), so an installer that tried to open
`/dev/tty` fails rather than taking over the developer's keyboard.

`PATH` is deliberately NOT the developer's: one stub directory plus `/usr/bin`
and `/bin`. That hides the real tmux, fzf, claude, codex and agy (which live in
`/opt/homebrew` and `~/.local/bin`) while leaving the system python3, curl, tar
and shasum, which is what makes the last step -- a REAL `flightdeck install
--yes` on a machine with no agent on it -- behave the same here and on a CI
runner.

That last step is real on purpose. `flightdeck install` ends by running the
doctor and takes its exit code, so "exits 0 on a machine with no agents
installed" is the assertion the orchestrator's own tests could not make (they
run on a machine where all three are on PATH): here the agents really are
absent.

The three DECLINED fzf answers are the only tests that need a terminal. R16 has
every question read from `/dev/tty`, which is the controlling terminal and not
stdin, so piping an answer in does nothing at all; `pty.fork` is what gives a
child a session whose controlling terminal is a pipe the test can write "n" to.
"""

import atexit
import hashlib
import io
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from flightdeck import config
from flightdeck.uninstall import is_our_line

REPO = config.code_dir()
INSTALL_SH = REPO / "install.sh"

# Every run happens from here and never from the repository root: a `cd` into
# the checkout would put this Flightdeck's modules on python's path and hide a
# release that did not carry them.
NEUTRAL_CWD = tempfile.mkdtemp(prefix="flightdeck-install-cwd-")
atexit.register(shutil.rmtree, NEUTRAL_CWD, True)

# The fzf release `install.sh` pins, read out of the script itself so that
# bumping it there does not silently leave these tests testing the old one.
FZF_VERSION = re.search(r"^FZF_VERSION=([0-9][0-9A-Za-z.]*)",
                        INSTALL_SH.read_text(encoding="utf-8"), re.M).group(1) \
    if INSTALL_SH.exists() else "0.0.0"

# What the whole repository weighs as a release, built once: every test that
# gets as far as step 4 unpacks the same one.
_RELEASE = {}


# ── the pieces a machine is made of ──────────────────────────────────────────

def _script(path, text):
    """An executable shell stub at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def tmux_stub(version):
    """A tmux that answers `-V` and refuses everything else.

    Refusing is not laziness: `tmux has-session` on a socket nobody listens on
    is exactly what the doctor and the installer ask, and rc 1 is what a real
    tmux answers there. Nothing in this file may start a server.

    `-V` is looked for ANYWHERE in the arguments, because that is where it
    turns up: everything on the Python side builds its command line as
    `tmux -L <socket> …`, and `tmux -V` asks the binary and never reaches a
    server, so a real tmux answers it whatever comes before. A stub that only
    read `$1` made the doctor report a tmux whose version it could not read --
    which passed the test and tested nothing.
    """
    return ("#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "-V" ]; then echo "tmux %s"; exit 0; fi\n'
            "done\n"
            "exit 1\n" % version)


def fzf_stub(version):
    return ("#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "%s"; exit 0; fi\n'
            "exit 1\n" % version)


def uname_stub(system, machine="arm64"):
    """A `uname` with an opinion, for the tests about other machines.

    `-s` and `-m` are the two the installer asks; anything else answers the
    system, which is what bare `uname` does.
    """
    return ("#!/bin/sh\n"
            'case "${1:-}" in\n'
            '  -s) echo "%s" ;;\n'
            '  -m) echo "%s" ;;\n'
            '  *)  echo "%s" ;;\n'
            "esac\n" % (system, machine, system))


def release_tarball():
    """This working tree packed the way a release is (R6). -> its `file://` URL.

    One top-level directory `flightdeck/` holding the tree. Built once and kept:
    it is the same archive for every test, and packing the package twenty times
    is twenty times the seconds.
    """
    if "url" in _RELEASE:
        return _RELEASE["url"]
    tmp = tempfile.mkdtemp(prefix="flightdeck-release-")
    atexit.register(shutil.rmtree, tmp, True)
    tree = Path(tmp) / "build" / "flightdeck"
    tree.mkdir(parents=True)
    for name in ("bin", "flightdeck"):
        shutil.copytree(str(REPO / name), str(tree / name))
    for name in ("VERSION", "LICENSE", "README.md", "install.sh"):
        source = REPO / name
        if source.exists():
            shutil.copy2(str(source), str(tree / name))
    _RELEASE["url"] = _pack(Path(tmp) / "flightdeck.tar.gz", tree, "flightdeck")
    return _RELEASE["url"]


def _pack(archive, tree, arcname):
    """`tree` into `archive` under `arcname`. -> the `file://` URL of the archive."""
    with tarfile.open(str(archive), "w:gz") as tar:
        tar.add(str(tree), arcname=arcname)
    return "file://" + str(archive)


def _entry(name, data=b"", mode=0o644):
    """One ordinary file member, built by hand."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info


def release_with(tmp, extra, filename="odd.tar.gz"):
    """A well-formed release plus ONE member of the caller's making.

    -> its `file://` URL. Everything else in the archive is in order (one top
    directory holding `bin/flightdeck` and `VERSION`), so a refusal can only be
    about `extra` -- which is what makes these tests about the guard and not
    about the shape of the tree.
    """
    archive = Path(tmp) / filename
    command = b"#!/bin/sh\nexit 0\n"
    version = b"9.9.9\n"
    with tarfile.open(str(archive), "w:gz") as tar:
        top = tarfile.TarInfo("flightdeck")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        tar.addfile(_entry("flightdeck/VERSION", version), io.BytesIO(version))
        tar.addfile(_entry("flightdeck/bin/flightdeck", command, 0o755),
                    io.BytesIO(command))
        tar.addfile(extra)
    return "file://" + str(archive)


def a_symlink(name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


class Machine(object):
    """One throwaway machine: a home, a PATH of stubs, and the environment.

    Everything a run of `install.sh` can see and write is under `tmp`. The
    XDG variables travel with HOME because Flightdeck's paths fall back to them,
    and a machine that exports `XDG_DATA_HOME` -- normal on Linux and in CI --
    would otherwise have a test writing into the developer's real one.
    """

    def __init__(self, tmp, tmux="3.6a", fzf="0.72.0", uname=None,
                 shell="/bin/zsh", lang="en_US.UTF-8", tarball=None,
                 fzf_base=None, path_tools=None, extras=()):
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data = self.home / ".local" / "share" / "flightdeck"
        self.bindir = self.home / ".local" / "bin"
        self.stub = self.tmp / "stub"
        self.stub.mkdir(parents=True, exist_ok=True)

        if tmux is not None:
            _script(self.stub / "tmux", tmux_stub(tmux))
        if fzf is not None:
            _script(self.stub / "fzf", fzf_stub(fzf))
        if uname is not None:
            _script(self.stub / "uname", uname_stub(*uname))
        for name in extras:
            _script(self.stub / name, "#!/bin/sh\nexit 0\n")

        if path_tools is None:
            path = "%s:/usr/bin:/bin" % self.stub
        else:
            # A PATH with only the named system tools on it, for the tests that
            # need a command to be genuinely MISSING (python3 lives in
            # /usr/bin, so it cannot be hidden while /usr/bin is on PATH).
            #
            # Which command that is depends on the machine, and that is the
            # point: `tmux` and `apt-get` are in /usr/bin on an Ubuntu and in
            # /opt/homebrew/bin on the developer's Mac, so a test that wants
            # either of them absent has to say so here rather than trust the
            # default PATH above to be missing them.
            lean = self.tmp / "tools"
            lean.mkdir(parents=True, exist_ok=True)
            for name in path_tools:
                real = shutil.which(name, path="/usr/bin:/bin")
                if real:
                    os.symlink(real, str(lean / name))
            path = "%s:%s" % (self.stub, lean)

        self.env = {
            "HOME": str(self.home),
            "PATH": path,
            "SHELL": shell,
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            # Set rather than inherited: the suite's defaults point at its own
            # sandbox, and a test must see what the machine it built ends up
            # holding.
            "FLIGHTDECK_STATE_DIR": str(self.home / ".local" / "state" / "flightdeck"),
            "FLIGHTDECK_CONFIG": str(self.home / ".config" / "flightdeck" / "config.json"),
            "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket",
            "FLIGHTDECK_TARBALL_URL": tarball if tarball is not None else release_tarball(),
            "LANG": lang,
            # Without a terminal python3 buffers its output; the tests read it
            # after the fact, so it only has to arrive.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if fzf_base is not None:
            self.env["FLIGHTDECK_FZF_BASE_URL"] = fzf_base

    def rc(self, name=".zshrc", text=""):
        """A shell rc file on this machine. -> its path."""
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def backups(self, name=".zshrc"):
        """The timestamped backups of one rc file, in the order they were made."""
        return sorted(self.home.glob(name + ".bak-flightdeck-*"))


def run(machine, args=(), timeout=240, env_extra=None):
    """`sh install.sh <args>` on that machine. -> the CompletedProcess.

    `start_new_session` puts the run in a session of its own, with no
    controlling terminal: `/dev/tty` cannot be opened, which is both what a
    `curl … | sh` in a CI job looks like and the guarantee that no test can ever
    ask the developer a question.
    """
    env = dict(machine.env)
    env.update(env_extra or {})
    return subprocess.run(["/bin/sh", str(INSTALL_SH)] + list(args),
                          capture_output=True, text=True, env=env,
                          cwd=NEUTRAL_CWD, timeout=timeout,
                          start_new_session=True)


def output(result):
    """Everything the run said, in one string."""
    return (result.stdout or "") + (result.stderr or "")


def run_on_a_tty(machine, args=(), answers="n\n" + "\n" * 8, timeout=240):
    """The same, with a pty as the run's CONTROLLING terminal. -> (rc, output)

    The only way to reach an answer of "no": R16 has every question read from
    `/dev/tty`, and that is the controlling terminal, not stdin. `pty.fork`
    gives the child a new session with the pty as its terminal, so `/dev/tty`
    opens and reads what the parent writes -- and the pty dies with the test,
    so the developer's own terminal is never in the picture.

    The answers are "no" once and then Enter: the "no" is for the installer's
    own question (the one being tested), and everything after it belongs to
    `flightdeck install`, which is handed the same terminal and asks its own.
    Enter is what its `[Y/n]` prompts take as yes, so a run that gets that far
    finishes instead of hanging on a read nobody answers.
    """
    env = dict(machine.env)
    argv = ["/bin/sh", str(INSTALL_SH)] + list(args)
    pid, master = pty.fork()
    if pid == 0:                                  # the child: it never returns
        try:
            os.chdir(NEUTRAL_CWD)
            os.execve(argv[0], argv, env)
        except BaseException:
            pass
        os._exit(127)

    os.write(master, answers.encode())
    chunks = []
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:           # it hung: kill it and let the
            os.kill(pid, signal.SIGKILL)     # assertion say what it printed
            break
        if not select.select([master], [], [], 0.5)[0]:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            break        # EIO: the child is gone and the pty went with it
        if not data:
            break
        chunks.append(data)
    os.close(master)
    status = os.waitpid(pid, 0)[1]
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    return code, b"".join(chunks).decode("utf-8", "replace")


# ── an fzf release of our own making ─────────────────────────────────────────

def fzf_release(tmp, system="darwin", arch="arm64", corrupt=False):
    """A directory holding a fake fzf archive and its checksums file.

    -> its `file://` URL, for `FLIGHTDECK_FZF_BASE_URL`. The archive holds one
    file called `fzf` that answers `--version`, which is what the real one
    holds; `corrupt` writes a checksums file that does not match it.
    """
    served = Path(tmp) / "fzf-release"
    served.mkdir(parents=True, exist_ok=True)
    build = Path(tmp) / "fzf-build"
    build.mkdir(parents=True, exist_ok=True)
    _script(build / "fzf", fzf_stub(FZF_VERSION))

    asset = "fzf-%s-%s_%s.tar.gz" % (FZF_VERSION, system, arch)
    with tarfile.open(str(served / asset), "w:gz") as tar:
        tar.add(str(build / "fzf"), arcname="fzf")
    digest = hashlib.sha256((served / asset).read_bytes()).hexdigest()
    if corrupt:
        digest = "0" * 64
    (served / ("fzf_%s_checksums.txt" % FZF_VERSION)).write_text(
        "%s  %s\n" % (digest, asset), encoding="utf-8")
    return "file://" + str(served)


# ── 0. the script itself ─────────────────────────────────────────────────────

class TestTheScriptItself(unittest.TestCase):

    def test_it_parses(self):
        # `sh -n` reads the whole file without running a line of it: the
        # cheapest guard against a quoting mistake in a branch no test reaches.
        r = subprocess.run(["/bin/sh", "-n", str(INSTALL_SH)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_nothing_runs_but_main_and_it_is_last(self):
        # R21: `curl … | sh` executes what has arrived so far. With the steps in
        # functions and `main "$@"` last, a half-downloaded script defines some
        # functions and does nothing; with a step at the top level it would run
        # that step and stop in the middle of an installation.
        lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
        executable = []
        for line in lines:
            if not line or line[0] in " \t#}":
                continue          # indented (inside a function), blank, comment
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\(\)\s*\{|=)", line):
                continue          # a function's opening line, or a constant
            if line.startswith("set -"):
                continue          # the shell's own options
            executable.append(line)
        self.assertEqual(executable, ['main "$@"'])
        self.assertEqual([line for line in lines if line.strip()][-1],
                         'main "$@"')

    def test_an_unknown_argument_is_refused_with_the_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--nope"])
        self.assertEqual(r.returncode, 2, output(r))
        self.assertIn("usage:", output(r))

    def test_version_without_a_value_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--version"])
        self.assertEqual(r.returncode, 2, output(r))

    def test_a_version_that_is_not_one_is_refused(self):
        # It ends up in a URL path: something with a slash in it would be a
        # request for somebody else's release, so it is refused and not escaped.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--version", "../../etc"])
        self.assertEqual(r.returncode, 2, output(r))


# ── 1. the system ────────────────────────────────────────────────────────────

class TestSystem(unittest.TestCase):

    def test_native_windows_is_sent_to_wsl2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp, uname=("MINGW64_NT-10.0", "x86_64")))
        self.assertEqual(r.returncode, 1, output(r))
        self.assertIn("WSL2", output(r))
        self.assertIn("wsl --install", output(r))

    def test_the_windows_variable_is_enough(self):
        # A python or a shell under Git Bash can report a Unix-looking uname;
        # `OS=Windows_NT` is Windows saying so itself.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), env_extra={"OS": "Windows_NT"})
        self.assertEqual(r.returncode, 1, output(r))
        self.assertIn("WSL2", output(r))

    def test_nothing_is_installed_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, uname=("MSYS_NT-10.0", "x86_64"))
            run(machine)
            self.assertFalse(machine.data.exists())
            self.assertFalse(machine.bindir.exists())


# ── 2. the dependencies ──────────────────────────────────────────────────────

class TestDependencies(unittest.TestCase):
    """What happens when something Flightdeck needs is not on the machine.

    The four tests about a MISSING tmux and about the package-manager hint all
    pass `path_tools`, and that is not decoration. `install.sh` asks `have tmux`
    and `have apt-get` of the whole PATH, and the default PATH a `Machine` builds
    carries `/usr/bin` -- where an Ubuntu keeps a real `tmux` and a real
    `apt-get`. Without a PATH of their own, `tmux=None` did not make tmux missing
    on a Linux runner, and `pkg_hint` answered apt whatever the `uname` stub
    said. They passed on the developer's Mac only because Homebrew installs into
    `/opt/homebrew/bin`, which is off that PATH -- an accident of where brew
    lives, not a property of the tests.

    `("sed", "grep", "uname")` is what the script needs to reach the tmux check:
    `uname` to work out the system, `grep` for the WSL2 probe on Linux, `sed` for
    reading versions.
    """

    def test_no_tmux_is_the_end_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The machine is SAID to be a Mac rather than inherited from
            # whichever one is running the suite, because the hint asserted
            # below is brew's. The three tests after this one are where the
            # other systems' hints are checked.
            machine = Machine(tmp, tmux=None, uname=("Darwin", "arm64"),
                              path_tools=("sed", "grep", "uname"))
            r = run(machine)
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("tmux", output(r))
            self.assertIn("brew install tmux fzf", output(r))
            self.assertFalse(machine.data.exists())

    def test_the_hint_is_apt_on_a_debian(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp, tmux=None, uname=("Linux", "x86_64"),
                            path_tools=("sed", "grep", "uname"),
                            extras=("apt-get",)))
        self.assertIn("sudo apt install tmux fzf", output(r))

    def test_the_hint_is_dnf_on_a_fedora(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The `dnf` stub is the only package manager on this PATH. On a
            # machine with a real `apt-get` in /usr/bin it would never be
            # reached: `pkg_hint` asks apt first.
            r = run(Machine(tmp, tmux=None, uname=("Linux", "x86_64"),
                            path_tools=("sed", "grep", "uname"),
                            extras=("dnf",)))
        self.assertIn("sudo dnf install tmux fzf", output(r))

    def test_a_linux_with_neither_gets_a_general_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = output(run(Machine(tmp, tmux=None, uname=("Linux", "x86_64"),
                                     path_tools=("sed", "grep", "uname"))))
        self.assertIn("package manager", out)
        self.assertNotIn("brew install", out)

    def test_tmux_31_is_below_the_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp, tmux="3.1"))
        self.assertEqual(r.returncode, 1, output(r))
        self.assertIn("3.2", output(r))

    def test_tmux_32a_goes_all_the_way_through(self):
        # The letter is part of tmux's own version numbering, and 3.2 is the
        # floor: an installer that read "3.2a" as unparseable would refuse the
        # tmux Ubuntu 22.04 ships.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, tmux="3.2a")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.data / "current" / "VERSION").exists())
            # And the doctor at the end reads it the same way: a warning about
            # the notices being escaped rather than literal, never a ✗.
            self.assertIn("! tmux", output(r))
            self.assertNotIn("✗ tmux", output(r))

    def test_a_tmux_that_does_not_say_its_version_is_a_warning(self):
        # The doctor calls the same thing a warning and carries on; refusing to
        # install on a tmux built from master would be the installer being
        # stricter than the check that follows it.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp, tmux="master"), ["--yes"])
        self.assertEqual(r.returncode, 0, output(r))
        self.assertIn("tmux", output(r))

    def test_no_python3_is_the_end_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, path_tools=("sed", "grep", "uname"))
            r = run(machine)
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("python3", output(r))
            self.assertFalse(machine.data.exists())

    def test_no_claude_is_only_a_warning(self):
        # Flightdeck is a cockpit; a cockpit with nothing flying in it yet still
        # installs. (No agent is on this PATH, so every full run proves it.)
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--yes"])
        self.assertEqual(r.returncode, 0, output(r))
        self.assertIn("claude", output(r).lower())


# ── 3. the locale ────────────────────────────────────────────────────────────

class TestLocale(unittest.TestCase):

    def test_a_locale_that_is_not_utf8_warns_and_carries_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp, lang="C"), ["--yes"])
        self.assertEqual(r.returncode, 0, output(r))
        self.assertIn("UTF-8", output(r))

    def test_utf8_says_nothing_about_glyphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = output(run(Machine(tmp, lang="en_GB.utf8"), ["--yes"]))
        self.assertNotIn("will not print", out)


# ── 4. the code ──────────────────────────────────────────────────────────────

class TestTheCode(unittest.TestCase):

    def test_the_command_lands_executable_under_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            r = run(machine, ["--yes"])
            command = machine.data / "current" / "bin" / "flightdeck"
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue(command.exists())
            self.assertTrue(os.access(str(command), os.X_OK))
            # A real directory and not a link: the agents' hooks name paths
            # inside it and `update` renames it (R6).
            self.assertFalse((machine.data / "current").is_symlink())

    def test_a_second_install_keeps_the_old_code_as_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            self.assertEqual(run(machine, ["--yes"]).returncode, 0)
            marker = machine.data / "current" / "MARKER"
            marker.write_text("first", encoding="utf-8")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.data / "previous" / "MARKER").exists())
            self.assertFalse((machine.data / "current" / "MARKER").exists())
            self.assertTrue((machine.data / "current" / "bin" / "flightdeck").exists())

    def test_a_third_install_does_not_pile_up_previouses(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            run(machine, ["--yes"])
            run(machine, ["--yes"])
            (machine.data / "previous" / "MARKER").write_text("second")
            run(machine, ["--yes"])
            self.assertFalse((machine.data / "previous" / "MARKER").exists())

    def test_a_release_that_cannot_be_fetched_stops_the_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, tarball="file://" + tmp + "/nothing.tar.gz")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_a_release_with_two_top_directories_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "build"
            (tree / "one" / "bin").mkdir(parents=True)
            (tree / "two").mkdir(parents=True)
            url = _pack(Path(tmp) / "two.tar.gz", tree, ".")
            machine = Machine(tmp, tarball=url)
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_a_release_that_climbs_out_of_its_directory_is_refused(self):
        # Nothing is extracted at all: the names are read first, because there
        # is no undoing half an extraction.
        #
        # The MESSAGE is asserted and not merely the exit code, and that is the
        # whole test: the bsdtar on macOS refuses a `..` member on its own, so a
        # run with no guard at all also ends non-zero -- for a different reason,
        # on a different machine's tar, and with no promise attached. Only this
        # line says the names were read before anything was written.
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "evil.tar.gz"
            payload = Path(tmp) / "payload"
            payload.write_text("pwned", encoding="utf-8")
            with tarfile.open(str(archive), "w:gz") as tar:
                tar.add(str(payload), arcname="flightdeck/../../escaped")
            machine = Machine(tmp, tarball="file://" + str(archive))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("outside the directory", output(r))
            self.assertFalse((machine.data / "current").exists())
            self.assertFalse((machine.home / ".local" / "escaped").exists())

    def test_a_link_pointing_at_an_absolute_path_is_refused(self):
        # The attack the name check alone does not see: neither `flightdeck/x`
        # nor the member after it is absolute or carries a `..`, but once `x` is
        # a link to somewhere else, writing `x/authorized_keys` writes there.
        with tempfile.TemporaryDirectory() as tmp:
            stolen = Path(tmp) / "stolen"
            url = release_with(tmp, a_symlink("flightdeck/x", str(stolen)))
            machine = Machine(tmp, tarball=url)
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("link pointing outside the directory", output(r))
            self.assertFalse((machine.data / "current").exists())
            self.assertFalse(stolen.exists())

    def test_a_link_that_climbs_out_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = release_with(tmp, a_symlink("flightdeck/x", "../../elsewhere"))
            machine = Machine(tmp, tarball=url)
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("link pointing outside the directory", output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_a_member_that_is_not_a_file_a_directory_or_a_link_is_refused(self):
        # A fifo in a directory of source code is not a thing that happens by
        # accident. Refused with the hard links, rather than reasoning about
        # what it would do.
        with tempfile.TemporaryDirectory() as tmp:
            fifo = tarfile.TarInfo("flightdeck/pipe")
            fifo.type = tarfile.FIFOTYPE
            machine = Machine(tmp, tarball=release_with(tmp, fifo))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("not files, directories or symlinks", output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_a_hard_link_is_refused_too(self):
        # `update.unsafe_members` checks a hard link's target instead; this is
        # the one place the shell is deliberately stricter than the python, and
        # a release of ours holds no link of either kind.
        with tempfile.TemporaryDirectory() as tmp:
            hard = tarfile.TarInfo("flightdeck/hard")
            hard.type = tarfile.LNKTYPE
            hard.linkname = "flightdeck/VERSION"
            machine = Machine(tmp, tarball=release_with(tmp, hard))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("not files, directories or symlinks", output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_an_ordinary_release_is_not_caught_by_any_of_that(self):
        # The other side of the guard: the tarball every other test installs
        # from holds nothing but files and directories, and goes through. Said
        # once, on purpose -- a guard nobody has watched let something PASS is
        # a guard that could be refusing everything.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, tarball=release_with(tmp, _entry(
                "flightdeck/README.md", b"hello\n")))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.data / "current" / "README.md").exists())

    def test_a_release_without_the_command_is_not_a_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "build" / "flightdeck"
            tree.mkdir(parents=True)
            (tree / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            url = _pack(Path(tmp) / "empty.tar.gz", tree, "flightdeck")
            machine = Machine(tmp, tarball=url)
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertFalse((machine.data / "current").exists())

    def test_the_release_url_is_the_one_github_publishes(self):
        # The one thing `FLIGHTDECK_TARBALL_URL` hides from every other test:
        # the URL actually built when nobody overrides it. A `curl` that refuses
        # everything is what makes this askable without a test reaching GitHub —
        # the script prints the URL before fetching it, and then stops.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            del machine.env["FLIGHTDECK_TARBALL_URL"]
            _script(machine.stub / "curl", "#!/bin/sh\nexit 7\n")

            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("https://github.com/jvillar/flightdeck/releases/"
                          "latest/download/flightdeck.tar.gz", output(r))

            r = run(machine, ["--yes", "--version", "1.2.3"])
            self.assertIn("https://github.com/jvillar/flightdeck/releases/"
                          "download/v1.2.3/flightdeck.tar.gz", output(r))

    def test_a_version_is_accepted_with_or_without_its_v(self):
        # `FLIGHTDECK_TARBALL_URL` names a file and so wins over `--version`,
        # exactly as it does in `flightdeck update`: what is tested here is that
        # both spellings parse and that neither is passed on to the orchestrator
        # (which would refuse it as an unknown option).
        for spelling in ("v1.2.3", "1.2.3"):
            with tempfile.TemporaryDirectory() as tmp:
                r = run(Machine(tmp), ["--yes", "--version", spelling])
            self.assertEqual(r.returncode, 0, output(r))


class TestTheGuardsPatternsReadBothTars(unittest.TestCase):
    """The two patterns in `install.sh`, against both tar listing formats.

    The end-to-end tests above run the real script, so they measure the guard
    against the bsdtar macOS ships and nothing else. CI runs on Linux, where the
    listing comes from GNU tar: the columns in the middle are different, and a
    guard that quietly matched nothing there would refuse nothing and never say
    so. **GNU tar is not installed on the machine this was written on**, so its
    listing is a fixture written from its documented format, and the bsdtar one
    is generated here for real and asserted to have the same shape.

    The patterns are READ OUT of the script rather than copied, so a change to
    either of them is a change to what these tests check.
    """

    # GNU tar's `-tvzf`: `<mode> <user>/<group> <size> <date> <time> <name>`,
    # with ` -> ` for a symlink and ` link to ` for a hard link, exactly as
    # bsdtar spells those two.
    GNU = ("drwxr-xr-x user/group        0 2026-09-17 00:00 flightdeck/\n"
           "-rw-r--r-- user/group        6 2026-09-17 00:00 flightdeck/VERSION\n"
           "lrwxrwxrwx user/group        0 2026-09-17 00:00 flightdeck/safe -> VERSION\n"
           "lrwxrwxrwx user/group        0 2026-09-17 00:00 flightdeck/abs -> /home/you/.ssh\n"
           "lrwxrwxrwx user/group        0 2026-09-17 00:00 flightdeck/up -> ../../elsewhere\n"
           "hrw-r--r-- user/group        0 2026-09-17 00:00 flightdeck/hard link to flightdeck/VERSION\n"
           "prw-r--r-- user/group        0 2026-09-17 00:00 flightdeck/pipe\n")

    def pattern(self, name):
        """One `RE_…` constant, as `install.sh` spells it."""
        found = re.search(r"^%s='([^']*)'" % name,
                          INSTALL_SH.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(found, "%s is not in install.sh any more" % name)
        return found.group(1)

    def grep(self, pattern, text):
        """The lines of `text` that `grep -E <pattern>` picks out."""
        done = subprocess.run(["grep", "-E", pattern], input=text,
                              capture_output=True, text=True)
        return done.stdout.splitlines()

    def targets(self, text):
        """The symlink targets, the way the script takes them out."""
        done = subprocess.run(["sed", "-n", "s/.* -> //p"], input=text,
                              capture_output=True, text=True)
        return done.stdout.splitlines()

    def bsdtar_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "odd.tar.gz"
            body = b"9.9.9\n"
            with tarfile.open(str(archive), "w:gz") as tar:
                top = tarfile.TarInfo("flightdeck")
                top.type = tarfile.DIRTYPE
                tar.addfile(top)
                tar.addfile(_entry("flightdeck/VERSION", body), io.BytesIO(body))
                tar.addfile(a_symlink("flightdeck/safe", "VERSION"))
                tar.addfile(a_symlink("flightdeck/abs", "/home/you/.ssh"))
                tar.addfile(a_symlink("flightdeck/up", "../../elsewhere"))
                hard = tarfile.TarInfo("flightdeck/hard")
                hard.type = tarfile.LNKTYPE
                hard.linkname = "flightdeck/VERSION"
                tar.addfile(hard)
                pipe = tarfile.TarInfo("flightdeck/pipe")
                pipe.type = tarfile.FIFOTYPE
                tar.addfile(pipe)
            done = subprocess.run(["tar", "-tvzf", str(archive)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

    def test_the_type_pattern_catches_the_same_two_in_both(self):
        pattern = self.pattern("RE_BAD_TYPE")
        for label, listing in (("GNU", self.GNU), ("bsdtar", self.bsdtar_listing())):
            caught = self.grep(pattern, listing)
            self.assertEqual(len(caught), 2, "%s: %s" % (label, caught))
            self.assertTrue(any("hard" in line for line in caught), label)
            self.assertTrue(any("pipe" in line for line in caught), label)
            # And it lets the file, the directory and the safe link through.
            self.assertFalse(any("VERSION" in line and "hard" not in line
                                 for line in caught), label)

    def test_the_name_pattern_catches_the_same_two_targets_in_both(self):
        pattern = self.pattern("RE_BAD_NAME")
        for label, listing in (("GNU", self.GNU), ("bsdtar", self.bsdtar_listing())):
            found = self.targets(listing)
            self.assertIn("VERSION", found, label)          # the safe one is there
            caught = [t for t in found if self.grep(pattern, t + "\n")]
            self.assertEqual(sorted(caught),
                             ["../../elsewhere", "/home/you/.ssh"], label)

    def test_the_name_pattern_reads_a_plain_name_list(self):
        pattern = self.pattern("RE_BAD_NAME")
        names = ("flightdeck/bin/flightdeck\n"
                 "flightdeck/VERSION\n"
                 "flightdeck/a..b\n"          # not a climb: two dots in a name
                 "/etc/passwd\n"
                 "flightdeck/../../escaped\n"
                 "..\n")
        self.assertEqual(sorted(self.grep(pattern, names)),
                         ["..", "/etc/passwd", "flightdeck/../../escaped"])


# ── 5. the command and the PATH line ─────────────────────────────────────────

class TestTheCommand(unittest.TestCase):

    def test_both_names_are_linked_at_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            run(machine, ["--yes"])
            target = machine.data / "current" / "bin" / "flightdeck"
            for name in ("flightdeck", "fld"):
                link = machine.bindir / name
                self.assertTrue(link.is_symlink(), name)
                self.assertEqual(Path(os.path.realpath(str(link))),
                                 Path(os.path.realpath(str(target))))

    def test_somebody_elses_fld_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            _script(machine.stub / "fld", "#!/bin/sh\necho not ours\n")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.bindir / "flightdeck").is_symlink())
            self.assertFalse((machine.bindir / "fld").exists())
            self.assertIn("fld", output(r))

    def test_an_fld_of_theirs_in_local_bin_is_not_overwritten(self):
        # The sharp case: their own `fld` sitting in `~/.local/bin` while that
        # directory is NOT yet on their PATH -- which is the normal state of
        # affairs during a first install, since the line putting it there is
        # written two steps later. `command -v` cannot see it, so only looking
        # at the file keeps `ln -sf` from deleting it without a word.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            machine.bindir.mkdir(parents=True, exist_ok=True)
            theirs = machine.bindir / "fld"
            theirs.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertEqual(theirs.read_text(encoding="utf-8"),
                             "#!/bin/sh\necho theirs\n")
            self.assertFalse(theirs.is_symlink())
            self.assertTrue((machine.bindir / "flightdeck").is_symlink())

    def test_our_own_fld_is_relinked_without_a_word_of_complaint(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            run(machine, ["--yes"])
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.bindir / "fld").is_symlink())


class TestThePathLine(unittest.TestCase):

    LINE = 'export PATH="$HOME/.local/bin:$PATH"  # flightdeck'

    def test_the_line_goes_into_zshrc_with_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            rc = machine.rc(".zshrc", "# mine\nalias ll='ls -l'\n")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            text = rc.read_text(encoding="utf-8")
            self.assertEqual(text.count(self.LINE), 1, text)
            self.assertIn("alias ll='ls -l'", text)
            self.assertEqual(len(machine.backups()), 1)
            self.assertIn("# mine", machine.backups()[0].read_text(encoding="utf-8"))

    def test_the_line_ends_with_the_mark_uninstall_looks_for(self):
        # `flightdeck uninstall` takes out the line that ENDS in `# flightdeck`
        # and nothing else. If the two ever disagree, the line stays behind for
        # ever and nobody can see why.
        self.assertTrue(is_our_line(self.LINE))
        self.assertTrue(is_our_line(
            'set -gx PATH "$HOME/.local/bin" $PATH  # flightdeck'))

    def test_a_second_run_writes_nothing_and_backs_up_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            rc = machine.rc(".zshrc", "# mine\n")
            run(machine, ["--yes"])
            first = rc.read_text(encoding="utf-8")
            run(machine, ["--yes"])
            self.assertEqual(rc.read_text(encoding="utf-8"), first)
            self.assertEqual(rc.read_text(encoding="utf-8").count(self.LINE), 1)
            self.assertEqual(len(machine.backups()), 1)

    def test_an_rc_without_a_final_newline_does_not_get_a_joined_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            rc = machine.rc(".zshrc", "export EDITOR=vi")
            run(machine, ["--yes"])
            lines = rc.read_text(encoding="utf-8").splitlines()
            self.assertIn("export EDITOR=vi", lines)
            self.assertIn(self.LINE, lines)

    def test_a_bin_already_on_path_needs_no_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            rc = machine.rc(".zshrc", "# mine\n")
            machine.bindir.mkdir(parents=True, exist_ok=True)
            r = run(machine, ["--yes"],
                    env_extra={"PATH": "%s:%s" % (machine.bindir,
                                                  machine.env["PATH"])})
            self.assertEqual(r.returncode, 0, output(r))
            self.assertNotIn(self.LINE, rc.read_text(encoding="utf-8"))
            self.assertEqual(machine.backups(), [])

    def test_bash_on_a_mac_writes_bash_profile(self):
        # A login shell on macOS reads `.bash_profile` and not `.bashrc`, which
        # is the whole difference between a PATH that takes and one that does
        # not.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, shell="/bin/bash", uname=("Darwin", "arm64"))
            profile = machine.rc(".bash_profile", "")
            rc = machine.rc(".bashrc", "")
            run(machine, ["--yes"])
            self.assertIn(self.LINE, profile.read_text(encoding="utf-8"))
            self.assertNotIn(self.LINE, rc.read_text(encoding="utf-8"))

    def test_bash_on_a_linux_writes_bashrc(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, shell="/usr/bin/bash", uname=("Linux", "x86_64"))
            rc = machine.rc(".bashrc", "")
            run(machine, ["--yes"])
            self.assertIn(self.LINE, rc.read_text(encoding="utf-8"))

    def test_fish_gets_the_line_fish_understands(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, shell="/opt/homebrew/bin/fish")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            rc = machine.home / ".config" / "fish" / "config.fish"
            self.assertIn('set -gx PATH "$HOME/.local/bin" $PATH  # flightdeck',
                          rc.read_text(encoding="utf-8"))

    def test_an_unknown_shell_is_told_what_to_add_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, shell="/bin/ksh")
            rc = machine.rc(".zshrc", "# mine\n")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertIn("$HOME/.local/bin", output(r))
            self.assertNotIn(self.LINE, rc.read_text(encoding="utf-8"))

    def test_the_run_says_a_new_shell_is_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp)
            machine.rc(".zshrc", "")
            out = output(run(machine, ["--yes"]))
        self.assertIn("new shell", out)


# ── fzf ──────────────────────────────────────────────────────────────────────

class TestFzf(unittest.TestCase):

    def test_a_recent_fzf_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.72.0 (Homebrew)")
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertFalse((machine.data / "bin" / "fzf").exists())

    def test_a_missing_fzf_is_downloaded_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf=None, uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            r = run(machine, ["--yes"])
            downloaded = machine.data / "bin" / "fzf"
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue(downloaded.exists(), output(r))
            self.assertTrue(os.access(str(downloaded), os.X_OK))
            # And it is the one in use. The doctor runs through `bin/flightdeck`,
            # which puts `<data>/bin` at the front of its PATH -- without that
            # half of R19 the download would sit there unused and the doctor
            # would still be saying fzf is not installed.
            self.assertIn("✓ fzf: %s" % downloaded, output(r))

    def test_an_fzf_below_the_recommended_one_is_offered_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.50.0", uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 0, output(r))
            self.assertTrue((machine.data / "bin" / "fzf").exists())

    def test_a_checksum_that_does_not_match_stops_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf=None, uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp, corrupt=True))
            r = run(machine, ["--yes"])
            self.assertEqual(r.returncode, 1, output(r))
            self.assertIn("checksum", output(r).lower())
            self.assertFalse((machine.data / "bin" / "fzf").exists())

    def test_an_architecture_with_no_build_is_said_out_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.50.0", uname=("Linux", "sparc64"),
                              fzf_base=fzf_release(tmp))
            r = run(machine, ["--yes"])
            out = output(r)
            self.assertIn("sparc64", out)
            self.assertFalse((machine.data / "bin" / "fzf").exists())
            # fzf 0.50 still works, so the install carries on without it.
            self.assertEqual(r.returncode, 0, out)

    def test_with_no_terminal_the_offer_takes_its_default_and_fzf_is_downloaded(self):
        # A headless `curl … | sh` with no `--yes` has nobody to ask, and the
        # prompt's own default is yes: the binary is fzf's official release,
        # checksummed, in a directory only the `flightdeck` command sees and
        # `uninstall` removes, so a scripted install that simply works was
        # judged worth more than a refusal nobody was there to read.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf=None, uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            r = run(machine)
            out = output(r)
            self.assertEqual(r.returncode, 0, out)
            self.assertIn("no terminal to ask on", out)
            self.assertTrue((machine.data / "bin" / "fzf").exists())
            self.assertTrue((machine.data / "current" / "VERSION").exists())

    def test_with_no_terminal_an_old_fzf_is_replaced_the_same_way(self):
        # The same unasked offer over an fzf that works but is below the
        # recommended 0.71: the default is taken here too, so the menu gets the
        # stable cursor instead of the warning a declined offer would leave.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.50.0", uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            r = run(machine)
            out = output(r)
            self.assertEqual(r.returncode, 0, out)
            self.assertTrue((machine.data / "bin" / "fzf").exists())
            # The declined-offer warning is what must NOT be there; the doctor's
            # own "stable cursor" line for the fzf just installed is fine.
            self.assertNotIn("cursor will not stay", out)

    def test_declining_with_no_fzf_at_all_is_the_end_of_it(self):
        # A lean PATH, for the same reason the tmux-less tests have one: on an
        # Ubuntu runner apt's fzf sits in /usr/bin, and with the default PATH
        # "no fzf" quietly became "the runner's fzf" -- 0.44 on 24.04, above the
        # floor, so the run carried on and exited 0 instead of ending here.
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf=None, uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp),
                              path_tools=("sed", "grep", "awk", "tr", "wc", "cat",
                                          "tail", "head", "uname", "python3", "curl",
                                          "tar", "shasum", "sha256sum", "mktemp",
                                          "dirname", "readlink", "date"))
            code, out = run_on_a_tty(machine)
        self.assertEqual(code, 1, out)
        self.assertIn("brew install tmux fzf", out)

    def test_declining_with_an_old_but_usable_fzf_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.50.0", uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            code, out = run_on_a_tty(machine)
        # It carries on: 0.36 is where `--listen` arrives, so the menu reloads;
        # what is lost is the cursor staying on the same row (`--id-nth`, 0.71).
        self.assertEqual(code, 0, out)
        self.assertIn("cursor", out)

    def test_declining_with_an_fzf_below_the_minimum_is_the_end_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = Machine(tmp, fzf="0.20.0", uname=("Darwin", "arm64"),
                              fzf_base=fzf_release(tmp))
            code, out = run_on_a_tty(machine)
        self.assertEqual(code, 1, out)
        self.assertIn("0.36", out)


# ── 6. handing over to `flightdeck install` ──────────────────────────────────

class TestHandingOver(unittest.TestCase):

    def test_the_doctor_has_the_last_word_and_it_is_healthy(self):
        # The assertion the orchestrator's own tests could not make: a REAL
        # `flightdeck install --yes` on a machine with no claude, no codex and
        # no agy exits 0. CI's smoke test depends on this being true.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--yes"])
            out = output(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("doctor", out)
        self.assertIn("✓ system", out)
        # The doctor read the same tmux and the same fzf this script checked:
        # both its lines are ✓, which is what says the stubs reached it rather
        # than it shrugging at a machine it could not measure.
        self.assertIn("✓ tmux", out)
        self.assertIn("✓ fzf", out)
        self.assertIn("Next steps", out)

    def test_the_command_resolves_for_the_doctor_that_follows(self):
        # R20: the doctor calls "linked but not resolvable" a ✗, and the rc line
        # it has just written has not been sourced by anybody. Exporting the
        # PATH for this one process is what keeps every fresh install from
        # ending on a failure it cannot do anything about.
        with tempfile.TemporaryDirectory() as tmp:
            out = output(run(Machine(tmp), ["--yes"]))
        self.assertIn("✓ flightdeck on PATH", out)
        self.assertNotIn("✗ flightdeck on PATH", out)

    def test_only_yes_force_and_keep_legacy_are_passed_on(self):
        # R5: `--version` is the installer's own. The orchestrator refuses an
        # option it does not know with exit 2, so a run that ends 0 is the proof
        # that it never saw it -- while the other three did reach it.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), ["--yes", "--force", "--keep-legacy",
                                   "--version", "v1.2.3"])
            out = output(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("--force", out)
        self.assertIn("--keep-legacy", out)
        self.assertIn("legacy entries kept", out)
        self.assertNotIn("unknown option", out)

    def test_and_without_it_nothing_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = output(run(Machine(tmp), ["--yes"]))
        self.assertNotIn("--keep-legacy", out)
        self.assertNotIn("legacy entries kept", out)

    def test_with_no_terminal_nobody_is_asked_anything(self):
        # R16's third case: no `--yes`, and no `/dev/tty` to ask on (a CI job, a
        # `curl … | sh` with its input closed). The questions are answered by
        # passing `--yes` on rather than by hanging on a read nobody can answer.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp))
            out = output(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("--yes", out)

    def test_the_environment_can_say_yes_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(Machine(tmp), env_extra={"FLIGHTDECK_YES": "1"})
        self.assertEqual(r.returncode, 0, output(r))


if __name__ == "__main__":
    unittest.main()
