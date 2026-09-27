# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLandingGithubEffect rules (OMN-19831, plan task T9).

- P1 and R4: an arm or enqueue reads draft, hold, head and the live merge
  policy in one call before its mutation, sends no mutation on any refusal, and
  the mutation it does send carries the expected head.
- F7: check reads carry each failed copy's run attempt; a re-run reports the
  attempt it started.
- Quota: readings come from response headers; a resource under the contract
  floor is refused before the next call, until its window resets.
- Conditional reads: read_pr_state and read_head_checks send the ETag and a 304
  carries no facts.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from uuid import UUID

import pytest

from omnimarket.github_landing.github_landing_requests import (
    ENABLE_AUTO_MERGE_AT_HEAD_MUTATION,
    ENQUEUE_AT_HEAD_MUTATION,
    LANDING_POLICY_QUERY,
    head_check_runs_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubRunAttempt,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    load_scenario,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("00000000-0000-4000-8000-000000019831")
_HEAD = "c5dea513fd94a0e940938f13a798c685ab7a88cb"
_NODE = "PR_kwDOR6jjtc8AAAABFQhbqA"
_REPO = "OmniNode-ai/omnimarket"


def _cmd(**kw: object) -> ModelPrLandingGithubRequest:
    data: dict[str, object] = {
        "correlation_id": _CORRELATION,
        "mode": EnumPrLandingGithubMode.ENFORCE,
        "repository": _REPO,
        "pr_number": 2962,
        "head_sha": _HEAD,
    }
    data.update(kw)
    return ModelPrLandingGithubRequest.model_validate(data)


def _scenario_cmd(name: str) -> ModelPrLandingGithubRequest:
    scenario = load_scenario(name)
    return ModelPrLandingGithubRequest.model_validate(
        {
            **scenario.command,
            "correlation_id": _CORRELATION,
            "operation": scenario.operation,
            "mode": EnumPrLandingGithubMode.ENFORCE,
        }
    )


async def _run_scenario(
    name: str,
) -> tuple[
    ModelPrLandingGithubCompleted | ModelPrLandingGithubFailed,
    FakeGithubLandingTransport,
]:
    transport = FakeGithubLandingTransport.for_scenario(name)
    result = await HandlerPrLandingGithubEffect(transport).handle(_scenario_cmd(name))
    transport.assert_drained()
    return result, transport


def _headers(
    remaining: int, resource: str = "core", reset: int = 2_000
) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(reset),
        "x-ratelimit-resource": resource,
    }


class _ScriptedTransport:
    """Answers each request with the next scripted response, recording what was sent."""

    def __init__(self, responses: Iterator[ModelGithubHttpResponse]) -> None:
        self._responses = responses
        self.sent: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        return next(self._responses)


# --- arm and enqueue: P1 and R4 ------------------------------------------------


async def test_arm_reads_first_then_sends_the_mutation_at_the_expected_head() -> None:
    result, transport = await _run_scenario("arm_auto_merge_200")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    policy_read, mutation = transport.sent
    assert policy_read.body is not None
    assert policy_read.body["query"] == LANDING_POLICY_QUERY
    assert mutation.body is not None
    assert mutation.body["query"] == ENABLE_AUTO_MERGE_AT_HEAD_MUTATION
    variables = mutation.body["variables"]
    assert isinstance(variables, dict)
    assert variables["head"] == result.head_sha
    # The fact the arm acted on is the recorded read.
    assert result.pr_state is not None
    assert result.pr_state.head_sha == result.head_sha
    assert result.pr_state.draft is False
    assert result.pr_state.auto_merge_allowed is True
    assert result.pr_state.merge_queue_enabled is False


async def test_enqueue_reads_first_then_enqueues_at_the_expected_head() -> None:
    result, transport = await _run_scenario("enqueue_200")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    mutation = transport.sent[1]
    assert mutation.body is not None
    assert mutation.body["query"] == ENQUEUE_AT_HEAD_MUTATION
    assert mutation.body["variables"] == {"id": _NODE, "head": _HEAD}


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("arm_auto_merge_refused_draft", EnumPrLandingGithubFailureReason.DRAFT),
        ("arm_auto_merge_refused_hold_label", EnumPrLandingGithubFailureReason.HELD),
        ("arm_auto_merge_refused_hold_title", EnumPrLandingGithubFailureReason.HELD),
        (
            "arm_auto_merge_refused_head_moved",
            EnumPrLandingGithubFailureReason.HEAD_MOVED,
        ),
        ("arm_auto_merge_refused_closed", EnumPrLandingGithubFailureReason.PR_NOT_OPEN),
        ("arm_auto_merge_refused_merged", EnumPrLandingGithubFailureReason.PR_NOT_OPEN),
        (
            "arm_auto_merge_refused_merge_queue",
            EnumPrLandingGithubFailureReason.MERGE_QUEUE_REQUIRED,
        ),
        (
            "arm_auto_merge_disabled_repo",
            EnumPrLandingGithubFailureReason.AUTO_MERGE_NOT_ALLOWED,
        ),
        (
            "enqueue_refused_no_merge_queue",
            EnumPrLandingGithubFailureReason.MERGE_QUEUE_NOT_ENABLED,
        ),
        ("enqueue_refused_draft", EnumPrLandingGithubFailureReason.DRAFT),
    ],
)
async def test_a_refused_arm_or_enqueue_sends_no_mutation(
    scenario: str, reason: EnumPrLandingGithubFailureReason
) -> None:
    result, transport = await _run_scenario(scenario)
    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is reason
    assert len(transport.sent) == 1
    only = transport.sent[0].body
    assert only is not None
    assert only["query"] == LANDING_POLICY_QUERY
    # The refusal carries the state it judged, for the orchestrator's snapshot.
    assert result.pr_state is not None
    assert result.http_status is None
    assert result.quota is not None


def _policy(
    *, draft: bool, labels: tuple[str, ...], title: str
) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(
        status=200,
        headers=_headers(4000, "graphql"),
        body={
            "data": {
                "repository": {
                    "autoMergeAllowed": True,
                    "pullRequest": {
                        "id": _NODE,
                        "number": 2962,
                        "headRefOid": _HEAD,
                        "baseRefName": "dev",
                        "isDraft": draft,
                        "title": title,
                        "state": "OPEN",
                        "merged": False,
                        "isMergeQueueEnabled": False,
                        "isInMergeQueue": False,
                        "labels": {"nodes": [{"name": n} for n in labels]},
                        "autoMergeRequest": None,
                    },
                }
            }
        },
    )


_MUTATION_OK = ModelGithubHttpResponse(
    status=200,
    headers=_headers(3999, "graphql"),
    body={"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 2962}}}},
)


@pytest.mark.parametrize(
    ("draft", "labels", "title"),
    list(
        itertools.product(
            (False, True),
            ((), ("do-not-merge",), ("needs-review",), ("WIP",)),
            ("feat: a change", "[WIP] feat: a change", "DNM feat: a change"),
        )
    ),
)
async def test_p1_the_arm_mutation_is_sent_only_when_neither_draft_nor_held(
    draft: bool, labels: tuple[str, ...], title: str
) -> None:
    transport = _ScriptedTransport(
        iter((_policy(draft=draft, labels=labels, title=title), _MUTATION_OK))
    )
    result = await HandlerPrLandingGithubEffect(transport).handle(
        _cmd(operation=EnumPrLandingGithubOperation.ARM_AUTO_MERGE, pr_node_id=_NODE)
    )
    held = bool({"do-not-merge", "WIP"} & set(labels)) or title != "feat: a change"
    mutation_sent = len(transport.sent) == 2
    assert mutation_sent is (not draft and not held)
    assert isinstance(result, ModelPrLandingGithubCompleted) is mutation_sent


async def test_a_github_refusal_of_the_expected_head_is_head_moved() -> None:
    result, transport = await _run_scenario("arm_auto_merge_mutation_head_moved")
    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.HEAD_MOVED
    assert len(transport.sent) == 2


def test_arm_and_enqueue_require_the_expected_head() -> None:
    for op in (
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
    ):
        with pytest.raises(ValueError, match="head_sha"):
            _cmd(operation=op, pr_node_id=_NODE, head_sha=None)


# --- read_pr_state --------------------------------------------------------------


async def test_read_pr_state_reports_the_recorded_facts() -> None:
    result, _ = await _run_scenario("read_pr_state_200")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    state = result.pr_state
    assert state is not None
    assert state.pr_number == 2984
    assert state.head_sha == "8635161156cf090a13af1a08ee53bdf434baefbf"
    assert state.base_ref == "dev"
    assert state.state == "open"
    assert state.draft is False
    assert state.merged is False
    assert state.auto_merge_armed is True
    assert state.auto_merge_method == "SQUASH"
    assert state.labels == ()
    assert result.etag is not None
    assert result.head_sha is None  # asked without a head, as the autobind prompt does


async def test_read_pr_state_304_carries_no_state() -> None:
    result, transport = await _run_scenario("read_pr_state_304")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.not_modified is True
    assert result.pr_state is None
    assert transport.sent[0].if_none_match is not None


# --- run attempts (F7) ------------------------------------------------------------


async def test_failed_copies_carry_the_attempt_that_produced_them() -> None:
    result, transport = await _run_scenario("read_head_checks_failed_run_attempts")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    by_id = {f.check_run_id: f for f in result.check_runs}
    # The re-run check: attempt 1 was cancelled, attempt 2 failed.
    assert by_id[108564410510].conclusion == "cancelled"
    assert by_id[108564410510].run_attempt == 1
    assert by_id[108564738808].conclusion == "failure"
    assert by_id[108564738808].run_attempt == 2
    assert {f.run_id for f in result.check_runs} == {36299500318}
    # One jobs read for the one run with a failed copy.
    assert len(transport.sent) == 2
    assert "/actions/runs/36299500318/jobs" in transport.sent[1].path


async def test_a_green_head_reads_no_jobs() -> None:
    result, transport = await _run_scenario("read_head_checks_200")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert len(transport.sent) == 1
    assert result.check_runs
    assert all(f.run_attempt is None for f in result.check_runs)
    assert all(f.run_id is not None for f in result.check_runs)


async def test_a_rerun_reports_the_attempt_it_started() -> None:
    result, _ = await _run_scenario("rerun_runs_201")
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert result.started_attempts == (
        ModelGithubRunAttempt(run_id=36278000979, run_attempt=2),
        ModelGithubRunAttempt(run_id=36278000786, run_attempt=2),
    )


def _check_run(check_id: int) -> dict[str, object]:
    return {
        "id": check_id,
        "name": f"check {check_id}",
        "status": "completed",
        "conclusion": "success",
        "head_sha": _HEAD,
        "details_url": f"https://github.com/{_REPO}/actions/runs/7/job/{check_id}",
        "app": {"slug": "github-actions"},
        "check_suite": {"id": 1},
    }


async def test_a_full_check_runs_page_reads_the_next_page() -> None:
    page_one = ModelGithubHttpResponse(
        status=200,
        headers={**_headers(4000), "etag": 'W/"one"'},
        body={"total_count": 101, "check_runs": [_check_run(i) for i in range(1, 101)]},
    )
    page_two = ModelGithubHttpResponse(
        status=200,
        headers=_headers(3999),
        body={"total_count": 101, "check_runs": [_check_run(101)]},
    )
    transport = _ScriptedTransport(iter((page_one, page_two)))
    result = await HandlerPrLandingGithubEffect(transport).handle(
        _cmd(operation=EnumPrLandingGithubOperation.READ_HEAD_CHECKS)
    )
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert len(result.check_runs) == 101
    assert transport.sent[1] == head_check_runs_request(_REPO, _HEAD, page=2)
    assert result.etag == 'W/"one"'


# --- quota ---------------------------------------------------------------------------


def _ok_update_branch(remaining: int, reset: int = 2_000) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(
        status=202, headers=_headers(remaining, reset=reset), body={"message": "ok"}
    )


async def test_a_reading_under_the_floor_refuses_the_next_call_without_sending() -> (
    None
):
    transport = _ScriptedTransport(iter((_ok_update_branch(120),)))
    handler = HandlerPrLandingGithubEffect(transport, clock=lambda: 1_000.0)
    command = _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH)
    first = await handler.handle(command)
    assert isinstance(first, ModelPrLandingGithubCompleted)
    assert first.quota is not None
    assert first.quota.remaining == 120
    second = await handler.handle(command)
    assert isinstance(second, ModelPrLandingGithubFailed)
    assert second.reason is EnumPrLandingGithubFailureReason.QUOTA_FLOOR
    assert second.http_status is None
    assert second.quota == first.quota
    assert len(transport.sent) == 1


async def test_the_floor_lifts_when_the_reading_window_resets() -> None:
    now = [1_000.0]
    transport = _ScriptedTransport(
        iter((_ok_update_branch(120, reset=1_500), _ok_update_branch(4_999)))
    )
    handler = HandlerPrLandingGithubEffect(transport, clock=lambda: now[0])
    command = _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH)
    await handler.handle(command)
    now[0] = 1_500.0
    second = await handler.handle(command)
    assert isinstance(second, ModelPrLandingGithubCompleted)
    assert len(transport.sent) == 2


async def test_the_floor_is_per_resource() -> None:
    """A low core reading does not block a GraphQL call."""
    transport = _ScriptedTransport(
        iter(
            (
                _ok_update_branch(120),
                _policy(draft=False, labels=(), title="feat: a change"),
                _MUTATION_OK,
            )
        )
    )
    handler = HandlerPrLandingGithubEffect(transport, clock=lambda: 1_000.0)
    await handler.handle(_cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH))
    armed = await handler.handle(
        _cmd(operation=EnumPrLandingGithubOperation.ARM_AUTO_MERGE, pr_node_id=_NODE)
    )
    assert isinstance(armed, ModelPrLandingGithubCompleted)


async def test_a_response_without_quota_headers_is_refused_not_defaulted() -> None:
    transport = _ScriptedTransport(
        iter((ModelGithubHttpResponse(status=202, headers={}, body=None),))
    )
    result = await HandlerPrLandingGithubEffect(transport).handle(
        _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH)
    )
    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.TRANSPORT_ERROR
    assert result.quota is None


async def test_no_response_is_a_transport_error() -> None:
    class _Down:
        async def send(
            self, request: ModelGithubHttpRequest
        ) -> ModelGithubHttpResponse:
            raise GithubLandingTransportError(
                f"{request.path}: no response (timed out)"
            )

    result = await HandlerPrLandingGithubEffect(_Down()).handle(
        _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH)
    )
    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.TRANSPORT_ERROR
    assert result.http_status is None


# --- dry_run -------------------------------------------------------------------------


async def test_dry_run_arm_plans_the_read_and_the_mutation_and_sends_neither() -> None:
    transport = _ScriptedTransport(iter(()))
    command = _cmd(
        operation=EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        pr_node_id=_NODE,
        mode=EnumPrLandingGithubMode.DRY_RUN,
    )
    result = await HandlerPrLandingGithubEffect(transport).handle(command)
    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert transport.sent == []
    assert len(result.requests) == 2
    assert result.quota is None
