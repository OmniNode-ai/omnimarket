"""Golden chain tests for node_projection_traces (OMN-13083).

Covers the traces projection consume-leg: platform log-entry events ->
correlation-grouped `traces` rows. Includes the OMN-13121 row-delta proof
pattern (before=0, project one entry, after=exactly 1 row) plus the
cross-event aggregation the dashboard trace-explorer depends on
(event_count, nodes_involved union, last_event_at monotonicity, sticky
has_error, is_running cleared on a terminal entry).
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_traces.handlers.handler_projection_traces import (
    HandlerProjectionTraces,
)
from omnimarket.nodes.node_projection_traces.models.model_trace_event import (
    ModelTraceProjectionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionTraces()
_TABLE = "traces"
_CORR = "corr-golden-001"


def _event(**overrides: object) -> ModelTraceProjectionEvent:
    base: dict[str, object] = {
        "correlation_id": _CORR,
        "node_name": "node_delegate",
        "level": "INFO",
        "message": "started",
        "timestamp": "2026-06-24T12:00:00Z",
    }
    base.update(overrides)
    return ModelTraceProjectionEvent(**base)


class TestTracesProjectionChain:
    def test_row_delta_before_zero_after_one(self) -> None:
        """OMN-13121 row-delta proof: before=0, project one entry, after=1 row."""
        db = InmemoryDatabaseAdapter()
        assert len(db.query(_TABLE)) == 0

        result = HANDLER.project(_event(), db)

        assert result.rows_upserted == 1
        rows = db.query(_TABLE)
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == _CORR
        assert rows[0]["event_count"] == 1
        assert rows[0]["is_running"] is True
        assert rows[0]["has_error"] is False

    def test_second_entry_aggregates_same_correlation(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_event(node_name="node_a"), db)
        HANDLER.project(
            _event(
                node_name="node_b",
                message="midway",
                timestamp="2026-06-24T12:00:02Z",
            ),
            db,
        )
        rows = db.query(_TABLE)
        assert len(rows) == 1, "same correlation_id must collapse to one row"
        row = rows[0]
        assert row["event_count"] == 2
        assert row["nodes_involved"] == ["node_a", "node_b"]
        assert row["last_event_at"] == "2026-06-24T12:00:02+00:00"
        assert row["duration_ms"] == 2000
        assert row["latest_message"] == "midway"

    def test_error_is_sticky_and_terminal_clears_running(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_event(level="ERROR", message="boom"), db)
        HANDLER.project(
            _event(
                level="INFO",
                message="done",
                timestamp="2026-06-24T12:00:05Z",
                is_terminal=True,
            ),
            db,
        )
        row = db.query(_TABLE)[0]
        assert row["has_error"] is True, "error status must stick across entries"
        assert row["is_running"] is False, "terminal entry clears running"

    def test_distinct_correlations_produce_distinct_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_event(correlation_id="corr-x"), db)
        HANDLER.project(_event(correlation_id="corr-y"), db)
        assert len(db.query(_TABLE)) == 2

    def test_project_batch_counts_upserts(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [_event(correlation_id=f"corr-{i}") for i in range(4)]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 4
        assert len(db.query(_TABLE)) == 4

    def test_handle_materializes_via_injected_db(self) -> None:
        """OMN-13083: handle() is the REAL runtime materialization leg.

        The runtime auto-wiring projection callback calls handle(input_data)
        with a DatabaseAdapter at input_data['_db'] and gates row materialization
        on the returned rows_upserted. Assert handle() writes one row through the
        injected adapter and reports the write (the previous snapshot-only return
        never touched the DB, which is why the table stayed at row_count=0).
        """
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "correlation_id": _CORR,
                "node_name": "node_delegate",
                "level": "INFO",
                "message": "started",
                "timestamp": "2026-06-24T12:00:00Z",
                "is_terminal": False,
                "_db": db,
                "_event_type": "log-entry",
            }
        )
        assert result["rows_upserted"] == 1
        rows = db.query(_TABLE, {"correlation_id": _CORR})
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == _CORR
        assert rows[0]["latest_message"] == "started"


class TestTracesContractWiring:
    def test_event_bus_subscribes_log_entry(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_traces/contract.yaml"
        with open(contract_path) as fh:
            contract = yaml.safe_load(fh)
        assert (
            "onex.evt.platform.log-entry.v1"
            in contract["event_bus"]["subscribe_topics"]
        )

    def test_projection_api_binds_traces_table(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_traces/contract.yaml"
        with open(contract_path) as fh:
            contract = yaml.safe_load(fh)
        assert contract["projection_api"]["table"] == _TABLE
        assert contract["projection_api"]["schema"] == "public"

    def test_migration_creates_traces_table(self) -> None:
        from pathlib import Path

        migration = (
            Path("src/omnimarket/nodes/node_projection_traces/migrations")
            / "0001_create_traces.sql"
        )
        sql = migration.read_text()
        assert "CREATE TABLE IF NOT EXISTS traces" in sql
        assert "correlation_id VARCHAR" in sql
