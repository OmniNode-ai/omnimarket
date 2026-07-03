# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded-from-real delegation golden chain (OMN-13499 reference migration).

This is the Phase-0 REFERENCE chain migrated end-to-end to the canonical
``omnibase_core.runtime.golden_chain`` harness. It replaces the previous
``patch("httpx.Client")`` boundary-fake (which returned canned bytes and hid the
real request the handler constructs) with the canonical
``RecordedReplayInferenceTransport``.

What runs LIVE on replay (everything except the model response bytes):

  * ``HandlerDelegationWorkflow.handle_delegation_request`` — routing intents,
  * ``HandlerRoutingIntent`` — routing-contract resolution + backend selection,
  * ``handle_routing_decision`` — inference-intent + endpoint/model resolution,
  * ``HandlerInferenceIntent._call_llm`` — REAL OpenAI-compatible request
    construction (system prompt + inference-protocol shaping + max_tokens +
    temperature + chat_template_kwargs) and the POST,
  * ``HandlerQualityGateIntent`` — quality gate over the artifact.

Only the model's HTTP response bytes come from the provenance-stamped fixture
``tests/fixtures/golden_chain/delegation_test_artifact_chain.json`` — recorded
once from the real request the live path constructs (concrete model
``Qwen3.6-35B-A3B`` at the resolved endpoint), pinned by ``request_hash``.

REPLAY IS EVIDENCE, NOT AUTHORITY — the planted routing-failure test below proves
a chain that resolves the WRONG model FAILS the replay (REQUEST_HASH_MISMATCH)
rather than "succeeding anyway".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from omnibase_core.runtime.golden_chain import (
    EnumGoldenChainFailureClass,
    GoldenChainReplayError,
    RecordedReplayInferenceTransport,
    load_fixture,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden_chain"
_FIXTURE_PATH = _FIXTURE_DIR / "delegation_test_artifact_chain.json"
_BIFROST_CONTRACT_PATH = _FIXTURE_DIR / "bifrost_delegation_reference.yaml"

_EXPECTED_MARKERS = ("@pytest.mark.unit", "with pytest.raises", "Edge case")


@pytest.fixture(autouse=True)
def _bifrost_contract(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the live router at the SAME committed contract the fixture recorded.

    The fixture's ``routing_contract_hash`` was recorded against
    ``bifrost_delegation_reference.yaml``; the chain resolves its route from that
    exact file so routing runs for real and the recorded request matches.
    """
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_BIFROST_CONTRACT_PATH))
    yield
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()


def _run_chain_to_inference_intent(prompt: str) -> ModelInferenceIntent:
    """Run routing + intent construction LIVE and return the inference intent."""
    workflow = HandlerDelegationWorkflow(workflows={})
    request = ModelDelegationRequest(
        prompt=prompt,
        task_type="test",
        correlation_id=uuid4(),
        max_tokens=4096,
        emitted_at=datetime.now(UTC),
    )
    routing_intents = workflow.handle_delegation_request(request)
    decision = HandlerRoutingIntent().handle(routing_intents[0])
    assert decision.task_type == "test"
    assert "final_artifact_only" in decision.dod_deterministic
    inference_intents = workflow.handle_routing_decision(decision)
    assert len(inference_intents) == 1
    intent = inference_intents[0]
    assert isinstance(intent, ModelInferenceIntent)
    return intent


@pytest.mark.integration
def test_delegation_chain_returns_useful_task_artifact() -> None:
    """End-to-end: real routing + real request construction + recorded replay + gate."""
    fixture = load_fixture(_FIXTURE_PATH)
    transport = RecordedReplayInferenceTransport([fixture])

    workflow = HandlerDelegationWorkflow(workflows={})
    request = ModelDelegationRequest(
        prompt="Write pytest unit tests for omnibase_infra normalize_unit_state(state: str).",
        task_type="test",
        correlation_id=uuid4(),
        max_tokens=4096,
        emitted_at=datetime.now(UTC),
    )
    routing_intents = workflow.handle_delegation_request(request)
    decision = HandlerRoutingIntent().handle(routing_intents[0])
    inference_intents = workflow.handle_routing_decision(decision)
    intent = inference_intents[0]

    # Swap ONLY the httpx.Client the inference handler uses. The handler still
    # constructs the OpenAI-compatible request LIVE; the transport returns the
    # recorded bytes only because the live request matches the recorded route +
    # request_hash.
    with patch("httpx.Client", return_value=transport):
        response = HandlerInferenceIntent().handle(intent)

    assert response.error_message == ""
    for marker in _EXPECTED_MARKERS:
        assert marker in response.content

    # The live path resolved the CONCRETE recorded model, not a tier name.
    assert transport.calls[0]["model"] == fixture.provenance.model_id

    gate_intents = workflow.handle_inference_response(response)
    assert len(gate_intents) == 1
    assert isinstance(gate_intents[0], ModelQualityGateIntent)
    gate_result = HandlerQualityGateIntent().handle(gate_intents[0])
    assert gate_result.passed is True
    assert gate_result.failure_reasons == ()


@pytest.mark.integration
def test_replay_is_deterministic_across_runs() -> None:
    """Replay returns identical recorded bytes on repeated runs (offline-of-model)."""
    fixture = load_fixture(_FIXTURE_PATH)
    intent = _run_chain_to_inference_intent(
        "Write pytest unit tests for omnibase_infra normalize_unit_state(state: str)."
    )
    contents = []
    for _ in range(2):
        transport = RecordedReplayInferenceTransport([fixture])
        with patch("httpx.Client", return_value=transport):
            response = HandlerInferenceIntent().handle(intent)
        contents.append(response.content)
    assert contents[0] == contents[1]


# ---------------------------------------------------------------------------
# THE ROUTING-FAILURE PROOF — replay is EVIDENCE, not AUTHORITY.
# A chain that resolves the WRONG model must FAIL the replay, not pass anyway.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_wrong_model_route_fails_replay_not_pass_anyway() -> None:
    """Planted wrong-route: the live path posts a DIFFERENT model than recorded.

    The handler still constructs a real request and posts it, but because the
    resolved model (hence request_hash) differs from the recorded fixture, the
    canonical transport raises REQUEST_HASH_MISMATCH. A fake adapter would have
    returned its canned bytes and let the broken route ship green — this proves
    the harness cannot hide a broken route.
    """
    fixture = load_fixture(_FIXTURE_PATH)
    transport = RecordedReplayInferenceTransport([fixture])
    intent = _run_chain_to_inference_intent(
        "Write pytest unit tests for omnibase_infra normalize_unit_state(state: str)."
    )
    # Plant the wrong route: same endpoint, but a different concrete model than
    # the fixture was recorded against (simulates a routing/selection regression).
    wrong_intent = intent.model_copy(update={"model": "gpt-4o-wrong-route"})

    with (
        pytest.raises(GoldenChainReplayError) as exc,
        patch("httpx.Client", return_value=transport),
    ):
        HandlerInferenceIntent()._call_llm(wrong_intent, "call-1")
    assert exc.value.failure_class is EnumGoldenChainFailureClass.REQUEST_HASH_MISMATCH


@pytest.mark.integration
def test_tier_name_as_model_fails_route_not_resolved() -> None:
    """A delegation TIER name reaching inference as the model fails closed."""
    fixture = load_fixture(_FIXTURE_PATH)
    transport = RecordedReplayInferenceTransport([fixture])
    intent = _run_chain_to_inference_intent(
        "Write pytest unit tests for omnibase_infra normalize_unit_state(state: str)."
    )
    tier_intent = intent.model_copy(update={"model": "cheap_cloud"})
    with (
        pytest.raises(GoldenChainReplayError) as exc,
        patch("httpx.Client", return_value=transport),
    ):
        HandlerInferenceIntent()._call_llm(tier_intent, "call-2")
    assert exc.value.failure_class is EnumGoldenChainFailureClass.ROUTE_NOT_RESOLVED
