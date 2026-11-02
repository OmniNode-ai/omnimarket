# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host and a tips record held in memory, loaded from a JSON fixture.

This is the only host the node ships: it makes no network call. The run command
and the tests use it; a live source-control host is a later, separate binding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_branch_prune_effect.models import (
    ModelBranchInfo,
    ModelPullRequestInfo,
)


class FixtureBranchHost:
    """``ProtocolBranchHost`` over ``{repo: {default, branches, pull_requests}}``."""

    def __init__(self, repositories: dict[str, dict[str, Any]]) -> None:
        self._repos = repositories
        self.calls: list[tuple[str, ...]] = []

    @classmethod
    def from_file(cls, path: Path) -> FixtureBranchHost:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data)

    def default_branch(self, repository: str) -> str:
        return str(self._repos[repository]["default"])

    def list_branches(self, repository: str) -> list[ModelBranchInfo]:
        return [
            ModelBranchInfo.model_validate(b)
            for b in self._repos[repository]["branches"]
        ]

    def list_pull_requests(self, repository: str) -> list[ModelPullRequestInfo]:
        return [
            ModelPullRequestInfo.model_validate(p)
            for p in self._repos[repository]["pull_requests"]
        ]

    def delete_branch(self, repository: str, branch: str, *, expected_sha: str) -> None:
        self.calls.append(("delete", repository, branch, expected_sha))
        branches = self._repos[repository]["branches"]
        for b in branches:
            if b["name"] == branch:
                if b["tip_sha"] != expected_sha:
                    raise RuntimeError(f"{branch} moved off {expected_sha}")
                branches.remove(b)
                return
        raise RuntimeError(f"{branch} not found")


class MemoryTipRecord:
    """``ProtocolTipRecord`` that keeps tips in a list, optionally mirrored to a file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.rows: list[tuple[str, str, str]] = []

    def record(self, repository: str, branch: str, tip_sha: str) -> None:
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps([repository, branch, tip_sha]) + "\n")
        self.rows.append((repository, branch, tip_sha))
