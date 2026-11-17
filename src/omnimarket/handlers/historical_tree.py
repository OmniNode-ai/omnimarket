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

import importlib.util
import os
import re
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

#: The top-level paths a replayed tree needs, in archive order. A commit that
#: predates one of them is archived without it.
REPLAY_PATHS: tuple[str, ...] = ("src", "tests", "scripts", "config", "pyproject.toml")


def replay_paths(repo_root: Path, commit: str) -> list[str]:
    """The replay paths that ``commit`` holds, so the archive names only those."""
    held: list[str] = []
    for path in REPLAY_PATHS:
        probe = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}:{path}"],
            env=scrub_git_location_env(os.environ),
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            held.append(path)
    return held


_MISSING_MODULE_RE = re.compile(
    r"^(?:E\s+)?ModuleNotFoundError: No module named '(?P<module>[^']+)'.*$",
    re.MULTILINE,
)


_FOREIGN_IMPORT_RE = re.compile(
    r"^(?:E\s+)?ImportError: cannot import name '[^']+' from '(?P<module>[^']+)'(?: \((?P<source>[^)]+)\))?.*$",
    re.MULTILINE,
)


def _importable_here(module: str) -> bool:
    """Whether the project environment can import ``module``."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _tree_holds(repo_root: Path, commit: str, module: str) -> bool:
    """Whether ``commit``'s own tree holds ``module`` as a file, package or directory."""
    stem = module.replace(".", "/")
    for root in ("src/", ""):
        for suffix in (".py", "/__init__.py", ""):
            probe = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "cat-file",
                    "-e",
                    f"{commit}:{root}{stem}{suffix}",
                ],
                env=scrub_git_location_env(os.environ),
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                return True
    return False


def import_failure(output: str, repo_root: Path, commit: str) -> str | None:
    """An unavailable module or a missing symbol in an installed sibling.

    A module the change itself adds is absent from the control tree (its files
    are put back to the parent) yet is exactly what the answer must create, and a
    symbol the change adds fails its control with an ``ImportError`` for the same
    reason; both are the task, not a broken tree. Only a module absent from the
    project environment and from ``commit``'s own tree is a fact about the replay
    that no answer can repair. A missing symbol from an installed sibling is
    also outside the editable tree and is reported as a broken replay.
    """
    for match in _MISSING_MODULE_RE.finditer(output):
        module = match.group("module")
        if _importable_here(module) or _tree_holds(repo_root, commit, module):
            continue
        return match.group(0).removeprefix("E").strip()
    for match in _FOREIGN_IMPORT_RE.finditer(output):
        module = match.group("module")
        source = match.group("source") or ""
        # A tree-owned module may intentionally lack the symbol in its control.
        # Installed siblings cannot be repaired by the model's allowed edits.
        installed_source = "site-packages/" in source or "dist-packages/" in source
        if not installed_source and _tree_holds(repo_root, commit, module):
            continue
        if installed_source or _importable_here(module):
            return match.group(0).removeprefix("E").strip()
    return None
