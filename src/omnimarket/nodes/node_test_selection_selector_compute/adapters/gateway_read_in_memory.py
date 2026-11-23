# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory twin of the repository gateway node, for the commit check run reads of the record source."""

from __future__ import annotations

from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelCheckRun,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)


class GatewayReadInMemory:
    """Answers ``commit_check_runs`` from the check runs it holds; any other operation is refused.

    ``unreachable`` stands for a gateway that gives no answer. Every request is kept in ``requests``.
    """

    def __init__(self, *, unreachable: bool = False) -> None:
        self.unreachable = unreachable
        self.requests: list[ModelGithubGatewayRequest] = []
        self._runs: list[tuple[str, ModelCheckRun]] = []
        self._next_id = 1

    def publish(
        self,
        head_sha: str,
        name: str,
        summary: str | None,
        *,
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> None:
        self._runs.append(
            (
                head_sha,
                ModelCheckRun(
                    id=self._next_id,
                    name=name,
                    status=status,
                    conclusion=conclusion,
                    output_summary=summary,
                ),
            )
        )
        self._next_id += 1

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.requests.append(request)
        if self.unreachable:
            raise GatewayReadError("no answer from the gateway")
        if request.operation is not EnumGithubGatewayOperation.COMMIT_CHECK_RUNS:
            raise GatewayReadError(f"operation {request.operation.value} not served")
        runs = [
            run.model_copy(
                update={
                    "output_summary": run.output_summary
                    if request.include_output
                    else None
                }
            )
            for head, run in self._runs
            if head == request.head_sha
            and (request.check_name is None or run.name == request.check_name)
        ]
        assert request.head_sha is not None
        return ModelCommitCheckRunsResult(
            repo=request.repo,
            head_sha=request.head_sha,
            check_name=request.check_name,
            truncated=False,
            check_runs=runs,
        )
