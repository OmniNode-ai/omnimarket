# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""pr_thread_resolve on a recorded fake transport (OMN-21641).

The review-thread pages are the GraphQL payloads kept under
``tests/fixtures/scm_review_threads``. Each scenario replays a strict, ordered
list of exchanges, so the request shapes are pinned. The planted cases prove the
refusals: a thread with no reply from the fixing lane is never resolved, a
thread that is not on the pull request is not found, and a moved head sends no
mutation.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.github_landing.github_landing_requests import (
    pull_request_request,
    review_threads_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    idempotency_marker,
    planned_requests,
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
from omnimarket.nodes.node_github_repo_write_effect.services.pr_thread_resolve import (
    resolve_review_thread_request,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "scm_review_threads"
_REPO = "OmniNode-ai/omnimarket"
_PR = 4000
_REPLIED = "PRRT_kwDOExample0001"  # reviewer comment, then a reply by author-lane
_UNREPLIED = "PRRT_kwDOExample0003"  # one reviewer comment, no reply
_RESOLVED = "PRRT_kwDOExample0002"  # already resolved
_ABSENT = "PRRT_kwDOExample9999"
_KEY = "thread-resolve-key-0001"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_MOVED = "a7f20d7a0" + "0" * 31
_NOW = 1_791_000_000.0
_CORRELATION = UUID("00000000-0000-4000-8000-000000000002")


def _pages() -> list[dict[str, Any]]:
    loaded = json.loads(
        (_FIXTURES / "graphql_review_threads_pages.json").read_text(encoding="utf-8")
    )
    pages: list[dict[str, Any]] = loaded["pages"]
    return pages


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "graphql",
    }


def _ok(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_headers(), body=body)


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


def _resolve(thread: str = _REPLIED, **kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_THREAD_RESOLVE,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "author-lane",
            "pr_number": _PR,
            "review_thread_id": thread,
            **kw,
        }
    )


def _read_pages(
    pages: list[dict[str, Any]],
) -> list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]:
    """The recorded thread reads, one exchange per page, cursor chained."""
    exchanges = []
    after: str | None = None
    for page in pages:
        exchanges.append((review_threads_request(_REPO, _PR, after=after), _ok(page)))
        info = page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]
        after = info["endCursor"]
    return exchanges


_MUTATION = resolve_review_thread_request(_REPLIED)
_MUTATED = _ok(
    {"data": {"resolveReviewThread": {"thread": {"id": _REPLIED, "isResolved": True}}}}
)


async def test_a_thread_with_a_reply_is_resolved_through_graphql() -> None:
    transport = _Recorded([*_read_pages(_pages()[:1]), (_MUTATION, _MUTATED)])
    result = await _handler(transport).handle(_resolve())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.replayed, result.mode) == (False, EnumRepoWriteMode.ENFORCE)
    assert transport.sent[-1] == _MUTATION
    assert "resolveReviewThread" in str(_MUTATION.body)
    assert not transport.exchanges


async def test_a_thread_on_a_later_page_is_found_and_resolved() -> None:
    mutation = resolve_review_thread_request("PRRT_kwDOExample0003")
    pages = _pages()
    pages[1]["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0][
        "comments"
    ]["nodes"].append(
        {
            "databaseId": 2410010010,
            "url": "https://github.com/OmniNode-ai/omnimarket/pull/4000#discussion_r2410010010",
            "body": f"Fixed.\n\n{idempotency_marker('another-key-0002')}",
            "createdAt": "2026-10-23T22:05:00Z",
            "author": {"login": "node-write-identity"},
        }
    )
    transport = _Recorded([*_read_pages(pages), (mutation, _MUTATED)])
    result = await _handler(transport).handle(_resolve("PRRT_kwDOExample0003"))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert transport.sent[-1] == mutation


async def test_not_replied_a_thread_without_a_reply_is_refused_and_not_mutated() -> (
    None
):
    transport = _Recorded(_read_pages(_pages()))
    result = await _handler(transport).handle(_resolve(_UNREPLIED))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.THREAD_NOT_REPLIED
    assert _UNREPLIED in result.detail
    assert all(
        r.method == "POST" and "mutation" not in str(r.body) for r in transport.sent
    )


async def test_not_replied_a_reply_by_the_reviewer_alone_is_not_a_fix_reply() -> None:
    pages = _pages()
    thread = pages[0]["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
    thread["comments"]["nodes"][1]["author"] = {"login": "review-bot"}
    transport = _Recorded(_read_pages(pages[:1]))
    result = await _handler(transport).handle(_resolve())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.THREAD_NOT_REPLIED
    assert not transport.exchanges


async def test_a_reply_posted_through_the_node_counts_even_for_one_identity() -> None:
    pages = _pages()
    thread = pages[0]["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
    reply = thread["comments"]["nodes"][1]
    reply["author"] = {"login": "review-bot"}
    reply["body"] = f"Bounded.\n\n{idempotency_marker('reply-key-0001')}"
    transport = _Recorded([*_read_pages(pages[:1]), (_MUTATION, _MUTATED)])
    result = await _handler(transport).handle(_resolve())
    assert isinstance(result, ModelRepoWriteCompleted)


async def test_an_already_resolved_thread_is_replayed_without_a_mutation() -> None:
    transport = _Recorded(_read_pages(_pages()))
    result = await _handler(transport).handle(_resolve(_RESOLVED))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert not transport.exchanges
    assert all("mutation" not in str(r.body) for r in transport.sent)


async def test_a_thread_that_is_not_on_the_pull_request_is_not_found() -> None:
    transport = _Recorded(_read_pages(_pages()))
    result = await _handler(transport).handle(_resolve(_ABSENT))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.NOT_FOUND
    assert all("mutation" not in str(r.body) for r in transport.sent)


async def test_a_moved_head_is_refused_before_any_read_of_threads() -> None:
    moved = {"number": _PR, "head": {"ref": "branch", "sha": _MOVED}}
    core = ModelGithubHttpResponse(
        status=200, headers={**_headers(), "x-ratelimit-resource": "core"}, body=moved
    )
    transport = _Recorded([(pull_request_request(_REPO, _PR), core)])
    result = await _handler(transport).handle(_resolve(expected_head_sha=_HEAD))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert len(transport.sent) == 1


async def test_a_graphql_error_on_the_mutation_is_github_refused() -> None:
    errored = _ok({"errors": [{"message": "Resource not accessible"}]})
    transport = _Recorded([*_read_pages(_pages()[:1]), (_MUTATION, errored)])
    result = await _handler(transport).handle(_resolve())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED


def test_the_dry_run_plan_is_the_thread_read_then_the_mutation() -> None:
    assert planned_requests(_resolve()) == (
        review_threads_request(_REPO, _PR, after=None),
        _MUTATION,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"review_thread_id": None},
        {"review_thread_id": "not-a-thread-id"},
        {"pr_number": None},
        {"thread_comment_id": 12},
    ],
)
def test_the_request_names_the_pull_request_and_a_thread_node_id(
    bad: dict[str, object],
) -> None:
    base = json.loads(_resolve().model_dump_json())
    candidate = copy.deepcopy(base)
    candidate.update(bad)
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate(candidate)


def test_other_operations_do_not_carry_the_thread_id_field() -> None:
    reply = ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_THREAD_REPLY,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "lane-x",
            "pr_number": _PR,
            "thread_comment_id": 2410010001,
            "body": "Fixed.",
        }
    )
    assert "review_thread_id" not in reply.model_dump(mode="json")
