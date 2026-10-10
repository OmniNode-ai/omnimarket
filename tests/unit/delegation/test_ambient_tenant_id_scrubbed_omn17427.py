# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 — unit tests must not inherit a lane runner's ONEX_TENANT_ID.

The ambient single-tenant fallback adds a tenant-attributed delegation terminal
and a savings-runner tenant-registry lookup the test never requested. CI exports
no ONEX_TENANT_ID, so it cannot see the leak otherwise. This module sets one
BEFORE the function-scoped autouse isolation fixture runs, just as a lane shell
would, by using a module-scoped fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_REDUCER_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "contract.yaml"
)
# The routing request the boundary terminal answers for, read from the
# reducer's own contract rather than restated here.
(_ROUTING_REQUEST_TOPIC,) = yaml.safe_load(
    _REDUCER_CONTRACT.read_text(encoding="utf-8")
)["event_bus"]["subscribe_topics"]


@pytest.fixture(scope="module")
def _ambient_lane_tenant() -> Iterator[None]:
    """Set the lane tenant before function-scoped autouse fixtures run."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ONEX_TENANT_ID", "820272f9-4aaf-5add-a2df-0af942852ab2")
        yield


@pytest.mark.usefixtures("_ambient_lane_tenant")
def test_ambient_tenant_id_is_scrubbed() -> None:
    assert "ONEX_TENANT_ID" not in os.environ, (
        "OMN-17427: the lane runner's ambient ONEX_TENANT_ID leaked into a unit test"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_ambient_lane_tenant")
async def test_ambient_tenant_does_not_add_a_terminal() -> None:
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(
        ModelDelegationRequest(
            prompt="Reply with the single word: alive.",
            task_type="research",
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
        )
    )
    events = await handler.handle(
        ModelBoundaryFailureTerminal(
            correlation_id=correlation_id,
            failure_class="ProtocolConfigurationError",
            failure_code="ONEX_CORE_041_INVALID_CONFIGURATION",
            retryable=False,
            failure_reason=(
                "ProtocolConfigurationError: [ONEX_CORE_041_INVALID_CONFIGURATION] "
                "No tier has a configured endpoint for task_type='summarization'"
            ),
            origin_topic=_ROUTING_REQUEST_TOPIC,
        )
    )
    assert len(events) == 1, (
        "OMN-17427: ambient ONEX_TENANT_ID added an unrequested tenant terminal; "
        f"got {[type(event).__name__ for event in events]}"
    )
    assert type(events[0]).__name__ == "ModelDelegationFailed"
