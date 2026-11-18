# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local git branch history of the release sibling-premise read effect."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_git import (
    expand_clone_dir,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_failed import (
    EnumSiblingPremiseReadFailure,
    SiblingPremiseReadError,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
Runner = Callable[..., Any]


class GitBranchHistory:
    """First-parent branch history from a local clone (no fetch, no network)."""

    def __init__(self, *, run: Runner = subprocess.run, timeout: float = 30.0) -> None:
        self._run = run
        self._timeout = timeout

    def _git(self, clone: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            done: subprocess.CompletedProcess[str] = self._run(
                ["git", "-C", clone, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.CLONE_UNREADABLE, str(exc)
            ) from exc
        if done.returncode != 0:
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.CLONE_UNREADABLE,
                f"git {' '.join(args)} failed: {done.stderr.strip()[-300:]}",
            )
        return done

    def head(self, clone_dir: str, branch: str) -> str:
        clone = str(expand_clone_dir(clone_dir))
        done = self._git(clone, ["rev-parse", branch])
        sha = done.stdout.strip()
        if not _SHA.match(sha):
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.CLONE_UNREADABLE, f"bad head {sha!r}"
            )
        return sha

    def first_parent_walk(
        self, clone_dir: str, branch: str, limit: int
    ) -> tuple[str, ...]:
        clone = str(expand_clone_dir(clone_dir))
        done = self._git(
            clone, ["rev-list", "--first-parent", "--max-count", str(limit), branch]
        )
        return tuple(sha for sha in done.stdout.split() if _SHA.match(sha))

    def commit_time(self, clone_dir: str, sha: str) -> datetime:
        clone = str(expand_clone_dir(clone_dir))
        done = self._git(clone, ["show", "-s", "--format=%cI", sha])
        return datetime.fromisoformat(done.stdout.strip())

    def descendants(self, clone_dir: str, branch: str, sha: str) -> tuple[str, ...]:
        clone = str(expand_clone_dir(clone_dir))
        done = self._git(clone, ["rev-list", "--first-parent", f"{sha}..{branch}"])
        return tuple(sha2 for sha2 in done.stdout.split() if _SHA.match(sha2))
