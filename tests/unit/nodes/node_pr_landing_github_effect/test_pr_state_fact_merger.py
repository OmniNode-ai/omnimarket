# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The REST pull read carries who merged and the merge commit (OMN-23772)."""

from __future__ import annotations

import pytest

from omnimarket.events.pr_landing_github.model_github_pr_state_fact import (
    GithubPrStateParseError,
    ModelGithubPrStateFact,
)

pytestmark = pytest.mark.unit
HEAD = "a" * 40
MERGE = "c" * 40


def _pull(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "node_id": "PR_example",
        "number": 7,
        "head": {"sha": HEAD},
        "base": {"ref": "dev", "sha": "d" * 40},
        "state": "closed",
        "draft": False,
        "title": "example",
        "labels": [],
        "merged": True,
        "auto_merge": None,
    }
    body.update(extra)
    return body


def test_rest_pull_carries_the_merger_and_the_merge_commit() -> None:
    fact = ModelGithubPrStateFact.from_rest_pull(
        _pull(merged_by={"login": "example-landing-app"}, merge_commit_sha=MERGE)
    )
    assert fact.merged_by == "example-landing-app"
    assert fact.merge_commit_sha == MERGE


def test_rest_pull_without_a_merger_leaves_none() -> None:
    fact = ModelGithubPrStateFact.from_rest_pull(_pull(merged_by=None))
    assert fact.merged_by is None
    assert fact.merge_commit_sha is None


def test_rest_pull_with_a_malformed_merge_commit_is_refused() -> None:
    with pytest.raises(GithubPrStateParseError):
        ModelGithubPrStateFact.from_rest_pull(_pull(merge_commit_sha="abc123"))
