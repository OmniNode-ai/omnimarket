# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory twin of the repository gateway node, for the reads of the stage-savings report.

Serves ``pr_heads``, ``workflow_runs`` and ``artifacts`` (list and bounded archive fetch) from what it holds;
any other operation is refused. ``unreachable`` stands for a gateway that gives no answer for everything, and
``unreachable_heads`` for one that gives none for the reads about those head commits.
"""

from __future__ import annotations

import base64
import hashlib

from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    EnumArtifactFetchRefusal,
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelArtifactContent,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelPrHead,
    ModelPrHeadsResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)

_Op = EnumGithubGatewayOperation


class GatewayPrRecordsInMemory:
    """Every request is kept in ``requests``."""

    def __init__(
        self,
        *,
        unreachable: bool = False,
        unreachable_heads: frozenset[str] = frozenset(),
        heads_truncated: bool = False,
    ) -> None:
        self.unreachable = unreachable
        self.unreachable_heads = unreachable_heads
        self.heads_truncated = heads_truncated
        self.requests: list[ModelGithubGatewayRequest] = []
        self._prs: list[ModelPrHead] = []
        self._runs: list[ModelWorkflowRunSummary] = []
        self._artifacts: dict[int, list[tuple[ModelArtifactSummary, bytes]]] = {}
        self._run_head: dict[int, str] = {}
        self._next_artifact_id = 1

    def add_pr(self, pr: ModelPrHead) -> None:
        self._prs.append(pr)

    def add_run(self, run: ModelWorkflowRunSummary) -> None:
        self._runs.append(run)
        self._run_head[run.id] = run.head_sha

    def add_artifact(
        self, run_id: int, name: str, archive: bytes, *, expired: bool = False
    ) -> None:
        summary = ModelArtifactSummary(
            id=self._next_artifact_id,
            name=name,
            size_in_bytes=len(archive),
            expired=expired,
        )
        self._next_artifact_id += 1
        self._artifacts.setdefault(run_id, []).append((summary, archive))

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.requests.append(request)
        if self.unreachable:
            raise GatewayReadError("no answer from the gateway")
        if request.operation is _Op.PR_HEADS:
            return ModelPrHeadsResult(
                repo=request.repo,
                truncated=self.heads_truncated,
                prs=sorted(self._prs, key=lambda pr: -pr.number),
            )
        if request.operation is _Op.WORKFLOW_RUNS:
            if request.head_sha in self.unreachable_heads:
                raise GatewayReadError("no answer for this head commit")
            return ModelWorkflowRunsResult(
                repo=request.repo,
                truncated=False,
                runs=[run for run in self._runs if run.head_sha == request.head_sha],
            )
        if request.operation is _Op.ARTIFACTS:
            assert request.run_id is not None
            if self._run_head.get(request.run_id) in self.unreachable_heads:
                raise GatewayReadError("no answer for this run")
            return self._artifacts_of(request, request.run_id)
        raise GatewayReadError(f"operation {request.operation.value} not served")

    def _artifacts_of(
        self, request: ModelGithubGatewayRequest, run_id: int
    ) -> ModelArtifactsResult:
        held = self._artifacts.get(run_id, [])
        fetched: ModelArtifactContent | None = None
        refused: EnumArtifactFetchRefusal | None = None
        if request.artifact_id is not None:
            match = [(s, b) for s, b in held if s.id == request.artifact_id]
            if not match:
                refused = EnumArtifactFetchRefusal.NOT_IN_RUN
            else:
                summary, archive = match[0]
                if summary.expired:
                    refused = EnumArtifactFetchRefusal.EXPIRED
                elif len(archive) > request.max_bytes:
                    refused = EnumArtifactFetchRefusal.OVER_SIZE_CAP
                else:
                    fetched = ModelArtifactContent(
                        artifact_id=summary.id,
                        name=summary.name,
                        size_bytes=len(archive),
                        sha256=hashlib.sha256(archive).hexdigest(),
                        content_base64=base64.b64encode(archive).decode("ascii"),
                    )
        return ModelArtifactsResult(
            repo=request.repo,
            run_id=run_id,
            truncated=False,
            artifacts=[s for s, _ in held],
            fetched=fetched,
            fetch_refused=refused,
        )
