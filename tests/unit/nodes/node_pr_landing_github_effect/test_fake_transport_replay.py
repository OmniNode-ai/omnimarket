# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-19826): the fake transport replays a recorded response per operation.

Every scenario is replayed end to end: the command in the fixture is turned
into HTTP requests by the node's own request-shape seam, each request is sent
through the strict fake transport, and the recorded responses are classified
and folded into the typed completed or failed result. A request-shape drift in
the seam makes the strict fake raise.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubCheckRunFact,
    ModelGithubHttpRequest,
    ModelGithubPrStateFact,
    ModelGithubQuotaReading,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
    classify_github_response,
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

_CORRELATION = UUID("00000000-0000-4000-8000-000000019826")
_IDENTITY = "GITHUB_TOKEN"
_SCENARIOS = all_scenarios()


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
) -> ModelPrLandingGithubCompleted | ModelPrLandingGithubFailed:
    """Drive one scenario through the seam the wave-2 handler will use."""
    command = _command(scenario)
    transport = FakeGithubLandingTransport(scenario)
    assert isinstance(transport, ProtocolPrLandingGithubTransport)
    statuses: list[int] = []
    last_quota: ModelGithubQuotaReading | None = None
    check_runs: tuple[ModelGithubCheckRunFact, ...] = ()
    pr_state: ModelGithubPrStateFact | None = None
    etag: str | None = None
    not_modified = False
    for request in command.to_http_requests():
        response = await transport.send(request)
        statuses.append(response.status)
        last_quota = ModelGithubQuotaReading.from_response_headers(
            response.headers, identity=_IDENTITY
        )
        reason = classify_github_response(command.operation, response)
        if reason is not None:
            transport.assert_drained()
            return ModelPrLandingGithubFailed(
                correlation_id=command.correlation_id,
                operation=command.operation,
                mode=command.mode,
                repository=command.repository,
                pr_number=command.pr_number,
                head_sha=command.head_sha,
                reason=reason,
                detail=response.message(),
                http_status=response.status,
                retry_after_seconds=response.retry_after_seconds(),
                quota=last_quota,
            )
        if command.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            etag = response.header("etag") or command.etag
            not_modified = response.status == 304
            if not not_modified:
                check_runs = ModelGithubCheckRunFact.from_check_runs_body(response.body)
        if command.operation is EnumPrLandingGithubOperation.READ_PR_STATE:
            etag = response.header("etag") or command.etag
            not_modified = response.status == 304
            if not not_modified:
                pr_state = ModelGithubPrStateFact.from_pull_body(response.body)
    transport.assert_drained()
    return ModelPrLandingGithubCompleted(
        correlation_id=command.correlation_id,
        operation=command.operation,
        mode=command.mode,
        repository=command.repository,
        pr_number=command.pr_number,
        head_sha=command.head_sha,
        requests=command.to_http_requests(),
        http_statuses=tuple(statuses),
        not_modified=not_modified,
        etag=etag,
        check_runs=check_runs,
        pr_state=pr_state,
        quota=last_quota,
    )


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s.scenario)
async def test_every_scenario_replays_and_classifies(
    scenario: ModelFixtureScenario,
) -> None:
    result = await _replay(scenario)
    assert result.quota is not None
    assert result.quota.identity == _IDENTITY
    if scenario.expected.outcome == "completed":
        assert isinstance(result, ModelPrLandingGithubCompleted)
        assert result.not_modified is scenario.expected.not_modified
    else:
        assert isinstance(result, ModelPrLandingGithubFailed)
        assert result.reason == scenario.expected.failure_reason
        if scenario.expected.retry_after_seconds is not None:
            assert result.retry_after_seconds == scenario.expected.retry_after_seconds


def test_every_operation_has_a_success_scenario() -> None:
    covered = {s.operation for s in _SCENARIOS if s.expected.outcome == "completed"}
    assert covered == set(EnumPrLandingGithubOperation)


@pytest.mark.parametrize(
    ("scenario_name", "operation", "check"),
    [
        ("read_head_checks_304", EnumPrLandingGithubOperation.READ_HEAD_CHECKS, 304),
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
    check: object,
) -> None:
    scenario = load_scenario(scenario_name)
    assert scenario.operation is operation
    result = await _replay(scenario)
    if check == 304:
        assert isinstance(result, ModelPrLandingGithubCompleted)
        assert result.not_modified is True
        assert result.http_statuses == (304,)
        assert result.check_runs == ()
        # The conditional request carried the ETag from the 200 recording.
        assert result.requests[0].if_none_match == scenario.command["etag"]
    else:
        assert isinstance(result, ModelPrLandingGithubFailed)
        assert result.reason is check


def test_the_304_and_200_reads_are_real_recordings_of_one_etag() -> None:
    first = load_scenario("read_head_checks_200")
    second = load_scenario("read_head_checks_304")
    assert first.provenance == "recorded"
    assert second.provenance == "recorded"
    etag = first.exchanges[0].response.header("etag")
    assert etag is not None
    assert second.exchanges[0].request.if_none_match == etag
    assert second.exchanges[0].response.status == 304
    assert second.exchanges[0].response.body is None


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


def test_dry_run_command_builds_requests_without_a_transport() -> None:
    scenario = load_scenario("update_branch_202")
    command = _command(scenario, EnumPrLandingGithubMode.DRY_RUN)
    transport = FakeGithubLandingTransport(scenario)
    result = ModelPrLandingGithubCompleted(
        correlation_id=command.correlation_id,
        operation=command.operation,
        mode=command.mode,
        repository=command.repository,
        pr_number=command.pr_number,
        head_sha=command.head_sha,
        requests=command.to_http_requests(),
        http_statuses=(),
        not_modified=False,
        etag=None,
        check_runs=(),
        quota=None,
    )
    assert transport.sent == []
    assert result.requests == tuple(e.request for e in scenario.exchanges)


async def test_read_pr_state_200_and_304_are_real_recordings_of_one_etag() -> None:
    """OMN-19829: the snapshot read the landing orchestrator orders (plan 5.1 rev 1, s6)."""
    first = load_scenario("read_pr_state_200")
    second = load_scenario("read_pr_state_304")
    assert first.provenance == "recorded"
    assert second.provenance == "recorded"
    etag = first.exchanges[0].response.header("etag")
    assert etag is not None
    assert second.exchanges[0].request.if_none_match == etag
    read = await _replay(first)
    assert isinstance(read, ModelPrLandingGithubCompleted)
    assert read.pr_state is not None
    assert read.pr_state.pr_number == 2984
    assert read.pr_state.state == "open"
    assert read.pr_state.draft is False
    assert read.pr_state.auto_merge_enabled is True
    assert read.pr_state.head_sha == first.command["head_sha"]
    unchanged = await _replay(second)
    assert isinstance(unchanged, ModelPrLandingGithubCompleted)
    assert unchanged.not_modified is True
    assert unchanged.pr_state is None
