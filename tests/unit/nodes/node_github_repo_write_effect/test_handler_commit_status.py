# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""commit_status_create on a recorded fake transport.

A commit status is what the write node's credential can post where it cannot create a
check run. Each scenario replays a strict, ordered list of exchanges, so the
POST /repos/{o}/{r}/statuses/{sha} shape is pinned. The refusals are planted: any
state but success or failure, any context but the review gate's, and an empty, short
or missing sha are refused by the request model before any call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    planned_requests,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_SHA = "38b569c4ee5443fd8e3ddfc6cb11653032c08c41"
_URL = "https://example.invalid/verdict/38b569c4ee54"
_NOW = 1_791_000_000.0
_CORRELATION = UUID("00000000-0000-4000-8000-000000000009")


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"no recorded exchange left for {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _handler(transport: _Recorded) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        clock=lambda: _NOW,
    )


def _command(**overrides: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": "commit_status_create",
            "repo": _REPO,
            "idempotency_key": "commit-status-24638",
            "requested_by_lane": "gate-status-2226",
            "commit_sha": _SHA,
            "status_context": "fable-review-gate",
            "status_state": "success",
            "status_target_url": _URL,
            "status_description": "Fable review clean for the head",
            **overrides,
        }
    )


def _post(state: str = "success") -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="POST",
        path=f"/repos/{_REPO}/statuses/{_SHA}",
        body={
            "state": state,
            "context": "fable-review-gate",
            "target_url": _URL,
            "description": "Fable review clean for the head",
        },
    )


def _created(state: str = "success") -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(
        status=201,
        headers=_headers(),
        body={
            "id": 77,
            "state": state,
            "context": "fable-review-gate",
            "url": f"https://example.invalid/repos/{_REPO}/statuses/{_SHA}",
        },
    )


@pytest.mark.parametrize("state", ["success", "failure"])
async def test_commit_status_posts_the_statuses_endpoint_and_replays(
    state: str,
) -> None:
    transport = _Recorded([(_post(state), _created(state))])
    handler = _handler(transport)
    command = _command(status_state=state)
    result = await handler.handle(command)
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.commit_status_id == 77
    assert result.head_sha == _SHA
    assert result.check_run_id is None
    assert result.http_statuses == (201,)
    assert transport.sent == [_post(state)]
    replay = await handler.handle(
        command.model_copy(update={"correlation_id": UUID(int=1)})
    )
    assert isinstance(replay, ModelRepoWriteCompleted)
    assert replay.replayed
    assert len(transport.sent) == 1


def test_commit_status_uses_the_statuses_endpoint_and_never_check_runs() -> None:
    planned = planned_requests(_command())
    assert planned == (_post(),)
    assert planned[0].path == f"/repos/{_REPO}/statuses/{_SHA}"
    assert "check-runs" not in planned[0].path


async def test_commit_status_dry_run_sends_nothing() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(_command(dry_run=True))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (_post(),)
    assert transport.sent == []


async def test_commit_status_github_refusal_is_typed() -> None:
    refused = ModelGithubHttpResponse(
        status=403, headers=_headers(), body={"message": "Resource not accessible"}
    )
    result = await _handler(_Recorded([(_post(), refused)])).handle(_command())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.http_statuses == (403,)


@pytest.mark.parametrize("state", ["pending", "error", "", "SUCCESS", "neutral"])
def test_commit_status_refuses_any_other_state(state: str) -> None:
    with pytest.raises(ValidationError):
        _command(status_state=state)


@pytest.mark.parametrize("context", ["CI Summary", "fable-review-gate ", "", "other"])
def test_commit_status_refuses_any_other_context(context: str) -> None:
    with pytest.raises(ValidationError):
        _command(status_context=context)


@pytest.mark.parametrize("sha", ["", "38b569c", _SHA[:-1], _SHA.upper(), " " + _SHA])
def test_commit_status_refuses_an_empty_or_malformed_sha(sha: str) -> None:
    with pytest.raises(ValidationError):
        _command(commit_sha=sha)


@pytest.mark.parametrize(
    "missing", ["commit_sha", "status_context", "status_state", "status_target_url"]
)
def test_commit_status_requires_its_fields(missing: str) -> None:
    payload = {
        "correlation_id": _CORRELATION,
        "operation": "commit_status_create",
        "repo": _REPO,
        "idempotency_key": "commit-status-24638",
        "requested_by_lane": "gate-status-2226",
        "commit_sha": _SHA,
        "status_context": "fable-review-gate",
        "status_state": "success",
        "status_target_url": _URL,
    }
    del payload[missing]
    with pytest.raises(ValidationError, match=missing):
        ModelRepoWriteRequest.model_validate(payload)


def test_commit_status_refuses_a_link_that_is_not_https() -> None:
    with pytest.raises(ValidationError):
        _command(status_target_url="javascript:alert(1)")


def test_commit_status_fields_are_refused_for_other_operations() -> None:
    with pytest.raises(ValidationError, match="read only by commit_status_create"):
        ModelRepoWriteRequest.model_validate(
            {
                "correlation_id": _CORRELATION,
                "operation": "check_run_create",
                "repo": _REPO,
                "idempotency_key": "check-run-24638",
                "requested_by_lane": "gate-status-2226",
                "name": "x",
                "head_sha": _SHA,
                "status": "queued",
                "commit_sha": _SHA,
            }
        )


def test_commit_status_serializes_only_its_own_fields() -> None:
    dumped = _command().model_dump(mode="json")
    assert dumped["commit_sha"] == _SHA
    assert "head_sha" not in dumped
    assert "protected_branch" not in dumped
    other = ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": "check_run_create",
            "repo": _REPO,
            "idempotency_key": "check-run-24638",
            "requested_by_lane": "gate-status-2226",
            "name": "x",
            "head_sha": _SHA,
            "status": "queued",
        }
    ).model_dump(mode="json")
    assert "commit_sha" not in other
    assert "status_state" not in other
