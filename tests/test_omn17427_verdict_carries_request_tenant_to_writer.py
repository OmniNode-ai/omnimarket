# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: a quality-gate verdict carries the tenant of the request it grades,
across every hop, and the projection writer accepts it and fills ``score_source``.

THE MEASUREMENT THIS IS WRITTEN FROM. On the .201 dev lane (2026-10-03
02:00-06:00Z) every one of 1212 quality-gate-result verdicts recorded no tenant,
so the delegation writer refused each of them to its dead-letter topic and
``delegation_events.score_source`` stayed NULL on all 875 rows. The writer's
refusal is deliberate (OMN-18139) and is not what is under test here.

WHAT IS UNDER TEST. The producer chain, hop by hop, with nothing between the
request and the row replaced by a double:

1. the REAL orchestrator dispatcher publishes the quality-gate-request, as wire
   bytes, from a delegation request that carries a tenant in its PAYLOAD and
   whose inbound envelope records none (the shape every ingress that stamps only
   ``payload.tenant_id`` produces);
2. the REAL quality-gate reducer contract is wired on an in-memory bus, with the
   REAL runtime dispatch-result applier, and consumes those wire bytes; its
   verdict is read back off the terminal topic as wire bytes;
3. both REAL projection writers (the async runner and the sync handler the
   deployed ``omnimarket-projection-delegation-writer`` runs) are handed that
   verdict. Each must write a row under the request's tenant with
   ``score_source`` filled from the verdict.

The converse is pinned beside it: a request that carries NO tenant produces a
verdict that records none, and the writer still refuses it. Nothing here supplies
a default tenant at the producer, and the writer's check is not loosened, so an
untenanted request is attributable only by being given a tenant at its origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_QUALITY_GATE_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_workflow import (
    DispatcherDelegationWorkflow,
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
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import (
    envelope_event_timestamp,
    envelope_tenant_identity,
    unwrap_envelope,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_GATE_CONTRACT = _NODES / "node_delegation_quality_gate_reducer" / "contract.yaml"
_VERDICT_TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"

# A tenant the registry resolves without a database (the slug/UUID pair the
# OMN-17422 writer tests already use).
# A verifiable task class: only these get deterministic acceptance, and so only
# their verdicts carry a ``score_source``. A prose class (document,
# summarization) is graded reject-only and its verdict names no source, so the
# writer stores NULL for it whether or not it is attributed.
_TASK_TYPE = "code_generation"

_TENANT_SLUG = "beta-business-proof"
_TENANT_UUID = "91c74442-1233-4c97-b191-911a10346fdf"


def _mock_bus() -> MagicMock:
    bus = MagicMock(spec=ProtocolEventBus)
    bus.publish_envelope = AsyncMock()
    return bus


def _inbound(payload: object, correlation_id: UUID) -> ModelEventEnvelope[object]:
    """An inbound envelope that records NO tenant: the tenant lives in the request
    payload only, so the orchestrator has to carry it, not forward it."""
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
    )


async def _gate_request_wire(*, request_tenant: str | None) -> tuple[UUID, bytes]:
    """Drive the real orchestrator dispatcher to the quality-gate-request and
    return that envelope exactly as it is serialized onto the bus."""
    bus = _mock_bus()
    handler = HandlerDelegationWorkflow()
    dispatcher = DispatcherDelegationWorkflow(handler, event_bus=cast("Any", bus))
    correlation_id = uuid4()

    await dispatcher.handle(
        _inbound(
            ModelDelegationRequest(
                prompt="Write a python function add(a, b) that returns their sum.",
                task_type=cast("Any", _TASK_TYPE),
                correlation_id=correlation_id,
                emitted_at=datetime.now(UTC),
                tenant_id=request_tenant,
            ),
            correlation_id,
        )
    )
    await dispatcher.handle(
        _inbound(
            ModelRoutingDecision(
                correlation_id=correlation_id,
                task_type=_TASK_TYPE,
                selected_model="glm-5.3-flash",
                selected_backend_id=uuid5(
                    NAMESPACE_DNS, "omninode.ai/backends/glm-5.3-flash"
                ),
                endpoint_url="http://delegation-llm.test:8000",
                cost_tier="low",
                max_context_tokens=65536,
                max_tokens=65536,
                system_prompt="You are an assistant.",
                rationale="Routing test.",
                dod_deterministic=("code_artifact_present", "no_refusal"),
            ),
            correlation_id,
        )
    )
    await dispatcher.handle(
        _inbound(
            ModelInferenceResponseData(
                correlation_id=correlation_id,
                content="```python\ndef add(a, b):\n    return a + b\n```",
                model_used="glm-5.3-flash",
                llm_call_id="chatcmpl-omn17427",
                latency_ms=14912,
                prompt_tokens=120,
                completion_tokens=67,
                total_tokens=187,
            ),
            correlation_id,
        )
    )

    published: list[ModelEventEnvelope[object]] = []
    for call in bus.publish_envelope.call_args_list:
        if call.kwargs.get("topic") != TOPIC_ID_QUALITY_GATE_REQUEST:
            continue
        envelope = call.kwargs.get("envelope")
        if envelope is None and call.args:
            envelope = call.args[0]
        published.append(envelope)
    assert len(published) == 1, "the orchestrator did not publish one gate request"
    return correlation_id, published[0].model_dump_json().encode("utf-8")


async def _verdict_wire(gate_request_wire: bytes) -> bytes:
    """Consume the gate request on the DEPLOYED seam and return the verdict as the
    wire bytes the projection writer is handed.

    ``wire_from_manifest`` is what the runtime boots: the real reducer contract,
    the real auto-wired subscription and the real dispatch-result applier, which
    carries the consumed envelope's tenant onto what the reducer returns. The
    generic ``EventBusSubcontractWiring`` applies outside that binding and so
    cannot answer this question.
    """
    manifest = discover_contracts_from_paths([_GATE_CONTRACT])
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="omn17427", group="omn17427")
    await bus.start()
    verdicts: list[bytes] = []
    arrived = asyncio.Event()

    async def _collect(message: object) -> None:
        verdicts.append(cast("bytes", getattr(message, "value", b"")))
        arrived.set()

    await bus.subscribe(
        _VERDICT_TOPIC, on_message=_collect, group_id="omn17427-verdict"
    )

    engine = MessageDispatchEngine()
    await wire_from_manifest(
        ModelAutoWiringManifest(contracts=(manifest.contracts[0],)),
        engine,
        event_bus=bus,
        environment="local",
    )
    engine.freeze()

    await bus.publish(TOPIC_ID_QUALITY_GATE_REQUEST, None, gate_request_wire, None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=30)
    await bus.close()

    assert len(verdicts) == 1, f"expected one verdict on the wire, got {len(verdicts)}"
    return verdicts[0]


def _async_runner() -> tuple[DelegationProjectionRunner, AsyncMock, AsyncMock]:
    """The async writer over a recording database, plus its dead-letter route."""

    async def _publish(topic: str, value: bytes) -> None:
        return

    runner = DelegationProjectionRunner(publish_fn=_publish)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    dead_letter = AsyncMock(return_value=True)
    wired: Any = runner
    wired._db = db
    wired._route_malformed_to_dlq = dead_letter
    return runner, db, dead_letter


def _insert(db: AsyncMock) -> dict[str, Any]:
    writes = [
        call
        for call in db.execute.await_args_list
        if f"INSERT INTO {TABLE}" in str(call.args[0])
    ]
    assert writes, "the writer issued no delegation_events INSERT"
    sql = str(writes[-1].args[0])
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    return dict(zip(columns, writes[-1].args[1:], strict=True))


class TestATenantStampedRequestYieldsAnAcceptedVerdict:
    async def test_the_verdict_envelope_carries_the_request_tenant(self) -> None:
        _cid, gate_request = await _gate_request_wire(request_tenant=_TENANT_SLUG)
        assert json.loads(gate_request)["tenant_id"] == _TENANT_SLUG

        verdict = json.loads(await _verdict_wire(gate_request))

        assert verdict["tenant_id"] == _TENANT_SLUG, (
            "the verdict recorded no tenant although the request it grades "
            "carried one: the writer will refuse it"
        )

    async def test_the_async_writer_accepts_it_and_fills_score_source(self) -> None:
        correlation_id, gate_request = await _gate_request_wire(
            request_tenant=_TENANT_SLUG
        )
        verdict_wire = await _verdict_wire(gate_request)
        data = unwrap_envelope(verdict_wire)
        assert data is not None
        assert envelope_tenant_identity(data) == _TENANT_SLUG
        runner, db, dead_letter = _async_runner()

        await runner._project_quality_gate_result(
            data,
            MessageMeta(partition=0, offset=1, fallback_id=str(correlation_id)),
        )

        assert dead_letter.await_count == 0
        row = _insert(db)
        assert row["correlation_id"] == str(correlation_id)
        assert row["tenant_id"] == _TENANT_UUID
        assert row["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE

    async def test_the_deployed_sync_writer_accepts_it_and_fills_score_source(
        self,
    ) -> None:
        correlation_id, gate_request = await _gate_request_wire(
            request_tenant=_TENANT_SLUG
        )
        data = unwrap_envelope(await _verdict_wire(gate_request))
        assert data is not None
        payload = {
            k: v for k, v in data.items() if not k.startswith("_") and k != "pass"
        }
        db = InmemoryDatabaseAdapter()

        result = HandlerProjectionDelegation().project_quality_gate_result(
            ModelQualityGateResult.model_validate(payload),
            db,
            tenant_identity=envelope_tenant_identity(data),
            event_timestamp=envelope_event_timestamp(data),
        )

        assert result.rows_upserted == 1
        rows = db.query(TABLE, {"correlation_id": str(correlation_id)})
        assert rows
        assert rows[0]["score_source"] == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE


class TestAnUntenantedRequestIsNotGivenATenant:
    """The converse, so the chain above cannot pass by stamping a default."""

    async def test_the_verdict_records_no_tenant_and_the_writer_still_refuses(
        self,
    ) -> None:
        correlation_id, gate_request = await _gate_request_wire(request_tenant=None)
        assert json.loads(gate_request).get("tenant_id") is None

        verdict_wire = await _verdict_wire(gate_request)
        assert json.loads(verdict_wire).get("tenant_id") is None
        data = unwrap_envelope(verdict_wire)
        assert data is not None
        runner, db, dead_letter = _async_runner()

        await runner._project_quality_gate_result(
            data,
            MessageMeta(partition=0, offset=1, fallback_id=str(correlation_id)),
        )

        assert dead_letter.await_count == 1
        assert not [
            call
            for call in db.execute.await_args_list
            if f"INSERT INTO {TABLE}" in str(call.args[0])
        ], "an untenanted verdict was written: a fallback tenant is back"
