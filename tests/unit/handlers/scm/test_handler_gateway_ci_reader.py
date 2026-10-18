# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway CI reader's typed request and mapping to the shared bridge model."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import pytest

from omnimarket.handlers.scm.handler_gateway_ci_reader import HandlerGatewayCiReader
from omnimarket.models.e2e_bridge import ModelE2ePrCiReading
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCiChecksResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
)

pytestmark = pytest.mark.unit

CID = UUID("11111111-2222-3333-4444-555555555555")
REPO = "OmniNode-ai/omnimarket"
PR = f"{REPO}#42"


@dataclass
class FakeGatewayOutput:
    """Gateway output exposing only the events the reader consumes."""

    events: tuple[object, ...]


class FakeGateway:
    """Records the gateway request and returns canned events."""

    def __init__(self, events: tuple[object, ...]) -> None:
        self.events = events
        self.requests: list[ModelGithubGatewayRequest] = []

    async def handle(self, request: ModelGithubGatewayRequest) -> FakeGatewayOutput:
        self.requests.append(request)
        return FakeGatewayOutput(events=self.events)


async def test_builds_a_ci_checks_gateway_request_with_the_pr_and_correlation_id() -> (
    None
):
    result = ModelCiChecksResult(
        repo=REPO,
        pr_number=42,
        overall="green",
        total=2,
        passed=2,
        failed=0,
        pending=0,
    )
    gateway = FakeGateway(
        (ModelGithubGatewayResponse(correlation_id=CID, result=result),)
    )

    await HandlerGatewayCiReader(gateway=gateway).read_ci(
        pr_ref=PR, repo=REPO, pr_number=42, correlation_id=CID
    )

    (request,) = gateway.requests
    assert request.operation is EnumGithubGatewayOperation.CI_CHECKS
    assert request.repo == REPO
    assert request.pr_number == 42
    assert request.correlation_id == CID


async def test_maps_the_ci_checks_result_to_a_shared_pr_reading() -> None:
    result = ModelCiChecksResult(
        repo=REPO,
        pr_number=42,
        overall="red",
        total=6,
        passed=2,
        failed=3,
        pending=1,
        failing_contexts=["lint", "types", "tests"],
    )
    gateway = FakeGateway(
        (
            object(),
            SimpleNamespace(result="unrelated result"),
            ModelGithubGatewayResponse(correlation_id=CID, result=result),
        )
    )
    pr_ref = f" {REPO} # 0042 "

    reading = await HandlerGatewayCiReader(gateway=gateway).read_ci(
        pr_ref=pr_ref, repo=REPO, pr_number=42, correlation_id=CID
    )

    assert reading == ModelE2ePrCiReading(
        pr_ref=pr_ref,
        overall="red",
        total=6,
        passed=2,
        failed=3,
        pending=1,
        failing_contexts=("lint", "types", "tests"),
    )


@pytest.mark.parametrize(
    "events", [(), (object(), SimpleNamespace(result="unrelated result"))]
)
async def test_raises_when_no_event_contains_a_ci_checks_result(
    events: tuple[object, ...],
) -> None:
    gateway = FakeGateway(events)

    with pytest.raises(
        RuntimeError, match=r"^the GitHub gateway returned no ci_checks result$"
    ):
        await HandlerGatewayCiReader(gateway=gateway).read_ci(
            pr_ref=PR, repo=REPO, pr_number=42, correlation_id=CID
        )
