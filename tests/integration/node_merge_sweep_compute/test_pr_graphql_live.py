# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live GitHub GraphQL integration test for node_merge_sweep_compute.

Guards against regressions of OMN-9563: inline fragments on
StatusCheckRollupContext must be spread INSIDE nodes{}, not directly
into the StatusCheckRollupContextConnection.
"""

from __future__ import annotations

import os

import pytest

from omnimarket.nodes.node_merge_sweep_compute.adapter_github_http import (
    GitHubHttpClient,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

_SKIP_REASON = "requires GH_PAT env var (export GH_PAT=$(gh auth token) before running)"


@pytest.fixture(scope="module")
def github_client() -> GitHubHttpClient:
    token = os.environ.get("GH_PAT") or os.environ.get("GH_TOKEN")
    if not token:
        pytest.skip(_SKIP_REASON)
    os.environ["GH_PAT"] = token
    return GitHubHttpClient()


@pytest.mark.integration
def test_fetch_open_prs_does_not_raise_cannot_spread_fragment(
    github_client: GitHubHttpClient,
) -> None:
    """Regression: pre-fix this raised GitHubTransportError with cannotSpreadFragment."""
    try:
        prs = github_client.fetch_open_prs("OmniNode-ai/omnibase_core")
    except GitHubTransportError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"Live GraphQL call raised: {exc}")
    assert isinstance(prs, list)


@pytest.mark.integration
def test_status_check_rollup_is_normalized_list_of_dicts(
    github_client: GitHubHttpClient,
) -> None:
    prs = github_client.fetch_open_prs("OmniNode-ai/omnibase_core")
    if not prs:
        pytest.skip("no open PRs on omnibase_core; cannot assert rollup shape")

    for pr in prs:
        rollup = pr.get("statusCheckRollup")
        assert isinstance(rollup, list), (
            f"PR #{pr.get('number')} statusCheckRollup not normalized to list: "
            f"got {type(rollup).__name__}"
        )
        for ctx in rollup:
            assert isinstance(ctx, dict)
            assert "conclusion" in ctx
            assert ctx.get("isRequired") is True
            # Either CheckRun (has name) or StatusContext (has context)
            assert ("name" in ctx) or ("context" in ctx)


@pytest.mark.integration
def test_at_least_one_pr_has_populated_rollup(
    github_client: GitHubHttpClient,
) -> None:
    """Sanity check that the query actually returns check data, not empty lists."""
    prs = github_client.fetch_open_prs("OmniNode-ai/omnibase_core")
    if not prs:
        pytest.skip("no open PRs on omnibase_core")

    total_contexts = sum(len(pr.get("statusCheckRollup", [])) for pr in prs)
    assert total_contexts > 0, (
        "Live fetch returned zero total statusCheckRollup contexts across all "
        "open PRs — regression: query succeeded but returned no check data."
    )
