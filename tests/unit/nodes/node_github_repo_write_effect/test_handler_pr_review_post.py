# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR reviews use the writer's transport, expected head and replay guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    idempotency_marker,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_PR = 4000
_HEAD = "4" * 40
_KEY = "review-receipt-0001"
_BODY = "Reviewed the fix."
_NOW = 1_791_000_000.0
_MARKED_BODY = f"{_BODY}\n\n{idempotency_marker(_KEY)}"
_URL = f"https://github.com/{_REPO}/pull/{_PR}#pullrequestreview-123"
_REVIEW = {"id": 123, "html_url": _URL, "body": _MARKED_BODY}
_GET_HEAD = ModelGithubHttpRequest(method="GET", path=f"/repos/{_REPO}/pulls/{_PR}")
_LIST = ModelGithubHttpRequest(
    method="GET", path=f"/repos/{_REPO}/pulls/{_PR}/reviews?per_page=100"
)
_POST = ModelGithubHttpRequest(
    method="POST",
    path=f"/repos/{_REPO}/pulls/{_PR}/reviews",
    body={"event": "COMMENT", "commit_id": _HEAD, "body": _MARKED_BODY},
)


def _ok(
    body: object, *, status: int = 200, headers: dict[str, str] | None = None
) -> ModelGithubHttpResponse:
    wrapped = {"value": body} if isinstance(body, list) else body
    assert isinstance(wrapped, dict)
    return ModelGithubHttpResponse(status=status, headers=headers or {}, body=wrapped)


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"unexpected request: {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected
        return response


def _handler(transport: _Recorded) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(github_mode=EnumRepoWriteMode.ENFORCE),
        clock=lambda: _NOW,
    )


def _review(**kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": UUID("00000000-0000-4000-8000-000000000001"),
            "operation": "pr_review_post",
            "repo": _REPO,
            "pr_number": _PR,
            "body": _BODY,
            "expected_head_sha": _HEAD,
            "idempotency_key": _KEY,
            "requested_by_lane": "lane-x",
            **kw,
        }
    )


def _head(sha: str = _HEAD) -> ModelGithubHttpResponse:
    return _ok({"number": _PR, "head": {"sha": sha}})


async def test_posts_one_comment_review_with_commit_id_and_marker() -> None:
    transport = _Recorded(
        [(_GET_HEAD, _head()), (_LIST, _ok([])), (_POST, _ok(_REVIEW, status=201))]
    )
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.review_id, result.html_url, result.head_sha) == (123, _URL, _HEAD)
    assert result.replayed is False
    assert result.mode is EnumRepoWriteMode.ENFORCE
    assert result.requests == (_GET_HEAD, _LIST, _POST)
    assert not transport.exchanges


async def test_same_receipt_twice_posts_nothing_the_second_time() -> None:
    transport = _Recorded(
        [(_GET_HEAD, _head()), (_LIST, _ok([])), (_POST, _ok(_REVIEW, status=201))]
    )
    handler = _handler(transport)
    first = await handler.handle(_review())
    second = await handler.handle(_review())
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.replayed, second.replayed) == (False, True)
    assert (second.review_id, second.html_url) == (first.review_id, first.html_url)
    assert transport.sent == [_GET_HEAD, _LIST, _POST]


async def test_existing_review_marker_replays_after_handler_restart() -> None:
    transport = _Recorded([(_GET_HEAD, _head()), (_LIST, _ok([_REVIEW]))])
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert (result.review_id, result.html_url) == (123, _URL)
    assert transport.sent == [_GET_HEAD, _LIST]


async def test_moved_head_is_refused_before_any_review_post() -> None:
    transport = _Recorded([(_GET_HEAD, _head("a" * 40))])
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert result.terminal_failure_cause is EnumRepoWriteRefusal.HEAD_MOVED
    assert transport.sent == [_GET_HEAD]


async def test_review_marker_on_a_later_page_is_replayed() -> None:
    next_page = ModelGithubHttpRequest(
        method="GET", path=f"/repos/{_REPO}/pulls/{_PR}/reviews?per_page=100&page=2"
    )
    transport = _Recorded(
        [
            (_GET_HEAD, _head()),
            (
                _LIST,
                _ok(
                    [{"id": 122, "body": "Another review."}],
                    headers={
                        "link": f'<https://api.github.com{next_page.path}>; rel="next"'
                    },
                ),
            ),
            (next_page, _ok([_REVIEW])),
        ]
    )
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.review_id, result.replayed) == (123, True)
    assert transport.sent == [_GET_HEAD, _LIST, next_page]


@pytest.mark.parametrize(
    ("status", "reason"),
    [(404, EnumRepoWriteRefusal.NOT_FOUND), (422, EnumRepoWriteRefusal.GITHUB_REFUSED)],
)
async def test_github_review_refusal_is_reported(
    status: int, reason: EnumRepoWriteRefusal
) -> None:
    transport = _Recorded(
        [
            (_GET_HEAD, _head()),
            (_LIST, _ok([])),
            (_POST, _ok({"message": "Review refused"}, status=status)),
        ]
    )
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is reason
    assert result.http_status == status
    assert "Review refused" in result.detail


async def test_quota_below_floor_refuses_review_post() -> None:
    transport = _Recorded(
        [
            (_GET_HEAD, _head()),
            (
                _LIST,
                _ok(
                    [],
                    headers={
                        "x-ratelimit-limit": "5000",
                        "x-ratelimit-remaining": "120",
                        "x-ratelimit-used": "4880",
                        "x-ratelimit-reset": str(int(_NOW) + 3600),
                        "x-ratelimit-resource": "core",
                    },
                ),
            ),
        ]
    )
    result = await _handler(transport).handle(_review())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.QUOTA_FLOOR
    assert transport.sent == [_GET_HEAD, _LIST]


async def test_dry_run_plans_head_scan_and_post_without_sending() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(_review(dry_run=True))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (_GET_HEAD, _LIST, _POST)
    assert transport.sent == []


@pytest.mark.parametrize("missing", ["pr_number", "body", "expected_head_sha"])
def test_review_requires_pr_body_and_expected_head(missing: str) -> None:
    with pytest.raises(ValueError, match=missing):
        _review(**{missing: None})
