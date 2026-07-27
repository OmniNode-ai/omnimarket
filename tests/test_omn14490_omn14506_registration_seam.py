"""Cross-boundary seam regression: producer wire dump -> BOTH consumer paths.

OMN-14490 (introspection) and OMN-14506 (heartbeat) are the same defect: the
consumer defined its own slim copy of the producer's event model with
``extra="ignore"``, so every field the producer sent but the copy did not
declare was silently dropped on every message.

These tests are deliberately NOT two independent unit suites. Each one:

  1. constructs the PRODUCER's canonical model (omnibase_infra) with rich values,
  2. wraps + serializes it exactly as the producer does (``ModelEventEnvelope``
     -> JSON bytes),
  3. decodes it through the consumer's REAL decode seam
     (``omnimarket.projection.envelope.unwrap_envelope``, which injects the
     ``_envelope`` transport key), and
  4. drives that dict through a REAL consumer path, asserting the dropped fields
     SURVIVE to the persisted row / SQL bind.

Both consumer paths are covered, because there are two of them and fixing only
one leaves the bug alive:

  * ``HandlerProjectionRegistration``  (the RuntimeLocal handler shim)
  * ``RegistrationProjectionRunner``   (the live Kafka -> Postgres projector)

RED-vs-exists-but-wrong: against the slim models these assertions fail because
the fields are absent from the persisted payload — not because the code is
missing. Green requires the canonical model to actually carry them through.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_infra.models.registration.model_node_heartbeat_event import (
    ModelNodeHeartbeatEvent,
)
from omnibase_infra.models.registration.model_node_introspection_event import (
    ModelNodeIntrospectionEvent,
)

from omnimarket.nodes.node_projection_registration.handlers.handler_projection_registration import (
    HandlerProjectionRegistration,
)
from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

TOPIC_INTROSPECTION = "onex.evt.platform.node-introspection.v1"
TOPIC_HEARTBEAT = "onex.evt.platform.node-heartbeat.v1"

NODE_ID = UUID("11111111-2222-3333-4444-555555555555")
CORRELATION_ID = UUID("99999999-8888-7777-6666-555555555555")


def _to_wire(
    event: ModelNodeIntrospectionEvent | ModelNodeHeartbeatEvent,
) -> dict[str, Any]:
    """Serialize via the producer's envelope, then decode via the consumer's seam.

    This is the actual boundary: the producer publishes
    ``ModelEventEnvelope(payload=event)`` and the consumer runs the bytes through
    ``unwrap_envelope``, which returns the payload dict PLUS an injected
    ``_envelope`` transport key. Any consumer model that is ``extra="forbid"``
    must tolerate that key, so the test must not hand-roll a clean dict.
    """
    envelope: ModelEventEnvelope[Any] = ModelEventEnvelope(payload=event)
    raw = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
    decoded = unwrap_envelope(raw)
    assert decoded is not None, "producer envelope failed to decode"
    # Guard the guard: if the transport key ever stops being injected, the
    # extra="forbid" tolerance below would be vacuously green.
    assert "_envelope" in decoded, "decode seam no longer injects _envelope"
    return decoded


def _producer_introspection() -> ModelNodeIntrospectionEvent:
    """A realistic producer-side introspection event with the rich fields set."""
    return ModelNodeIntrospectionEvent(
        node_id=NODE_ID,
        node_name="node_build_loop",
        node_type=EnumNodeKind.EFFECT,
        node_version=ModelSemVer(major=1, minor=2, patch=3),
        endpoints={"http": "http://localhost:8080"},
        current_state="degraded",
        correlation_id=CORRELATION_ID,
        timestamp=datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC),
    )


def _producer_heartbeat() -> ModelNodeHeartbeatEvent:
    """A realistic producer-side heartbeat carrying the health metrics."""
    return ModelNodeHeartbeatEvent(
        node_id=NODE_ID,
        node_type=EnumNodeKind.EFFECT,
        node_version=ModelSemVer(major=1, minor=2, patch=3),
        uptime_seconds=3600.5,
        active_operations_count=5,
        memory_usage_mb=256.0,
        cpu_usage_percent=15.5,
        correlation_id=CORRELATION_ID,
        timestamp=datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------
# Path 1: HandlerProjectionRegistration (RuntimeLocal handler shim)
# --------------------------------------------------------------------------


class TestHandlerPathSeam:
    def test_introspection_rich_fields_survive(self) -> None:
        """OMN-14490: endpoints/capabilities/current_state must reach the row."""
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_introspection())

        HandlerProjectionRegistration().handle(
            {**payload, "_db": db, "_event_type": "introspection"}
        )

        row = db.tables["node_service_registry"][0]
        metadata = row["metadata"]
        assert isinstance(metadata, dict)

        # The fields the slim extra="ignore" copy silently dropped.
        assert metadata["endpoints"] == {"http": "http://localhost:8080"}
        assert metadata["current_state"] == "degraded"
        assert "declared_capabilities" in metadata
        assert "discovered_capabilities" in metadata
        assert metadata["correlation_id"] == str(CORRELATION_ID)

        # And the derived columns the slim copy could never populate.
        assert row["service_name"] == "node_build_loop"
        assert row["service_url"] == "http://localhost:8080"
        assert row["service_type"] == EnumNodeKind.EFFECT.value
        assert row["health_status"] == "degraded"

    def test_heartbeat_health_metrics_survive(self) -> None:
        """OMN-14506: node_type/version + all health metrics must reach the row."""
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_heartbeat())

        HandlerProjectionRegistration().handle(
            {**payload, "_db": db, "_event_type": "heartbeat"}
        )

        row = db.tables["node_service_registry"][0]
        metadata = row["metadata"]
        assert isinstance(metadata, dict)

        # The six fields the slim extra="ignore" copy dropped on EVERY heartbeat.
        assert metadata["node_type"] == EnumNodeKind.EFFECT.value
        assert metadata["active_operations_count"] == 5
        assert metadata["memory_usage_mb"] == 256.0
        assert metadata["cpu_usage_percent"] == 15.5
        assert metadata["correlation_id"] == str(CORRELATION_ID)
        assert metadata["node_version"] is not None

        assert row["uptime_seconds"] == 3600
        assert row["service_name"] == str(NODE_ID)

    def test_heartbeat_lands_on_the_introspection_row(self) -> None:
        """The heartbeat must UPDATE the node's row, not create a phantom one.

        Introspection keys the registry on node_name; the canonical heartbeat
        carries only node_id. Resolving the heartbeat to str(node_id) would key
        it to a row introspection never created — so the metrics would be
        persisted to a phantom row and the OMN-14506 fix would be cosmetic. The
        join goes through metadata.node_id.
        """
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionRegistration()

        handler.handle(
            {
                **_to_wire(_producer_introspection()),
                "_db": db,
                "_event_type": "introspection",
            }
        )
        handler.handle(
            {**_to_wire(_producer_heartbeat()), "_db": db, "_event_type": "heartbeat"}
        )

        rows = db.tables["node_service_registry"]
        assert len(rows) == 1, f"heartbeat created a phantom row: {rows}"

        row = rows[0]
        assert row["service_name"] == "node_build_loop"

        metadata = row["metadata"]
        assert isinstance(metadata, dict)
        # Heartbeat metrics merged in...
        assert metadata["active_operations_count"] == 5
        assert metadata["cpu_usage_percent"] == 15.5
        # ...without clobbering the introspection data on the same row.
        assert metadata["endpoints"] == {"http": "http://localhost:8080"}
        assert row["service_url"] == "http://localhost:8080"
        assert row["uptime_seconds"] == 3600


# --------------------------------------------------------------------------
# Path 2: RegistrationProjectionRunner (live Kafka -> Postgres projector)
# --------------------------------------------------------------------------


def _bind_args(mock_db: AsyncMock) -> tuple[Any, ...]:
    mock_db.execute.assert_called_once()
    return tuple(mock_db.execute.call_args.args)


class TestRunnerPathSeam:
    @pytest.mark.asyncio
    async def test_introspection_rich_fields_survive(self) -> None:
        """OMN-14490 on the path that actually runs against Postgres."""
        mock_db = AsyncMock()
        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        payload = _to_wire(_producer_introspection())
        assert await runner.project_event(TOPIC_INTROSPECTION, payload, _meta()) is True

        args = _bind_args(mock_db)
        metadata = json.loads(args[5])

        assert metadata["endpoints"] == {"http": "http://localhost:8080"}
        assert metadata["current_state"] == "degraded"
        assert "declared_capabilities" in metadata
        assert "discovered_capabilities" in metadata
        # Transport metadata must never be persisted as if it were payload.
        assert "_envelope" not in metadata

    @pytest.mark.asyncio
    async def test_heartbeat_health_metrics_survive(self) -> None:
        """OMN-14506 on the path that actually runs against Postgres.

        The previous UPDATE bound only health_status + service_name, so all six
        health metrics were dropped before they ever reached the database.
        """
        mock_db = AsyncMock()
        runner = RegistrationProjectionRunner()
        runner._db = mock_db

        payload = _to_wire(_producer_heartbeat())
        assert await runner.project_event(TOPIC_HEARTBEAT, payload, _meta()) is True

        sql, *binds = _bind_args(mock_db)
        metrics = json.loads(binds[2])

        assert metrics["node_type"] == EnumNodeKind.EFFECT.value
        assert metrics["active_operations_count"] == 5
        assert metrics["memory_usage_mb"] == 256.0
        assert metrics["cpu_usage_percent"] == 15.5
        assert metrics["correlation_id"] == str(CORRELATION_ID)
        assert metrics["node_version"] is not None
        assert "_envelope" not in metrics

        assert binds[1] == 3600  # uptime_seconds

        # Heartbeat metadata must MERGE, never replace: a bare `metadata = $n`
        # would clobber the rich introspection metadata on the same row.
        assert "||" in sql

        # And it must join on node_id. The registry is keyed by node_name (from
        # introspection); the canonical heartbeat has only node_id, so a
        # service_name-only WHERE clause matches ZERO rows and the metrics land
        # nowhere at all.
        assert "metadata->>'node_id'" in sql
        assert binds[4] == str(NODE_ID)


def _meta() -> Any:
    from omnimarket.projection.runner import MessageMeta

    return MessageMeta(partition=0, offset=0, fallback_id=str(uuid4()))
