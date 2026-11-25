# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A GitHub rate-limit answer is a typed refusal carrying its reset time (OMN-26326).

Before this change every non-2xx answer of the read gateway became a bare
``GitHubTransportError`` whose terminal said only ``HTTP 403`` or ``HTTP 429``,
so a caller could not tell "the credential's hourly quota is spent, wait until
T" from "the credential may not read this". The refusal is a subclass of
``GitHubTransportError``: every existing ``except GitHubTransportError`` keeps
catching it, and the terminal's ``failure_class`` names the typed class.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.github_landing.model_transport_error import (
    GITHUB_RATE_LIMITED,
    GitHubRateLimitedError,
    GitHubTransportError,
    rate_limit_refusal,
)
from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
    _check_log,
    _checked,
)

RESET_EPOCH = 1_795_540_800  # 2026-11-24T17:20:00Z
RESET_ISO = datetime.fromtimestamp(RESET_EPOCH, UTC).isoformat()
REQUEST = ModelGithubHttpRequest(
    method="GET", path="/repos/OmniNode-ai/omnimarket/pulls"
)


def _quota_headers(remaining: int = 0) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(RESET_EPOCH),
        "x-ratelimit-resource": "core",
    }


def test_primary_limit_403_is_a_typed_refusal_with_its_reset_time() -> None:
    response = ModelGithubHttpResponse(
        status=403,
        headers=_quota_headers(0),
        body={"message": "API rate limit exceeded for user ID 1."},
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        _checked(REQUEST, response)
    error = caught.value
    assert isinstance(error, GitHubTransportError)
    assert error.status == 403
    assert error.reset_epoch == RESET_EPOCH
    assert error.resource == "core"
    assert str(error).startswith(GITHUB_RATE_LIMITED)
    assert RESET_ISO in str(error)


def test_429_with_retry_after_carries_the_wait_and_no_invented_reset() -> None:
    response = ModelGithubHttpResponse(
        status=429,
        headers={"retry-after": "60"},
        body={"message": "You have exceeded a secondary rate limit."},
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        _checked(REQUEST, response)
    assert caught.value.retry_after_seconds == 60
    assert caught.value.reset_epoch is None
    assert "retry after 60 s" in str(caught.value)


def test_secondary_limit_403_without_quota_headers_is_still_typed() -> None:
    response = ModelGithubHttpResponse(
        status=403,
        headers={"retry-after": "30"},
        body={"message": "You have exceeded a secondary rate limit. Please wait."},
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        _checked(REQUEST, response)
    assert caught.value.retry_after_seconds == 30


def test_429_with_no_headers_says_the_reset_was_not_reported() -> None:
    response = ModelGithubHttpResponse(status=429, headers={}, body=None)
    with pytest.raises(GitHubRateLimitedError) as caught:
        _checked(REQUEST, response)
    assert caught.value.reset_epoch is None
    assert caught.value.retry_after_seconds is None
    assert "reset time not reported" in str(caught.value)


def test_forbidden_with_quota_left_stays_a_plain_transport_error() -> None:
    """Positive control: a 403 that is not a rate limit is not relabelled."""
    response = ModelGithubHttpResponse(
        status=403,
        headers=_quota_headers(4321),
        body={"message": "Resource not accessible by integration"},
    )
    with pytest.raises(GitHubTransportError) as caught:
        _checked(REQUEST, response)
    assert not isinstance(caught.value, GitHubRateLimitedError)
    assert "HTTP 403" in str(caught.value)


def test_a_404_stays_a_plain_transport_error() -> None:
    response = ModelGithubHttpResponse(
        status=404, headers=_quota_headers(0), body={"message": "Not Found"}
    )
    with pytest.raises(GitHubTransportError) as caught:
        _checked(REQUEST, response)
    assert not isinstance(caught.value, GitHubRateLimitedError)


def test_job_log_read_rate_limit_is_typed_and_names_the_reset() -> None:
    log_request = ModelGithubHttpRequest(
        method="GET", path="/repos/OmniNode-ai/omnimarket/actions/jobs/1/logs"
    )
    response = ModelGithubBytesResponse(
        status=403,
        headers=_quota_headers(0),
        content=b'{"message":"API rate limit exceeded for user ID 1."}',
        total_bytes=52,
        over_limit=False,
        keep="tail",
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        _check_log(log_request, response)
    assert caught.value.reset_epoch == RESET_EPOCH
    assert RESET_ISO in str(caught.value)


def test_classifier_reads_header_names_case_insensitively() -> None:
    refusal = rate_limit_refusal(
        status=403,
        headers={k.upper(): v for k, v in _quota_headers(0).items()},
        message="API rate limit exceeded",
        request_line="GitHub GET /rate",
    )
    assert refusal is not None
    assert refusal.reset_epoch == RESET_EPOCH


def test_classifier_answers_none_for_a_success_status() -> None:
    assert (
        rate_limit_refusal(
            status=200,
            headers=_quota_headers(0),
            message="",
            request_line="GitHub GET /rate",
        )
        is None
    )


def test_code_scanning_read_does_not_call_a_spent_quota_forbidden() -> None:
    """A rate-limited 403 raised the typed refusal, not available=False/forbidden."""
    from omnimarket.models.model_github_gateway_io import (
        EnumGithubGatewayOperation,
        ModelGithubGatewayRequest,
    )
    from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
        read_code_scanning_alerts,
    )

    class _Spent:
        def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
            return ModelGithubHttpResponse(
                status=403,
                headers=_quota_headers(0),
                body={"message": "API rate limit exceeded for user ID 1."},
            )

    request = ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.CODE_SCANNING_ALERTS,
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 1,
        }
    )
    with pytest.raises(GitHubRateLimitedError) as caught:
        read_code_scanning_alerts(_Spent(), request)  # type: ignore[arg-type]
    assert caught.value.reset_epoch == RESET_EPOCH


def test_code_scanning_forbidden_with_quota_left_is_still_unavailable() -> None:
    """Positive control: the existing forbidden answer is unchanged."""
    from omnimarket.models.model_github_gateway_io import (
        EnumGithubGatewayOperation,
        ModelGithubGatewayRequest,
    )
    from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
        read_code_scanning_alerts,
    )

    class _Forbidden:
        def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
            return ModelGithubHttpResponse(
                status=403,
                headers=_quota_headers(4000),
                body={"message": "Resource not accessible by integration"},
            )

    request = ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.CODE_SCANNING_ALERTS,
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 1,
        }
    )
    result = read_code_scanning_alerts(_Forbidden(), request)  # type: ignore[arg-type]
    assert result.available is False
    assert result.unavailable_reason == "forbidden"


def test_api_helper_error_with_quota_headers_becomes_the_typed_refusal() -> None:
    """The urllib helper path (pr detail, open PRs, branch protection) keeps its headers."""
    from omnimarket.github_api import GitHubApiError
    from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
        RealGitHubReadTransport,
    )

    class _Client:
        def fetch_open_prs(self, repo: str) -> list[dict[str, object]]:
            raise GitHubApiError(
                '{"message":"API rate limit exceeded for user ID 1."}',
                status_code=403,
                headers=_quota_headers(0),
            )

        def fetch_branch_protection(self, repo: str) -> int | None:
            raise GitHubApiError(
                '{"message":"Resource not accessible by integration"}',
                status_code=403,
                headers=_quota_headers(4000),
            )

    transport = RealGitHubReadTransport("placeholder-not-a-credential")
    transport._client = _Client()  # type: ignore[assignment]
    with pytest.raises(GitHubRateLimitedError) as caught:
        transport.fetch_open_prs("OmniNode-ai/omnimarket")
    assert caught.value.reset_epoch == RESET_EPOCH
    with pytest.raises(GitHubTransportError) as plain:
        transport.fetch_branch_protection("OmniNode-ai/omnimarket")
    assert not isinstance(plain.value, GitHubRateLimitedError)


def test_api_helper_error_without_headers_is_unchanged() -> None:
    from omnimarket.github_api import GitHubApiError

    assert GitHubApiError("boom", status_code=500).headers == {}
