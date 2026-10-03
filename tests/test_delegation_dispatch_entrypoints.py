# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20187: delegation handlers expose the entrypoint auto-wiring binds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
)

from omnimarket.nodes.node_ab_compare_reducer.handlers.handler_ab_compare_reducer import (
    HandlerAbCompareReducer,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_ab_compare_state import (
    ModelAbCompareState,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_inference_result_entry import (
    ModelInferenceResultEntry,
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
from omnimarket.nodes.node_verified_dispatch_orchestrator.handlers.handler_verified_dispatch_orchestrator import (
    HandlerVerifiedDispatchOrchestrator,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_dispatch_request import (
    ModelDispatchRequest,
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


def _declared_entry(node: str) -> tuple[Any, Any]:
    """Return the discovered contract and its single declared handler entry."""
    (contract,) = discover_contracts_from_paths(
        [_NODES / node / "contract.yaml"]
    ).contracts
    (entry,) = contract.handler_routing.handlers
    return contract, entry


async def test_ab_compare_reducer_dispatch_materializes_completed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed state dispatched through the real callback yields the comparison."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    _, entry = _declared_entry("node_ab_compare_reducer")
    state = ModelAbCompareState(
        correlation_id="ab-corr",
        expected_count=1,
        completed=True,
        results=[
            ModelInferenceResultEntry(
                model_key="local-model",
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                correlation_id="ab-corr",
            )
        ],
    )
    callback = _make_dispatch_callback(HandlerAbCompareReducer(), entry.event_model)
    result = await callback(ModelEventEnvelope[object](payload=state.model_dump()))

    assert result is not None
    assert [
        (completed.correlation_id, completed.model_count)
        for completed in result.output_events
    ] == [("ab-corr", 1)]


async def test_verified_dispatch_orchestrator_dispatch_runs_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract request dispatched through the real callback runs the loop.

    The runtime publishes nothing for the handler's dict outcome, so the proof
    that the loop ran is the probes it made, not an output event.
    """
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    contract, entry = _declared_entry("node_verified_dispatch_orchestrator")
    probed: list[str] = []

    class _RecordingHandler(HandlerVerifiedDispatchOrchestrator):
        def _probe_surface(
            self, *, surface: str, worker_claim: str, ticket_id: str
        ) -> tuple[bool, str]:
            probed.append(surface)
            return super()._probe_surface(
                surface=surface, worker_claim=worker_claim, ticket_id=ticket_id
            )

    request = ModelDispatchRequest(
        ticket_id="OMN-1", worker_prompt="work", max_attempts=1, cooldown_seconds=0
    )
    callback = _make_dispatch_callback(_RecordingHandler(), entry.event_model)
    result = await callback(ModelEventEnvelope[object](payload=request.model_dump()))

    assert result is not None
    assert result.status == EnumDispatchStatus.SUCCESS
    assert len(probed) == 7
    # Known gap, pinned so closing it is a visible change: the contract declares
    # both topics below, yet the dict outcome is dropped and nothing is emitted.
    assert list(contract.event_bus.publish_topics) == [
        "onex.evt.omnimarket.verified-dispatch-completed.v1",
        "onex.cmd.omnimarket.verified-dispatch-escalate.v1",
    ]
    assert result.output_events == []
