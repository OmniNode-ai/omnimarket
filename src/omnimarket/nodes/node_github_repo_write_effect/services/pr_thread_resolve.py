# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr_thread_resolve pieces of node_github_repo_write_effect (OMN-21641).

The GraphQL mutation request, the read of a PR's review-thread page, and the
test of whether a thread has a reply from the fixing lane. The handler sends
the requests; this module only builds them and reads the answers.
"""

from __future__ import annotations

from typing import Final

from omnimarket.github_landing.github_landing_requests import review_threads_request
from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)

#: Every comment the node posts carries this prefix on its marker line.
IDEMPOTENCY_MARKER_PREFIX: Final[str] = "<!-- onex-idempotency-key: "

#: GitHub's documented resolveReviewThread mutation.
RESOLVE_REVIEW_THREAD_MUTATION: Final[str] = (
    "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id})"
    " { thread { id isResolved } } }"
)

#: Review-thread pages of 50 read before a thread is called absent.
THREAD_SCAN_PAGES: Final[int] = 10


def resolve_review_thread_request(thread_node_id: str) -> ModelGithubHttpRequest:
    """Resolve one pull request review thread, by its GraphQL node id."""
    return ModelGithubHttpRequest(
        method="POST",
        path=GITHUB_GRAPHQL_PATH,
        body={
            "query": RESOLVE_REVIEW_THREAD_MUTATION,
            "variables": {"id": thread_node_id},
        },
    )


def threads_page_request(
    repository: str, number: int, after: str | None
) -> ModelGithubHttpRequest:
    """The read of one page of the PR's review threads."""
    return review_threads_request(repository, number, after=after)


def dict_at(body: object, *path: str) -> dict[str, object]:
    node = body
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def thread_nodes(response: ModelGithubHttpResponse) -> tuple[list[object], str | None]:
    """The review-thread nodes of one GraphQL page and the next cursor, if any."""
    threads = dict_at(
        response.body, "data", "repository", "pullRequest", "reviewThreads"
    )
    nodes = threads.get("nodes")
    info = dict_at(threads, "pageInfo")
    cursor = info.get("endCursor")
    more = info.get("hasNextPage") is True and isinstance(cursor, str)
    return (nodes if isinstance(nodes, list) else [], str(cursor) if more else None)


def _login(comment: object) -> str | None:
    login = dict_at(comment, "author").get("login")
    return login if isinstance(login, str) else None


def has_fix_reply(comments: list[object]) -> bool:
    """True when a comment after the thread's first is a reply from the fixing side.

    A fixing lane replies through ``pr_thread_reply``, so its reply carries the
    node's idempotency marker; a reply by a login other than the first
    comment's author also counts. A reply by the reviewer alone does not.
    """
    first_author = _login(comments[0]) if comments else None
    for reply in comments[1:]:
        if not isinstance(reply, dict):
            continue
        marked = IDEMPOTENCY_MARKER_PREFIX in str(reply.get("body", ""))
        author = _login(reply)
        if marked or (author is not None and author != first_author):
            return True
    return False
