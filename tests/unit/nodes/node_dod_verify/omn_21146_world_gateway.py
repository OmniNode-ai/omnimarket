# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21146 -- node_github_repo_gateway_effect as a repo world's GitHub holds it.

Answers the two reads node_dod_verify sends the gateway: one PR's merge facts
(``merged_pr``) and one commit's check runs (``commit_check_runs``). Every
other operation fails the test.
"""

from __future__ import annotations

from typing import Any

from omnimarket.handlers.scm.handler_gateway_read_client import GatewayReadError
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelCheckRun,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
    ModelMergedPrResult,
)


class WorldGateway:
    """A recorded gateway client: ``read(request) -> result``."""

    def __init__(
        self,
        check_runs: dict[tuple[str, str], list[dict[str, object]]],
        github_prs: dict[str, dict[str, Any]],
    ) -> None:
        self._check_runs = check_runs
        self._github_prs = github_prs
        self.requests: list[ModelGithubGatewayRequest] = []

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.requests.append(request)
        if request.operation is EnumGithubGatewayOperation.COMMIT_CHECK_RUNS:
            assert request.head_sha is not None
            rows = self._check_runs.get((request.repo, request.head_sha)) or []
            if rows == [{"unreadable": True}]:
                raise GatewayReadError("gateway commit_check_runs: HTTP 502")
            return ModelCommitCheckRunsResult(
                repo=request.repo,
                head_sha=request.head_sha,
                check_name=request.check_name,
                truncated=False,
                check_runs=[
                    ModelCheckRun.model_validate(row)
                    for row in rows
                    if request.check_name in (None, row["name"])
                ],
            )
        if request.operation is EnumGithubGatewayOperation.MERGED_PR:
            short = request.repo.split("/", 1)[1]
            pr = self._github_prs.get(f"{short}#{request.pr_number}")
            if pr is None:
                raise GatewayReadError(
                    f"gateway merged_pr: {request.repo}#{request.pr_number} not found"
                )
            return ModelMergedPrResult(
                repo=request.repo,
                pr_number=pr["number"],
                state="MERGED",
                merged=True,
                head_sha=pr["head_sha"],
                head_ref=pr["head_ref"],
                base_ref=pr["base"],
                merge_commit_sha=pr["merge_sha"],
                merged_at=pr["merged_at"],
            )
        raise AssertionError(f"unexpected gateway read: {request.operation.value}")
