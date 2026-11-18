# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""commit_statuses: the statuses posted on a commit, read through the gateway.

The review gate posts its verdict as a commit status and reads it back with this
operation. The reads are proven on the recorded fake transport: the request is the
statuses endpoint of the exact head, the answer keeps each status's context, state and
link, and a request with no head is refused before any call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitStatusesResult,
    ModelGithubGatewayRequest,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    HEAD,
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
)

_BASE = f"/repos/{REPO}"


def _request(**kw: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest.model_validate(
        {"operation": EnumGithubGatewayOperation.COMMIT_STATUSES, "repo": REPO, **kw}
    )


def test_commit_statuses_reads_the_statuses_endpoint_of_the_head() -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(f"{_BASE}/commits/{HEAD}/statuses?per_page=30"),
                json_response(
                    [
                        {
                            "id": 9,
                            "context": "fable-review-gate",
                            "state": "success",
                            "target_url": "https://example.invalid/verdict",
                            "description": "clean",
                            "created_at": "2026-11-15T22:00:00Z",
                            "creator": {"login": "someone"},
                        },
                        {"id": 8, "context": "ci/other", "state": "pending"},
                    ]
                ),
            )
        ]
    )
    result = dispatch(_request(head_sha=HEAD, limit=30), transport)
    transport.assert_drained()
    assert isinstance(result, ModelCommitStatusesResult)
    assert result.head_sha == HEAD
    assert [(s.id, s.context, s.state) for s in result.statuses] == [
        (9, "fable-review-gate", "success"),
        (8, "ci/other", "pending"),
    ]
    assert result.statuses[0].target_url == "https://example.invalid/verdict"
    assert result.statuses[1].target_url is None


def test_commit_statuses_with_no_status_is_an_empty_list() -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(f"{_BASE}/commits/{HEAD}/statuses?per_page=30"),
                json_response([]),
            )
        ]
    )
    result = dispatch(_request(head_sha=HEAD, limit=30), transport)
    assert isinstance(result, ModelCommitStatusesResult)
    assert result.statuses == []
    assert result.truncated is False


def test_commit_statuses_refuses_a_request_with_no_head() -> None:
    with pytest.raises(ValidationError, match="requires head_sha"):
        _request()
