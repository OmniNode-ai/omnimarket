# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests: the write-node and gateway request shapes."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCheckRun,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.gateway_check_run_reader import (
    GatewayCheckRunReader,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.write_node_check_run_poster import (
    WriteNodeCheckRunPoster,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ReviewContextPortError,
)

pytestmark = pytest.mark.unit

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
REPO = "example-org/example-repo"
CHECK = "review-verdict / clean-head"


class FakeRoute:
    bus_lane = "lane"
    workspace_env = "WS"
    timeout_seconds = 5.0
    requested_by_lane = "lane"


class FakeWriteCall:
    def __init__(self) -> None:
        self.last_request: ModelRepoWriteRequest | None = None

    def __call__(self, request: ModelRepoWriteRequest) -> tuple[bool, dict[str, Any]]:
        self.last_request = request
        return (
            False,
            ModelRepoWriteCompleted(
                correlation_id="abc12345-0000-0000-0000-000000000000",
                operation=EnumRepoWriteOperation.CHECK_RUN_CREATE,
                repo=REPO,
                mode="enforce",
                idempotency_key="k",
                requested_by_lane="lane",
                head_sha=HEAD,
                check_run_id=42,
            ).model_dump(),
        )


class FakeGatewayCall:
    def __init__(self) -> None:
        self.last_request: ModelGithubGatewayRequest | None = None

    def __call__(
        self, request: ModelGithubGatewayRequest
    ) -> tuple[bool, dict[str, Any]]:
        self.last_request = request
        return (
            False,
            ModelCommitCheckRunsResult(
                truncated=False,
                repo=REPO,
                head_sha=HEAD,
                check_runs=(
                    ModelCheckRun(
                        id=7,
                        name=CHECK,
                        status="completed",
                        conclusion="failure",
                    ),
                ),
            ).model_dump(),
        )


def test_poster_builds_valid_request() -> None:
    call = FakeWriteCall()
    poster = WriteNodeCheckRunPoster(FakeRoute(), call)
    poster.post(
        correlation_id="abc12345-0000-0000-0000-000000000000",
        repository=REPO,
        head_sha=HEAD,
        check_name=CHECK,
        conclusion="failure",
        title="Review verdict",
        summary="ok",
    )
    assert call.last_request is not None
    req = call.last_request
    assert req.operation == EnumRepoWriteOperation.CHECK_RUN_CREATE
    assert req.status == "completed"
    assert req.conclusion == "failure"
    assert req.name == CHECK
    assert req.head_sha == HEAD


def test_poster_failed_terminal_raises() -> None:
    class FailedCall:
        def __call__(
            self, request: ModelRepoWriteRequest
        ) -> tuple[bool, dict[str, Any]]:
            return True, ModelRepoWriteFailed(
                correlation_id="abc12345-0000-0000-0000-000000000000",
                operation=EnumRepoWriteOperation.CHECK_RUN_CREATE,
                reason="quota_floor",
                mode="enforce",
                repo=REPO,
                idempotency_key="k",
                requested_by_lane="lane",
                detail="quota floor",
            ).model_dump()

    poster = WriteNodeCheckRunPoster(FakeRoute(), FailedCall())
    with pytest.raises(ReviewContextPortError):
        poster.post(
            correlation_id="abc12345-0000-0000-0000-000000000000",
            repository=REPO,
            head_sha=HEAD,
            check_name=CHECK,
            conclusion="failure",
            title="Review verdict",
            summary="ok",
        )


def test_reader_builds_valid_request() -> None:
    call = FakeGatewayCall()
    reader = GatewayCheckRunReader(FakeRoute(), call)
    runs = reader.read(repository=REPO, head_sha=HEAD, check_name=CHECK)
    assert call.last_request is not None
    req = call.last_request
    assert req.operation == EnumGithubGatewayOperation.COMMIT_CHECK_RUNS
    assert req.repo == REPO
    assert req.head_sha == HEAD
    assert req.check_name == CHECK
    assert len(runs) == 1
    assert runs[0].check_run_id == 7
    assert runs[0].conclusion == "failure"
