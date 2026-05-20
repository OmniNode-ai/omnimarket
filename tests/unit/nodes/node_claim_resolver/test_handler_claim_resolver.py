# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_claim_resolver."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from io import StringIO

import pytest

from omnimarket.nodes.node_claim_resolver import __main__ as claim_resolver_main
from omnimarket.nodes.node_claim_resolver.handlers.handler_claim_resolver import (
    HandlerClaimResolver,
)
from omnimarket.nodes.node_claim_resolver.models import (
    EnumAgentClaimKind,
    EnumClaimResolutionStatus,
    ModelAgentClaim,
    ModelClaimResolutionRequest,
)

pytestmark = pytest.mark.unit


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]
    ):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: str | None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        key = tuple(args)
        self.calls.append(key)
        return self.responses.get(
            key,
            subprocess.CompletedProcess(list(args), 1, "", "missing fake response"),
        )


def _request(*claims: ModelAgentClaim) -> ModelClaimResolutionRequest:
    return ModelClaimResolutionRequest(
        claims=claims,
        repo_hint="omniclaude",
        repo_root="/repo",
    )


def _gh_view(
    number: str,
    *,
    state: str = "MERGED",
) -> tuple[tuple[str, ...], subprocess.CompletedProcess[str]]:
    cmd = (
        "gh",
        "pr",
        "view",
        number,
        "--repo",
        "OmniNode-ai/omniclaude",
        "--json",
        "state,number,url,headRefOid",
    )
    return cmd, subprocess.CompletedProcess(
        list(cmd),
        0,
        json.dumps({"state": state, "number": int(number)}),
        "",
    )


def test_pr_merged_passes_when_github_state_is_merged() -> None:
    cmd, response = _gh_view("12", state="MERGED")
    resolver = HandlerClaimResolver(FakeRunner({cmd: response}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(kind=EnumAgentClaimKind.PR_MERGED, ref="omniclaude#12")
        )
    )

    assert result.mismatches == ()
    assert result.results[0].status is EnumClaimResolutionStatus.VERIFIED


def test_pr_merged_fails_when_github_state_is_open() -> None:
    cmd, response = _gh_view("12", state="OPEN")
    resolver = HandlerClaimResolver(FakeRunner({cmd: response}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(kind=EnumAgentClaimKind.PR_MERGED, ref="omniclaude#12")
        )
    )

    assert len(result.mismatches) == 1
    assert result.mismatches[0].actual == "OPEN"


def test_pr_opened_passes_when_pr_exists() -> None:
    cmd, response = _gh_view("77", state="OPEN")
    resolver = HandlerClaimResolver(FakeRunner({cmd: response}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(kind=EnumAgentClaimKind.PR_OPENED, ref="omniclaude#77")
        )
    )

    assert result.results[0].status is EnumClaimResolutionStatus.VERIFIED


def test_ci_passing_fails_on_failing_check() -> None:
    cmd = (
        "gh",
        "pr",
        "checks",
        "77",
        "--repo",
        "OmniNode-ai/omniclaude",
        "--json",
        "name,state,conclusion",
    )
    response = subprocess.CompletedProcess(
        list(cmd),
        0,
        json.dumps(
            [
                {"name": "lint", "conclusion": "SUCCESS"},
                {"name": "tests", "conclusion": "FAILURE"},
            ]
        ),
        "",
    )
    resolver = HandlerClaimResolver(FakeRunner({cmd: response}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(kind=EnumAgentClaimKind.CI_PASSING, ref="omniclaude#77")
        )
    )

    assert len(result.mismatches) == 1
    assert result.mismatches[0].actual == "tests"


def test_commit_sha_passes_when_git_finds_commit() -> None:
    cmd = ("git", "cat-file", "-e", "abc1234^{commit}")
    resolver = HandlerClaimResolver(
        FakeRunner({cmd: subprocess.CompletedProcess(list(cmd), 0, "", "")})
    )

    result = resolver.verify(
        _request(ModelAgentClaim(kind=EnumAgentClaimKind.COMMIT_SHA, ref="abc1234"))
    )

    assert result.results[0].status is EnumClaimResolutionStatus.VERIFIED


def test_file_committed_fails_when_head_path_missing() -> None:
    cmd = ("git", "cat-file", "-e", "HEAD:plugins/onex/hooks/lib/missing.py")
    resolver = HandlerClaimResolver(
        FakeRunner(
            {
                cmd: subprocess.CompletedProcess(
                    list(cmd), 128, "", "fatal: path does not exist"
                )
            }
        )
    )

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.FILE_COMMITTED,
                ref="plugins/onex/hooks/lib/missing.py",
            )
        )
    )

    assert len(result.mismatches) == 1
    assert result.mismatches[0].expected == "HEAD:plugins/onex/hooks/lib/missing.py"


def test_blocker_on_x_fails_without_quoted_gh_json_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(ModelAgentClaim(kind=EnumAgentClaimKind.BLOCKER_ON_X, ref="OMN-9107"))
    )

    assert len(result.mismatches) == 1
    assert "gh pr view --json" in result.mismatches[0].reason


def test_blocker_on_x_passes_with_quoted_gh_json_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.BLOCKER_ON_X,
                ref="OMN-9107",
                evidence=("gh pr view 12 --repo OmniNode-ai/omniclaude --json state",),
            )
        )
    )

    assert result.mismatches == ()
    assert result.results[0].status is EnumClaimResolutionStatus.VERIFIED


def test_thread_resolved_passes_with_graphql_resolved_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.THREAD_RESOLVED,
                ref="PRRT_abc",
                evidence=('{"isResolved": true}',),
            )
        )
    )

    assert result.results[0].status is EnumClaimResolutionStatus.VERIFIED


def test_thread_resolved_fails_with_unresolved_graphql_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.THREAD_RESOLVED,
                ref="PRRT_abc",
                evidence=('{"isResolved": false}',),
            )
        )
    )

    assert len(result.mismatches) == 1
    assert result.results[0].status is EnumClaimResolutionStatus.FAILED


def test_linear_state_skips_without_api_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.LINEAR_STATE,
                ref="OMN-9107",
                expected="Done",
            )
        )
    )

    assert result.mismatches == ()
    assert result.results[0].status is EnumClaimResolutionStatus.SKIPPED


def test_linear_state_fails_with_conflicting_api_evidence() -> None:
    resolver = HandlerClaimResolver(FakeRunner({}))

    result = resolver.verify(
        _request(
            ModelAgentClaim(
                kind=EnumAgentClaimKind.LINEAR_STATE,
                ref="OMN-9107",
                expected="Done",
                evidence=('{"state":"Todo"}',),
            )
        )
    )

    assert len(result.mismatches) == 1
    assert result.results[0].status is EnumClaimResolutionStatus.FAILED
    assert result.mismatches[0].expected == "Done"


def test_cli_reports_runtime_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = ModelClaimResolutionRequest(claims=()).model_dump_json()
    monkeypatch.setattr(sys, "stdin", StringIO(request))

    class RaisingResolver:
        def verify(self, request: ModelClaimResolutionRequest) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(claim_resolver_main, "HandlerClaimResolver", RaisingResolver)

    assert claim_resolver_main.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "CLAIM_RESOLVER_RUNTIME_ERROR:boom" in captured.err
