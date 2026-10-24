# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""pr_thread_reply on a recorded fake transport.

The review-comment list and the created reply are GitHub REST payloads kept
under ``tests/fixtures/scm_review_threads``. Each scenario replays a strict,
ordered list of exchanges, so the request shapes are pinned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    create_review_comment_reply_request,
    pull_request_request,
    review_comments_request,
)
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
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "scm_review_threads"
_REPO = "OmniNode-ai/omnimarket"
_PR = 4000
_THREAD = 2410010001
_KEY = "thread-reply-key-0001"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_MOVED = "a7f20d7a0" + "0" * 31
_NOW = 1_791_000_000.0
_CORRELATION = UUID("00000000-0000-4000-8000-000000000001")


def _recorded(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _headers(remaining: int = 4_900) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _ok(
    body: object, *, status: int = 200, remaining: int = 4_900
) -> ModelGithubHttpResponse:
    wrapped = {"value": body} if isinstance(body, list) else body
    assert wrapped is None or isinstance(wrapped, dict)
    return ModelGithubHttpResponse(
        status=status, headers=_headers(remaining), body=wrapped
    )


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


def _handler(
    transport: _Recorded, *, enforce: bool = True
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository=(
                {_REPO: EnumRepoWriteMode.ENFORCE} if enforce else {}
            ),
        ),
        clock=lambda: _NOW,
    )


def _reply(**kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_THREAD_REPLY,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "lane-x",
            "pr_number": _PR,
            "thread_comment_id": _THREAD,
            "body": "Fixed in the head commit.",
            **kw,
        }
    )


_LIST = review_comments_request(_REPO, _PR, per_page=LIST_PAGE_SIZE)
_POST = create_review_comment_reply_request(
    _REPO, _PR, _THREAD, f"Fixed in the head commit.\n\n{idempotency_marker(_KEY)}"
)


async def test_the_reply_is_posted_to_the_thread_with_its_idempotency_marker() -> None:
    transport = _Recorded(
        [
            (_LIST, _ok(_recorded("review_comments_page.json"))),
            (_POST, _ok(_recorded("review_comment_reply_created.json"), status=201)),
        ]
    )
    result = await _handler(transport).handle(_reply())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.comment_id, result.replayed) == (2410010003, False)
    assert result.html_url is not None
    assert result.html_url.endswith("#discussion_r2410010003")
    assert result.mode is EnumRepoWriteMode.ENFORCE
    assert transport.sent[-1].path == (
        f"/repos/{_REPO}/pulls/{_PR}/comments/{_THREAD}/replies"
    )
    assert not transport.exchanges


async def test_a_review_comment_carrying_the_key_is_not_replied_to_again() -> None:
    comments = _recorded("review_comments_page.json")
    assert isinstance(comments, list)
    comments.append(_recorded("review_comment_reply_created.json"))
    transport = _Recorded([(_LIST, _ok(comments))])
    result = await _handler(transport).handle(_reply())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.comment_id, result.replayed) == (2410010003, True)
    assert [r.method for r in transport.sent] == ["GET"]


async def test_the_same_key_twice_sends_nothing_the_second_time() -> None:
    transport = _Recorded(
        [
            (_LIST, _ok(_recorded("review_comments_page.json"))),
            (_POST, _ok(_recorded("review_comment_reply_created.json"), status=201)),
        ]
    )
    handler = _handler(transport)
    first = await handler.handle(_reply())
    second = await handler.handle(_reply())
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.replayed, second.replayed) == (False, True)
    assert second.comment_id == first.comment_id
    assert len(transport.sent) == 2


async def test_a_moved_head_is_refused_before_any_reply() -> None:
    moved = {"number": _PR, "head": {"ref": "branch", "sha": _MOVED}}
    transport = _Recorded([(pull_request_request(_REPO, _PR), _ok(moved))])
    result = await _handler(transport).handle(_reply(expected_head_sha=_HEAD))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert [r.method for r in transport.sent] == ["GET"]


async def test_the_reply_is_sent_when_the_expected_head_holds() -> None:
    current = {"number": _PR, "head": {"ref": "branch", "sha": _HEAD}}
    transport = _Recorded(
        [
            (pull_request_request(_REPO, _PR), _ok(current)),
            (_LIST, _ok([])),
            (_POST, _ok(_recorded("review_comment_reply_created.json"), status=201)),
        ]
    )
    result = await _handler(transport).handle(_reply(expected_head_sha=_HEAD))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.head_sha == _HEAD


async def test_a_thread_that_is_not_on_the_pull_request_is_not_found() -> None:
    transport = _Recorded(
        [
            (_LIST, _ok(_recorded("review_comments_page.json"))),
            (
                _POST,
                _ok({"message": "Not Found", "status": "404"}, status=404),
            ),
        ]
    )
    result = await _handler(transport).handle(_reply())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.NOT_FOUND
    assert result.http_status == 404


async def test_a_reply_to_a_reply_is_refused_with_githubs_message() -> None:
    transport = _Recorded(
        [
            (_LIST, _ok(_recorded("review_comments_page.json"))),
            (
                _POST,
                _ok(
                    {
                        "message": "Validation Failed",
                        "errors": [
                            {
                                "resource": "PullRequestReviewComment",
                                "code": "invalid",
                                "field": "in_reply_to",
                            }
                        ],
                    },
                    status=422,
                ),
            ),
        ]
    )
    result = await _handler(transport).handle(_reply())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED
    assert result.http_status == 422
    assert "Validation Failed" in result.detail


async def test_a_quota_under_the_floor_refuses_the_reply() -> None:
    transport = _Recorded(
        [(_LIST, _ok(_recorded("review_comments_page.json"), remaining=120))]
    )
    result = await _handler(transport).handle(_reply())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.QUOTA_FLOOR
    assert [r.method for r in transport.sent] == ["GET"]


async def test_dry_run_plans_the_list_and_the_reply_and_sends_nothing() -> None:
    transport = _Recorded([])
    result = await _handler(transport, enforce=False).handle(_reply())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (_LIST, _POST)
    assert transport.sent == []


@pytest.mark.parametrize("missing", ["thread_comment_id", "body"])
def test_a_reply_needs_its_thread_and_its_body(missing: str) -> None:
    kw = {missing: None}
    with pytest.raises(ValueError, match=missing):
        _reply(**kw)


def test_the_thread_id_is_carried_by_a_reply_and_by_no_other_operation() -> None:
    assert _reply().model_dump(mode="json")["thread_comment_id"] == _THREAD
    comment = ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_COMMENT,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "lane-x",
            "pr_number": _PR,
            "body": "hello",
        }
    )
    assert "thread_comment_id" not in comment.model_dump(mode="json")
    with pytest.raises(ValueError, match="thread_comment_id"):
        ModelRepoWriteRequest.model_validate(
            {**comment.model_dump(mode="json"), "thread_comment_id": _THREAD}
        )
