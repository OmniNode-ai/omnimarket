"""In-node golden-chain coverage for node_projection_pattern_learning.

Co-located with the node (under src/) so the dependency-health sweep
(`--repo-roots src/`) recognizes test coverage for the contract-referenced
handlers handler_projection_pattern_learning and handler_pattern_learning.

Exercises the pattern_learning consume-leg (OMN-13124): pattern-stored.v1 ->
pattern_learning_artifacts. The broader assertion matrix lives in
tests/test_golden_chain_projection_pattern_learning.py; this module keeps a
self-contained row-delta proof plus the runner-wiring check next to the node.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_projection_pattern_learning.handlers.handler_pattern_learning import (
    KNOWN_PROJECTION_TABLES,
    PatternLearningProjectionRunner,
)
from omnimarket.nodes.node_projection_pattern_learning.handlers.handler_projection_pattern_learning import (
    HandlerProjectionPatternLearning,
    ModelPatternStoredEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_PATTERN_ID = "22222222-2222-2222-2222-222222222222"
_TABLE = "pattern_learning_artifacts"


def test_row_delta_before_zero_after_one() -> None:
    """OMN-13121 row-delta proof: before=0, project one terminal, after=1 row."""
    handler = HandlerProjectionPatternLearning()
    db = InmemoryDatabaseAdapter()
    assert len(db.query(_TABLE)) == 0

    result = handler.project(
        ModelPatternStoredEvent(
            pattern_id=_PATTERN_ID,
            pattern_name="local-projection",
            pattern_type="delegation",
            composite_score=0.81,
            correlation_id="corr-local",
        ),
        db,
    )

    assert result.rows_upserted == 1
    rows = db.query(_TABLE)
    assert len(rows) == 1
    assert rows[0]["pattern_id"] == _PATTERN_ID
    assert rows[0]["correlation_id"] == "corr-local"


def test_runner_subscribes_to_pattern_stored_topic() -> None:
    assert _TABLE in KNOWN_PROJECTION_TABLES
    runner = PatternLearningProjectionRunner()
    assert runner.subscribe_topics == ["onex.evt.omniintelligence.pattern-stored.v1"]


def test_migration_present_for_node() -> None:
    migration = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "0000_create_pattern_learning_artifacts.sql"
    )
    sql = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS pattern_learning_artifacts" in sql
    assert "uq_pattern_learning_pattern_id" in sql
