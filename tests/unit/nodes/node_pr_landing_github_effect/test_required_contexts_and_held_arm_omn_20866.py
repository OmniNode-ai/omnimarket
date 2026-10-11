# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contexts beside the check runs, and an arm already in place (OMN-20866).

read_head_checks asked with a base branch reads that branch's required status
contexts (classic protection and rulesets), recorded from omnimarket's dev
branch. An arm whose policy read shows auto-merge already armed completes
without sending the mutation again, so a second arm path changes nothing.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_landing_github.model_github_check_run_fact import (
    required_contexts_from_branch_body,
    required_contexts_from_rules_body,
)
from omnimarket.github_landing.github_landing_requests import LANDING_POLICY_QUERY
from omnimarket.nodes.node_pr_landing_github_effect.handlers.handler_pr_landing_github import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
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


async def test_a_head_check_read_with_a_base_reads_its_required_contexts() -> None:
    name = "read_head_checks_required_contexts"
    transport = FakeGithubLandingTransport.for_scenario(name)
    result = await HandlerPrLandingGithubEffect(transport).handle(_command(name))
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.required_contexts is not None
    # 31 classic-protection contexts plus the one ruleset context, recorded.
    assert len(result.required_contexts) == 32
    assert "CI Summary" in result.required_contexts
    assert "repo-evidence / dod-verify" in result.required_contexts
    assert list(result.required_contexts) == sorted(result.required_contexts)
    assert [r.path for r in transport.sent][1:] == [
        "/repos/OmniNode-ai/omnimarket/branches/dev",
        "/repos/OmniNode-ai/omnimarket/rules/branches/dev?per_page=100",
    ]


async def test_a_head_check_read_without_a_base_reads_no_contexts() -> None:
    transport = FakeGithubLandingTransport.for_scenario("read_head_checks_200")
    result = await HandlerPrLandingGithubEffect(transport).handle(
        _command("read_head_checks_200")
    )
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.required_contexts is None


async def test_an_arm_already_in_place_sends_no_mutation() -> None:
    name = "arm_auto_merge_already_armed"
    transport = FakeGithubLandingTransport.for_scenario(name)
    result = await HandlerPrLandingGithubEffect(transport).handle(_command(name))
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted)
    (policy_read,) = transport.sent
    assert policy_read.body is not None
    assert policy_read.body["query"] == LANDING_POLICY_QUERY
    assert result.pr_state is not None
    assert result.pr_state.auto_merge_armed is True
    assert result.http_statuses == (200,)


def test_branch_protection_without_required_checks_requires_none() -> None:
    assert required_contexts_from_branch_body({"protected": False}) == set()
    assert (
        required_contexts_from_branch_body({"protection": {"enabled": True}}) == set()
    )
    with pytest.raises(ValueError, match="no body"):
        required_contexts_from_branch_body(None)


def test_rules_other_than_required_status_checks_add_nothing() -> None:
    body: dict[str, object] = {
        "value": [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "Gate A"}]},
            },
        ]
    }
    assert required_contexts_from_rules_body(body) == {"Gate A"}
    with pytest.raises(ValueError, match="not an array"):
        required_contexts_from_rules_body({"message": "Not Found"})


def test_base_ref_is_refused_on_any_other_operation() -> None:
    with pytest.raises(ValidationError, match="base_ref is only valid"):
        ModelPrLandingGithubRequest(
            correlation_id=_CORRELATION,
            operation=EnumPrLandingGithubOperation.READ_PR_STATE,
            mode=EnumPrLandingGithubMode.ENFORCE,
            repository="OmniNode-ai/omnimarket",
            pr_number=1,
            base_ref="dev",
        )


def test_the_rest_pull_read_carries_mergeable_state() -> None:
    body = load_scenario("read_pr_state_200").exchanges[0].response.body
    assert body is not None
    fact = ModelGithubPrStateFact.from_rest_pull({**body, "mergeable_state": "clean"})
    assert fact.mergeable_state == "clean"
    unknown = ModelGithubPrStateFact.from_rest_pull({**body, "mergeable_state": None})
    assert unknown.mergeable_state is None


@pytest.mark.parametrize(
    ("name", "queued", "armed"),
    [
        ("enqueue_already_queued", True, False),
        ("enqueue_already_armed", False, True),
    ],
)
async def test_an_enqueue_already_in_place_sends_no_mutation(
    name: str, queued: bool, armed: bool
) -> None:
    """A PR already queued, or armed to join the queue, holds: no second enqueue."""
    transport = FakeGithubLandingTransport.for_scenario(name)
    result = await HandlerPrLandingGithubEffect(transport).handle(_command(name))
    transport.assert_drained()
    assert isinstance(result, ModelPrLandingGithubCompleted)
    (policy_read,) = transport.sent
    assert policy_read.body is not None
    assert policy_read.body["query"] == LANDING_POLICY_QUERY
    assert result.pr_state is not None
    assert result.pr_state.in_merge_queue is queued
    assert result.pr_state.auto_merge_armed is armed
    assert result.http_statuses == (200,)
