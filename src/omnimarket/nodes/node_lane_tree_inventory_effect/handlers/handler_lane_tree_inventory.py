# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inventory Git trees under a lane directory without changing them."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.lane_closeout.model_lane_tree_inventory_request import (
    ModelLaneTreeInventoryRequest,
)
from omnimarket.models.lane_closeout.model_lane_tree_inventory_result import (
    ModelLaneTreeInventoryResult,
)
from omnimarket.models.lane_closeout.model_lane_tree_line import ModelLaneTreeLine


def _git(tree: Path, *argv: str, optional: bool = False) -> str:
    """Read Git output, allowing absent branch and upstream refs."""
    done = subprocess.run(
        ["git", "-C", str(tree), *argv],
        check=False,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    if done.returncode:
        if optional:
            return ""
        done.check_returncode()
    return done.stdout.strip()


class HandlerLaneTreeInventory:
    """List the outermost trees whose .git entry is at depth two or three."""

    def handle(
        self, request: ModelLaneTreeInventoryRequest
    ) -> ModelLaneTreeInventoryResult:
        lane_dir = Path(request.lane_dir)
        paths: list[Path] = []
        for root, directories, filenames in os.walk(lane_dir):
            tree = Path(root)
            depth = len(tree.relative_to(lane_dir).parts)
            if depth in (1, 2) and ".git" in (*directories, *filenames):
                paths.append(tree)
                # Descendants of an inventoried tree are nested, so skip them.
                directories[:] = []
            elif depth >= 2:
                directories[:] = []
            else:
                directories[:] = [name for name in directories if name != ".git"]

        trees = tuple(
            ModelLaneTreeLine(
                path=str(tree),
                repository=tree.name,
                branch=_git(
                    tree, "symbolic-ref", "-q", "--short", "HEAD", optional=True
                ),
                head=_git(tree, "rev-parse", "HEAD"),
                clean=not _git(tree, "status", "--porcelain"),
                unpushed=int(
                    _git(tree, "rev-list", "--count", "HEAD", "--not", "--remotes")
                ),
                upstream=_git(tree, "rev-parse", "--abbrev-ref", "@{u}", optional=True),
            )
            for tree in sorted(paths, key=os.fsencode)
        )
        return ModelLaneTreeInventoryResult(
            correlation_id=request.correlation_id,
            trees=trees,
            lines=tuple(tree.line() for tree in trees),
        )
