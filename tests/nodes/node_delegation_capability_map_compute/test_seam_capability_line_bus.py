# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: a capability-map run record with its class line survives the bus (OMN-23628).

The map-run event ``ModelCapabilityMapRunRecorded`` and its ``line`` are the seam
between the offline map-run producers (``build_map_run``, ``build_grid_map_run``,
``build_harness_capability_run``) and the projection node
``node_projection_delegation_capability_map``. The tests run the record over a
bus (the in-memory bus always, a real broker when ``RUN_REDPANDA_E3_SEAM=1``),
fold the delivered bytes through the real handler, dead-letter a malformed
record to the contract's DLQ topic, and pin that a producer-side key drift
turns the seam red.
"""

from __future__ import annotations

import json
import os
from uuid import UUID

import pytest
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from omnimarket.handlers.overlay_capability_line import resolve_capability_line
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMap,
)
from omnimarket.projection.dlq import route_to_dlq
from tests.chains.piece16.test_piece16_chain import (
    MemoryChainBus,
    ProtocolChainBus,
    RedpandaChainBus,
)
from tests.nodes.node_delegation_capability_map_compute.test_cells_from_code_eval_run import (
    _OBSERVED_AT,
    _TENANT,
    _observations,
    _run_and_result,
    _write_overlay,
)

pytestmark = pytest.mark.unit

RUN_TOPIC = "onex.evt.omnimarket.delegation-capability-map-run-recorded.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-capability-map-malformed.v1"
REDPANDA_FLAG = "RUN_REDPANDA_E3_SEAM"
HANDLER_NAME = "HandlerDelegationCapabilityMap"

BACKENDS = ["memory"] + (["redpanda"] if os.environ.get(REDPANDA_FLAG) == "1" else [])

_LINE = ModelCapabilityLine(floor=0.50, margin=0.07, confidence=0.95, power=0.8)


class _OlderRunShape(BaseModel):
    """The dev-shape record before this change: the line key never left the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    map_run_id: str
    observed_at: AwareDatetime
    rows: tuple[ModelCapabilityVerdictRow, ...] = ()
    assignments: tuple[ModelCapabilityServingAssignment, ...] = ()
    line: ModelCapabilityLine = Field(
        default_factory=lambda: ModelCapabilityLine(
            floor=0.9, margin=0.07, confidence=0.95, power=0.8
        )
    )


@pytest.fixture(params=BACKENDS)
def bus(request: pytest.FixtureRequest) -> ProtocolChainBus:
    return MemoryChainBus() if request.param == "memory" else RedpandaChainBus()


def seam_problems(run_json: dict) -> list[str]:
    """The producer's serialized keys the consumer model does not expect."""
    problems: list[str] = []
    for key in sorted(set(run_json) - set(ModelCapabilityMapRunRecorded.model_fields)):
        problems.append(f"producer key {key!r} not expected by the consumer")
    return problems


@pytest.mark.asyncio
async def test_golden_line_configured_run_survives_the_bus_and_folds(
    bus: ProtocolChainBus,
    tmp_path,
) -> None:
    """A record carrying the overlay line round-trips the bus and folds 4 cells."""
    reading = resolve_capability_line(_write_overlay(tmp_path, 0.50))
    assert reading.line is not None
    run, _ = _run_and_result(_observations(), reading.line)

    delivered = await bus.roundtrip(RUN_TOPIC, run.model_dump_json().encode())
    parsed = ModelCapabilityMapRunRecorded.model_validate_json(delivered)
    assert parsed == run

    result = HandlerDelegationCapabilityMap().handle(parsed.fold_request())
    assert len(result.cells) == 4
    assert result.line_not_configured is False


@pytest.mark.asyncio
async def test_key_absent_record_is_safe_alone_over_the_bus(
    bus: ProtocolChainBus,
) -> None:
    """A record without the line key drops the key on the wire and the old shape still reads it."""
    run, _ = _run_and_result(_observations(), resolve_capability_line(environ={}).line)

    delivered = await bus.roundtrip(RUN_TOPIC, run.model_dump_json().encode())
    assert "line" not in json.loads(delivered)
    parsed = ModelCapabilityMapRunRecorded.model_validate_json(delivered)
    assert parsed == run

    result = HandlerDelegationCapabilityMap().handle(parsed.fold_request())
    assert result.cells == ()
    assert result.line_not_configured is True
    _, configured_result = _run_and_result(_observations(), _LINE)
    assert result.folded_row_count == configured_result.folded_row_count

    payload = json.loads(delivered)
    _OlderRunShape.model_validate(payload)
    explicit_null = dict(payload)
    explicit_null["line"] = None
    with pytest.raises(ValidationError):
        _OlderRunShape.model_validate(explicit_null)


@pytest.mark.asyncio
async def test_the_overlay_floor_read_at_the_producer_decides_the_verdict(
    bus: ProtocolChainBus,
    tmp_path,
) -> None:
    """Over the bus, the S-band verdict follows the overlay floor at the producer."""
    verdicts: dict[float, object] = {}
    for floor in (0.50, 0.90):
        environ = _write_overlay(tmp_path, floor)
        line = resolve_capability_line(environ).line
        assert line.floor == floor
        run, _ = _run_and_result(_observations(), line)
        delivered = await bus.roundtrip(RUN_TOPIC, run.model_dump_json().encode())
        result = HandlerDelegationCapabilityMap().handle(
            ModelCapabilityMapRunRecorded.model_validate_json(delivered).fold_request()
        )
        cell = next(
            c
            for c in result.cells
            if c.size_band is EnumSizeBand.S
            and c.backend_id == "harness:local-agent:turns12-600s"
        )
        verdicts[floor] = cell.verdict
    assert verdicts[0.50] is not verdicts[0.90]


@pytest.mark.asyncio
async def test_error_seam_an_invalid_line_after_the_bus_dead_letters(
    bus: ProtocolChainBus,
) -> None:
    """A record whose line does not validate raises after the bus and dead-letters."""
    run = ModelCapabilityMapRunRecorded(
        tenant_id=_TENANT, map_run_id="run-seam-bad", observed_at=_OBSERVED_AT
    )
    message = json.loads(run.model_dump_json())
    message["line"] = {"floor": 2.0, "margin": 0.07, "confidence": 0.95, "power": 0.8}

    delivered = await bus.roundtrip(RUN_TOPIC, json.dumps(message).encode())
    payload = json.loads(delivered)
    with pytest.raises(ValidationError):
        ModelCapabilityMapRunRecorded.model_validate(payload)

    published: list[tuple[str, bytes]] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    routed = await route_to_dlq(
        publish=_publish,
        dlq_topics=[DLQ_TOPIC],
        original_message=payload,
        failure_reason="malformed map run",
        handler=HANDLER_NAME,
        correlation_id=run.map_run_id,
    )
    assert routed is True
    assert [topic for topic, _ in published] == [DLQ_TOPIC]
    envelope = json.loads(published[0][1])
    assert envelope["handler"] == HANDLER_NAME

    dead = await bus.roundtrip(DLQ_TOPIC, json.dumps(envelope).encode())
    assert json.loads(dead)["handler"] == HANDLER_NAME


def test_failing_control_an_unknown_producer_key_turns_the_seam_red() -> None:
    """A producer-side shape change adds a key the consumer's extra forbid rejects."""
    run, _ = _run_and_result(_observations(), _LINE)
    payload = run.model_dump(mode="json")
    assert seam_problems(payload) == []

    producer_drift = dict(payload)
    producer_drift["new_producer_key"] = 1
    assert seam_problems(producer_drift)
    with pytest.raises(ValidationError):
        ModelCapabilityMapRunRecorded.model_validate(producer_drift)
