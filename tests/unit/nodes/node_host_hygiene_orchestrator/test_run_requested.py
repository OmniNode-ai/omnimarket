# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the on-demand host hygiene run route (OMN-24557)."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
    ModelHostHygieneRunRequested,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_run_requested import (
    HandlerHostHygieneRunRequested,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_capacity_readings import (
    HostCapacityStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_factory import (
    InMemoryHostHygieneSender,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _overlay() -> ModelHostHygieneOrchestratorOverlay:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _overlay as _overlay_base,
    )

    return _overlay_base()


def _store_with_reading(
    used_percent: int, at: dt.datetime
) -> HostDiskReadingStore:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _store_with_reading as _store_base,
    )

    return _store_base(used_percent, at)


def _fresh_capacity(
    overlay: ModelHostHygieneOrchestratorOverlay, at: dt.datetime
) -> HostCapacityStore:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _fresh_capacity as _capacity_base,
    )

    return _capacity_base(overlay, at)


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _tick as _tick_base,
    )

    return _tick_base(now)


def _envelope(
    payload: ModelHostHygieneRunRequested, correlation_id: uuid.UUID
) -> ModelEventEnvelope[ModelHostHygieneRunRequested]:
    return ModelEventEnvelope(
        payload=payload,
        correlation_id=correlation_id,
        event_type="onex.cmd.omnimarket.host-hygiene-run-requested.v1",
    )


def _handler(
    store: HostDiskReadingStore,
    sender: InMemoryHostHygieneSender,
    environ: dict[str, str] | None = None,
) -> HandlerHostHygieneRunRequested:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _handler as _handler_base,
    )

    # Build a HandlerHostHygieneRunRequested with the same wiring as the scheduled handler.
    gate = ScheduleGate(
        "node_host_hygiene_orchestrator",
        model=ModelHostHygieneOrchestratorOverlay,
        environ=environ,
    )
    return HandlerHostHygieneRunRequested(
        gate=gate,
        store=store,
        capacity=_fresh_capacity(_overlay(), T0),
        sender=sender,
    )


def _payloads(
    output: object,
) -> tuple[ModelHostHygieneRunCompleted, ...]:
    """Extract run-completed payloads from a handler output."""
    from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

    assert isinstance(output, ModelHandlerOutput)
    return tuple(
        e.payload
        for e in output.events
        if isinstance(e.payload, ModelHostHygieneRunCompleted)
    )


async def test_golden_chain_on_demand_run_returns_one_event_with_caller_correlation_id() -> None:
    """The on-demand handler returns exactly one run-completed event with the caller's correlation id."""
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender({host: (EnumLabWorkUnitStatus.COMPLETED, 0, "")})
    store = _store_with_reading(90, T0 - dt.timedelta(seconds=30))
    handler = _handler(store, sender)
    correlation_id = uuid.uuid4()
    request = ModelHostHygieneRunRequested(requested_by="operator", reason="manual")
    envelope = _envelope(request, correlation_id)

    output = await handler.handle(envelope)

    runs = _payloads(output)
    assert len(runs) == 1
    run = runs[0]
    assert run.outcome is EnumHostHygieneRunOutcome.COMPLETED
    assert len(run.units) > 0
    # The correlation id on the event must equal the caller's.
    assert output.correlation_id == correlation_id


async def test_on_demand_when_not_due_still_returns_run_event() -> None:
    """When the gate would say nothing is due, the on-demand route still returns a run event."""
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender({host: (EnumLabWorkUnitStatus.COMPLETED, 0, "")})
    store = _store_with_reading(90, T0 - dt.timedelta(seconds=30))
    handler = _handler(store, sender)
    correlation_id = uuid.uuid4()
    request = ModelHostHygieneRunRequested()
    envelope = _envelope(request, correlation_id)

    # Call the scheduled handler first at T0 to establish the gate state.
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
        _handler as _sched_handler_base,
    )

    sched_handler = _sched_handler_base(store, sender)
    await sched_handler.handle(_tick(T0))

    # Now call the on-demand handler at T0+1s; the gate would say not due, but on-demand ignores the gate.
    later = T0 + dt.timedelta(seconds=1)
    store2 = _store_with_reading(90, later - dt.timedelta(seconds=30))
    handler2 = _handler(store2, sender)
    envelope2 = _envelope(request, correlation_id)
    output = await handler2.handle(envelope2)

    runs = _payloads(output)
    assert len(runs) == 1
    assert runs[0].outcome is EnumHostHygieneRunOutcome.COMPLETED
    assert output.correlation_id == correlation_id


async def test_nothing_due_returns_nothing_due_outcome() -> None:
    """When no reading is above threshold, the outcome is NOTHING_DUE."""
    overlay = _overlay()
    host = overlay.hosts[0].name
    sender = InMemoryHostHygieneSender({host: (EnumLabWorkUnitStatus.COMPLETED, 0, "")})
    # Reading below threshold (e.g., 10% used).
    store = _store_with_reading(10, T0 - dt.timedelta(seconds=30))
    handler = _handler(store, sender)
    correlation_id = uuid.uuid4()
    request = ModelHostHygieneRunRequested()
    envelope = _envelope(request, correlation_id)

    output = await handler.handle(envelope)

    runs = _payloads(output)
    assert len(runs) == 1
    assert runs[0].outcome is EnumHostHygieneRunOutcome.NOTHING_DUE
    assert output.correlation_id == correlation_id


async def test_missing_overlay_returns_no_events() -> None:
    """When the overlay is missing, the handler returns no events and sends nothing."""
    sender = InMemoryHostHygieneSender()
    handler = HandlerHostHygieneRunRequested(
        gate=ScheduleGate(
            "node_host_hygiene_orchestrator",
            model=ModelHostHygieneOrchestratorOverlay,
            environ={"ONEX_SKILL_OVERLAY_ROOTS": "/nonexistent"},
        ),
        store=HostDiskReadingStore(max_hosts=8),
        sender=sender,
    )
    correlation_id = uuid.uuid4()
    request = ModelHostHygieneRunRequested()
    envelope = _envelope(request, correlation_id)

    output = await handler.handle(envelope)

    assert output.events == ()
    assert sender.sent == []


def test_contract_seam_command_topic_is_first() -> None:
    """The contract's subscribe_topics[0] is the command topic."""
    import yaml

    contract_path = (
        Path(__file__).parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_host_hygiene_orchestrator"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    topics = contract["event_bus"]["subscribe_topics"]
    assert topics[0] == "onex.cmd.omnimarket.host-hygiene-run-requested.v1"


def test_contract_seam_handler_routing_entry() -> None:
    """The contract has a handler_routing entry for the command topic."""
    import yaml

    contract_path = (
        Path(__file__).parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_host_hygiene_orchestrator"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    entry = next(
        (h for h in handlers if h.get("topic") == "onex.cmd.omnimarket.host-hygiene-run-requested.v1"),
        None,
    )
    assert entry is not None, "handler_routing entry for the command topic not found"
    assert entry["handler"]["name"] == "HandlerHostHygieneRunRequested"
    assert (
        entry["handler"]["module"]
        == "omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_run_requested"
    )
    assert entry["event_model"]["name"] == "ModelHostHygieneRunRequested"
    assert (
        entry["event_model"]["module"]
        == "omnimarket.models.model_host_hygiene_orchestrator"
    )


def test_contract_seam_acl_rows() -> None:
    """The contract has ACL rows for the command topic."""
    import yaml

    contract_path = (
        Path(__file__).parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_host_hygiene_orchestrator"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    grants = contract["broker_acl_grants"]
    topic = "onex.cmd.omnimarket.host-hygiene-run-requested.v1"
    for grant in grants:
        for g in grant["grants"]:
            if g["name"] == topic:
                assert g["operation"] in ("READ", "DESCRIBE")
                return
    pytest.fail(f"ACL rows for {topic} not found")


def test_overlay_lane_tie_mismatch_raises() -> None:
    """When event_bus.lane differs from lab_work.bus_lane, a ValidationError is raised."""
    doc = _valid_overlay_doc()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    doc["lab_work"]["bus_lane"] = "prod"
    with pytest.raises(ValidationError, match="dev.*prod"):
        ModelHostHygieneOrchestratorOverlay.model_validate(doc)


def test_overlay_lane_tie_match_accepted() -> None:
    """When event_bus.lane equals lab_work.bus_lane, the overlay is accepted."""
    doc = _valid_overlay_doc()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    doc["lab_work"]["bus_lane"] = "dev"
    overlay = ModelHostHygieneOrchestratorOverlay.model_validate(doc)
    assert overlay.event_bus is not None
    assert overlay.event_bus.lane == "dev"


def _valid_overlay_doc() -> dict:
    from tests.unit.nodes.node_host_hygiene_orchestrator.test_overlay_model import (
        _valid_overlay,
    )

    return _valid_overlay()
