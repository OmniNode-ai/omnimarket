# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Remove the root ``CHANGELOG.md`` that a release cut added.

The release train stages a ``CHANGELOG.md`` entry in every omnimarket release cut,
because the ticket-contract prerequisite admits a release cut only while
``CHANGELOG.md`` is in its diff. The repository's removed-prose test forbids that
file on a tree, so the post-release dev bump takes it out again, the way the
0.4.324 bump did by hand.

Only a changelog the cut itself added is removed: a ``CHANGELOG.md`` that dev
carried before the cut, or one the cut merely edited, stays. A second run finds the
file gone and does nothing.

Stdlib only, and loaded by file path from ``scripts/ci/post_release_dev_bump.py``,
which runs in the release job before any project install.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CHANGELOG_PATH = "CHANGELOG.md"
_GIT_TIMEOUT_SECONDS = 60


def release_cut_added_changelog(root: Path, tag: str) -> bool:
    """Whether ``tag``'s commit added a root ``CHANGELOG.md`` over its first parent.

    A tag, a parent or a git repository that cannot be read answers ``False``: an
    unreadable cut never removes a file.
    """
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=A",
            "--no-renames",
            f"{tag}^1",
            f"{tag}^{{commit}}",
            "--",
            CHANGELOG_PATH,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return listing.returncode == 0 and listing.stdout.strip() == CHANGELOG_PATH


def remove_release_cut_changelog(root: Path, tag: str, *, apply: bool) -> bool:
    """Whether ``CHANGELOG.md`` is on the tree and ``tag``'s cut added it.

    ``apply`` deletes the file when the answer is ``True``; staging that deletion
    is the caller's.
    """
    changelog = root / CHANGELOG_PATH
    if not changelog.is_file() or not release_cut_added_changelog(root, tag):
        return False
    if apply:
        changelog.unlink()
    return True
