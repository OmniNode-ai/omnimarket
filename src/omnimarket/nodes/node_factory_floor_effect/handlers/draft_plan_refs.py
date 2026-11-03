# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find a plan that is not on the plans repository's default branch yet on the head of its open pull request (OMN-21930).

A plan on an open pull request is drawn from that pull request's head branch, read from the local clone's refs
(``origin/<head_ref>``) only; the lane never asks GitHub. The overlay's ``plan_refs`` are kept and win: this adds
the refs the overlay does not name, for the declared plans no file on the default branch matches.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from omnimarket.models.model_factory_floor_input import ModelFloorWatcher
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorPlanRef,
)

PR_NUMBER_RE = re.compile(r"#(\d+)$")
GIT_TIMEOUT_SECONDS = 60


def _same_repo(pr_key: str, repo: str) -> bool:
    name = pr_key.rsplit("/", 1)[-1].rsplit("#", 1)[0]
    return name.replace("-", "_") == repo.replace("-", "_")


def _paths_on_ref(kb: Path, ref: str, plans_relpath: str) -> tuple[str, ...]:
    """The plan files the ref holds under the plans directory; none when the clone has no such ref."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(kb),
            "ls-tree",
            "-r",
            "--name-only",
            ref,
            "--",
            plans_relpath,
        ],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return ()
    return tuple(
        line
        for line in result.stdout.splitlines()
        if line.endswith(".md") and not line.endswith(".readable.md")
    )


def _on_default_branch(kb: Path, plans_relpath: str, token: str) -> bool:
    directory = kb / plans_relpath
    return directory.is_dir() and any(
        token in path.relative_to(kb).as_posix()
        for path in directory.rglob("*.md")
        if not path.name.endswith(".readable.md")
    )


def draft_plan_refs(
    *,
    kb: Path,
    plans_relpath: str,
    contains: tuple[str, ...],
    watcher: ModelFloorWatcher,
    declared: tuple[ModelFactoryFloorPlanRef, ...],
) -> tuple[ModelFactoryFloorPlanRef, ...]:
    """The declared refs, then one ref per plan an open pull request of the plans repository adds."""
    missing = tuple(
        token
        for token in dict.fromkeys(contains)
        if not _on_default_branch(kb, plans_relpath, token)
        and not any(token in ref.path for ref in declared)
    )
    if not missing:
        return declared
    open_prs = sorted(
        (
            (int(number.group(1)), pr.head_ref)
            for pr in watcher.prs
            if pr.state == "OPEN"
            and pr.head_ref
            and _same_repo(pr.key, kb.name)
            and (number := PR_NUMBER_RE.search(pr.key)) is not None
        ),
        reverse=True,
    )
    found: dict[str, str] = {}
    for token in missing:
        for _, head_ref in open_prs:
            ref = f"origin/{head_ref}"
            paths = [
                path for path in _paths_on_ref(kb, ref, plans_relpath) if token in path
            ]
            if paths:
                found.update({path: ref for path in paths if path not in found})
                break
    return declared + tuple(
        ModelFactoryFloorPlanRef(path=path, ref=ref) for path, ref in found.items()
    )
