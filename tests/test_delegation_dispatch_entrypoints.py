# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20187: delegation handlers expose the entrypoint auto-wiring binds."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

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
    PricingMap,
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

_AB_COMPARE_PRICING: PricingMap = {
    "cloud": {
        "display_name": "Cloud model",
        "cost_per_1k_input": 0.003,
        "cost_per_1k_output": 0.015,
    }
}


def _ab_compare_handle_corpus() -> list[ModelAbCompareState]:
    """The adequacy recorder and parity test use these same contract inputs."""
    results = [
        ModelInferenceResultEntry(
            model_key=model_key,
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
            latency_ms=50,
            correlation_id="ab-flip",
        )
        for model_key in ("cloud", "unpriced-local")
    ]
    return [
        ModelAbCompareState(
            correlation_id="ab-flip", expected_count=2, completed=True, results=results
        ),
        ModelAbCompareState(
            correlation_id="ab-flip", expected_count=2, results=results[:1]
        ),
        ModelAbCompareState(correlation_id="ab-flip", expected_count=2),
        ModelAbCompareState(correlation_id="ab-flip", expected_count=2, completed=True),
    ]


def test_ab_compare_reducer_handle_matches_materialize_corpus() -> None:
    """The new adapter preserves materialization, including constructor pricing."""
    assert callable(getattr(HandlerAbCompareReducer, "handle", None))
    for pricing in (None, _AB_COMPARE_PRICING):
        handler = HandlerAbCompareReducer(pricing=pricing)
        for state in _ab_compare_handle_corpus():
            assert handler.handle(state) == handler.materialize(state, pricing or {})


def _verified_dispatch_handle_corpus() -> list[ModelDispatchRequest]:
    """Pass, retry then pass, and both escalation policies; no live services."""
    return [
        ModelDispatchRequest(
            ticket_id=ticket_id,
            worker_prompt="verify the adapter boundary",
            max_attempts=max_attempts,
            cooldown_seconds=0,
            escalation_action=action,
            correlation_id=None if ticket_id == "pass" else "dispatch-flip",
        )
        for ticket_id, max_attempts, action in (
            ("pass", 1, "linear_ticket"),
            ("retry", 3, "linear_ticket"),
            ("escalate-linear", 2, "linear_ticket"),
            ("escalate-human", 2, "human_review"),
        )
    ]


class _CorpusVerifiedDispatchHandler(HandlerVerifiedDispatchOrchestrator):
    """Inject probe outcomes while executing the preserved dispatch/verifier loop."""

    def __init__(self) -> None:
        self._attempt = 0

    def _run_worker(self, *, worker_run_id: str, prompt: str, ticket_id: str) -> str:
        self._attempt += 1
        return super()._run_worker(
            worker_run_id=worker_run_id, prompt=prompt, ticket_id=ticket_id
        )

    def _probe_surface(
        self, *, surface: str, worker_claim: str, ticket_id: str
    ) -> tuple[bool, str]:
        passed, result = super()._probe_surface(
            surface=surface, worker_claim=worker_claim, ticket_id=ticket_id
        )
        if ticket_id.startswith("escalate") or (
            ticket_id == "retry" and self._attempt == 1
        ):
            return False, f"surface={surface} rejected attempt={self._attempt}"
        return passed, result


def _run_verified_dispatch_corpus_request(
    request: ModelDispatchRequest, *, canonical: bool = True
) -> dict[str, Any]:
    """Reset probe state and freeze only volatile UUID/time fields for parity."""
    module = HandlerVerifiedDispatchOrchestrator.__module__
    with (
        patch(f"{module}.uuid.uuid4", return_value=UUID(int=1)),
        patch(f"{module}.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 10, 3, tzinfo=UTC)
        handler = _CorpusVerifiedDispatchHandler()
        return handler.handle(request) if canonical else handler.dispatch(request)


def test_verified_dispatch_orchestrator_handle_matches_dispatch_corpus() -> None:
    """The adapter preserves bundles, retries, and escalation for every input."""
    assert callable(getattr(HandlerVerifiedDispatchOrchestrator, "handle", None))
    expected = [
        ("accept", 1, False),
        ("accept", 2, False),
        ("reject", 2, True),
        ("reject", 2, True),
    ]
    for request, outcome in zip(
        _verified_dispatch_handle_corpus(), expected, strict=True
    ):
        result = _run_verified_dispatch_corpus_request(request)
        assert result == _run_verified_dispatch_corpus_request(request, canonical=False)
        assert (
            result["decision"],
            result["attempt_count"],
            result["escalated"],
        ) == outcome


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
