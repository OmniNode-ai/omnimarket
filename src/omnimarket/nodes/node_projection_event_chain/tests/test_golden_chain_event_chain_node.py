"""Node-local handler coverage for node_projection_event_chain."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_event_chain.handlers.handler_projection_event_chain import (
    CONFLICT_KEY,
    PUBLISH_TOPIC_CHAIN_APPLIED,
    SUBSCRIBE_TOPIC_LOG_ENTRY,
    TABLE,
    HandlerProjectionEventChain,
)
from omnimarket.nodes.node_projection_event_chain.models.model_event_chain_event import (
    ModelEventChainProjectionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"


def _payload(envelope_id: str = "env-1") -> dict[str, object]:
    return {
        "correlation_id": "corr-handler-local",
        "envelope_id": envelope_id,
        "causation_id": "corr-handler-local",
        "topic": SUBSCRIBE_TOPIC_LOG_ENTRY,
        "source_node": "node_source",
        "timestamp": "2026-06-24T12:00:00Z",
        "payload": {"step": envelope_id},
    }


def _event(payload: dict[str, object]) -> ModelEventChainProjectionEvent:
    return ModelEventChainProjectionEvent.model_validate(payload)


def _contract() -> dict[str, object]:
    data: dict[str, object] = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return data


def test_topics_are_contract_derived() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["subscribe_topics"][0] == SUBSCRIBE_TOPIC_LOG_ENTRY
    assert event_bus["publish_topics"][0] == PUBLISH_TOPIC_CHAIN_APPLIED


def test_handle_requires_projection_db_adapter() -> None:
    with pytest.raises(TypeError, match="ProtocolProjectionDatabaseSync"):
        HandlerProjectionEventChain().handle(_payload())


def test_handle_materializes_ordered_projection_row() -> None:
    db = InmemoryDatabaseAdapter()
    payload = _payload()
    payload["_db"] = db
    payload["_event_type"] = "log-entry"

    result = HandlerProjectionEventChain().handle(payload)

    assert result == {"rows_upserted": 1, "table": TABLE}
    rows = db.query(TABLE, {"correlation_id": "corr-handler-local"})
    assert len(rows) == 1
    assert rows[0]["sequence"] == 0
    assert rows[0]["envelope_id"] == "env-1"
    assert rows[0]["topic"] == SUBSCRIBE_TOPIC_LOG_ENTRY


def test_replayed_envelope_reuses_existing_sequence() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionEventChain()
    event = _payload("env-replay")
    updated = dict(event)
    updated["payload"] = {"step": "updated"}

    handler.project(_event(event), db)
    handler.project(_event(updated), db)

    rows = db.query(TABLE, {"correlation_id": "corr-handler-local"})
    assert len(rows) == 1
    assert rows[0]["sequence"] == 0
    assert rows[0]["payload"] == {"step": "updated"}


def test_conflict_key_matches_contract_idempotency_fields() -> None:
    assert CONFLICT_KEY == "correlation_id, envelope_id"
