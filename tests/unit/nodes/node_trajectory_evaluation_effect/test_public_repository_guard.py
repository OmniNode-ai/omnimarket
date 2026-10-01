# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public-repository guard tests (OMN-20087)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.public_repository_guard import (
    EnumGuardVerdict,
    PublicRepositoryGuard,
    is_valid_repository,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationFailed,
    ModelTrajectoryEvaluationRefused,
)
from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
    Github,
    make_handler,
    submit_cmd,
)

pytestmark = pytest.mark.unit


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("reply", "kind", "reason"),
    [
        (
            lambda _r: httpx.Response(404),
            ModelTrajectoryEvaluationRefused,
            "repository_not_public",
        ),
        (
            lambda _r: httpx.Response(200, json={"private": True}),
            ModelTrajectoryEvaluationRefused,
            "repository_not_public",
        ),
        (
            lambda _r: httpx.Response(200, json={}),
            ModelTrajectoryEvaluationRefused,
            "repository_not_public",
        ),
        (
            lambda _r: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}),
            ModelTrajectoryEvaluationFailed,
            "guard_rate_limited",
        ),
        (
            lambda _r: httpx.Response(429, headers={"Retry-After": "30"}),
            ModelTrajectoryEvaluationFailed,
            "guard_rate_limited",
        ),
        (_timeout, ModelTrajectoryEvaluationFailed, "guard_unresolved"),
        (
            lambda _r: httpx.Response(500),
            ModelTrajectoryEvaluationFailed,
            "guard_unresolved",
        ),
        (
            lambda _r: httpx.Response(403),
            ModelTrajectoryEvaluationFailed,
            "guard_unresolved",
        ),
        (
            lambda _r: httpx.Response(
                302, headers={"Location": "https://example.invalid/"}
            ),
            ModelTrajectoryEvaluationFailed,
            "guard_unresolved",
        ),
        (
            lambda _r: httpx.Response(200, content=b"<html>"),
            ModelTrajectoryEvaluationFailed,
            "guard_unresolved",
        ),
    ],
)
async def test_guard_outcomes_reach_no_evaluator(reply, kind, reason) -> None:  # type: ignore[no-untyped-def]
    github = Github(reply)
    handler, _, ev, _ = make_handler("in_memory", github=github)
    out = await handler.handle(submit_cmd())
    assert isinstance(out, kind), out
    assert out.reason == reason
    if isinstance(out, ModelTrajectoryEvaluationFailed):
        assert out.retryable is True
    assert ev.submissions == []
    assert len(github.requests) == 1


async def test_public_repository_read_is_anonymous_and_unredirected() -> None:
    github = Github()
    guard = PublicRepositoryGuard(transport=github.transport())
    verdict = await guard.check("OmniNode-ai/omnimarket", uuid4())
    assert verdict is EnumGuardVerdict.PUBLIC
    (request,) = github.requests
    assert request.url.path == "/repos/OmniNode-ai/omnimarket"
    assert "authorization" not in request.headers


@pytest.mark.parametrize(
    "repo",
    [
        "../x",
        "x/..",
        "a/b/c",
        "a",
        "/a",
        "a/",
        "-a/b",
        "a/b?x",
        "a/b#c",
        "a b/c",
        "a/.",
    ],
)
def test_repository_pattern_refuses(repo: str) -> None:
    assert not is_valid_repository(repo)


@pytest.mark.parametrize(
    "repo", ["OmniNode-ai/omnimarket", "a/b.c_d-e", "a1/.github", "x/a..b"]
)
def test_repository_pattern_accepts(repo: str) -> None:
    assert is_valid_repository(repo)
