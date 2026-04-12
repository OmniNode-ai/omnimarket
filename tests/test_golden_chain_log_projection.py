"""Golden chain tests for node_log_projection.

Verifies the pure reducer: project entries, query filtering, counter
accumulation, snapshot emission, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.logging.structured_logger import LOG_ENTRY_TOPIC, StructuredEventLogger
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    EnumLogLevel,
    ModelLogEntry,
    ModelLogProjectionSnapshot,
    ModelLogProjectionState,
    ModelLogQuery,
    NodeLogProjection,
)

SNAPSHOT_TOPIC = "onex.evt.omnimarket.log-projection-snapshot.v1"


def _make_entry(
    *,
    node_name: str = "node_build_loop",
    function_name: str = "advance",
    level: EnumLogLevel = EnumLogLevel.INFO,
    message: str = "Phase transition complete",
    correlation_id: str | None = None,
    duration_ms: float | None = None,
    metadata: dict[str, str] | None = None,
) -> ModelLogEntry:
    return ModelLogEntry(
        entry_id=str(uuid4()),
        timestamp=datetime.now(tz=UTC).isoformat(),
        node_name=node_name,
        function_name=function_name,
        level=level,
        message=message,
        correlation_id=correlation_id,
        duration_ms=duration_ms,
        metadata=metadata or {},
    )


@pytest.mark.unit
class TestLogProjectionGoldenChain:
    """Golden chain: log-entry event -> projection -> snapshot."""

    def test_project_single_entry(self) -> None:
        """Projecting a single entry updates state correctly."""
        state = ModelLogProjectionState()
        entry = _make_entry()

        new_state = NodeLogProjection.project(entry, state)

        assert new_state.total_count == 1
        assert new_state.error_count == 0
        assert len(new_state.entries) == 1
        assert new_state.entries[0] == entry
        assert new_state.last_entry_at == entry.timestamp
        assert new_state.by_node["node_build_loop"] == 1
        assert new_state.by_level["info"] == 1

    def test_project_multiple_entries(self) -> None:
        """Projecting multiple entries accumulates counters accurately."""
        state = ModelLogProjectionState()
        entries = [
            _make_entry(node_name="node_a", level=EnumLogLevel.INFO, message="msg1"),
            _make_entry(node_name="node_b", level=EnumLogLevel.WARNING, message="msg2"),
            _make_entry(node_name="node_a", level=EnumLogLevel.ERROR, message="msg3"),
            _make_entry(
                node_name="node_c", level=EnumLogLevel.CRITICAL, message="msg4"
            ),
            _make_entry(node_name="node_a", level=EnumLogLevel.DEBUG, message="msg5"),
        ]

        for entry in entries:
            state = NodeLogProjection.project(entry, state)

        assert state.total_count == 5
        assert state.error_count == 2  # ERROR + CRITICAL
        assert len(state.entries) == 5
        assert state.by_node["node_a"] == 3
        assert state.by_node["node_b"] == 1
        assert state.by_node["node_c"] == 1

    def test_query_by_node_name(self) -> None:
        """Query filters entries by node_name."""
        state = ModelLogProjectionState()
        state = NodeLogProjection.project(
            _make_entry(node_name="node_a", message="a1"), state
        )
        state = NodeLogProjection.project(
            _make_entry(node_name="node_b", message="b1"), state
        )
        state = NodeLogProjection.project(
            _make_entry(node_name="node_a", message="a2"), state
        )

        results = NodeLogProjection.query(state, ModelLogQuery(node_name="node_a"))

        assert len(results) == 2
        assert all(e.node_name == "node_a" for e in results)

    def test_query_by_level(self) -> None:
        """Query filters entries by log level."""
        state = ModelLogProjectionState()
        state = NodeLogProjection.project(
            _make_entry(level=EnumLogLevel.INFO, message="info1"), state
        )
        state = NodeLogProjection.project(
            _make_entry(level=EnumLogLevel.ERROR, message="err1"), state
        )
        state = NodeLogProjection.project(
            _make_entry(level=EnumLogLevel.INFO, message="info2"), state
        )

        results = NodeLogProjection.query(
            state, ModelLogQuery(level=EnumLogLevel.ERROR)
        )

        assert len(results) == 1
        assert results[0].level == EnumLogLevel.ERROR

    def test_query_by_correlation_id(self) -> None:
        """Query filters entries by correlation_id."""
        cid = str(uuid4())
        state = ModelLogProjectionState()
        state = NodeLogProjection.project(
            _make_entry(correlation_id=cid, message="correlated"), state
        )
        state = NodeLogProjection.project(
            _make_entry(correlation_id=str(uuid4()), message="other"), state
        )
        state = NodeLogProjection.project(
            _make_entry(correlation_id=None, message="none"), state
        )

        results = NodeLogProjection.query(state, ModelLogQuery(correlation_id=cid))

        assert len(results) == 1
        assert results[0].correlation_id == cid

    def test_error_count_tracks_error_and_critical_only(self) -> None:
        """error_count increments only for ERROR and CRITICAL levels."""
        state = ModelLogProjectionState()
        for level in EnumLogLevel:
            state = NodeLogProjection.project(
                _make_entry(level=level, message=f"{level} msg"), state
            )

        assert state.total_count == 5
        assert state.error_count == 2  # ERROR + CRITICAL only

    def test_by_node_and_by_level_accumulate(self) -> None:
        """by_node and by_level dicts accumulate correctly across entries."""
        state = ModelLogProjectionState()
        state = NodeLogProjection.project(
            _make_entry(node_name="n1", level=EnumLogLevel.INFO), state
        )
        state = NodeLogProjection.project(
            _make_entry(node_name="n1", level=EnumLogLevel.ERROR), state
        )
        state = NodeLogProjection.project(
            _make_entry(node_name="n2", level=EnumLogLevel.INFO), state
        )

        assert state.by_node == {"n1": 2, "n2": 1}
        assert state.by_level == {"info": 2, "error": 1}

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """EventBusInmemory: log-entry event -> projection -> snapshot."""
        handler = NodeLogProjection()
        state = ModelLogProjectionState()
        snapshots: list[dict[str, object]] = []

        async def on_log_entry(message: object) -> None:
            nonlocal state
            payload = json.loads(message.value)  # type: ignore[union-attr]
            entry = ModelLogEntry(**payload)
            state = handler.project(entry, state)

            snapshot = handler.emit_snapshot(state)
            snapshot_bytes = handler.serialize_snapshot(snapshot)
            snapshots.append(json.loads(snapshot_bytes))
            await event_bus.publish(SNAPSHOT_TOPIC, key=None, value=snapshot_bytes)

        await event_bus.start()
        await event_bus.subscribe(
            LOG_ENTRY_TOPIC,
            on_message=on_log_entry,
            group_id="test-log-projection",
        )

        entry = _make_entry(message="test event bus wiring")
        entry_bytes = handler.serialize_entry(entry)
        await event_bus.publish(LOG_ENTRY_TOPIC, key=None, value=entry_bytes)

        assert len(snapshots) == 1
        assert snapshots[0]["total_count"] == 1

        snapshot_history = await event_bus.get_event_history(topic=SNAPSHOT_TOPIC)
        assert len(snapshot_history) == 1

        await event_bus.close()

    async def test_structured_event_logger_emits(
        self, event_bus: EventBusInmemory
    ) -> None:
        """StructuredEventLogger emits correct events to the bus."""
        received: list[dict[str, object]] = []

        async def on_log_entry(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            received.append(payload)

        await event_bus.start()
        await event_bus.subscribe(
            LOG_ENTRY_TOPIC,
            on_message=on_log_entry,
            group_id="test-structured-logger",
        )

        logger = StructuredEventLogger("node_test", event_bus=event_bus)
        await logger.info(
            "Test message",
            function_name="test_fn",
            correlation_id="cid-123",
            duration_ms=42.5,
            extra_key="extra_val",
        )
        await logger.error("Error message", function_name="err_fn")

        assert len(received) == 2

        info_evt = received[0]
        assert info_evt["node_name"] == "node_test"
        assert info_evt["function_name"] == "test_fn"
        assert info_evt["level"] == "info"
        assert info_evt["message"] == "Test message"
        assert info_evt["correlation_id"] == "cid-123"
        assert info_evt["duration_ms"] == 42.5
        assert info_evt["metadata"]["extra_key"] == "extra_val"

        error_evt = received[1]
        assert error_evt["level"] == "error"
        assert error_evt["node_name"] == "node_test"

        await event_bus.close()

    def test_snapshot_serialization(self) -> None:
        """Snapshot serializes to valid JSON and round-trips."""
        state = ModelLogProjectionState()
        state = NodeLogProjection.project(
            _make_entry(level=EnumLogLevel.ERROR, message="err"), state
        )
        state = NodeLogProjection.project(
            _make_entry(level=EnumLogLevel.INFO, message="ok"), state
        )

        snapshot = NodeLogProjection.emit_snapshot(state)
        serialized = NodeLogProjection.serialize_snapshot(snapshot)
        deserialized = json.loads(serialized)

        assert deserialized["total_count"] == 2
        assert deserialized["error_count"] == 1
        assert deserialized["by_level"]["error"] == 1
        assert deserialized["by_level"]["info"] == 1
        assert "snapshot_id" in deserialized
        assert "snapshot_at" in deserialized

        round_tripped = ModelLogProjectionSnapshot(**deserialized)
        assert round_tripped.total_count == 2
        assert round_tripped.error_count == 1
