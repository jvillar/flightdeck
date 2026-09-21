# The four things CI runs, spelled once so a workflow step and a developer type
# the same words. Nothing here is a build: Flightdeck is python and a bash
# script, and `make tarball` only packs what git already has.
#
# Kept to what every make understands -- `?=`, `.PHONY`, tabs -- because it runs
# on GNU make 3.81 (the one macOS ships), on the GNU make of an Ubuntu runner,
# and on whatever a user has. No pattern rules, no functions, no `:=`.

# Where `tarball` writes. Overridable so a test can point it at a temporary
# directory instead of leaving a `dist/` in somebody's working tree.
DIST ?= dist

# Overridable so a machine with no `shellcheck` on PATH can still lint with one
# it has somewhere else.
SHELLCHECK ?= shellcheck

.PHONY: test lint tarball clean

# The suite, with the socket every test defaults to anyway. It is named here as
# well because the default lives in `tests/__init__.py`, and a `make test` that
# relies on a python file to keep it away from the developer's tmux server is
# one import away from not doing so.
test:
	FLIGHTDECK_TMUX_SOCKET=no-such-socket python3 -m unittest discover -s tests -t . -q

# The two shells, each with the dialect it is actually written in: the command
# is bash (arrays, `local`), the installer is POSIX sh so it runs under the dash
# that is `/bin/sh` on Debian and Ubuntu. Linting the installer as bash would
# pass things dash cannot do.
lint:
	$(SHELLCHECK) -s bash bin/flightdeck
	$(SHELLCHECK) -s sh install.sh

# The release archive, in the shape `install.sh` and `flightdeck update` both
# insist on: ONE top-level directory `flightdeck/` with the whole tree inside
# it.
#
# `git archive` and not `tar` of the working tree, for two reasons. It packs a
# COMMIT, so a release can never carry somebody's half-finished edit or a stray
# file sitting in the directory; and it honours `export-ignore`, which is how
# `.github` and the git dotfiles stay behind while the tests and the docs
# travel (they are deliberately IN: a release you unpack can be run against its
# own suite).
#
# The consequence to remember: it reads HEAD. An uncommitted change is not in
# the archive, and `tests/test_tarball.py` says the same thing in its docstring.
tarball:
	mkdir -p "$(DIST)"
	git archive --format=tar.gz --prefix=flightdeck/ -o "$(DIST)/flightdeck.tar.gz" HEAD

clean:
	rm -rf "$(DIST)"
