# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Git-backed fixture repositories for the sweep shelf's tests (OMN-18472).

The aislop, compliance and contract sweeps enumerate their scan corpus from git
rather than walking the filesystem, and refuse a scan root that is not inside a
git working tree. That refusal is deliberate: a filesystem fallback would let a
run over the wrong corpus produce the same shaped verdict as a correct one.

It also means a test fixture built as a bare temporary directory is no longer a
valid scan target. :func:`init_fixture_repo` turns one into a real repository so
the tests exercise the same enumeration path production does, instead of a
special case that only tests carry.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo`` with the inherited repository pointers removed.

    Git exports ``GIT_DIR`` and friends into every hook environment and those
    OVERRIDE ``git -C``, so a fixture that shells out to git under a pre-push
    hook would mutate the REAL invoking worktree rather than the temporary one
    (OMN-14891 / OMN-18434). The scrub is the canonical shared helper, not a
    local reimplementation, so this fixture cannot drift from the guard.
    """
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
    )


def init_fixture_repo(
    root: str | os.PathLike[str],
    *,
    commit: bool = True,
    track: list[str] | None = None,
) -> Path:
    """Make ``root`` a git repository whose existing files are all visible.

    By default every file already written under ``root`` is committed, so the
    fixture's corpus is tracked. Pass ``commit=False`` to leave the files
    untracked-but-not-ignored, which the sweeps also scan — that is the state of
    a file an author has written but not yet ``git add``ed. Pass ``track`` to
    commit only the paths named, leaving the rest untracked, which is how a
    fixture carries both classes at once.

    Identity and signing are set as repository-local config: a developer with a
    global ``commit.gpgsign=true`` would otherwise fail these tests for a reason
    that has nothing to do with the code under test (OMN-16584). Hooks are
    pointed at an empty directory the same way, so a global hook path cannot run
    the caller's governance against a throwaway fixture. Both are local config
    on a temporary repository, never a flag that skips a real gate.
    """
    repo = Path(root)
    repo.mkdir(parents=True, exist_ok=True)
    no_hooks = repo / ".git_fixture_nohooks"
    no_hooks.mkdir(exist_ok=True)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(no_hooks))
    if commit:
        _git(repo, "add", *(track if track is not None else ["-A"]))
        # An empty fixture has nothing to commit; the repository is still valid
        # and enumerates to an empty corpus, which is a state the sweeps must
        # handle rather than a fixture error.
        _git(repo, "commit", "-q", "--allow-empty", "-m", "fixture")
    return repo


def init_fixture_repos_under(omni_home: str | os.PathLike[str]) -> None:
    """Make every immediate subdirectory of a synthetic ``$OMNI_HOME`` a git repo.

    Sweeps that resolve bare repo NAMES against ``$OMNI_HOME`` scan each repo
    directory as its own root, so each one has to be its own repository — the
    parent temp directory being a repo would not help.
    """
    root = Path(omni_home)
    for child in sorted(root.iterdir()):
        if child.is_dir():
            init_fixture_repo(child)
