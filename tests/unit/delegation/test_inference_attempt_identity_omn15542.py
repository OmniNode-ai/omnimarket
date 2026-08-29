# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""OMN-15542: bind an inference response to the attempt that produced it.

Live defect this pins (2026-07-30 ``.201`` 13-class dev matrix, correlation
``7a300730-...011``): a durable delegation terminal carried an internally
impossible provenance pair —

* ``provider = https://generativelanguage.googleapis.com/v1beta/openai/...``
* ``model_name = Qwen3.6-35B-A3B``
* ``escalation_count = 1``

Mechanism: ``ModelInferenceIntent`` / ``ModelInferenceResponseData`` carried
only the workflow ``correlation_id`` and no per-attempt identity.
``HandlerDelegationWorkflow.handle_inference_response`` looked the workflow up
by that root correlation and, while the workflow was ``ROUTED``, accepted the
response against whatever ``routing_decision`` was *current*. After an
escalation reset the route, a delayed response from the PRIOR (local) attempt
was therefore combined with the NEW (cloud) endpoint and tier. The inference
effect stamps ``model_used=intent.model`` truthfully; the loss is entirely at
response-to-attempt correlation.

The wire half already exists: ``omnibase-core`` carries the optional
``inference_attempt_id: UUID | None`` on both DTOs (landed under OMN-15539,
released in 0.46.13, which is the version omnimarket pins and locks). These
tests cover the omnimarket half — minting, echoing, and enforcing it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_LOCAL_MODEL = "qwen3-coder-30b"
_LOCAL_ENDPOINT = "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for the local AIPC LLM endpoint"
_CLOUD_MODEL = "gemini-2.5-flash"
_CLOUD_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_decision(
    correlation_id: UUID,
    *,
    tier_name: str,
    selected_model: str,
    endpoint_url: str,
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model=selected_model,
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}/{selected_model}"
        ),
        endpoint_url=endpoint_url,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
        tier_name=tier_name,
    )


def _local_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return _make_decision(
        correlation_id,
        tier_name="local",
        selected_model=_LOCAL_MODEL,
        endpoint_url=_LOCAL_ENDPOINT,
    )


def _cloud_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return _make_decision(
        correlation_id,
        tier_name="cheap_cloud",
        selected_model=_CLOUD_MODEL,
        endpoint_url=_CLOUD_ENDPOINT,
    )


def _make_response(
    correlation_id: UUID,
    *,
    content: str,
    model_used: str,
    inference_attempt_id: UUID | None,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        inference_attempt_id=inference_attempt_id,
        content=content,
        model_used=model_used,
    )


def _error_response(
    correlation_id: UUID, *, error_message: str
) -> ModelInferenceResponseData:
    """A transport-class error on the local tier, which escalates the ladder."""
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="",
        model_used=_LOCAL_MODEL,
        latency_ms=50,
        error_message=error_message,
    )


def _escalate_to_cloud(
    handler: HandlerDelegationWorkflow, cid: UUID
) -> tuple[UUID, UUID]:
    """Drive local -> transport error -> escalate -> cloud route.

    Returns ``(local_attempt_id, cloud_attempt_id)``: the identity of the
    superseded local attempt and of the live cloud attempt.
    """
    handler.handle_delegation_request(_make_request(cid))

    local_intents = handler.handle_routing_decision(_local_decision(cid))
    local_intent = next(i for i in local_intents if isinstance(i, ModelInferenceIntent))
    assert local_intent.inference_attempt_id is not None
    local_attempt_id = local_intent.inference_attempt_id

    escalation = handler.handle_inference_response(
        _error_response(cid, error_message="401 Unauthorized: missing API key")
    )
    assert any(isinstance(e, ModelRoutingIntent) for e in escalation), (
        "fixture precondition: the local transport error must escalate the ladder"
    )

    cloud_intents = handler.handle_routing_decision(_cloud_decision(cid))
    cloud_intent = next(i for i in cloud_intents if isinstance(i, ModelInferenceIntent))
    assert cloud_intent.inference_attempt_id is not None
    cloud_attempt_id = cloud_intent.inference_attempt_id

    assert local_attempt_id != cloud_attempt_id
    return local_attempt_id, cloud_attempt_id


@pytest.mark.unit
@pytest.mark.usefixtures("frontier_unconfigured_bifrost")
class TestInferenceAttemptIdentity:
    """AC1-AC5: response<->route identity binding across an escalation."""

    def test_every_emitted_inference_intent_carries_a_distinct_attempt_id(self) -> None:
        """AC1: the orchestrator mints a typed UUID attempt identity per attempt."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        local_attempt_id, cloud_attempt_id = _escalate_to_cloud(handler, cid)

        assert isinstance(local_attempt_id, UUID)
        assert isinstance(cloud_attempt_id, UUID)
        # AC2: the workflow durably records the ONE current in-flight attempt.
        assert handler.workflows[cid].current_inference_attempt_id == cloud_attempt_id

    def test_delayed_local_response_is_rejected_after_escalation_to_cloud(self) -> None:
        """AC4 (the reported defect): the delayed LOCAL response must not reach
        the quality gate once the workflow has re-routed to the cloud tier."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        local_attempt_id, cloud_attempt_id = _escalate_to_cloud(handler, cid)
        workflow = handler.workflows[cid]

        stale = handler.handle_inference_response(
            _make_response(
                cid,
                content="local Qwen draft that arrived late",
                model_used="Qwen3.6-35B-A3B",
                inference_attempt_id=local_attempt_id,
            )
        )

        # Rejected: no gate intent, and NO workflow state was mutated.
        assert stale == []
        assert not any(isinstance(e, ModelQualityGateIntent) for e in stale)
        assert workflow.state == EnumDelegationState.ROUTED
        assert workflow.inference_content is None
        assert workflow.inference_model_used is None
        assert workflow.current_inference_attempt_id == cloud_attempt_id
        assert workflow.routing_decision is not None
        assert workflow.routing_decision.endpoint_url == _CLOUD_ENDPOINT

    def test_current_cloud_response_is_accepted_and_stays_coherent(self) -> None:
        """AC4 (the other half): the response from the CURRENT attempt reaches
        the gate and its model/endpoint/tier remain internally coherent."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        local_attempt_id, cloud_attempt_id = _escalate_to_cloud(handler, cid)

        # The stale one arrives first and must not poison the workflow.
        handler.handle_inference_response(
            _make_response(
                cid,
                content="local Qwen draft that arrived late",
                model_used="Qwen3.6-35B-A3B",
                inference_attempt_id=local_attempt_id,
            )
        )

        accepted = handler.handle_inference_response(
            _make_response(
                cid,
                content="def test_verify_registration():\n    assert True",
                model_used=_CLOUD_MODEL,
                inference_attempt_id=cloud_attempt_id,
            )
        )

        gate_intents = [e for e in accepted if isinstance(e, ModelQualityGateIntent)]
        assert len(gate_intents) == 1

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.INFERENCE_COMPLETED
        # The provenance pair the live terminal got wrong: a Gemini route must
        # never carry a Qwen model identity.
        assert workflow.inference_model_used == _CLOUD_MODEL
        assert workflow.routing_decision is not None
        assert workflow.routing_decision.endpoint_url == _CLOUD_ENDPOINT
        assert workflow.routing_decision.tier_name == "cheap_cloud"

    def test_attempt_id_less_response_is_accepted_documented_residual(self) -> None:
        """AC3, as decided: an id-less response is ACCEPTED, deliberately.

        The ticket asked for a model-name fallback. Both readings of it were
        implemented and measured, and both reject GOOD responses on the live
        escalation path (see ``_response_belongs_to_current_attempt``): strict
        ``model_used == selected_model`` fails on the provider/contract model
        divergence OMN-16419 records live, and the narrower "attributable to a
        superseded tier" form fails on the OMN-14402 same-tier sibling retry —
        it stopped three existing escalation tests from escalating at all. A
        dropped response leaves the workflow ROUTED with no terminal, so the
        heuristic trades a provenance defect for an availability defect.

        This test pins the accepted behaviour so the residual is explicit and a
        future re-introduction is a deliberate, visible change rather than a
        silent one. The residual window is bounded: the orchestrator and the
        inference effect ship in the same artifact, so only events already on
        the bus at deploy time lack an id.
        """
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        _escalate_to_cloud(handler, cid)
        workflow = handler.workflows[cid]
        assert [a.model_used for a in workflow.escalation_history] == [_LOCAL_MODEL]

        accepted = handler.handle_inference_response(
            _make_response(
                cid,
                content="pre-OMN-15542 wire, no attempt id",
                model_used=_LOCAL_MODEL,
                inference_attempt_id=None,
            )
        )

        assert len([e for e in accepted if isinstance(e, ModelQualityGateIntent)]) == 1

    def test_legacy_response_matching_the_current_route_is_still_accepted(self) -> None:
        """The guard must not reject a legitimate pre-upgrade response from the
        CURRENT route during the rollout window."""
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        _escalate_to_cloud(handler, cid)

        accepted = handler.handle_inference_response(
            _make_response(
                cid,
                content="def test_verify_registration():\n    assert True",
                model_used=_CLOUD_MODEL,
                inference_attempt_id=None,
            )
        )

        assert len([e for e in accepted if isinstance(e, ModelQualityGateIntent)]) == 1
        assert handler.workflows[cid].inference_model_used == _CLOUD_MODEL

    def test_state_codec_round_trips_the_current_attempt_id(self) -> None:
        """AC5: the attempt identity survives durable state persistence — a leg
        replayed in another process must still reject the superseded attempt."""
        from omnimarket.nodes.node_delegation_orchestrator import state_codec

        handler = HandlerDelegationWorkflow()
        cid = uuid4()
        _, cloud_attempt_id = _escalate_to_cloud(handler, cid)

        decoded = state_codec.decode(state_codec.encode(handler.workflows[cid]))

        assert decoded.current_inference_attempt_id == cloud_attempt_id
        assert isinstance(decoded.current_inference_attempt_id, UUID)


@pytest.mark.unit
class TestInferenceEffectEchoesAttemptId:
    """AC1 (effect half): the inference effect echoes the attempt identity on
    BOTH the success and the error response path."""

    @staticmethod
    def _intent(attempt_id: UUID) -> ModelInferenceIntent:
        return ModelInferenceIntent(
            base_url=_LOCAL_ENDPOINT,
            model=_LOCAL_MODEL,
            system_prompt="s",
            prompt="p",
            max_tokens=128,
            correlation_id=uuid4(),
            inference_attempt_id=attempt_id,
        )

    def test_round_trip_fields_carry_the_attempt_id(self) -> None:
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
            _attempt_round_trip_fields,
        )

        attempt_id = uuid4()
        fields = _attempt_round_trip_fields(self._intent(attempt_id))

        assert fields == {"inference_attempt_id": attempt_id}

        response = ModelInferenceResponseData(
            correlation_id=uuid4(),
            content="ok",
            model_used=_LOCAL_MODEL,
            **fields,
        )
        assert response.inference_attempt_id == attempt_id

    def test_round_trip_fields_are_empty_for_an_unstamped_intent(self) -> None:
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
            _attempt_round_trip_fields,
        )

        intent = ModelInferenceIntent(
            base_url=_LOCAL_ENDPOINT,
            model=_LOCAL_MODEL,
            system_prompt="s",
            prompt="p",
            max_tokens=128,
            correlation_id=uuid4(),
        )
        assert _attempt_round_trip_fields(intent) == {}
