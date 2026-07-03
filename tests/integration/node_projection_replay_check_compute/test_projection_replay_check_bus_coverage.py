# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for node_projection_replay_check_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster wave-D-projection-correctness-verification, archetype
compute). The pure ``HandlerProjectionReplayCheck.check`` fold is registered on
``EventBusInmemory`` (via the ``integration_event_bus`` fixture +
``LocalRuntimeBusAdapter``) through a thin bus-facing shim: a
``ModelReplayCheckRequest`` lands on the declared command topic
``onex.cmd.omnimarket.projection-replay-check-start.v1`` and the terminal
``ModelReplayCheckResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.projection-replay-check-completed.v1``. No live Kafka /
``.201``.

The shim (a test-only wrapper, no production change) exists because the handler's
public method is ``check`` while the canonical adapter dispatches ``handle``.

COMPUTE DoD covered:
  * every declared output field asserted off the terminal event
    (``status``, ``total_correlations``, ``replay_proven``, ``runtime_observed``,
    ``blocked``, ``superseded``, ``findings``) — never a "returned without
    raising";
  * every reachable classification verdict:
      - ``runtime-observed`` (single occurrence)   -> status ``clean``,
      - ``replay-proven``    (identical re-delivery) -> status ``clean``,
      - ``superseded``       (later partition/offset) -> status ``findings``;
  * a negative control that MUST produce the finding: the superseded case emits
    a non-empty ``findings`` tuple with the earlier correlation flagged;
  * two boundary-rejection controls (no terminal event published): an empty
    ``events`` list and an event with an empty ``correlation_id`` — both are
    rejected at the model boundary before the handler runs;
  * idempotency: identical input yields an identical terminal event.

Honest findings:
  * The ``blocked`` verdict (empty ``correlation_id``) is UNREACHABLE through the
    declared contract surface: ``ModelProjectionEvent`` rejects an empty
    ``correlation_id`` at construction, so a blank-correlation event never
    reaches ``check``. The ``blocked`` output *field* is still asserted
    (``result.blocked == 0``), and the boundary rejection is proven directly.
  * The ``EnumReplayStatus.DASHBOARD_RENDERED`` verdict and the ``status ==
    "error"`` value are declared but never produced by this pure handler (they
    require a live projection-API effect); they are consequently unreachable
    here and not asserted as emitted values.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_projection_replay_check_compute.handlers.handler_projection_replay_check import (
    HandlerProjectionReplayCheck,
)
from omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check import (
    ModelProjectionEvent,
    ModelReplayCheckRequest,
    ModelReplayCheckResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.projection-replay-check-start.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.projection-replay-check-completed.v1"

_TABLE = "projection_delegation_inference_response_text"
_TOPIC = "onex.evt.omnibase-infra.inference-response.v1"


class _ReplayCheckBusHandler:
    """Bus-facing shim: exposes ``handle`` so the canonical adapter can dispatch
    the pure ``HandlerProjectionReplayCheck.check`` core over the in-memory bus.

    This is a test-only wrapper — the production handler is unchanged.
    """

    def __init__(self) -> None:
        self._handler = HandlerProjectionReplayCheck()

    def handle(self, request: ModelReplayCheckRequest) -> ModelReplayCheckResult:
        return self._handler.check(request)


def _event(correlation_id: str, partition: int, offset: int) -> ModelProjectionEvent:
    return ModelProjectionEvent(
        correlation_id=correlation_id,
        source_topic=_TOPIC,
        partition=partition,
        offset=offset,
        table=_TABLE,
    )


async def _drive(
    bus: Any, command: ModelReplayCheckRequest, *, group: str
) -> ModelReplayCheckResult:
    adapter = LocalRuntimeBusAdapter(
        handler=_ReplayCheckBusHandler(),
        handler_name="projection-replay-check",
        input_model_cls=ModelReplayCheckRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(TOPIC_COMMAND, on_message=adapter.on_message, group_id=group)
    await bus.publish(TOPIC_COMMAND, None, command.model_dump_json().encode("utf-8"))
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == (
        "onex.evt.omnimarket.projection-replay-check-completed.v1"
    )
    return ModelReplayCheckResult.model_validate(json.loads(completed[-1].value))


async def _drive_raw(bus: Any, raw: bytes, *, group: str) -> list[Any]:
    """Publish a raw (possibly invalid) payload; return the terminal history.

    An empty list means the payload was rejected at the model boundary before
    the handler ran — the boundary-rejection negative-control signal.
    """
    adapter = LocalRuntimeBusAdapter(
        handler=_ReplayCheckBusHandler(),
        handler_name="projection-replay-check",
        input_model_cls=ModelReplayCheckRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(TOPIC_COMMAND, on_message=adapter.on_message, group_id=group)
    await bus.publish(TOPIC_COMMAND, None, raw)
    return await bus.get_event_history(topic=TOPIC_COMPLETED)


# ---------------------------------------------------------------------------
# clean — a single occurrence classifies as runtime-observed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_single_occurrence_runtime_observed_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelReplayCheckRequest(events=(_event("corr-1", 0, 10),)),
            group="replay-observed",
        )
        assert result.status == "clean"
        assert result.total_correlations == 1
        assert result.runtime_observed == 1
        assert result.replay_proven == 0
        assert result.superseded == 0
        assert result.blocked == 0
        assert result.findings == ()
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# clean — identical re-delivery (same partition/offset) is replay-proven.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_identical_redelivery_replay_proven_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelReplayCheckRequest(
                events=(
                    _event("corr-2", 0, 42),
                    _event("corr-2", 0, 42),
                    _event("corr-2", 0, 42),
                )
            ),
            group="replay-proven",
        )
        # replay-proven is not a finding -> the run is still clean.
        assert result.status == "clean"
        assert result.total_correlations == 1
        assert result.replay_proven == 1
        assert result.runtime_observed == 0
        assert result.superseded == 0
        assert result.findings == ()
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# findings — later partition/offset supersedes the earlier occurrence.
# Negative control: the superseded correlation MUST appear in findings.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_superseded_produces_findings_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelReplayCheckRequest(
                events=(
                    _event("corr-3", 0, 5),
                    _event("corr-3", 1, 9),  # later (partition, offset)
                )
            ),
            group="replay-superseded",
        )
        assert result.status == "findings"
        assert result.total_correlations == 1
        assert result.superseded == 1
        assert result.runtime_observed == 0
        assert result.replay_proven == 0
        assert result.blocked == 0
        # Negative control: the superseded correlation is surfaced as a finding.
        assert len(result.findings) == 1
        assert result.findings[0].correlation_id == "corr-3"
        assert result.findings[0].status == "superseded"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Mixed batch — clean + superseded aggregate correctly across correlations.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_mixed_batch_aggregates_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            ModelReplayCheckRequest(
                events=(
                    _event("obs", 0, 1),  # runtime-observed
                    _event("dup", 2, 7),  # replay-proven (identical pair)
                    _event("dup", 2, 7),
                    _event("sup", 0, 3),  # superseded
                    _event("sup", 0, 8),
                )
            ),
            group="replay-mixed",
        )
        assert result.status == "findings"
        assert result.total_correlations == 3
        assert result.runtime_observed == 1
        assert result.replay_proven == 1
        assert result.superseded == 1
        assert result.blocked == 0
        assert {f.correlation_id for f in result.findings} == {"sup"}
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Boundary-rejection controls — no terminal event is published.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_empty_events_rejected_at_boundary_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        history = await _drive_raw(
            bus, json.dumps({"events": []}).encode("utf-8"), group="replay-empty"
        )
        assert history == [], "empty events must be rejected before the handler runs"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_blank_correlation_rejected_at_boundary_over_bus(
    integration_event_bus: Any,
) -> None:
    """The ``blocked`` verdict is unreachable: a blank correlation_id is rejected
    at the model boundary, so it never reaches the classifier."""
    bus = integration_event_bus
    await bus.start()
    try:
        raw = json.dumps(
            {
                "events": [
                    {
                        "correlation_id": "   ",
                        "source_topic": _TOPIC,
                        "partition": 0,
                        "offset": 1,
                        "table": _TABLE,
                    }
                ]
            }
        ).encode("utf-8")
        history = await _drive_raw(bus, raw, group="replay-blank")
        assert history == [], "blank correlation_id must be rejected at the boundary"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Idempotency — identical input yields an identical terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_input_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    command = ModelReplayCheckRequest(
        events=(
            _event("a", 0, 1),
            _event("b", 0, 2),
            _event("b", 1, 4),
        )
    )
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            result = await _drive(bus, command, group="replay-idem")
            payloads.append(result.model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
