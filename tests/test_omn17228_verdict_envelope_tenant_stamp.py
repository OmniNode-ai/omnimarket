# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17228: the orchestrator records the delegation's tenant on every envelope
it publishes, so the quality-gate verdict is attributable when it reaches the
projection writer.

MEASURED CAUSE (onex-dev, deploy-onex-staging run 35063077145, job
104687512372, 2026-09-16T06:32:33Z). The terminal business proof failed on
``quality_gate`` alone, and the restored unscoped existence probe answered::

    existence_probe=ROW_EXISTS row for correlation
    acd78c36-94cb-4151-92d6-9cb6545c30c3 exists under
    tenant=820272f9-4aaf-5add-a2df-0af942852ab2 ; reader queried
    tenant=91c74442-1233-4c97-b191-911a10346fdf
    (positive control: the relation holds 380 row(s) in total)

``820272f9-4aaf-5add-a2df-0af942852ab2`` is not another representation of the
submitting tenant. It is ``uuid5(NAMESPACE_DNS, "house-tenant.omninode.ai")`` --
the HOUSE tenant, a different tenant entirely (``tenant_isolation.py`` derives
the same value and ``test_house_tenant_identity.py`` re-derives it). So this is
NOT the OMN-15683 slug-vs-UUID form split, which is Done and settled the
canonical form as the registry UUID. It is a MISATTRIBUTION: the verdict row was
authored under a tenant that did not submit the delegation.

WHY THE VERDICT AND ONLY THE VERDICT. Every other event on this chain carries
its tenant in its own PAYLOAD, and both terminal write paths resolve it from
there (``handler_projection_delegation`` lines 812 and 951), which is why the
reader's own page of rows sits under the registry tenant.
``ModelQualityGateResult`` is ``frozen``/``extra="forbid"`` with no tenant field,
so for that one event the producer-recorded attribution is the ENVELOPE stamp
``ModelEventEnvelope.tenant_id`` -- the seam OMN-17422 built and
``envelope.envelope_tenant_identity`` reads.

Nothing on this chain ever wrote that stamp. ``to_canonical_wire_envelope``
(onex-api) puts the verified tenant in ``payload.tenant_id`` and in
``metadata.tags.source_tenant_id`` but sets no envelope-level ``tenant_id``, and
``DispatcherDelegationWorkflow._publish_events_direct`` -- which publishes the
quality-gate-request intent the reducer consumes -- constructed its envelopes
without one. ``service_dispatch_result_applier`` then carries
``consumed_envelope.tenant_id`` faithfully (OMN-16831) and carries ``None``,
because ``None`` is what it was handed. The writer's house fallback
(``handler_projection_delegation`` line 1146) then authored the house tenant.

THE FIX IS AT THE PRODUCER, AND IT IS A CARRY, NOT AN INVENTION. The
orchestrator already holds this delegation's tenant: ``_resolve_tenant_id``
resolves it from the gateway-verified ``ModelDelegationRequest.tenant_id`` and
stamps it onto the terminal payloads it emits. The same value now rides the
envelope of every event the orchestrator's dispatchers publish, so the verdict
that returns from the reducer is attributable to the tenant that submitted it.
No slug-or-UUID fallback is added anywhere, and no reader-side fallback: there
is one tenant identity and the producer records it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_INFERENCE_REQUEST,
    TOPIC_ID_QUALITY_GATE_REQUEST,
    TOPIC_ID_ROUTING_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_workflow import (
    DispatcherDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    DispatcherQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.projection.envelope import envelope_tenant_identity
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

# The two identities the live probe printed, verbatim.
BETA_TENANT_UUID = "91c74442-1233-4c97-b191-911a10346fdf"
_TEST_ENDPOINT_URL = "http://delegation-llm.test:8000"


def _mock_bus() -> MagicMock:
    bus = MagicMock(spec=ProtocolEventBus)
    bus.publish_envelope = AsyncMock()
    return bus


def _envelope(payload: object, correlation_id: UUID) -> ModelEventEnvelope[object]:
    """An inbound envelope with NO tenant stamp.

    Deliberately unattributed: this is the shape the onex-api gateway actually
    publishes (``to_canonical_wire_envelope`` writes ``payload.tenant_id`` and
    ``metadata.tags.source_tenant_id``, never an envelope-level ``tenant_id``).
    The orchestrator must therefore record the tenant from the delegation it is
    already tracking, not merely forward one it was handed.
    """
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
    )


def _published(bus: MagicMock, topic: str) -> list[ModelEventEnvelope[object]]:
    out: list[ModelEventEnvelope[object]] = []
    for call in bus.publish_envelope.call_args_list:
        if call.kwargs.get("topic") != topic:
            continue
        envelope = call.kwargs.get("envelope")
        if envelope is None and call.args:
            envelope = call.args[0]
        out.append(envelope)
    return out


async def _drive_to_quality_gate_request(
    bus: MagicMock,
    *,
    tenant_id: str | None,
) -> tuple[HandlerDelegationWorkflow, DispatcherDelegationWorkflow, UUID]:
    """Drive the REAL dispatch path to the hop that emits the quality-gate intent.

    Every step goes through ``DispatcherDelegationWorkflow.handle``, which is
    what ``wiring.py`` registers on the MessageDispatchEngine and what publishes
    the intent envelopes on the lane -- not a direct call into the FSM.
    """
    handler = HandlerDelegationWorkflow()
    dispatcher = DispatcherDelegationWorkflow(handler, event_bus=bus)  # type: ignore[arg-type]
    correlation_id = uuid4()

    request = ModelDelegationRequest(
        prompt="Summarise the deploy",
        task_type="summarization",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        tenant_id=tenant_id,
    )
    await dispatcher.handle(_envelope(request, correlation_id))

    decision = ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="summarization",
        selected_model="glm-5.3-flash",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/glm-5.3-flash"),
        endpoint_url=_TEST_ENDPOINT_URL,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are an assistant.",
        rationale="Routing test.",
    )
    await dispatcher.handle(_envelope(decision, correlation_id))

    response = ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="The deploy succeeded.",
        model_used="glm-5.3-flash",
        llm_call_id="chatcmpl-omn17228",
        latency_ms=14912,
        prompt_tokens=120,
        completion_tokens=67,
        total_tokens=187,
    )
    await dispatcher.handle(_envelope(response, correlation_id))

    return handler, dispatcher, correlation_id


@pytest.mark.unit
@pytest.mark.asyncio
class TestOrchestratorRecordsTheTenantOnEveryEnvelopeItPublishes:
    """RED before this change: every envelope below carried ``tenant_id=None``."""

    async def test_quality_gate_request_envelope_carries_the_delegation_tenant(
        self,
    ) -> None:
        """The load-bearing one.

        The quality-gate-request envelope is what the reducer consumes, and
        ``service_dispatch_result_applier`` copies ``consumed_envelope.tenant_id``
        onto the ``ModelQualityGateResult`` it publishes in return. An
        unattributed request therefore produces an unattributed verdict, which
        is the whole of the staging failure.
        """
        bus = _mock_bus()
        await _drive_to_quality_gate_request(bus, tenant_id=BETA_TENANT_UUID)

        envelopes = _published(bus, TOPIC_ID_QUALITY_GATE_REQUEST)
        assert envelopes, "no quality-gate-request was published"
        for envelope in envelopes:
            assert envelope.tenant_id == BETA_TENANT_UUID, (
                "the quality-gate-request envelope records no tenant, so the "
                "verdict that returns from the reducer is unattributable and "
                "the writer authors the house tenant "
                f"({HOUSE_TENANT_UUID}) onto a row the submitting tenant's "
                "reader can never see"
            )

    async def test_every_intent_envelope_on_the_chain_carries_it(self) -> None:
        """Not only the verdict hop.

        The routing and inference intents ride the same publish site. Stamping
        one and not the others would leave the carry depending on which hop a
        future consumer happens to read from.
        """
        bus = _mock_bus()
        await _drive_to_quality_gate_request(bus, tenant_id=BETA_TENANT_UUID)

        for topic in (TOPIC_ID_ROUTING_REQUEST, TOPIC_ID_INFERENCE_REQUEST):
            envelopes = _published(bus, topic)
            assert envelopes, f"nothing published to {topic}"
            for envelope in envelopes:
                assert envelope.tenant_id == BETA_TENANT_UUID, topic

    async def test_an_untenanted_delegation_records_nothing_rather_than_a_house_default(
        self,
    ) -> None:
        """Carried, never sourced.

        A delegation that recorded no tenant publishes envelopes that record
        none either. The house tenant is exactly what this change exists to stop
        being authored downstream, so it must not be authored here instead.
        """
        bus = _mock_bus()
        await _drive_to_quality_gate_request(bus, tenant_id=None)

        for envelope in _published(bus, TOPIC_ID_QUALITY_GATE_REQUEST):
            assert envelope.tenant_id is None, (
                "an untenanted delegation invented an identity at the publish "
                "site; None must stay None"
            )

    async def test_terminal_envelopes_from_the_verdict_dispatcher_carry_it(
        self,
    ) -> None:
        """``DispatcherQualityGateResult`` publishes the delegation terminals on
        its own direct-bus path, with the same envelope-construction site and
        the same omission."""
        bus = _mock_bus()
        handler, _dispatcher, correlation_id = await _drive_to_quality_gate_request(
            bus, tenant_id=BETA_TENANT_UUID
        )
        bus.publish_envelope.reset_mock()

        gate_dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]
        verdict = ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=True,
            quality_score=1.0,
            failure_reasons=(),
            fallback_recommended=False,
        )
        await gate_dispatcher.handle(_envelope(verdict, correlation_id))

        envelopes = _published(bus, TOPIC_ID_DELEGATION_COMPLETED)
        assert envelopes, "no delegation terminal was published"
        for envelope in envelopes:
            assert envelope.tenant_id == BETA_TENANT_UUID


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheStampIsTheIdentityTheProjectionWriterReads:
    """The cross-boundary half: the value the producer records is exactly the
    value the projection writer's attribution reader returns for it.

    Two independent assertions about ``tenant_id`` would not catch a producer
    that records the right identity in the wrong PLACE -- which is precisely the
    defect, since the gateway records it in ``payload.tenant_id`` and in
    ``metadata.tags`` and the writer reads neither.
    """

    async def test_envelope_tenant_identity_reads_back_what_the_producer_stamped(
        self,
    ) -> None:
        bus = _mock_bus()
        await _drive_to_quality_gate_request(bus, tenant_id=BETA_TENANT_UUID)
        envelopes = _published(bus, TOPIC_ID_QUALITY_GATE_REQUEST)
        assert envelopes

        # The shape the projection runner hands ``project_event``: the payload
        # with the whole raw envelope attached under ``_envelope``.
        wire = envelopes[0].model_dump(mode="json")
        unwrapped = dict(wire["payload"]) if isinstance(wire["payload"], dict) else {}
        unwrapped["_envelope"] = wire

        assert envelope_tenant_identity(unwrapped) == BETA_TENANT_UUID
        assert envelope_tenant_identity(unwrapped) != str(HOUSE_TENANT_UUID)
