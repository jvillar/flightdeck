"""`make tarball`: the archive a release is made of, and the shape it must have.

`install.sh` and `flightdeck update` both download one file --
`flightdeck.tar.gz` -- and both refuse it unless it holds exactly ONE top-level
directory with `bin/flightdeck` and `VERSION` inside. That shape is
not written down anywhere the release job can check: it is whatever `git
archive` happens to produce. So it is checked here, on the developer's machine,
where a mistake costs a test run instead of a broken release nobody can install.

**This test reads HEAD, not the working tree.** `git archive` packs a commit,
and `export-ignore` comes from the `.gitattributes` in the commit being packed.
So what it proves is that the last COMMITTED state builds a good release -- an
uncommitted change to `.gitattributes` or to `VERSION` is invisible to it, and
so is a file that has not been added yet. That is the right thing for a release
(the tag is what gets packed) and a real limitation for anybody editing: commit
first, then believe this test.
"""

import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from flightdeck import config

REPO = config.code_dir()

# The directory `install.sh` looks for inside the archive, and the two files it
# checks are in there before it moves anything into place.
TOP = "flightdeck"
REQUIRED = ("bin/flightdeck", "VERSION", "install.sh")

# What must never travel in a release: the repository's own machinery, and the
# demo pictures. `.git` `git archive` never packs; the rest are `export-ignore`
# entries in `.gitattributes`, and this is what proves they took. `docs/img` is
# there for its weight -- the GIF and the two stills are ~650 KB of README
# decoration that GitHub serves from the repository, and packing them would
# almost double the archive somebody downloads to install.
# `.superpowers` is a scratch directory some agent tooling creates; it is never
# tracked and must never ship, so the guard stays even while nothing packs it.
FORBIDDEN_TOPS = (".git", ".github", ".superpowers", ".gitattributes",
                  ".gitignore", "docs/img")


def _build(dist, repo=None):
    """`make tarball DIST=<dist>` from the repository root. -> the archive's path.

    `repo` is the directory to build in, and it is a parameter so the skip below
    can be tested without a second checkout to point at.
    """
    repo = REPO if repo is None else Path(repo)
    for tool in ("make", "git"):
        if not shutil.which(tool):
            raise unittest.SkipTest("%s is not installed" % tool)
    # And there has to be a repository to archive. This is not a formality: a
    # RELEASE carries `tests/` (that is why `tests/` is not in `.gitattributes`),
    # and an unpacked release has no `.git` -- `config.code_dir()` there is
    # `<data>/current`, which the installer extracted from a tarball. Somebody
    # running this suite from their installation would otherwise get seven
    # errors out of `git archive`'s "fatal: not a git repository", which says
    # nothing about their Flightdeck and everything about where they ran it.
    inside = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(repo),
                            capture_output=True, text=True, timeout=30)
    if inside.returncode != 0:
        raise unittest.SkipTest(
            "%s is not a git checkout, so there is no commit to archive "
            "(this is what an unpacked release looks like)" % repo)
    result = subprocess.run(["make", "tarball", "DIST=%s" % dist],
                            cwd=str(repo), capture_output=True, text=True,
                            timeout=120)
    if result.returncode != 0:
        raise AssertionError("make tarball failed (%d)\n%s\n%s"
                             % (result.returncode, result.stdout, result.stderr))
    archive = Path(dist) / "flightdeck.tar.gz"
    if not archive.exists():
        raise AssertionError("make tarball wrote no %s\n%s" % (archive, result.stdout))
    return archive


class TestTheReleaseArchive(unittest.TestCase):
    """The layout `install.sh` and `update` refuse a release for not having."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="flightdeck-tarball-")
        # Into a temporary DIST and never the repository's own: `make tarball`
        # with no argument writes `dist/`, and a test must not leave one behind
        # in somebody's working tree.
        cls.archive = _build(cls._tmp)
        with tarfile.open(str(cls.archive), "r:gz") as tar:
            cls.members = tar.getmembers()
        cls.names = [member.name for member in cls.members]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _member(self, name):
        """One member by its name inside the archive, or a failure saying so."""
        for member in self.members:
            if member.name == name:
                return member
        self.fail("%s is not in the archive" % name)

    def test_exactly_one_top_level_directory_and_it_is_flightdeck(self):
        # The whole of `install.sh`'s "extract, then rename the result into
        # place" rests on this: what it renames is the one directory it found,
        # whatever it is called, and two of them are refused outright.
        tops = {name.split("/")[0] for name in self.names}
        self.assertEqual(tops, {TOP}, sorted(tops))

    def test_it_holds_what_the_installer_checks_for(self):
        for tail in REQUIRED:
            self._member("%s/%s" % (TOP, tail))

    def test_the_command_is_executable(self):
        # `install.sh` chmods it anyway, but a release whose command arrives
        # without its bit is one `update` hands to a shell that refuses to run
        # it -- and there the chmod is the python's, not tar's.
        member = self._member("%s/bin/flightdeck" % TOP)
        self.assertTrue(member.mode & 0o111, oct(member.mode))

    def test_the_version_inside_is_the_repositorys(self):
        # The one number `bin/flightdeck --version` and `config.version()` both
        # read. `release.yml` refuses a tag that disagrees with it, so an
        # archive carrying a different one would make that check meaningless.
        with tarfile.open(str(self.archive), "r:gz") as tar:
            packed = tar.extractfile("%s/VERSION" % TOP).read().decode("utf-8")
        head = subprocess.run(["git", "show", "HEAD:VERSION"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(packed.strip(), head.stdout.strip())

    def test_the_tests_and_the_docs_travel_with_it(self):
        # Deliberate (R23): a release somebody unpacks can be run against its
        # own suite, and the docs are how they find out what it does.
        self._member("%s/tests/test_tarball.py" % TOP)
        self.assertTrue([name for name in self.names
                         if name.startswith("%s/docs/" % TOP)], self.names)

    def test_the_repositorys_own_machinery_stays_behind(self):
        for tail in FORBIDDEN_TOPS:
            self.assertNotIn("%s/%s" % (TOP, tail), self.names)
            self.assertFalse([name for name in self.names
                              if name.startswith("%s/%s/" % (TOP, tail))],
                             "%s travelled in the release" % tail)

    def test_nothing_in_it_is_a_link_or_a_device(self):
        # `install.sh` reads `tar -tvzf` and refuses anything that is not a
        # file, a directory or a symlink, and refuses a symlink whose target
        # climbs out. A release that trips its own installer's guard is the one
        # failure nobody would find until the day of the release.
        odd = [member.name for member in self.members
               if not (member.isfile() or member.isdir())]
        self.assertEqual(odd, [])


class TestOutsideAGitCheckout(unittest.TestCase):
    """Run from an unpacked release, this file skips instead of failing.

    That machine is one this repository deliberately creates: `tests/` travels
    in the tarball so a release can be run against its own suite, and there is
    no `.git` anywhere near `<data>/current`. `make` and `git` are both likely
    to be installed there, so their presence is not the question -- having
    something to archive is.
    """

    def test_a_directory_with_no_repository_is_a_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A temporary directory is normally nowhere near a checkout, but if
            # this machine's temp lives inside one, `git rev-parse` would find
            # it and the case being tested would not exist here.
            found = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=tmp,
                                   capture_output=True, text=True, timeout=30)
            if found.returncode == 0:
                raise unittest.SkipTest(
                    "this machine's temporary directory is inside a git "
                    "checkout, so it cannot stand in for an unpacked release")
            with self.assertRaises(unittest.SkipTest):
                _build(tmp, repo=tmp)


if __name__ == "__main__":
    unittest.main()
