"""Golden chain tests for node_projection_intent_classification (OMN-13078).

Covers:
  - Single event UPSERT
  - Dedup by correlation_id (latest-state-wins)
  - Batch projection
  - Missing emitted_at uses default timestamp
  - Extra fields are silently ignored
  - Contract event_bus wiring
  - Projection API exposures declared for both snapshot topics
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_intent_classification.handlers.handler_projection_intent_classification import (
    HandlerProjectionIntentClassification,
    ModelIntentClassifiedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionIntentClassification()
NODE_DIR = Path("src/omnimarket/nodes/node_projection_intent_classification")
SUBSCRIBE_TOPIC = "onex.evt.omniintelligence.intent-classified.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-intent-classification-applied.v1"
SNAPSHOT_TOPIC = "onex.snapshot.projection.intent-classification.v1"
DISTRIBUTION_TOPIC = "onex.snapshot.projection.intent-classification.distribution.v1"
TABLE = "intent_classification_events"


class TestIntentClassificationProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelIntentClassifiedEvent(
            correlation_id="corr-001",
            session_id="sess-001",
            intent_class="feature",
            confidence=0.92,
            emitted_at="2026-06-28T10:00:00Z",
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "corr-001"
        assert rows[0]["session_id"] == "sess-001"
        assert rows[0]["intent_class"] == "feature"
        assert rows[0]["confidence"] == 0.92

    def test_upsert_overwrites_same_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelIntentClassifiedEvent(
                correlation_id="corr-002",
                session_id="sess-002",
                intent_class="analysis",
                confidence=0.5,
            ),
            db,
        )
        HANDLER.project(
            ModelIntentClassifiedEvent(
                correlation_id="corr-002",
                session_id="sess-002",
                intent_class="bugfix",
                confidence=0.88,
            ),
            db,
        )
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["intent_class"] == "bugfix"
        assert rows[0]["confidence"] == 0.88

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelIntentClassifiedEvent(
                correlation_id=f"corr-{i:03d}",
                session_id=f"sess-{i:03d}",
                intent_class="refactor",
                confidence=0.75,
            )
            for i in range(5)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 5
        assert len(db.query(TABLE)) == 5

    def test_missing_emitted_at_uses_default(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelIntentClassifiedEvent(
            correlation_id="corr-010",
            session_id="sess-010",
            intent_class="documentation",
            confidence=0.6,
        )
        HANDLER.project(event, db)
        rows = db.query(TABLE)
        assert rows[0]["emitted_at"] is not None

    def test_extra_fields_ignored(self) -> None:
        """Event model with extra='ignore' accepts unknown fields."""
        event = ModelIntentClassifiedEvent.model_validate(
            {
                "correlation_id": "corr-020",
                "session_id": "sess-020",
                "intent_class": "security",
                "confidence": 0.99,
                "unknown_field_x": "should be ignored",
            }
        )
        assert event.intent_class == "security"

    def test_keywords_default_empty(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelIntentClassifiedEvent(
            correlation_id="corr-030",
            session_id="sess-030",
            intent_class="migration",
            confidence=0.7,
        )
        HANDLER.project(event, db)
        rows = db.query(TABLE)
        assert rows[0]["keywords"] == []

    def test_keywords_stored(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelIntentClassifiedEvent(
            correlation_id="corr-031",
            session_id="sess-031",
            intent_class="feature",
            confidence=0.85,
            keywords=["api", "auth", "jwt"],
        )
        HANDLER.project(event, db)
        rows = db.query(TABLE)
        assert rows[0]["keywords"] == ["api", "auth", "jwt"]

    def test_multiple_sessions_distinct_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        for idx in range(3):
            HANDLER.project(
                ModelIntentClassifiedEvent(
                    correlation_id=f"corr-s{idx}",
                    session_id=f"sess-s{idx}",
                    intent_class="configuration",
                    confidence=0.8,
                ),
                db,
            )
        rows = db.query(TABLE)
        assert len(rows) == 3
        session_ids = {r["session_id"] for r in rows}
        assert len(session_ids) == 3


class TestIntentClassificationProjectionContract:
    def _contract(self) -> dict[str, object]:
        with open(NODE_DIR / "contract.yaml") as f:
            return yaml.safe_load(f)

    def test_node_type_is_reducer(self) -> None:
        contract = self._contract()
        assert contract["node_type"] == "reducer"

    def test_subscribes_to_intent_classified_topic(self) -> None:
        contract = self._contract()
        subscribe_topics = contract["event_bus"]["subscribe_topics"]
        assert SUBSCRIBE_TOPIC in subscribe_topics

    def test_publishes_snapshot_topics(self) -> None:
        contract = self._contract()
        publish_topics = contract["event_bus"]["publish_topics"]
        assert SNAPSHOT_TOPIC in publish_topics
        assert DISTRIBUTION_TOPIC in publish_topics

    def test_terminal_event_is_declared_externally_consumed(self) -> None:
        contract = self._contract()
        assert contract["terminal_event"] == APPLIED_TOPIC
        assert APPLIED_TOPIC in contract["externally_consumed_topics"]

    def test_db_io_declares_events_table(self) -> None:
        contract = self._contract()
        tables = contract["db_io"]["db_tables"]
        assert any(t["name"] == TABLE for t in tables)
        events_table = next(t for t in tables if t["name"] == TABLE)
        assert events_table["role"] == "events"
        assert events_table["access"] == "write"

    def test_projection_api_exposes_snapshot_topic(self) -> None:
        contract = self._contract()
        exposures = contract["projection_api"]["exposures"]
        topics = [e["topic"] for e in exposures]
        assert SNAPSHOT_TOPIC in topics

    def test_projection_api_exposes_distribution_topic(self) -> None:
        contract = self._contract()
        exposures = contract["projection_api"]["exposures"]
        topics = [e["topic"] for e in exposures]
        assert DISTRIBUTION_TOPIC in topics

    def test_snapshot_exposure_columns_present(self) -> None:
        contract = self._contract()
        exposures = contract["projection_api"]["exposures"]
        events_exposure = next(e for e in exposures if e["topic"] == SNAPSHOT_TOPIC)
        columns = events_exposure["columns"]
        for required_col in (
            "correlation_id",
            "session_id",
            "intent_class",
            "confidence",
            "emitted_at",
        ):
            assert required_col in columns, f"Missing column {required_col!r}"

    def test_migration_file_exists(self) -> None:
        migration_path = (
            NODE_DIR / "migrations" / "0000_create_intent_classification_events.sql"
        )
        assert migration_path.exists(), f"Migration not found: {migration_path}"

    def test_migration_creates_correct_table(self) -> None:
        migration_path = (
            NODE_DIR / "migrations" / "0000_create_intent_classification_events.sql"
        )
        sql = migration_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS intent_classification_events" in sql
        assert "correlation_id" in sql
        assert "session_id" in sql
        assert "intent_class" in sql
        assert "confidence" in sql


class TestIntentClassificationProjectionQuery:
    def test_query_by_intent_class(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelIntentClassifiedEvent(
                correlation_id="q-001",
                session_id="qs-001",
                intent_class="feature",
                confidence=0.9,
            ),
            db,
        )
        HANDLER.project(
            ModelIntentClassifiedEvent(
                correlation_id="q-002",
                session_id="qs-002",
                intent_class="bugfix",
                confidence=0.8,
            ),
            db,
        )
        results = db.query(TABLE, {"intent_class": "bugfix"})
        assert len(results) == 1
        assert results[0]["correlation_id"] == "q-002"
