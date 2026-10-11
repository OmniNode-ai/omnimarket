# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every recorded scenario replays through the real handler (OMN-19826, OMN-19831).

OMN-19826 froze the scenarios as the seam; OMN-19831 drives each one through
``HandlerPrLandingGithubEffect`` over the strict fake transport. The command in
the fixture is the handler's input, every request the handler sends must match
the next recorded request byte for byte, every recorded exchange must be used,
and the typed result must match the scenario's expected outcome.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_github_effect.protocols import (
    ProtocolPrLandingGithubTransport,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    FakeTransportMismatchError,
    ModelFixtureScenario,
    all_scenarios,
    load_scenario,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("00000000-0000-4000-8000-000000019831")
_IDENTITY = "GITHUB_TOKEN"
_SCENARIOS = all_scenarios()
# Scenarios whose policy read chose a mutation other than the planned one.
_READ_PICKS_THE_MUTATION = frozenset({"arm_auto_merge_clean_merges"})


def _command(
    scenario: ModelFixtureScenario,
    mode: EnumPrLandingGithubMode = EnumPrLandingGithubMode.ENFORCE,
) -> ModelPrLandingGithubRequest:
    return ModelPrLandingGithubRequest.model_validate(
        {
            **scenario.command,
            "correlation_id": _CORRELATION,
            "operation": scenario.operation,
            "mode": mode,
        }
    )


async def _replay(
    scenario: ModelFixtureScenario,
) -> tuple[
    ModelPrLandingGithubCompleted | ModelPrLandingGithubFailed,
    FakeGithubLandingTransport,
]:
    transport = FakeGithubLandingTransport(scenario)
    assert isinstance(transport, ProtocolPrLandingGithubTransport)
    handler = HandlerPrLandingGithubEffect(transport)
    result = await handler.handle(_command(scenario))
    transport.assert_drained()
    return result, transport


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s.scenario)
async def test_every_scenario_replays_through_the_handler(
    scenario: ModelFixtureScenario,
) -> None:
    result, transport = await _replay(scenario)
    assert result.quota is not None
    assert result.quota.identity == _IDENTITY
    assert result.operation is scenario.operation
    if scenario.expected.outcome == "completed":
        assert isinstance(result, ModelPrLandingGithubCompleted), result
        assert result.not_modified is scenario.expected.not_modified
        assert result.requests == tuple(transport.sent)
        assert result.http_statuses == tuple(
            e.response.status for e in scenario.exchanges
        )
    else:
        assert isinstance(result, ModelPrLandingGithubFailed), result
        assert result.reason == scenario.expected.failure_reason
        if scenario.expected.retry_after_seconds is not None:
            assert result.retry_after_seconds == scenario.expected.retry_after_seconds


def test_every_operation_has_a_success_scenario() -> None:
    covered = {s.operation for s in _SCENARIOS if s.expected.outcome == "completed"}
    assert covered == set(EnumPrLandingGithubOperation)


@pytest.mark.parametrize(
    ("scenario_name", "operation", "reason"),
    [
        (
            "rerun_runs_403_secondary_rate_limit",
            EnumPrLandingGithubOperation.RERUN_RUNS,
            EnumPrLandingGithubFailureReason.SECONDARY_RATE_LIMIT,
        ),
        (
            "update_branch_422_conflict",
            EnumPrLandingGithubOperation.UPDATE_BRANCH,
            EnumPrLandingGithubFailureReason.UPDATE_BRANCH_CONFLICT,
        ),
        (
            "arm_auto_merge_disabled_repo",
            EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
            EnumPrLandingGithubFailureReason.AUTO_MERGE_NOT_ALLOWED,
        ),
    ],
)
async def test_named_acceptance_scenarios(
    scenario_name: str,
    operation: EnumPrLandingGithubOperation,
    reason: EnumPrLandingGithubFailureReason,
) -> None:
    scenario = load_scenario(scenario_name)
    assert scenario.operation is operation
    result, _ = await _replay(scenario)
    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is reason


async def test_the_conditional_check_read_answers_304_with_no_facts() -> None:
    scenario = load_scenario("read_head_checks_304")
    result, transport = await _replay(scenario)
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.not_modified is True
    assert result.http_statuses == (304,)
    assert result.check_runs == ()
    assert transport.sent[0].if_none_match == scenario.command["etag"]


def test_the_304_and_200_reads_are_real_recordings_of_one_etag() -> None:
    for first_name, second_name in (
        ("read_head_checks_200", "read_head_checks_304"),
        ("read_pr_state_200", "read_pr_state_304"),
    ):
        first = load_scenario(first_name)
        second = load_scenario(second_name)
        assert first.provenance == "recorded"
        assert second.provenance == "recorded"
        etag = first.exchanges[0].response.header("etag")
        assert etag is not None
        assert second.exchanges[0].request.if_none_match == etag
        assert second.exchanges[0].response.status == 304
        assert second.exchanges[0].response.body is None


def test_the_recorded_304_cost_no_quota() -> None:
    """The recorded 200 and 304 PR reads show the same remaining count."""
    first = load_scenario("read_pr_state_200").exchanges[0].response
    second = load_scenario("read_pr_state_304").exchanges[0].response
    assert first.header("x-ratelimit-remaining") == second.header(
        "x-ratelimit-remaining"
    )


def test_auto_merge_disabled_rests_on_a_recorded_policy_read() -> None:
    scenario = load_scenario("arm_auto_merge_disabled_repo")
    evidence = scenario.precondition_evidence
    assert evidence is not None
    assert evidence.provenance == "recorded"
    body = evidence.response.body
    assert isinstance(body, dict)
    assert body["data"]["repository"]["autoMergeAllowed"] is False  # type: ignore[index]


def test_mutation_scenarios_are_marked_documented() -> None:
    reads = {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
    for scenario in _SCENARIOS:
        if scenario.operation not in reads:
            assert scenario.provenance == "documented", scenario.scenario


async def test_strict_fake_refuses_a_drifted_request() -> None:
    scenario = load_scenario("update_branch_202")
    transport = FakeGithubLandingTransport(scenario)
    drifted = ModelGithubHttpRequest(
        method="PUT",
        path=scenario.exchanges[0].request.path,
        body={"expected_head_sha": "0" * 40},
        if_none_match=None,
    )
    with pytest.raises(FakeTransportMismatchError):
        await transport.send(drifted)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s.scenario)
async def test_dry_run_records_the_planned_requests_and_sends_nothing(
    scenario: ModelFixtureScenario,
) -> None:
    transport = FakeGithubLandingTransport(scenario)
    handler = HandlerPrLandingGithubEffect(transport)
    command = _command(scenario, EnumPrLandingGithubMode.DRY_RUN)
    result = await handler.handle(command)
    assert transport.sent == []
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.http_statuses == ()
    assert result.quota is None
    assert result.requests == command.to_http_requests()
    # The planned requests are the first recorded ones, in order (a refused
    # arm or enqueue recorded only its policy read, so compare the overlap).
    # An arm whose read finds the PR mergeable now merges instead of arming
    # (OMN-20866): its plan holds the arm, so only the read is compared.
    recorded = tuple(e.request for e in scenario.exchanges)
    overlap = min(len(recorded), len(result.requests))
    if scenario.scenario in _READ_PICKS_THE_MUTATION:
        overlap = 1
    assert overlap >= 1
    assert recorded[:overlap] == result.requests[:overlap]
