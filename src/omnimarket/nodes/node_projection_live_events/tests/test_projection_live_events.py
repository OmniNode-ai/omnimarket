"""Unit tests for node_projection_live_events — pure reducer path.

All tests run without live Kafka or Postgres. The InmemoryDatabaseAdapter
stands in for the real DB. Tests are organised as RED→GREEN proofs:

  1. ModelLiveEvent construction and normalisation
  2. from_raw factory for each subscribe_topic
  3. HandlerProjectionLiveEvents.project() — happy path and dedup
  4. HandlerProjectionLiveEvents.handle() — protocol shim validation
  5. Projection contract schema sanity
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_live_events.handlers.handler_projection_live_events import (
    CONFLICT_KEY,
    TABLE,
    HandlerProjectionLiveEvents,
    ModelLiveEvent,
    ModelProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_LOG_TOPIC = "onex.evt.platform.log-entry.v1"
_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"
_INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"
_STATE_CHANGE_TOPIC = "onex.evt.platform.node-state-change.v1"
_DELEGATION_DONE_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_DELEGATION_FAIL_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"
_DELEGATE_COMMAND_TOPIC = "onex.cmd.omnimarket.delegate-skill.v1"
_DELEGATE_DONE_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
_DELEGATE_FAIL_TOPIC = "onex.evt.omnimarket.delegate-skill-failed.v1"
_GENERATION_COMMAND_TOPIC = "onex.cmd.omnimarket.node-generation-requested.v1"
_GENERATION_DONE_TOPIC = "onex.evt.omnimarket.node-generation-completed.v1"
_GENERATION_FAIL_TOPIC = "onex.evt.omnimarket.node-generation-failed.v1"


def _make_handler() -> tuple[HandlerProjectionLiveEvents, InmemoryDatabaseAdapter]:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionLiveEvents()
    return handler, db


def _make_log_event(
    *,
    correlation_id: str | None = None,
    message: str = "Phase transition complete",
) -> dict[str, Any]:
    return {
        "entry_id": str(uuid4()),
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "node_name": "node_build_loop",
        "level": "info",
        "message": message,
        "correlation_id": correlation_id,
    }


def _make_delegation_event(
    *,
    failed: bool = False,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "correlation_id": correlation_id or str(uuid4()),
        "delegated_to": "qwen3-coder-30b",
        "task_type": "code_review",
        "quality_gate_passed": not failed,
        "reason": "Delegation failed — quality gate rejected" if failed else "",
    }


# ---------------------------------------------------------------------------
# 1. ModelLiveEvent construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelLiveEventConstruction:
    """ModelLiveEvent accepts fields and applies defaults correctly."""

    def test_minimal_construction_with_topic(self) -> None:
        event = ModelLiveEvent(topic=_LOG_TOPIC)
        assert event.topic == _LOG_TOPIC
        assert event.type == "ACTION"
        assert event.source == "platform"
        assert event.summary == ""
        assert event.payload == "{}"
        assert event.correlation_id is None
        assert event.event_id != ""

    def test_full_construction(self) -> None:
        eid = str(uuid4())
        ts = datetime.now(tz=UTC).isoformat()
        event = ModelLiveEvent(
            event_id=eid,
            type="ROUTING",
            timestamp=ts,
            source="omnibase-infra",
            topic=_DELEGATION_DONE_TOPIC,
            summary="Delegation completed",
            payload='{"key": "val"}',
            correlation_id="corr-001",
        )
        assert event.event_id == eid
        assert event.type == "ROUTING"
        assert event.source == "omnibase-infra"
        assert event.summary == "Delegation completed"
        assert event.correlation_id == "corr-001"

    def test_model_is_frozen(self) -> None:
        event = ModelLiveEvent(topic=_LOG_TOPIC)
        with pytest.raises(ValidationError):
            event.type = "MUTATED"  # type: ignore[misc]

    def test_correlation_id_alias(self) -> None:
        # Validate that from_raw correctly resolves correlationId from raw payloads
        raw = {"event_id": str(uuid4()), "correlationId": "alias-cid"}
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)
        assert event.correlation_id == "alias-cid"

    def test_correlation_id_is_not_event_id_fallback(self) -> None:
        raw = {"correlation_id": "corr-shared-001"}
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        assert event.correlation_id == "corr-shared-001"
        assert event.event_id != "corr-shared-001"
        UUID(event.event_id)


# ---------------------------------------------------------------------------
# 2. from_raw factory
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelLiveEventFromRaw:
    """from_raw correctly normalises each subscribe_topic payload shape."""

    def test_from_log_entry(self) -> None:
        raw = _make_log_event(message="Test log message", correlation_id="cid-1")
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        assert event.topic == _LOG_TOPIC
        assert event.type == "ACTION"
        # source resolves from node_name when present (the emitting service)
        assert event.source == "node_build_loop"
        assert event.summary == "Test log message"
        assert event.correlation_id == "cid-1"
        assert event.event_id == raw["entry_id"]

    def test_from_heartbeat(self) -> None:
        raw = {
            "event_id": str(uuid4()),
            "node_name": "node_projection_registration",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        event = ModelLiveEvent.from_raw(raw, _HEARTBEAT_TOPIC)

        assert event.topic == _HEARTBEAT_TOPIC
        assert event.type == "ACTION"
        assert event.source == "node_projection_registration"
        assert event.event_id == raw["event_id"]

    def test_from_introspection(self) -> None:
        raw = {
            "event_id": str(uuid4()),
            "service_name": "node_log_projection",
            "description": "Node registered",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        event = ModelLiveEvent.from_raw(raw, _INTROSPECTION_TOPIC)

        assert event.topic == _INTROSPECTION_TOPIC
        assert event.type == "ACTION"
        assert event.summary == "Node registered"

    def test_from_state_change(self) -> None:
        raw = {
            "event_id": str(uuid4()),
            "service_name": "node_build_loop",
            "new_state": "active",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        event = ModelLiveEvent.from_raw(raw, _STATE_CHANGE_TOPIC)

        assert event.topic == _STATE_CHANGE_TOPIC
        assert event.type == "TRANSFORMATION"

    def test_from_delegation_completed(self) -> None:
        raw = _make_delegation_event(failed=False, correlation_id="corr-abc")
        event = ModelLiveEvent.from_raw(raw, _DELEGATION_DONE_TOPIC)

        assert event.topic == _DELEGATION_DONE_TOPIC
        assert event.type == "ROUTING"
        assert event.source == "omnibase-infra"
        assert event.correlation_id == "corr-abc"

    def test_from_delegation_failed(self) -> None:
        raw = _make_delegation_event(failed=True)
        event = ModelLiveEvent.from_raw(raw, _DELEGATION_FAIL_TOPIC)

        assert event.topic == _DELEGATION_FAIL_TOPIC
        assert event.type == "ERROR"
        assert event.source == "omnibase-infra"
        assert "Delegation failed" in event.summary

    def test_from_command_keeps_task_class_and_correlation_visible(self) -> None:
        raw = {
            "event_id": str(uuid4()),
            "task_type": "code_review",
            "correlation_id": "corr-command-1",
        }
        event = ModelLiveEvent.from_raw(raw, _DELEGATE_COMMAND_TOPIC)

        assert event.type == "COMMAND"
        assert event.source == "omnimarket"
        assert event.summary == "code_review"
        assert event.correlation_id == "corr-command-1"

    def test_from_raw_generates_event_id_when_absent(self) -> None:
        raw: dict[str, Any] = {"message": "no id present", "topic": _LOG_TOPIC}
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)
        assert event.event_id != ""
        assert len(event.event_id) > 0

    def test_from_raw_serialises_payload_as_json(self) -> None:
        raw = _make_log_event(message="check payload")
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)
        parsed = json.loads(event.payload)
        assert parsed["message"] == "check payload"

    def test_from_raw_unknown_topic_defaults_to_action(self) -> None:
        raw = {"event_id": str(uuid4()), "message": "unknown topic event"}
        event = ModelLiveEvent.from_raw(
            raw, "onex.evt.omnimarket.unknown-live-event.v1"
        )
        assert event.type == "ACTION"
        # source is derived from the topic's service segment (onex.evt.<service>.*)
        assert event.source == "omnimarket"


# ---------------------------------------------------------------------------
# 3. HandlerProjectionLiveEvents.project() — happy path and dedup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerProjectionLiveEventsProject:
    """project() upserts rows correctly and deduplicates by event_id."""

    def test_project_single_event(self) -> None:
        handler, db = _make_handler()
        raw = _make_log_event(message="single event")
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        result = handler.project(event, db)

        assert isinstance(result, ModelProjectionResult)
        assert result.rows_upserted == 1
        assert result.table == TABLE
        assert result.event_id == event.event_id

        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0][CONFLICT_KEY] == event.event_id
        assert rows[0]["topic"] == _LOG_TOPIC
        assert rows[0]["type"] == "ACTION"

    def test_project_multiple_different_events(self) -> None:
        handler, db = _make_handler()
        for i in range(5):
            raw = _make_log_event(message=f"event {i}")
            event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)
            handler.project(event, db)

        rows = db.query(TABLE)
        assert len(rows) == 5

    def test_project_deduplicates_by_event_id(self) -> None:
        handler, db = _make_handler()
        raw = _make_log_event(message="first write")
        event_id = raw["entry_id"]
        event1 = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        # First write
        handler.project(event1, db)
        assert len(db.query(TABLE)) == 1

        # Same event_id, updated summary — should UPSERT not INSERT
        raw2 = dict(raw)
        raw2["message"] = "updated write"
        event2 = ModelLiveEvent.from_raw(raw2, _LOG_TOPIC)
        assert event2.event_id == event_id  # same id
        handler.project(event2, db)

        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["summary"] == "updated write"

    def test_project_preserves_created_at_on_dedup_upsert(self) -> None:
        handler, db = _make_handler()
        raw = _make_log_event(message="first write")
        event1 = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        handler.project(event1, db)
        rows = db.query(TABLE)
        assert len(rows) == 1
        original_created_at = rows[0]["created_at"]

        raw2 = dict(raw)
        raw2["message"] = "updated write"
        event2 = ModelLiveEvent.from_raw(raw2, _LOG_TOPIC)
        handler.project(event2, db)

        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["summary"] == "updated write"
        assert rows[0]["created_at"] == original_created_at

    def test_project_across_multiple_topics(self) -> None:
        handler, db = _make_handler()

        raw_log = _make_log_event(message="log event")
        handler.project(ModelLiveEvent.from_raw(raw_log, _LOG_TOPIC), db)

        raw_del = _make_delegation_event(failed=False)
        handler.project(ModelLiveEvent.from_raw(raw_del, _DELEGATION_DONE_TOPIC), db)

        raw_fail = _make_delegation_event(failed=True)
        handler.project(ModelLiveEvent.from_raw(raw_fail, _DELEGATION_FAIL_TOPIC), db)

        rows = db.query(TABLE)
        assert len(rows) == 3
        topics = {r["topic"] for r in rows}
        assert _LOG_TOPIC in topics
        assert _DELEGATION_DONE_TOPIC in topics
        assert _DELEGATION_FAIL_TOPIC in topics

    def test_project_stores_correlation_id(self) -> None:
        handler, db = _make_handler()
        cid = "corr-test-001"
        raw = _make_log_event(correlation_id=cid)
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        handler.project(event, db)

        rows = db.query(TABLE, {"correlation_id": cid})
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == cid

    def test_project_result_contains_event_id(self) -> None:
        handler, db = _make_handler()
        raw = _make_log_event(message="event id check")
        event = ModelLiveEvent.from_raw(raw, _LOG_TOPIC)

        result = handler.project(event, db)

        assert result.event_id == event.event_id


# ---------------------------------------------------------------------------
# 4. HandlerProjectionLiveEvents.handle() — protocol shim validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerProjectionLiveEventsHandle:
    """handle() protocol shim dispatches correctly and validates inputs."""

    def test_handle_happy_path(self) -> None:
        handler = HandlerProjectionLiveEvents()
        db = InmemoryDatabaseAdapter()
        raw = _make_log_event(message="shim test")
        input_data: dict[str, object] = {
            "_db": db,
            "_topic": _LOG_TOPIC,
            **raw,
        }

        result = handler.handle(input_data)

        assert isinstance(result, dict)
        assert result["rows_upserted"] == 1
        assert result["table"] == TABLE

    def test_handle_raises_without_db(self) -> None:
        handler = HandlerProjectionLiveEvents()
        raw = _make_log_event(message="no db")
        input_data: dict[str, object] = {"_topic": _LOG_TOPIC, **raw}

        with pytest.raises(TypeError, match="DatabaseAdapter"):
            handler.handle(input_data)

    def test_handle_raises_without_topic(self) -> None:
        handler = HandlerProjectionLiveEvents()
        db = InmemoryDatabaseAdapter()
        raw = _make_log_event(message="no topic")
        input_data: dict[str, object] = {"_db": db, **raw}

        with pytest.raises(ValueError, match="_topic"):
            handler.handle(input_data)

    def test_handle_raises_with_empty_topic(self) -> None:
        handler = HandlerProjectionLiveEvents()
        db = InmemoryDatabaseAdapter()
        raw = _make_log_event()
        input_data: dict[str, object] = {"_db": db, "_topic": "   ", **raw}

        with pytest.raises(ValueError, match="_topic"):
            handler.handle(input_data)

    def test_handle_persists_to_db(self) -> None:
        handler = HandlerProjectionLiveEvents()
        db = InmemoryDatabaseAdapter()
        raw = _make_log_event(message="persistence check")
        input_data: dict[str, object] = {
            "_db": db,
            "_topic": _LOG_TOPIC,
            **raw,
        }

        handler.handle(input_data)

        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["topic"] == _LOG_TOPIC

    def test_handle_delegation_topic(self) -> None:
        handler = HandlerProjectionLiveEvents()
        db = InmemoryDatabaseAdapter()
        raw = _make_delegation_event(failed=False, correlation_id="corr-xyz")
        input_data: dict[str, object] = {
            "_db": db,
            "_topic": _DELEGATION_DONE_TOPIC,
            **raw,
        }

        result = handler.handle(input_data)

        assert result["rows_upserted"] == 1
        rows = db.query(TABLE)
        assert rows[0]["type"] == "ROUTING"
        assert rows[0]["source"] == "omnibase-infra"
        assert rows[0]["correlation_id"] == "corr-xyz"


# ---------------------------------------------------------------------------
# 5. Contract schema sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractSchema:
    """Verify the contract.yaml is valid and declares required fields."""

    def test_contract_is_valid_yaml(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_contract_node_type_is_reducer(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        assert raw["node_type"] == "reducer"

    def test_contract_declares_subscribe_topics(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        topics: list[str] = raw["event_bus"]["subscribe_topics"]
        assert _LOG_TOPIC in topics
        assert _HEARTBEAT_TOPIC in topics
        assert _DELEGATION_DONE_TOPIC in topics
        assert _DELEGATION_FAIL_TOPIC in topics
        assert _DELEGATE_COMMAND_TOPIC in topics
        assert _DELEGATE_DONE_TOPIC in topics
        assert _DELEGATE_FAIL_TOPIC in topics
        assert _GENERATION_COMMAND_TOPIC in topics
        assert _GENERATION_DONE_TOPIC in topics
        assert _GENERATION_FAIL_TOPIC in topics

    def test_contract_declares_snapshot_publish_topic(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        publish_topics: list[str] = raw["event_bus"]["publish_topics"]
        assert any("live-events" in t for t in publish_topics)

    def test_contract_declares_dlq_topic(self) -> None:
        """OMN-13992: malformed/erroring events must be dead-lettered, not
        dropped with only a log line — the runtime routes to
        event_bus.dlq_topics[0] when the projection handler raises."""
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        dlq_topics: list[str] = raw["event_bus"]["dlq_topics"]
        assert dlq_topics
        assert any("dlq" in t and "live-events" in t for t in dlq_topics)

    def test_contract_declares_projection_api_exposure(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        assert raw.get("projection_api", {}).get("expose") is True
        exposures: list[dict[str, object]] = raw["projection_api"]["exposures"]
        assert any("live-events" in str(e.get("topic", "")) for e in exposures)

    def test_contract_declares_db_io(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        tables: list[dict[str, object]] = raw["db_io"]["db_tables"]
        assert any(t["name"] == "live_events" for t in tables)

    def test_contract_input_model_path_is_correct(self) -> None:
        raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
        assert (
            raw["input_model"]
            == "omnimarket.nodes.node_projection_live_events.handlers."
            "handler_projection_live_events.ModelLiveEvent"
        )

    def test_migration_file_exists(self) -> None:
        migration_path = (
            _CONTRACT_PATH.parent / "migrations" / "0000_create_live_events.sql"
        )
        assert migration_path.exists(), f"Migration not found: {migration_path}"
        content = migration_path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS live_events" in content
