# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revision of a source that changes only by a commit: the default branch's tip in the repository's canonical clone.

A workflow that GitHub answers 404 for stays missing until a commit adds its file to the default branch, so the answer is current while
that tip is unchanged. The tip is read from the local clone (the clone sync keeps it current), never from GitHub; a clone that is absent
or cannot be read gives no revision, and the caller then reads its source.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)

CommandRunner = Callable[[Sequence[str], int], tuple[int, str, str]]


def run_git(argv: Sequence[str], timeout_seconds: int) -> tuple[int, str, str]:
    """Run one local git command; a timeout or a missing binary is a non-zero answer, never an exception."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout_seconds} s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def cached_repo_head(
    cache: CachedSourceReader | None,
    home: Path,
    repo: str,
    run: CommandRunner | None = None,
) -> str | None:
    """The default branch's commit for ``repo`` using the cache overlay's clone settings; ``None`` with no cache or no readable clone."""
    settings = cache.clone_revision_settings if cache is not None else None
    if settings is None:
        return None
    template, timeout = settings
    return repo_head_revision(
        home,
        repo,
        clone_relpath_template=template,
        timeout_seconds=timeout,
        run=run or run_git,
    )


def repo_head_revision(
    home: Path,
    repo: str,
    *,
    clone_relpath_template: str,
    timeout_seconds: int,
    run: CommandRunner,
) -> str | None:
    """The default branch's commit in ``repo``'s clone under ``home``, or ``None`` when it cannot be read."""
    clone = home / clone_relpath_template.format(name=repo.rsplit("/", 1)[-1])
    if not clone.is_dir():
        return None
    code, out, _err = run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "refs/remotes/origin/HEAD"],
        timeout_seconds,
    )
    sha = out.strip()
    if code != 0 or len(sha) < 7 or not sha.isalnum():
        return None
    return sha
