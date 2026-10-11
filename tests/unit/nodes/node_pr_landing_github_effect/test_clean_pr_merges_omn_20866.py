# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR GitHub already reports mergeable is merged, not left unarmed (OMN-20866).

GitHub refuses ``enablePullRequestAutoMerge`` on a PR whose merge state is
clean ("Pull request is in clean status"), and the landing orchestrator asks
for the arm only once a head is green and GitHub reports it clean, so the arm
it sends was refused every time. The arm's policy read now reads
``mergeStateStatus``; a PR GitHub would merge now, with no arm in place, is
merged at the expected head with ``mergePullRequest``. Any other merge state
still arms.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.github_landing.github_landing_requests import (
    ENABLE_AUTO_MERGE_AT_HEAD_MUTATION,
    LANDING_POLICY_QUERY,
    MERGE_AT_HEAD_MUTATION,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers.handler_pr_landing_github import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    ModelGithubPrStateFact,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubRequest,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    load_scenario,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("00000000-0000-0000-0000-000000020866")
_CLEAN = "arm_auto_merge_clean_merges"


def _command(name: str) -> ModelPrLandingGithubRequest:
    scenario = load_scenario(name)
    return ModelPrLandingGithubRequest.model_validate(
        {
            **scenario.command,
            "correlation_id": _CORRELATION,
            "operation": scenario.operation,
            "mode": EnumPrLandingGithubMode.ENFORCE,
        }
    )


def test_the_policy_read_asks_for_the_merge_state() -> None:
    assert "mergeStateStatus" in LANDING_POLICY_QUERY


def test_the_policy_read_carries_the_merge_state_in_lower_case() -> None:
    body = load_scenario(_CLEAN).exchanges[0].response.body
    assert body is not None
    assert ModelGithubPrStateFact.from_policy_read(body).mergeable_state == "clean"
    unread = load_scenario("arm_auto_merge_200").exchanges[0].response.body
    assert ModelGithubPrStateFact.from_policy_read(unread).mergeable_state is None


async def test_a_clean_unarmed_pr_is_merged_at_its_head_not_armed() -> None:
    transport = FakeGithubLandingTransport.for_scenario(_CLEAN)
    command = _command(_CLEAN)
    result = await HandlerPrLandingGithubEffect(transport).handle(command)
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted), result
    policy_read, mutation = transport.sent
    assert policy_read.body is not None
    assert policy_read.body["query"] == LANDING_POLICY_QUERY
    assert mutation.body is not None
    assert mutation.body["query"] == MERGE_AT_HEAD_MUTATION
    assert mutation.body["variables"] == {
        "id": command.pr_node_id,
        "method": "SQUASH",
        "head": command.head_sha,
    }
    assert result.http_statuses == (200, 200)
    assert result.requests == tuple(transport.sent)


async def test_a_pr_with_no_merge_state_read_is_still_armed() -> None:
    transport = FakeGithubLandingTransport.for_scenario("arm_auto_merge_200")
    result = await HandlerPrLandingGithubEffect(transport).handle(
        _command("arm_auto_merge_200")
    )
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted), result
    _, mutation = transport.sent
    assert mutation.body is not None
    assert mutation.body["query"] == ENABLE_AUTO_MERGE_AT_HEAD_MUTATION
