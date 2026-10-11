# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded fake transport for the gateway's OMN-20912 reads.

A strict, ordered replay: each exchange holds the exact request the node must
send and the response GitHub gave. A request that differs in method, path, body
or conditional header raises instead of answering, so a drift in the node's
request shape fails the replay rather than passing on a loose match (the
pattern of the landing effect's ``FakeGithubLandingTransport``).

A bytes exchange stores the whole body GitHub served (a log or a zip); the fake
reads it through the transport's own ``_read_capped`` so the cap the node asks
for is applied exactly as the live transport applies it.

Provenance: the response bodies follow GitHub's documented REST shapes for
these endpoints (trimmed to the fields plus a few the node must drop). They
are not captured from a live call, because lanes no longer read GitHub
directly; the lab proof on the dev lane exercises the live endpoints.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Literal

from omnimarket.github_landing.github_landing_transport import _read_capped
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)

REPO = "OmniNode-ai/omnimarket"
API_ROOT = "https://api.github.com"
HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"


def quota_headers(remaining: int = 4_900, resource: str = "core") -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": "2000000000",
        "x-ratelimit-resource": resource,
    }


def json_response(
    body: dict[str, object] | list[object] | None,
    *,
    status: int = 200,
    next_path: str | None = None,
) -> ModelGithubHttpResponse:
    headers = quota_headers()
    if next_path is not None:
        headers["Link"] = f'<{API_ROOT}{next_path}>; rel="next"'
    wrapped = {"value": body} if isinstance(body, list) else body
    return ModelGithubHttpResponse(status=status, headers=headers, body=wrapped)


def get(path: str) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(method="GET", path=path)


class RecordedTransportMismatchError(AssertionError):
    """A request did not match the next recorded exchange."""


@dataclass(frozen=True)
class JsonExchange:
    request: ModelGithubHttpRequest
    response: ModelGithubHttpResponse


@dataclass(frozen=True)
class BytesExchange:
    request: ModelGithubHttpRequest
    status: int
    body: bytes
    limit: int
    keep: Literal["head", "tail"]


@dataclass
class RecordedGatewayTransport:
    """Satisfies ``GitHubReadTransportProtocol`` for the OMN-20912 reads."""

    exchanges: list[JsonExchange | BytesExchange]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    def _next(self, request: ModelGithubHttpRequest) -> JsonExchange | BytesExchange:
        self.sent.append(request)
        if not self.exchanges:
            raise RecordedTransportMismatchError(f"no exchange left for {request!r}")
        exchange = self.exchanges.pop(0)
        if exchange.request != request:
            raise RecordedTransportMismatchError(
                f"request {request!r} does not match recorded {exchange.request!r}"
            )
        return exchange

    def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        exchange = self._next(request)
        if not isinstance(exchange, JsonExchange):
            raise RecordedTransportMismatchError(f"{request.path} is a bytes exchange")
        return exchange.response

    def send_bytes_sync(
        self,
        request: ModelGithubHttpRequest,
        *,
        limit: int,
        keep: Literal["head", "tail"],
    ) -> ModelGithubBytesResponse:
        exchange = self._next(request)
        if not isinstance(exchange, BytesExchange):
            raise RecordedTransportMismatchError(f"{request.path} is a JSON exchange")
        if (limit, keep) != (exchange.limit, exchange.keep):
            raise RecordedTransportMismatchError(
                f"{request.path}: asked limit={limit} keep={keep}, recorded "
                f"limit={exchange.limit} keep={exchange.keep}"
            )
        if exchange.status >= 300:
            return ModelGithubBytesResponse(
                status=exchange.status,
                headers=quota_headers(),
                content=exchange.body,
                total_bytes=len(exchange.body),
                over_limit=False,
                keep=keep,
            )
        content, total, over = _read_capped(
            io.BytesIO(exchange.body), limit=limit, keep=keep
        )
        return ModelGithubBytesResponse(
            status=exchange.status,
            headers=quota_headers(),
            content=content,
            total_bytes=total,
            over_limit=over,
            keep=keep,
        )

    def assert_drained(self) -> None:
        if self.exchanges:
            raise RecordedTransportMismatchError(
                f"{len(self.exchanges)} recorded exchange(s) were never requested"
            )

    # The GraphQL-era reads are not part of these scenarios.
    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        raise RecordedTransportMismatchError("fetch_open_prs is not recorded")

    def fetch_branch_protection(self, repo: str) -> int | None:
        raise RecordedTransportMismatchError("fetch_branch_protection is not recorded")

    def fetch_pr_detail(self, repo: str, pr_number: int) -> dict[str, Any]:
        raise RecordedTransportMismatchError("fetch_pr_detail is not recorded")


# --- documented response bodies ------------------------------------------------


def workflow_run(
    run_id: int, *, conclusion: str | None = "success"
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": "CI",
        "node_id": f"WFR_{run_id}",
        "workflow_id": 161335,
        "head_branch": "dev",
        "head_sha": HEAD,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        "run_number": 9000 + run_id % 1000,
        "run_attempt": 1,
        "created_at": "2026-10-10T18:00:00Z",
        "updated_at": "2026-10-10T18:20:00Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "actor": {"login": "someone", "id": 1},
        "repository": {"full_name": REPO},
    }


def run_job(job_id: int, *, attempt: int, conclusion: str | None) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": 30433642,
        "run_attempt": attempt,
        "node_id": f"CR_{job_id}",
        "head_sha": HEAD,
        "name": "unit-tests (3/8)",
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-10-10T18:01:00Z",
        "completed_at": "2026-10-10T18:09:00Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/30433642/job/{job_id}",
        "labels": ["ubuntu-latest"],
        "runner_name": "GitHub Actions 7",
        "steps": [
            {
                "name": "Set up job",
                "status": "completed",
                "conclusion": "success",
                "number": 1,
                "started_at": "2026-10-10T18:01:00Z",
                "completed_at": "2026-10-10T18:01:02Z",
            },
            {
                "name": "Run pytest",
                "status": "completed",
                "conclusion": conclusion,
                "number": 2,
                "started_at": "2026-10-10T18:01:02Z",
                "completed_at": "2026-10-10T18:09:00Z",
            },
        ],
    }


def artifact(
    artifact_id: int, *, run_id: int, size: int, expired: bool = False
) -> dict[str, object]:
    return {
        "id": artifact_id,
        "node_id": f"MDg6QXJ0aWZhY3Q{artifact_id}",
        "name": f"coverage-{artifact_id}",
        "size_in_bytes": size,
        "url": f"{API_ROOT}/repos/{REPO}/actions/artifacts/{artifact_id}",
        "archive_download_url": f"{API_ROOT}/repos/{REPO}/actions/artifacts/{artifact_id}/zip",
        "expired": expired,
        "digest": "sha256:" + "ab" * 32,
        "created_at": "2026-10-10T18:09:00Z",
        "expires_at": "2026-10-24T18:09:00Z",
        "updated_at": "2026-10-10T18:09:00Z",
        "workflow_run": {
            "id": run_id,
            "repository_id": 1,
            "head_branch": "dev",
            "head_sha": HEAD,
        },
    }


def pull_request(number: int, *, body: str) -> dict[str, object]:
    return {
        "number": number,
        "node_id": "PR_kwDOR6jjtc8AAAABFQhbqA",
        "state": "open",
        "title": "feat: an example",
        "body": body,
        "draft": False,
        "merged": False,
        "comments": 3,
        "user": {"login": "pr-author", "id": 2},
        "head": {"ref": "jonah/omn-20912-example", "sha": HEAD},
        "base": {"ref": "dev", "sha": "0" * 40},
        "labels": [],
    }


def issue_comment(comment_id: int, *, body: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "node_id": f"IC_{comment_id}",
        "user": {"login": "reviewer-bot", "id": 3},
        "created_at": "2026-10-10T18:10:00Z",
        "updated_at": "2026-10-10T18:11:00Z",
        "body": body,
        "author_association": "NONE",
    }


def release(release_id: int, tag: str) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag,
        "name": f"omnimarket {tag}",
        "draft": False,
        "prerelease": False,
        "target_commitish": "dev",
        "created_at": "2026-10-09T10:00:00Z",
        "published_at": "2026-10-09T10:05:00Z",
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "assets": [],
        "author": {"login": "release-bot"},
    }


def tag(name: str, sha: str) -> dict[str, object]:
    return {
        "name": name,
        "commit": {"sha": sha, "url": f"{API_ROOT}/repos/{REPO}/commits/{sha}"},
        "zipball_url": f"{API_ROOT}/repos/{REPO}/zipball/{name}",
        "node_id": f"REF_{name}",
    }
