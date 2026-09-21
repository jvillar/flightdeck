"""`flightdeck update`: fetch a release, put it in place, reinstall, restart.

Nothing here reaches GitHub and nothing here reinstalls anything. Every test
builds its OWN release -- a tar.gz holding a `flightdeck/` directory whose
`bin/flightdeck` is a shell stub that appends its arguments to a log file -- and
serves it over a `file://` URL through `FLIGHTDECK_TARBALL_URL`. So the download
is real, the extraction is real, the swap of `current` for the new code is real,
and the two things that would touch the machine (`install --yes` and `restart`)
are observed through that stub or through an injected `run`.

Everything lives under one temporary directory: `XDG_DATA_HOME` (and with it
`<data>/current`), the state directory and the config. `config.code_dir` is
patched to the fake `current`, which is what makes R8's guard say "this IS the
installed code" without the repository checkout being involved at any point.
"""
import contextlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flightdeck import config, update

# A `bin/flightdeck` that records what it was asked to do and says yes. `$FDLOG`
# is in the environment of the test process, so the stub writes where the test
# can read it without the path having to be baked into the archive.
STUB = """#!/bin/sh
echo "$@" >> "$FDLOG"
exit 0
"""

# The same, refusing: what a reinstall that goes wrong looks like.
STUB_THAT_FAILS = """#!/bin/sh
echo "$@" >> "$FDLOG"
exit 1
"""


@contextlib.contextmanager
def sandbox(tmp):
    """A throwaway machine: data, state and config under `tmp`. -> the data dir."""
    tmp = Path(tmp)
    env = {"HOME": str(tmp / "home"),
           "XDG_DATA_HOME": str(tmp / "share"),
           "FLIGHTDECK_STATE_DIR": str(tmp / "state"),
           "FLIGHTDECK_CONFIG": str(tmp / "config" / "config.json"),
           "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
           "XDG_STATE_HOME": str(tmp / "xdg-state"),
           # A socket nothing listens on, set HERE and not left to the suite's
           # defaults: the tests that use the real `subprocess.run` ask tmux
           # `has-session` to decide whether to restart the menus, and run as a
           # file rather than through `python3 -m unittest` (which imports
           # `tests/__init__.py`) that question would reach the server the
           # developer is working in -- and the answer would be yes.
           "FLIGHTDECK_TMUX_SOCKET": "flightdeck-tests-no-such-socket",
           "FDLOG": str(tmp / "stub.log")}
    (tmp / "home").mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, env):
        yield config.data_dir()


def installed(data, version="0.1.0"):
    """A fake `<data>/current`, as if Flightdeck had been installed. -> its path."""
    current = Path(data) / "current"
    (current / "bin").mkdir(parents=True, exist_ok=True)
    (current / "bin" / "flightdeck").write_text("#!/bin/sh\necho old\n")
    (current / "VERSION").write_text(version + "\n")
    (current / "flightdeck").mkdir(exist_ok=True)
    (current / "flightdeck" / "__init__.py").write_text("")
    return current


def a_release(tmp, version="0.9.9", stub=STUB, top="flightdeck", extra=None):
    """A release tarball of our own making. -> its `file://` URL.

    `top` is the name of the one directory inside it, and `extra` a callable that
    gets the built tree before it is packed, for the tests about what a tarball
    is allowed to hold.
    """
    tree = Path(tmp) / "build" / top
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    (tree / "bin" / "flightdeck").write_text(stub)
    (tree / "bin" / "flightdeck").chmod(0o644)   # the archive's modes are not trusted
    (tree / "VERSION").write_text(version + "\n")
    (tree / "flightdeck").mkdir(exist_ok=True)
    (tree / "flightdeck" / "__init__.py").write_text("")
    if extra is not None:
        extra(tree)
    archive = Path(tmp) / "flightdeck.tar.gz"
    with tarfile.open(str(archive), "w:gz") as tar:
        tar.add(str(tree), arcname=top)
    return "file://" + str(archive)


def code_at(path):
    """Pretend the code running right now lives at `path`."""
    return mock.patch.object(config, "code_dir", return_value=Path(path))


def run(argv=(), **kwargs):
    """`update.main(argv)` with its output captured. -> (exit code, output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = update.main(list(argv), **kwargs)
    return code, out.getvalue()


def log_lines(tmp):
    """What the stub `bin/flightdeck` was asked to do, in order."""
    path = Path(tmp) / "stub.log"
    return path.read_text().splitlines() if path.exists() else []


class FakeRun:
    """`subprocess.run`, recording. -> whatever rc the test asked for.

    `has-session` is answered separately because that is the one call whose
    answer changes what the command does next.
    """

    def __init__(self, tmux_alive=False, install_rc=0):
        self.calls = []
        self.tmux_alive = tmux_alive
        self.install_rc = install_rc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc = 0
        if "has-session" in argv:
            rc = 0 if self.tmux_alive else 1
        elif "install" in argv:
            rc = self.install_rc
        return subprocess.CompletedProcess(argv, rc, "", "")

    def words(self):
        """The calls as strings, for readable assertions."""
        return [" ".join(call) for call in self.calls]


class TestTheUrl(unittest.TestCase):
    """Which release is fetched, and where the changelog is."""

    def test_with_no_version_it_is_the_latest_release(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLIGHTDECK_TARBALL_URL", None)
            url = update.tarball_url()
        self.assertIn("/releases/latest/download/flightdeck.tar.gz", url)

    def test_a_version_names_its_tag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLIGHTDECK_TARBALL_URL", None)
            self.assertIn("/releases/download/v1.2.3/flightdeck.tar.gz",
                          update.tarball_url("v1.2.3"))

    def test_the_environment_wins_which_is_how_the_tests_stay_off_github(self):
        with mock.patch.dict(os.environ, {"FLIGHTDECK_TARBALL_URL": "file:///x.tgz"}):
            self.assertEqual(update.tarball_url(), "file:///x.tgz")
            self.assertEqual(update.tarball_url("v1.2.3"), "file:///x.tgz")

    def test_the_v_is_optional_when_you_ask_for_a_version(self):
        self.assertEqual(update.normalise_tag("1.2.3"), "v1.2.3")
        self.assertEqual(update.normalise_tag("v1.2.3"), "v1.2.3")
        self.assertEqual(update.normalise_tag("2.0.0-rc1"), "v2.0.0-rc1")

    def test_something_that_is_not_a_version_is_refused(self):
        # It goes into a URL path: `--version ../../other/thing` must not build
        # a request for somebody else's release.
        for bad in ("", "v", "../../x", "latest/../x", "1.2.3 4", "v1.2.3/../y"):
            self.assertIsNone(update.normalise_tag(bad), bad)

    def test_the_changelog_is_the_tag_or_main(self):
        self.assertIn("/blob/v1.2.3/CHANGELOG.md", update.changelog_url("v1.2.3"))
        self.assertIn("/blob/main/CHANGELOG.md", update.changelog_url())


class TestWhatATarballIsAllowedToHold(unittest.TestCase):
    """`unsafe_members` decides BEFORE a single byte is written to disk."""

    def _info(self, name, kind=tarfile.REGTYPE, linkname=""):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        return info

    def test_an_ordinary_tree_is_fine(self):
        members = [self._info("flightdeck", tarfile.DIRTYPE),
                   self._info("flightdeck/VERSION"),
                   self._info("flightdeck/bin/flightdeck")]
        self.assertEqual(update.unsafe_members(members), [])

    def test_an_absolute_name_is_refused(self):
        self.assertEqual(update.unsafe_members([self._info("/etc/passwd")]),
                         ["/etc/passwd"])

    def test_a_name_that_climbs_out_is_refused(self):
        bad = self._info("flightdeck/../../.ssh/authorized_keys")
        self.assertEqual(update.unsafe_members([bad]), [bad.name])

    def test_a_symlink_pointing_outside_is_refused(self):
        bad = self._info("flightdeck/bin/x", tarfile.SYMTYPE, "../../../../etc/passwd")
        self.assertEqual(update.unsafe_members([bad]), [bad.name])

    def test_an_absolute_symlink_is_refused_too(self):
        bad = self._info("flightdeck/bin/x", tarfile.SYMTYPE, "/etc/passwd")
        self.assertEqual(update.unsafe_members([bad]), [bad.name])

    def test_a_symlink_that_does_not_climb_at_all_is_allowed(self):
        ok = self._info("flightdeck/bin/x", tarfile.SYMTYPE, "flightdeck")
        self.assertEqual(update.unsafe_members([ok]), [])

    def test_a_symlink_carrying_dotdot_is_refused_even_where_it_lands_inside(self):
        """The same rule `install.sh` applies, and for the reason below."""
        bad = self._info("flightdeck/bin/x", tarfile.SYMTYPE, "../VERSION")
        self.assertEqual(update.unsafe_members([bad]), [bad.name])

    def test_a_chain_of_links_each_landing_inside_is_refused(self):
        """Every hop lands inside on its own; followed, they walk one level out
        per hop. Where a chain ends is only knowable by resolving it, so a `..`
        anywhere in a link target is refused rather than worked out."""
        chain = [self._info("flightdeck/a", tarfile.SYMTYPE, ".."),
                 self._info("flightdeck/a/b", tarfile.SYMTYPE, ".."),
                 self._info("flightdeck/a/b/c", tarfile.SYMTYPE, "..")]
        self.assertEqual(update.unsafe_members(chain), [m.name for m in chain])

    def test_a_hard_link_pointing_outside_is_refused(self):
        bad = self._info("flightdeck/x", tarfile.LNKTYPE, "../../etc/passwd")
        self.assertEqual(update.unsafe_members([bad]), [bad.name])

    def test_a_device_or_a_fifo_is_refused(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            self.assertEqual(update.unsafe_members([self._info("flightdeck/d", kind)]),
                             ["flightdeck/d"], kind)

    def test_a_release_holding_one_is_refused_and_nothing_is_swapped(self):
        def with_a_climbing_link(tree):
            (tree / "bin" / "escape").symlink_to("../../../../../../etc/passwd")

        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp, extra=with_a_climbing_link)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual((current / "VERSION").read_text().strip(), "0.1.0")
            self.assertFalse((Path(data) / "previous").exists())
        self.assertEqual(code, 1)
        self.assertIn("outside", out)


class TestWhatTheArchiveHasToBe(unittest.TestCase):
    """One top-level directory, holding the command and a VERSION."""

    def test_two_top_level_directories_are_refused(self):
        def alongside(tree):
            (tree.parent / "other").mkdir(exist_ok=True)
            (tree.parent / "other" / "x").write_text("")

        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            # built by hand: two directories at the top of the archive
            tree = Path(tmp) / "build" / "flightdeck"
            (tree / "bin").mkdir(parents=True)
            (tree / "bin" / "flightdeck").write_text(STUB)
            (tree / "VERSION").write_text("0.9.9\n")
            alongside(tree)
            archive = Path(tmp) / "two.tar.gz"
            with tarfile.open(str(archive), "w:gz") as tar:
                tar.add(str(tree), arcname="flightdeck")
                tar.add(str(tree.parent / "other"), arcname="other")
            with code_at(current), mock.patch.dict(
                    os.environ, {"FLIGHTDECK_TARBALL_URL": "file://" + str(archive)}):
                code, out = run()
        self.assertEqual(code, 1)
        self.assertIn("one directory", out)

    def test_a_tree_without_the_command_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            tree = Path(tmp) / "build" / "flightdeck"
            tree.mkdir(parents=True)
            (tree / "VERSION").write_text("0.9.9\n")
            archive = Path(tmp) / "bare.tar.gz"
            with tarfile.open(str(archive), "w:gz") as tar:
                tar.add(str(tree), arcname="flightdeck")
            with code_at(current), mock.patch.dict(
                    os.environ, {"FLIGHTDECK_TARBALL_URL": "file://" + str(archive)}):
                code, out = run()
            self.assertEqual((current / "VERSION").read_text().strip(), "0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("bin/flightdeck", out)

    def test_the_command_comes_out_executable_whatever_the_archive_said(self):
        # The tarball is built with the file at 0644 on purpose: a release packed
        # on a machine with a strange umask must not leave the user with a
        # command their shell refuses to run.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual(code, 0, out)
            self.assertTrue(os.access(str(current / "bin" / "flightdeck"), os.X_OK))

    def test_a_data_directory_it_cannot_write_in_is_one_line_too(self):
        # Not a release problem but the same answer: one line and exit 1, never
        # a traceback on top of a cockpit that is still working perfectly well.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}), \
                    mock.patch.object(update.tempfile, "mkstemp",
                                      side_effect=OSError("read-only file system")):
                code, out = run()
            self.assertEqual((current / "VERSION").read_text().strip(), "0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("read-only file system", out)

    def test_a_download_that_fails_is_one_line_and_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            missing = "file://" + str(Path(tmp) / "no-such-release.tar.gz")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": missing}):
                code, out = run()
            self.assertEqual((current / "VERSION").read_text().strip(), "0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("could not", out.lower())


class TestTheSwap(unittest.TestCase):
    """`current` becomes `previous`, the new code becomes `current`."""

    def test_the_old_code_is_kept_as_previous(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual(code, 0, out)
            self.assertEqual((current / "VERSION").read_text().strip(), "0.9.9")
            self.assertEqual((Path(data) / "previous" / "VERSION").read_text().strip(),
                             "0.1.0")

    def test_a_previous_from_an_earlier_update_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            old = Path(data) / "previous"
            old.mkdir(parents=True)
            (old / "VERSION").write_text("0.0.1\n")
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                run()
            self.assertEqual((old / "VERSION").read_text().strip(), "0.1.0")

    def test_the_path_the_hooks_name_does_not_move(self):
        # The whole reason for the `current` name: the agents' settings carry
        # absolute paths into it, so an update must swap what is BEHIND the name.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                run()
            self.assertTrue((current / "bin" / "flightdeck").exists())
            self.assertEqual(current, Path(data) / "current")

    def test_a_swap_that_cannot_finish_puts_the_old_code_back(self):
        real_rename = os.rename
        calls = []

        def rename(src, dst):
            calls.append((str(src), str(dst)))
            # The first rename is current -> previous; the SECOND is the new code
            # into place, and that is the one made to fail.
            if len(calls) == 2:
                raise OSError("no space left on device")
            return real_rename(src, dst)

        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}), \
                    mock.patch.object(update.os, "rename", side_effect=rename):
                code, out = run()
            self.assertEqual(code, 1)
            self.assertIn("no space left", out)
            # the code the user had is where it was, and still works
            self.assertTrue((current / "bin" / "flightdeck").exists())
            self.assertEqual((current / "VERSION").read_text().strip(), "0.1.0")


class TestTheSameVersion(unittest.TestCase):

    def test_nothing_is_swapped_and_it_says_so(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data, version="0.9.9")
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual(code, 0)
            self.assertIn("already at v0.9.9", out)
            self.assertFalse((Path(data) / "previous").exists())
            self.assertEqual(log_lines(tmp), [], "it reinstalled anyway")

    def test_and_no_temporary_files_are_left_in_the_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data, version="0.9.9")
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                run()
            self.assertEqual(sorted(p.name for p in Path(data).iterdir()),
                             ["current"])


class TestASourceCheckout(unittest.TestCase):
    """R8 again: this command manages `<data>/current` and nothing else."""

    def test_it_refuses_before_anything_is_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            checkout = Path(tmp) / "checkout"
            (checkout / "bin").mkdir(parents=True)
            url = a_release(tmp)
            with code_at(checkout), mock.patch.dict(os.environ,
                                                    {"FLIGHTDECK_TARBALL_URL": url}), \
                    mock.patch.object(update, "download",
                                      side_effect=AssertionError("it downloaded")):
                code, out = run()
            self.assertFalse(Path(data).exists(), "it created the data directory")
        self.assertEqual(code, 1)
        self.assertIn("source checkout", out)
        self.assertIn("git pull", out)

    def test_this_very_repository_is_one(self):
        self.assertFalse(config.installed_here())


class TestReinstallingAndRestarting(unittest.TestCase):
    """What the new code is asked to do once it is in place."""

    def test_the_NEW_command_is_what_reinstalls(self):
        # Never in this process: the interpreter running holds the OLD modules,
        # so `install` has to be the command that has just been put in place.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual(code, 0, out)
            self.assertEqual(log_lines(tmp), ["install --yes"])

    def test_with_no_tmux_server_it_does_not_restart_and_says_so(self):
        fake = FakeRun(tmux_alive=False)
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run(run=fake)
        self.assertEqual(code, 0, out)
        self.assertNotIn("restart", " ".join(fake.words()))
        self.assertIn("flightdeck", out)
        self.assertIn("no tmux server", out)

    def test_with_a_server_the_menus_are_restarted_after_the_install(self):
        fake = FakeRun(tmux_alive=True)
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, _out = run(run=fake)
        self.assertEqual(code, 0)
        words = fake.words()
        install = [i for i, w in enumerate(words) if "install --yes" in w]
        restart = [i for i, w in enumerate(words) if w.endswith("restart")]
        self.assertTrue(install and restart, words)
        self.assertLess(install[0], restart[0], words)
        # and both are the command that has just been put in place
        for index in install + restart:
            self.assertIn(str(Path(data) / "current"), words[index])

    def test_a_reinstall_that_refuses_is_exit_1_with_the_code_still_swapped(self):
        # The swap has already happened and there is no going back to old code
        # the agents' settings no longer point at: what is reported is the
        # reinstall, and the user is told to run it again.
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp, stub=STUB_THAT_FAILS)
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run()
            self.assertEqual((current / "VERSION").read_text().strip(), "0.9.9")
        self.assertEqual(code, 1)
        self.assertIn("flightdeck install", out)


class TestWhatItSays(unittest.TestCase):

    def test_it_prints_the_old_and_the_new_version_and_the_changelog(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data, version="0.1.0")
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                _code, out = run()
        self.assertIn("v0.1.0", out)
        self.assertIn("v0.9.9", out)
        self.assertIn("CHANGELOG.md", out)

    def test_asking_for_a_version_points_the_changelog_at_that_tag(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                _code, out = run(["--version", "0.9.9"])
        self.assertIn("/blob/v0.9.9/CHANGELOG.md", out)


class TestTheWords(unittest.TestCase):

    def test_an_unknown_flag_is_the_usage_on_stderr_and_exit_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = update.main(["--nightly"])
        self.assertEqual(code, 2)
        self.assertIn("usage: flightdeck update", err.getvalue())

    def test_version_with_nothing_after_it_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = update.main(["--version"])
        self.assertEqual(code, 2)
        self.assertIn("usage: flightdeck update", err.getvalue())

    def test_a_version_that_is_not_one_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = update.main(["--version", "../../elsewhere"])
        self.assertEqual(code, 2)
        self.assertIn("v1.2.3", err.getvalue())

    def test_the_equals_form_works_too(self):
        with tempfile.TemporaryDirectory() as tmp, sandbox(tmp) as data:
            current = installed(data)
            url = a_release(tmp, version="0.9.9")
            with code_at(current), mock.patch.dict(os.environ,
                                                   {"FLIGHTDECK_TARBALL_URL": url}):
                code, out = run(["--version=0.9.9"])
        self.assertEqual(code, 0, out)
        self.assertIn("/blob/v0.9.9/CHANGELOG.md", out)


if __name__ == "__main__":
    unittest.main()
