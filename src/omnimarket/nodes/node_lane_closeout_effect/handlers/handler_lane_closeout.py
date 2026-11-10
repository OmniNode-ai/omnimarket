# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard a lane tree, preserve unpushed commits, and optionally remove it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import EnumLaneGuardVerdict
from omnimarket.models.lane_closeout.model_lane_closeout_request import (
    ModelLaneCloseoutRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_result import (
    ModelLaneCloseoutResult,
)


def _git(tree: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Invoke Git with an argument vector and noninteractive pushes."""
    return subprocess.run(
        ["git", "-C", str(tree), *argv],
        check=False,
        capture_output=True,
        text=True,
        env={**scrub_git_location_env(os.environ), "GIT_TERMINAL_PROMPT": "0"},
    )


def _guard(
    request: ModelLaneCloseoutRequest, tree: Path
) -> tuple[EnumLaneGuardVerdict, str]:
    """Apply the lane guard before any removal is considered."""
    try:
        status = _git(tree, "status", "--porcelain")
    except OSError:
        return EnumLaneGuardVerdict.KEEP, "status-unreadable"
    if status.returncode:
        return EnumLaneGuardVerdict.KEEP, "status-unreadable"
    if status.stdout.strip():
        return EnumLaneGuardVerdict.KEEP, "uncommitted-changes"

    branch = _git(tree, "symbolic-ref", "-q", "--short", "HEAD")
    why = "unpushed-commits"
    count_args: tuple[str, ...] = ()
    unreadable = ""
    if branch.returncode == 0 and branch.stdout.strip():
        upstream = _git(tree, "rev-parse", "-q", "--verify", "@{u}")
        if upstream.returncode == 0:
            count_args = ("@{u}..HEAD",)
            unreadable = "upstream-unreadable"
        else:
            why = "no-upstream"
    elif request.base:
        count_args = ("HEAD", f"^{request.base}")
        unreadable = "base-unreadable"
    else:
        count_args = ("HEAD", "--not", "--remotes")
        unreadable = "remotes-unreadable"

    if count_args:
        count = _git(tree, "rev-list", "--count", *count_args)
        if count.returncode:
            return EnumLaneGuardVerdict.KEEP, unreadable
        try:
            unpushed = int(count.stdout.strip())
        except ValueError:
            return EnumLaneGuardVerdict.KEEP, unreadable
        if unpushed == 0:
            return EnumLaneGuardVerdict.OK, ""

    wip_branch = f"wip/{request.tag}"
    try:
        pushed = _git(
            tree, "push", "-q", request.url or "origin", f"HEAD:refs/heads/{wip_branch}"
        )
    except OSError:
        return EnumLaneGuardVerdict.KEEP, f"{why}-push-failed"
    if pushed.returncode == 0:
        return EnumLaneGuardVerdict.WIP, wip_branch
    return EnumLaneGuardVerdict.KEEP, f"{why}-push-failed"


def _remove(tree: Path) -> bool:
    """Unregister and remove a linked worktree; True once the path is gone."""
    common = _git(tree, "rev-parse", "--git-common-dir")
    if common.returncode or not common.stdout.strip():
        return False
    git_dir = Path(common.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = tree / git_dir
    subprocess.run(
        ["git", "--git-dir", str(git_dir), "worktree", "remove", "--force", str(tree)],
        check=False,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return not tree.exists()


class HandlerLaneCloseout:
    """Keep unsafe trees and close out trees whose commits are preserved."""

    def handle(self, request: ModelLaneCloseoutRequest) -> ModelLaneCloseoutResult:
        tree = Path(request.tree)
        if (
            not tree.is_absolute()
            or not tree.is_dir()
            or not ((tree / ".git").is_file() or (tree / ".git").is_dir())
        ):
            raise ValueError(
                f"Expected an absolute path to an existing Git tree: {request.tree}"
            )

        verdict, detail = _guard(request, tree)
        removed = False
        if verdict is EnumLaneGuardVerdict.KEEP:
            reason = detail
        elif request.remove and (tree / ".git").is_dir():
            # A standalone clone is never removed here, only a linked worktree.
            reason = "standalone-clone"
        elif request.remove:
            removed = _remove(tree)
            reason = "remove-failed"
        else:
            reason = "asked-to-keep"
        cell = (
            f"worktree=removed:{request.tree}"
            if removed
            else f"worktree=kept:{request.tree}:{reason}"
        )
        return ModelLaneCloseoutResult(
            correlation_id=request.correlation_id,
            tree=request.tree,
            verdict=verdict,
            detail=detail,
            wip_push=detail if verdict is EnumLaneGuardVerdict.WIP else "",
            removed=removed,
            cell=cell,
        )
