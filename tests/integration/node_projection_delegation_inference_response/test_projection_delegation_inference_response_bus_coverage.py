# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state REDUCER coverage for
node_projection_delegation_inference_response, driven over the canonical
in-memory bus.

OMN-13674 (cluster wave-D-projection-correctness-verification, archetype
reducer). The reducer folds inference-response events into a singleton snapshot
row. It is driven over ``EventBusInmemory`` (via the ``integration_event_bus``
fixture + ``LocalRuntimeBusAdapter``) through a thin bus-facing shim that injects
an ``InmemoryDatabaseAdapter`` (constructor injection — the reducer's I/O
boundary is exercised without any real Postgres): an inference-response event
lands on the declared subscribe topic
``onex.evt.omnibase-infra.inference-response.v1`` and the reduce result is
republished onto the declared snapshot topic
``onex.snapshot.projection.delegation.inference-response-text.v1``. The
materialized projection row is asserted directly on the injected adapter. No
live Kafka / ``.201``.

REDUCER DoD covered:
  * folds the single declared event type (inference-response) and asserts the
    materialized projection state (every ``latest_*`` scalar, ``source_topic``,
    ``provisioned``, and the ``recent_responses`` rolling window);
  * the rolling window accumulates newest-first and is capped at ``MAX_HISTORY``;
  * idempotency / duplicate handling: the table stays a singleton (exactly one
    row) no matter how many events — including duplicate ``correlation_id`` — are
    folded (``duplicate_key_fields: [singleton_key]``);
  * out-of-order / last-write-wins: the most recently folded event owns the
    ``latest_*`` scalar fields;
  * the terminal reduce result is published on the declared snapshot topic
    (``rows_upserted == 1``).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation_inference_response.handlers.handler_projection_delegation_inference_response import (
    TABLE,
    HandlerProjectionDelegationInferenceResponse,
)
from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    MAX_HISTORY,
    SINGLETON_KEY,
    ModelInferenceResponseProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.runtime_local_compat import LocalRuntimeBusAdapter

SUBSCRIBE_TOPIC = "onex.evt.omnibase-infra.inference-response.v1"
SNAPSHOT_TOPIC = "onex.snapshot.projection.delegation.inference-response-text.v1"


class _InferenceReducerBusHandler:
    """Bus-facing shim: exposes ``handle`` and injects the in-memory DB adapter so
    the reducer's pure ``project`` fold materializes state over the canonical
    adapter. Test-only wrapper — the production handler is unchanged.
    """

    def __init__(self, db: InmemoryDatabaseAdapter) -> None:
        self._handler = HandlerProjectionDelegationInferenceResponse()
        self._db = db

    def handle(self, event: dict[str, Any]) -> ModelInferenceResponseProjectionResult:
        return self._handler.project(event, self._db, topic=SUBSCRIBE_TOPIC)


def _inference_event(
    *,
    correlation_id: str | None = None,
    content: str = "Hello from the model",
    model_used: str = "glm-5.2",
    task_type: str = "chat",
    prompt_tokens: int = 120,
    completion_tokens: int = 45,
    latency_ms: int = 350,
) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id or str(uuid4()),
        "content": content,
        "model_used": model_used,
        "task_type": task_type,
        "llm_call_id": "chatcmpl-abc123",
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "error_message": "",
    }


async def _fold(
    bus: Any,
    db: InmemoryDatabaseAdapter,
    events: list[dict[str, Any]],
    *,
    group: str,
) -> list[Any]:
    """Subscribe the reducer on the declared subscribe topic, fold each event,
    and return the republished snapshot-topic history."""
    shim = _InferenceReducerBusHandler(db)
    adapter = LocalRuntimeBusAdapter(
        handler=shim,
        handler_name="projection-delegation-inference-response",
        input_model_cls=None,
        output_topic=SNAPSHOT_TOPIC,
        bus=bus,
    )
    await bus.subscribe(SUBSCRIBE_TOPIC, on_message=adapter.on_message, group_id=group)
    for event in events:
        await bus.publish(SUBSCRIBE_TOPIC, None, json.dumps(event).encode("utf-8"))
    return await bus.get_event_history(topic=SNAPSHOT_TOPIC)


def _recent(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row["recent_responses"]
    if isinstance(raw, str):
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        return parsed
    assert isinstance(raw, list)
    return raw


# ---------------------------------------------------------------------------
# Fold the declared event type — assert the materialized projection state.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_single_event_materializes_singleton_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())
        history = await _fold(
            bus,
            db,
            [
                _inference_event(
                    correlation_id=correlation_id,
                    content="materialized text",
                    model_used="glm-5.2",
                    prompt_tokens=120,
                    completion_tokens=45,
                    latency_ms=350,
                )
            ],
            group="reducer-single",
        )
        # Terminal reduce result published on the declared snapshot topic.
        assert len(history) == 1
        assert history[-1].topic == (
            "onex.snapshot.projection.delegation.inference-response-text.v1"
        )
        terminal = json.loads(history[-1].value)
        assert terminal["rows_upserted"] == 1

        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["singleton_key"] == SINGLETON_KEY
        assert row["latest_correlation_id"] == correlation_id
        assert row["latest_model_name"] == "glm-5.2"
        assert row["latest_task_type"] == "chat"
        assert row["latest_generated_text"] == "materialized text"
        assert row["latest_prompt_tokens"] == 120
        assert row["latest_completion_tokens"] == 45
        assert row["latest_latency_ms"] == 350
        assert row["provisioned"] is True
        assert row["source_topic"] == SUBSCRIBE_TOPIC

        recent = _recent(row)
        assert len(recent) == 1
        assert recent[0]["correlation_id"] == correlation_id
        assert recent[0]["generated_text"] == "materialized text"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Rolling window — accumulates newest-first, capped at MAX_HISTORY.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recent_window_capped_newest_first_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        db = InmemoryDatabaseAdapter()
        events = [
            _inference_event(content=f"response {i}") for i in range(MAX_HISTORY + 3)
        ]
        await _fold(bus, db, events, group="reducer-window")

        rows = db.query(TABLE)
        assert len(rows) == 1, "reducer must keep a singleton row"
        recent = _recent(rows[0])
        assert len(recent) == MAX_HISTORY, "recent_responses must cap at MAX_HISTORY"
        # Newest-first: the last folded event is at the head of the window.
        assert recent[0]["generated_text"] == f"response {MAX_HISTORY + 2}"
        assert rows[0]["latest_generated_text"] == f"response {MAX_HISTORY + 2}"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Idempotency — duplicate correlation_id keeps the table a singleton.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_correlation_keeps_singleton_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())
        events = [
            _inference_event(correlation_id=correlation_id, content="first"),
            _inference_event(correlation_id=correlation_id, content="second"),
            _inference_event(correlation_id=correlation_id, content="third"),
        ]
        await _fold(bus, db, events, group="reducer-dup")

        rows = db.query(TABLE)
        # duplicate_key_fields: [singleton_key] -> exactly one row regardless of
        # how many duplicate-correlation events are folded (idempotent upsert).
        assert len(rows) == 1
        assert rows[0]["latest_correlation_id"] == correlation_id
        # Last write wins for the latest_* scalar fields.
        assert rows[0]["latest_generated_text"] == "third"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Out-of-order / last-write-wins — the most recent fold owns latest_* fields.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_last_write_wins_across_correlations_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        db = InmemoryDatabaseAdapter()
        first = str(uuid4())
        second = str(uuid4())
        await _fold(
            bus,
            db,
            [
                _inference_event(
                    correlation_id=first, content="older", model_used="model-a"
                ),
                _inference_event(
                    correlation_id=second, content="newer", model_used="model-b"
                ),
            ],
            group="reducer-order",
        )
        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["latest_correlation_id"] == second
        assert row["latest_generated_text"] == "newer"
        assert row["latest_model_name"] == "model-b"
        # Both events are retained in the newest-first window.
        recent = _recent(row)
        assert [r["generated_text"] for r in recent] == ["newer", "older"]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Determinism — identical event sequence yields an identical projection row.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_sequence_over_bus(
    integration_event_bus: Any,
) -> None:
    bus_factory = type(integration_event_bus)
    events = [
        _inference_event(correlation_id="corr-A", content="a"),
        _inference_event(correlation_id="corr-B", content="b"),
    ]
    projections: list[list[dict[str, Any]]] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            db = InmemoryDatabaseAdapter()
            await _fold(bus, db, events, group="reducer-idem")
            rows = db.query(TABLE)
            # Strip the non-deterministic captured_at timestamps before comparing.
            row = dict(rows[0])
            row.pop("captured_at", None)
            recent = [
                {k: v for k, v in entry.items() if k != "captured_at"}
                for entry in _recent(row)
            ]
            row["recent_responses"] = recent
            projections.append([row])
        finally:
            await bus.close()
    assert projections[0] == projections[1]
