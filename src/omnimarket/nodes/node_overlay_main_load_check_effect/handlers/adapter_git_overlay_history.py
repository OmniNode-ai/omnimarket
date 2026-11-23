# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The git reads of the overlay required check (OMN-26490): the nodes a branch adds and the history of the overlays checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

NODES_DIR = "src/omnimarket/nodes"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout


def _node_dirs(repo: Path, ref: str) -> set[str]:
    listing = _git(repo, "ls-tree", "-d", "--name-only", ref, f"{NODES_DIR}/")
    return {
        Path(line).name
        for line in listing.splitlines()
        if Path(line).name.startswith("node_")
    }


def added_node_names(repo: Path, base_ref: str) -> tuple[str, ...]:
    """The ``node_*`` directories the head holds that ``base_ref`` does not, sorted."""
    return tuple(sorted(_node_dirs(repo, "HEAD") - _node_dirs(repo, base_ref)))


def merged_pr_adds_overlay(checkout: Path, pr: int, overlay_id: str) -> bool:
    """True when a commit on the checkout's HEAD names ``(#pr)`` and added ``overlays/<overlay_id>/overlay.yaml``.

    A squash merge carries the number as the trailing ``(#n)`` of its subject, so a number no commit on main carries is an
    unmerged one, and a merged one that added another document is an unrelated one. The checkout must hold the history.
    """
    path = f"overlays/{overlay_id}/overlay.yaml"
    commits = _git(
        checkout, "log", "--format=%H", "--fixed-strings", f"--grep=(#{pr})"
    ).split()
    for commit in commits:
        added = _git(
            checkout,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=A",
            "-r",
            commit,
        ).splitlines()
        if path in added:
            return True
    return False


def first_commit_subject(repo: Path, node: str) -> str:
    """The subject of the commit that first added the node's contract, or ``""``."""
    subjects = _git(
        repo,
        "log",
        "--diff-filter=A",
        "--format=%s",
        "--",
        f"{NODES_DIR}/{node}/contract.yaml",
    ).splitlines()
    return subjects[-1] if subjects else ""
