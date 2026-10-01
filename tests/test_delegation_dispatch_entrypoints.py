# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20187: delegation handlers expose the entrypoint auto-wiring binds."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
)

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)
from omnimarket.nodes.node_model_router.models.model_routing_result import (
    ModelRoutingResult,
)

pytestmark = pytest.mark.unit
_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"


async def test_model_router_dispatch_returns_routing_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the real callback and the contract-declared request model."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    (contract,) = discover_contracts_from_paths(
        [_NODES / "node_model_router" / "contract.yaml"]
    ).contracts
    (entry,) = contract.handler_routing.handlers
    assert entry.event_model is not None
    assert entry.event_model.name == ModelRoutingRequest.__name__
    router = HandlerModelRouter(
        policy=ModelRoutingPolicy(
            primary="test-primary",
            timeout_per_attempt_s=1.0,
            max_retries=1,
        ),
        registry={"test-primary": {"base_url": "https://router.invalid"}},
    )
    request = ModelRoutingRequest(prompt="hello", role="fixer", correlation_id="test")
    # No health_path means routing performs no network I/O, per existing logic.
    callback = _make_dispatch_callback(router, entry.event_model)
    result = await callback(ModelEventEnvelope[object](payload=request.model_dump()))

    assert result is not None
    assert result.output_events == [
        ModelRoutingResult(
            model_key="test-primary",
            endpoint_url="https://router.invalid",
            used_fallback=False,
            correlation_id=request.correlation_id,
        )
    ]
