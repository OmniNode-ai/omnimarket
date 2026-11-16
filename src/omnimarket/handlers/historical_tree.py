# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read what a historical commit holds, for replaying its tree.

The delegation ladder replays a merged change by writing the commit's own tree
and running the commit's own test. Two facts about that tree decide whether the
replay means anything: which of the paths the replay wants the commit actually
holds (a ``git archive`` of an absent path fails the whole archive), and whether
the tree's test failed because its answer was wrong or because the tree could
not import a package this repository no longer installs.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: The top-level paths a replayed tree needs, in archive order. A commit that
#: predates one of them is archived without it.
REPLAY_PATHS: tuple[str, ...] = ("src", "tests", "scripts", "config", "pyproject.toml")

_IMPORT_FAILURE_RE = re.compile(
    r"^(?:E\s+)?(?:ModuleNotFoundError|ImportError):.*$", re.MULTILINE
)


def replay_paths(repo_root: Path, commit: str) -> list[str]:
    """The replay paths that ``commit`` holds, so the archive names only those."""
    held: list[str] = []
    for path in REPLAY_PATHS:
        probe = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}:{path}"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            held.append(path)
    return held


def import_failure(output: str) -> str | None:
    """The first import error in a test run's output, or ``None`` when it has none.

    An import error is a fact about the replayed tree, not about the answer under
    test, so a scorer reports it apart from a failed answer.
    """
    match = _IMPORT_FAILURE_RE.search(output)
    if match is None:
        return None
    return match.group(0).removeprefix("E").strip()
