# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI reads through the GitHub gateway, returned as the shared E2E bridge model.

The gateway effect runs in process and owns token resolution, transport and
dispatch. This adapter builds its typed ``ci_checks`` request and maps the
result to the reading consumed by the build-to-lifecycle bridge.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from omnimarket.models.e2e_bridge import ModelE2ePrCiReading
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
    HandlerGithubRepoGatewayEffect,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCiChecksResult,
    ModelGithubGatewayRequest,
)


class _ProtocolGatewayOutput(Protocol):
    """The result events returned by a gateway read."""

    @property
    def events(self) -> Sequence[object]: ...


class _ProtocolGateway(Protocol):
    """Runs a typed gateway request and returns its result events."""

    async def handle(
        self, request: ModelGithubGatewayRequest
    ) -> _ProtocolGatewayOutput: ...


class HandlerGatewayCiReader:
    """Read a PR's CI through the GitHub gateway effect."""

    def __init__(self, gateway: _ProtocolGateway | None = None) -> None:
        self._gateway = (
            gateway if gateway is not None else HandlerGithubRepoGatewayEffect()
        )

    async def read_ci(
        self, *, pr_ref: str, repo: str, pr_number: int, correlation_id: UUID
    ) -> ModelE2ePrCiReading:
        """Run one ``ci_checks`` request and map its result to a bridge reading."""
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.CI_CHECKS,
            repo=repo,
            pr_number=pr_number,
            correlation_id=correlation_id,
        )
        output = await self._gateway.handle(request)
        for event in output.events:
            result = getattr(event, "result", None)
            if isinstance(result, ModelCiChecksResult):
                return ModelE2ePrCiReading(
                    pr_ref=pr_ref,
                    overall=result.overall,
                    total=result.total,
                    passed=result.passed,
                    failed=result.failed,
                    pending=result.pending,
                    failing_contexts=tuple(result.failing_contexts),
                )
        raise RuntimeError("the GitHub gateway returned no ci_checks result")


__all__: list[str] = ["HandlerGatewayCiReader"]
