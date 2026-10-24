# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture runtime dispatch proof; this is not live dev-bus build evidence."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.models.context_compaction import (
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionPackageReply,
    ModelContextCompactionPrepared,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers import (
    compaction_runtime_binding as binding_module,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers import (
    handler_context_compaction_orchestrator as handler_module,
)

from .test_compaction_pack_build import _pack_orchestrator, _sources
from .test_session_barrier_wait import _probe, _request, _Source

if TYPE_CHECKING:
    from omnibase_infra.protocols import ProtocolEventBusLike
    from omnibase_infra.runtime.auto_wiring.handler_wiring import ProtocolHandleable

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_compaction_orchestrator/contract.yaml"
)
_TRANSPORT_CORRELATION = UUID("99999999-9999-4999-8999-999999999999")


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_met", [True, False], ids=["built", "deadline"])
async def test_runtime_compaction_pack_build_barrier_dispatch_and_publish(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    barrier_met: bool,
    compaction_bus,
) -> None:
    """Real discovery, typed callback, pack construction, and result publication.

    Only the applied projection source and package factor sources are fixtures.
    Routing promotions come from the explicit test fixture, not deployed data.
    """
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "fixture-tenant")
    probe = _probe((1, 2, 3) if barrier_met else (1, 3), (1, 2, 3))
    probe = probe.model_copy(update={"source_revision": "2026-10-19T12:00:00Z"})
    source = _Source([probe])
    monkeypatch.setattr(
        binding_module, "PostgresSessionBarrierProbeSource", lambda _binding: source
    )
    source_factors = _sources()
    pack = _pack_orchestrator(source_factors)
    bus_harness = compaction_bus(pack)
    binding = binding_module.load_compaction_runtime_binding().model_copy(
        update={
            "barrier_deadline_s": 1.0 if barrier_met else 0.005,
            "barrier_poll_interval_s": 0.001,
        }
    )
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert manifest.errors == ()
    (contract,) = manifest.contracts
    assert contract.handler_routing is not None
    assert contract.event_bus is not None
    (route,) = contract.handler_routing.handlers
    assert route.event_model is not None
    model = getattr(
        importlib.import_module(route.event_model.module), route.event_model.name
    )
    request = _request()
    assert isinstance(request, model)
    handler_class = getattr(
        importlib.import_module(route.handler.module), route.handler.name
    )
    handler = handler_class(
        event_publisher=bus_harness.publisher,
        event_consumer=bus_harness.consumer,
        binding=binding,
        barrier_probe_source=source,
    )
    assert isinstance(handler, handler_module.HandlerContextCompactionOrchestrator)
    # A generated UUID on the domain result must never replace transport correlation.
    assert request.invocation_id != _TRANSPORT_CORRELATION
    callback = _make_dispatch_callback(
        cast("ProtocolHandleable", handler),
        event_model=route.event_model,
        handler_node_kind=EnumNodeKind.ORCHESTRATOR,
    )
    envelope = ModelEventEnvelope[object](
        payload=request.model_dump(mode="json"),
        correlation_id=_TRANSPORT_CORRELATION,
        event_type=route.topic,
    )
    with caplog.at_level(logging.WARNING, logger=handler_module.__name__):
        result = await callback(envelope)
    assert result is not None
    assert result.correlation_id == _TRANSPORT_CORRELATION
    events = result.output_events
    reply = events[-1]
    assert isinstance(reply, ModelContextCompactionPackageReply)
    assert reply.invocation_id == request.invocation_id
    warnings = [
        record
        for record in caplog.records
        if record.name == handler_module.__name__ and record.levelno == logging.WARNING
    ]
    if barrier_met:
        assert [type(event) for event in events] == [
            ModelContextCompactionPrepared,
            ModelContextCompactionActivated,
            ModelContextCompactionPackageReply,
        ]
        assert reply.status == "built"
        assert isinstance(events[0], ModelContextCompactionPrepared)
        assert reply.package_id == events[0].candidate_id
        assert reply.messages
        assert reply.installed_messages_hash
        assert len(bus_harness.commands) == 1
        command_cid = bus_harness.commands[0]["correlation_id"]
        assert bus_harness.consumer.waits[0][0] == command_cid
        assert all(
            body["correlation_id"] == command_cid for _, body in bus_harness.records
        )
        assert all(factor.requests for factor in source_factors.values())
        assert warnings == []
    else:
        assert reply.status == "native"
        assert reply.package_id is None
        assert reply.messages == ()
        assert len(events) == 2
        assert isinstance(events[0], ModelContextCompactionFallback)
        assert events[0].failure_class.value == "source_not_caught_up"
        assert not bus_harness.commands
        assert not any(factor.requests for factor in source_factors.values())
        (warning,) = warnings
        assert "source_not_caught_up" in warning.getMessage()
        assert str(request.invocation_id) in warning.getMessage()

    bus = EventBusInmemory(environment="local", group="compaction-fixture-runtime")
    await bus.start()
    try:
        applier = build_contract_result_applier(
            event_bus=cast("ProtocolEventBusLike", bus),
            contract_path=_CONTRACT,
            publish_topics=contract.event_bus.publish_topics,
        )
        await applier.apply(result)
        history = await bus.get_event_history()
        declared = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        topics = {
            event["event_type"]: event["topic"]
            for event in declared["published_events"]
        }
        assert [message.topic for message in history] == [
            topics[type(event).__name__.removeprefix("Model")] for event in events
        ]
        published = [json.loads(message.value) for message in history]
        assert all(
            record["correlation_id"] == str(_TRANSPORT_CORRELATION)
            for record in published
        )
        assert published[-1]["payload"]["status"] == reply.status
        assert published[-1]["payload"]["package_id"] == (
            str(reply.package_id) if reply.package_id is not None else None
        )
    finally:
        await bus.close()
