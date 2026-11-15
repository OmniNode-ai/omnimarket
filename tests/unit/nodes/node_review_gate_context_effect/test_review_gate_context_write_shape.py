# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests: the review gate posts the fable-review-gate commit status, never a check run.

The write node's token cannot create a check run (GitHub answers 403 and asks for an App), and can post a commit
status. These tests pin the request shapes the gate sends to the write node and the gateway, and prove a gate run
over a write node that refuses check runs ends posted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitStatus,
    ModelCommitStatusesResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
    ModelReviewGateContextDecision,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.gateway_commit_status_reader import (
    GatewayCommitStatusReader,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.write_node_commit_status_poster import (
    WriteNodeCommitStatusPoster,
)
from omnimarket.nodes.node_review_gate_context_effect.handlers.handler_review_gate_context_effect import (
    HandlerReviewGateContextEffect,
)
from omnimarket.nodes.node_review_gate_context_effect.models import (
    EnumReviewContextPostOutcome,
    ModelReviewContextPostCommand,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)

pytestmark = pytest.mark.unit

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
REPO = "example-org/example-repo"
CONTEXT = "fable-review-gate"
LINK = f"https://github.com/{REPO}/pull/12"
CORRELATION = "abc12345-0000-0000-0000-000000000000"


class FakeRoute:
    bus_lane = "lane"
    workspace_env = "WS"
    timeout_seconds = 5.0
    requested_by_lane = "lane"


def _completed(operation: EnumRepoWriteOperation, **extra: Any) -> dict[str, Any]:
    return ModelRepoWriteCompleted(
        correlation_id=CORRELATION,
        operation=operation,
        repo=REPO,
        mode="enforce",
        idempotency_key="k",
        requested_by_lane="lane",
        head_sha=HEAD,
        **extra,
    ).model_dump()


class FakeWriteCall:
    def __init__(self) -> None:
        self.last_request: ModelRepoWriteRequest | None = None

    def __call__(self, request: ModelRepoWriteRequest) -> tuple[bool, dict[str, Any]]:
        self.last_request = request
        return False, _completed(
            EnumRepoWriteOperation.COMMIT_STATUS_CREATE, commit_status_id=42
        )


class TokenOnlyWriteCall:
    """A write node whose credential is a token: it refuses a check run with 403 and accepts a commit status."""

    def __init__(self) -> None:
        self.operations: list[EnumRepoWriteOperation] = []
        self.statuses: list[ModelCommitStatus] = []

    def __call__(self, request: ModelRepoWriteRequest) -> tuple[bool, dict[str, Any]]:
        self.operations.append(request.operation)
        if request.operation is EnumRepoWriteOperation.CHECK_RUN_CREATE:
            return True, ModelRepoWriteFailed(
                correlation_id=CORRELATION,
                operation=request.operation,
                reason="github_refused",
                mode="enforce",
                repo=REPO,
                idempotency_key="k",
                requested_by_lane="lane",
                detail="You must authenticate via a GitHub App",
                http_status=403,
            ).model_dump()
        assert request.operation is EnumRepoWriteOperation.COMMIT_STATUS_CREATE
        self.statuses.append(
            ModelCommitStatus(
                id=100 + len(self.statuses),
                context=request.status_context or "",
                state=request.status_state or "",
                target_url=request.status_target_url,
            )
        )
        return False, _completed(
            request.operation, commit_status_id=self.statuses[-1].id
        )


class StatusesGatewayCall:
    def __init__(self, statuses: list[ModelCommitStatus]) -> None:
        self.statuses = statuses
        self.requests: list[ModelGithubGatewayRequest] = []

    def __call__(
        self, request: ModelGithubGatewayRequest
    ) -> tuple[bool, dict[str, Any]]:
        self.requests.append(request)
        return False, ModelCommitStatusesResult(
            truncated=False,
            repo=REPO,
            head_sha=HEAD,
            statuses=list(reversed(self.statuses)),
        ).model_dump()


class _Sink(ProtocolEmissionSink):
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def append(self, row: Any) -> None:
        self.rows.append(row)


def _decision(state: EnumReviewContextState) -> ModelReviewGateContextDecision:
    clean = state is EnumReviewContextState.SUCCESS
    return ModelReviewGateContextDecision(
        repository=REPO,
        pr_number=12,
        head_sha=HEAD,
        check_name=CONTEXT,
        state=state,
        reason=(
            EnumReviewContextReason.CLEAN_ON_HEAD
            if clean
            else EnumReviewContextReason.REVIEW_MISSING
        ),
        bypass_class=None,
        title="Review verdict for the head",
        summary="summary",
    )


def test_review_gate_status_poster_builds_a_commit_status_request() -> None:
    call = FakeWriteCall()
    poster = WriteNodeCommitStatusPoster(FakeRoute(), call)
    posted = poster.post(
        correlation_id=CORRELATION,
        repository=REPO,
        head_sha=HEAD,
        context=CONTEXT,
        state="failure",
        target_url=LINK,
        description="Review verdict",
    )
    assert posted == 42
    req = call.last_request
    assert req is not None
    assert req.operation == EnumRepoWriteOperation.COMMIT_STATUS_CREATE
    assert req.operation != EnumRepoWriteOperation.CHECK_RUN_CREATE
    assert (req.status_context, req.status_state) == (CONTEXT, "failure")
    assert req.commit_sha == HEAD
    assert req.status_target_url == LINK
    assert req.name is None
    assert req.conclusion is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "pending"},
        {"context": "ci/other"},
        {"head_sha": ""},
        {"target_url": "not-a-link"},
    ],
    ids=["state", "context", "empty-sha", "link"],
)
def test_review_gate_status_poster_refuses_before_any_call(
    overrides: dict[str, str],
) -> None:
    call = FakeWriteCall()
    poster = WriteNodeCommitStatusPoster(FakeRoute(), call)
    args = {
        "correlation_id": CORRELATION,
        "repository": REPO,
        "head_sha": HEAD,
        "context": CONTEXT,
        "state": "success",
        "target_url": LINK,
        "description": "d",
    }
    args.update(overrides)
    with pytest.raises(ReviewContextPortError, match="refused before any call"):
        poster.post(**args)
    assert call.last_request is None


def test_review_gate_status_poster_failed_terminal_raises() -> None:
    class FailedCall:
        def __call__(
            self, request: ModelRepoWriteRequest
        ) -> tuple[bool, dict[str, Any]]:
            return True, ModelRepoWriteFailed(
                correlation_id=CORRELATION,
                operation=EnumRepoWriteOperation.COMMIT_STATUS_CREATE,
                reason="quota_floor",
                mode="enforce",
                repo=REPO,
                idempotency_key="k",
                requested_by_lane="lane",
                detail="quota floor",
            ).model_dump()

    poster = WriteNodeCommitStatusPoster(FakeRoute(), FailedCall())
    with pytest.raises(ReviewContextPortError, match="quota_floor"):
        poster.post(
            correlation_id=CORRELATION,
            repository=REPO,
            head_sha=HEAD,
            context=CONTEXT,
            state="failure",
            target_url=LINK,
            description="d",
        )


def test_review_gate_status_reader_builds_a_commit_statuses_request() -> None:
    call = StatusesGatewayCall(
        [
            ModelCommitStatus(id=7, context=CONTEXT, state="failure"),
            ModelCommitStatus(id=8, context="ci/other", state="success"),
        ]
    )
    reader = GatewayCommitStatusReader(FakeRoute(), call)
    statuses = reader.read(repository=REPO, head_sha=HEAD, context=CONTEXT)
    req = call.requests[0]
    assert req.operation == EnumGithubGatewayOperation.COMMIT_STATUSES
    assert (req.repo, req.head_sha) == (REPO, HEAD)
    assert [(s.status_id, s.context, s.state) for s in statuses] == [
        (7, CONTEXT, "failure")
    ]


@pytest.mark.parametrize(
    "state", [EnumReviewContextState.SUCCESS, EnumReviewContextState.FAILURE]
)
def test_review_gate_status_ends_posted_over_a_token_only_write_node(
    state: EnumReviewContextState,
) -> None:
    """A write node that 403s every check run still ends posted: the gate posts a commit status and no check run."""
    write = TokenOnlyWriteCall()
    gateway = StatusesGatewayCall(write.statuses)
    route = FakeRoute()
    handler = HandlerReviewGateContextEffect(
        reader=GatewayCommitStatusReader(route, gateway),
        poster=WriteNodeCommitStatusPoster(route, write),
        sink=_Sink(),
        emission_node="node_review_gate_context_effect",
        emission_piece=1,
        emission_backend="journal",
        route_backend="bus",
        clock=lambda: datetime(2026, 11, 15, tzinfo=UTC),
    )
    result = handler.handle(
        ModelReviewContextPostCommand(
            correlation_id=CORRELATION, decision=_decision(state)
        )
    )
    assert result.outcome is EnumReviewContextPostOutcome.POSTED
    assert result.status_id == 100
    assert write.operations == [EnumRepoWriteOperation.COMMIT_STATUS_CREATE]
    assert EnumRepoWriteOperation.CHECK_RUN_CREATE not in write.operations
    assert [(s.context, s.state) for s in write.statuses] == [(CONTEXT, state.value)]
    assert write.statuses[0].target_url == LINK
    again = handler.handle(
        ModelReviewContextPostCommand(
            correlation_id=CORRELATION, decision=_decision(state)
        )
    )
    assert again.outcome is EnumReviewContextPostOutcome.UNCHANGED
    assert len(write.statuses) == 1
