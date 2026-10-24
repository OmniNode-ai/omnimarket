# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's pr_review_threads read on a recorded GraphQL payload.

The two pages live in ``tests/fixtures/scm_review_threads``. The recorded
transport replays them in order and fails on any request that differs, so the
GraphQL query, its variables and its cursor are pinned.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.github_landing.github_landing_requests import review_threads_request
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    MAX_TEXT_CHARS,
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelPrReviewThreadsResult,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    json_response,
)

pytestmark = pytest.mark.unit

_PR = 4000
_CURSOR = "Y3Vyc29yOnYyOpHOAAAAAg=="
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "scm_review_threads"
    / "graphql_review_threads_pages.json"
)


def _pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))[
        "pages"
    ]
    return pages


def _transport(pages: list[dict[str, Any]]) -> RecordedGatewayTransport:
    requests = [
        review_threads_request(REPO, _PR),
        review_threads_request(REPO, _PR, after=_CURSOR),
    ]
    return RecordedGatewayTransport(
        [
            JsonExchange(request, json_response(page))
            for request, page in zip(requests, pages, strict=False)
        ]
    )


def _read(transport: RecordedGatewayTransport, **kw: Any) -> ModelPrReviewThreadsResult:
    result = dispatch(
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.PR_REVIEW_THREADS,
            repo=REPO,
            pr_number=_PR,
            **kw,
        ),
        transport,
    )
    assert isinstance(result, ModelPrReviewThreadsResult)
    return result


def test_threads_page_through_with_ids_path_line_and_resolved_state() -> None:
    transport = _transport(_pages())
    result = _read(transport)

    assert (result.repo, result.pr_number) == (REPO, _PR)
    assert (result.total_count, result.truncated) == (4, False)
    assert transport.exchanges == []
    # The fourth thread has no comment left, so it names nothing to reply to.
    assert [t.root_comment_id for t in result.threads] == [
        2410010001,
        2410010005,
        2410010009,
    ]
    first, resolved, readme = result.threads
    assert (first.thread_id, first.is_resolved, first.is_outdated) == (
        "PRRT_kwDOExample0001",
        False,
        False,
    )
    assert (first.path, first.line, first.comment_count) == (
        "src/omnimarket/example/handler.py",
        12,
        2,
    )
    assert [c.comment_id for c in first.comments] == [2410010001, 2410010002]
    assert [c.author for c in first.comments] == ["review-bot", "author-lane"]
    assert first.comments[0].url.endswith("#discussion_r2410010001")
    assert (resolved.is_resolved, resolved.is_outdated, resolved.line) == (
        True,
        True,
        None,
    )
    assert resolved.comments[0].author == ""
    assert (readme.path, readme.line) == ("README.md", 3)


def test_a_limit_cuts_the_list_and_says_so() -> None:
    transport = _transport(_pages())
    result = _read(transport, limit=2)

    assert [t.root_comment_id for t in result.threads] == [2410010001, 2410010005]
    assert result.truncated is True
    assert len(transport.sent) == 1


def test_a_long_comment_is_cut_and_flagged() -> None:
    pages = _pages()
    long_body = "x" * (MAX_TEXT_CHARS + 5)
    comment = pages[0]["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][
        0
    ]["comments"]["nodes"][0]
    comment["body"] = long_body
    result = _read(_transport(pages))

    cut = result.threads[0].comments[0]
    assert (len(cut.body), cut.body_truncated) == (MAX_TEXT_CHARS, True)
    assert result.threads[0].comments[1].body_truncated is False


def test_graphql_errors_are_raised_not_read_as_an_empty_list() -> None:
    page = {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}
    with pytest.raises(GitHubTransportError, match="Resource not accessible"):
        _read(_transport([page]))


def test_a_missing_pull_request_is_raised() -> None:
    page = {"data": {"repository": {"pullRequest": None}}}
    with pytest.raises(GitHubTransportError, match="no pull request"):
        _read(_transport([page]))


def test_a_http_failure_is_raised() -> None:
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                review_threads_request(REPO, _PR),
                json_response({"message": "Bad Gateway"}, status=502),
            )
        ]
    )
    with pytest.raises(GitHubTransportError, match="502"):
        _read(transport)


def test_the_read_needs_a_pull_request_number() -> None:
    with pytest.raises(ValidationError, match="pr_number"):
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.PR_REVIEW_THREADS, repo=REPO
        )


def test_the_result_travels_in_the_gateway_response() -> None:
    result = _read(_transport(_pages()))
    wire = ModelGithubGatewayResponse(
        correlation_id="00000000-0000-4000-8000-000000000001", result=result
    ).model_dump(mode="json")
    assert wire["result"]["operation"] == "pr_review_threads"
    again = ModelGithubGatewayResponse.model_validate(copy.deepcopy(wire))
    assert again.result == result
