"""Golden chain tests for node_projection_pattern_learning.

Covers the pattern_learning chain consume-leg (OMN-13124): pattern-stored.v1 ->
pattern_learning_artifacts. Includes the OMN-13121 row-delta proof pattern
(before=0, project one terminal, after=exactly 1 row).
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_pattern_learning.handlers.handler_projection_pattern_learning import (
    HandlerProjectionPatternLearning,
    ModelPatternStoredEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionPatternLearning()

_PATTERN_ID = "11111111-1111-1111-1111-111111111111"
_TABLE = "pattern_learning_artifacts"


class TestPatternLearningProjection:
    def test_row_delta_before_zero_after_one(self) -> None:
        """OMN-13121 row-delta proof: before=0, publish a terminal, after=1 row."""
        db = InmemoryDatabaseAdapter()
        assert len(db.query(_TABLE)) == 0

        event = ModelPatternStoredEvent(
            pattern_id=_PATTERN_ID,
            pattern_name="delegation-routing",
            pattern_type="delegation",
            composite_score=0.92,
            correlation_id="corr-001",
        )
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        rows = db.query(_TABLE)
        assert len(rows) == 1
        assert rows[0]["pattern_id"] == _PATTERN_ID
        assert rows[0]["pattern_name"] == "delegation-routing"
        assert rows[0]["correlation_id"] == "corr-001"

    def test_upsert_overwrites_same_pattern_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelPatternStoredEvent(
                pattern_id=_PATTERN_ID,
                pattern_name="v1",
                lifecycle_state="candidate",
                composite_score=0.4,
            ),
            db,
        )
        HANDLER.project(
            ModelPatternStoredEvent(
                pattern_id=_PATTERN_ID,
                pattern_name="v2",
                lifecycle_state="promoted",
                composite_score=0.8,
            ),
            db,
        )
        rows = db.query(_TABLE)
        assert len(rows) == 1
        assert rows[0]["pattern_name"] == "v2"
        assert rows[0]["lifecycle_state"] == "promoted"
        assert rows[0]["composite_score"] == 0.8

    def test_preserve_existing_evidence_on_sparse_followup(self) -> None:
        """A later sparse event must not erase richer materialized state."""
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelPatternStoredEvent(
                pattern_id=_PATTERN_ID,
                pattern_name="rich-pattern",
                pattern_type="delegation",
                composite_score=0.9,
                scoring_evidence={"signal": "strong"},
                signature={"shape": "abc"},
                correlation_id="corr-001",
            ),
            db,
        )
        # Sparse follow-up: only the id, blank evidence, zeroed score.
        HANDLER.project(ModelPatternStoredEvent(pattern_id=_PATTERN_ID), db)

        rows = db.query(_TABLE)
        assert len(rows) == 1
        preserved = rows[0]
        assert preserved["pattern_name"] == "rich-pattern"
        assert preserved["pattern_type"] == "delegation"
        assert preserved["composite_score"] == 0.9
        assert preserved["scoring_evidence"] == {"signal": "strong"}
        assert preserved["signature"] == {"shape": "abc"}
        assert preserved["correlation_id"] == "corr-001"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelPatternStoredEvent(
                pattern_id=f"00000000-0000-0000-0000-00000000000{i}",
                pattern_name=f"pat-{i}",
                composite_score=0.5,
            )
            for i in range(5)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 5
        assert len(db.query(_TABLE)) == 5

    def test_handle_shim_requires_db_adapter(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "_db": db,
                "_event_type": "pattern-stored",
                "pattern_id": _PATTERN_ID,
                "pattern_name": "via-shim",
                "composite_score": 0.7,
                "correlation_id": "corr-shim",
            }
        )
        assert result["rows_upserted"] == 1
        rows = db.query(_TABLE)
        assert len(rows) == 1
        assert rows[0]["pattern_name"] == "via-shim"

    def test_extra_fields_ignored(self) -> None:
        event = ModelPatternStoredEvent.model_validate(
            {
                "pattern_id": _PATTERN_ID,
                "pattern_name": "p",
                "stored_at": "2026-06-13T00:00:00Z",
                "domain": "ignored-extra",
            }
        )
        assert event.pattern_id == _PATTERN_ID

    def test_event_bus_wiring(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_pattern_learning/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniintelligence.pattern-stored.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert len(contract["event_bus"]["publish_topics"]) >= 1

    def test_projection_api_schema_is_public(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_pattern_learning/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert contract["projection_api"]["schema"] == "public"
        assert contract["projection_api"]["table"] == _TABLE


class TestPatternLearningRunnerWiring:
    def test_runner_knows_artifacts_table(self) -> None:
        from omnimarket.nodes.node_projection_pattern_learning.handlers.handler_pattern_learning import (
            KNOWN_PROJECTION_TABLES,
            PatternLearningProjectionRunner,
        )

        assert _TABLE in KNOWN_PROJECTION_TABLES
        runner = PatternLearningProjectionRunner()
        assert runner.subscribe_topics == [
            "onex.evt.omniintelligence.pattern-stored.v1"
        ]
        assert runner.topics == ["onex.evt.omniintelligence.pattern-stored.v1"]

    def test_migration_file_creates_table(self) -> None:
        from pathlib import Path

        migration = (
            Path("src/omnimarket/nodes/node_projection_pattern_learning/migrations")
            / "0000_create_pattern_learning_artifacts.sql"
        )
        sql = migration.read_text()
        assert "CREATE TABLE IF NOT EXISTS pattern_learning_artifacts" in sql
        assert "pattern_id" in sql
        assert "correlation_id" in sql
        assert "uq_pattern_learning_pattern_id" in sql
