"""Golden chain tests for node_projection_sandbox_decisions (OMN-13085).

Verifies the projection handler correctly projects sandbox invocation decision
events from onex.evt.omnimarket.generated-node-invoked.v1 into the
sandbox_decisions table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_sandbox_decisions.handlers.handler_projection_sandbox_decisions import (
    HandlerProjectionSandboxDecisions,
    ModelProjectionResult,
    ModelSandboxDecisionEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSandboxDecisions()
CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_sandbox_decisions/contract.yaml"
)

# ---------------------------------------------------------------------------
# Fixtures: canonical event shapes emitted by HandlerGeneratedExecutor
# ---------------------------------------------------------------------------

COMPLETED_EVENT_DATA = {
    "correlation_id": "corr-abc-001",
    "node_name": "node_example_compute",
    "status": "completed",
    "_runtime_backend": "sandbox",
    "hot_load": False,
    "error": None,
}

FAILED_EVENT_DATA = {
    "correlation_id": "corr-abc-002",
    "node_name": "node_bad_compute",
    "status": "failed",
    "_runtime_backend": "sandbox",
    "hot_load": False,
    "error": "Generated handler missing handle() function",
}


class TestSandboxDecisionsProjection:
    """Golden chain tests: sandbox invocation decision -> sandbox_decisions table."""

    def test_project_single_event(self) -> None:
        """A completed sandbox invocation inserts one row."""
        db = InmemoryDatabaseAdapter()
        event = ModelSandboxDecisionEvent.model_validate(COMPLETED_EVENT_DATA)
        result = HANDLER.project(event, db)
        assert result.rows_inserted == 1
        rows = db.query("sandbox_decisions")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "corr-abc-001"
        assert rows[0]["node_name"] == "node_example_compute"
        assert rows[0]["status"] == "completed"
        assert rows[0]["runtime_backend"] == "sandbox"
        assert rows[0]["hot_load"] is False
        assert rows[0]["error"] is None

    def test_row_delta_before_zero_after_one(self) -> None:
        """Row-delta proof: before projection = 0 rows, after = 1 row."""
        db = InmemoryDatabaseAdapter()
        assert len(db.query("sandbox_decisions")) == 0
        event = ModelSandboxDecisionEvent.model_validate(COMPLETED_EVENT_DATA)
        HANDLER.project(event, db)
        assert len(db.query("sandbox_decisions")) == 1

    def test_dedup_by_correlation_id(self) -> None:
        """Append-only dedup: second INSERT with same correlation_id is a no-op (DO NOTHING).

        The InmemoryDatabaseAdapter.upsert treats a duplicate conflict key as
        an UPDATE (last-writer-wins), so this projects a conflicting second
        event and verifies the first row remains canonical.
        """
        db = InmemoryDatabaseAdapter()
        first = ModelSandboxDecisionEvent.model_validate(COMPLETED_EVENT_DATA)
        second = ModelSandboxDecisionEvent.model_validate(
            {
                **FAILED_EVENT_DATA,
                "correlation_id": COMPLETED_EVENT_DATA["correlation_id"],
            }
        )
        first_result = HANDLER.project(first, db)
        second_result = HANDLER.project(second, db)
        rows = db.query("sandbox_decisions")
        assert first_result.rows_inserted == 1
        assert second_result.rows_inserted == 0
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "corr-abc-001"
        assert rows[0]["node_name"] == "node_example_compute"
        assert rows[0]["status"] == "completed"
        assert rows[0]["runtime_backend"] == "sandbox"
        assert rows[0]["error"] is None

    def test_project_failed_event(self) -> None:
        """A failed sandbox invocation is projected with status=failed and error text."""
        db = InmemoryDatabaseAdapter()
        event = ModelSandboxDecisionEvent.model_validate(FAILED_EVENT_DATA)
        result = HANDLER.project(event, db)
        assert result.rows_inserted == 1
        rows = db.query("sandbox_decisions")
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "Generated handler missing handle() function"

    def test_project_batch(self) -> None:
        """Batch projection inserts multiple distinct events."""
        db = InmemoryDatabaseAdapter()
        events = [
            ModelSandboxDecisionEvent.model_validate(
                {
                    "correlation_id": f"corr-batch-{i:03d}",
                    "node_name": f"node_batch_{i}",
                    "status": "completed",
                    "_runtime_backend": "sandbox",
                    "hot_load": False,
                }
            )
            for i in range(4)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_inserted == 4
        assert len(db.query("sandbox_decisions")) == 4

    def test_extra_fields_ignored(self) -> None:
        """Event model with extra='ignore' accepts unknown fields from the executor."""
        event = ModelSandboxDecisionEvent.model_validate(
            {
                **COMPLETED_EVENT_DATA,
                "output": {"result": "some_value"},  # extra field from executor
                "scaffold": {"status": "ok"},  # extra field from executor
                "_event_bus_backend": "kafka",  # extra field from executor
                "_state_store_backend": "sandbox",  # extra field from executor
            }
        )
        assert event.correlation_id == "corr-abc-001"
        assert event.status == "completed"

    def test_missing_error_defaults_to_none(self) -> None:
        """error field is optional; missing from completed event defaults to None."""
        data = {k: v for k, v in COMPLETED_EVENT_DATA.items() if k != "error"}
        event = ModelSandboxDecisionEvent.model_validate(data)
        assert event.error is None

    def test_runtime_backend_alias(self) -> None:
        """_runtime_backend (alias) is parsed into runtime_backend field."""
        event = ModelSandboxDecisionEvent.model_validate(COMPLETED_EVENT_DATA)
        assert event.runtime_backend == "sandbox"

    def test_event_bus_wiring(self) -> None:
        """Contract event_bus subscribe_topics includes the generated-node-invoked topic."""
        with CONTRACT_PATH.open() as f:
            contract = yaml.safe_load(f)
        subscribe_topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omnimarket.generated-node-invoked.v1" in subscribe_topics

    def test_snapshot_topic_declared(self) -> None:
        """Contract declares the sandbox.decisions.v1 snapshot topic in publish_topics."""
        with CONTRACT_PATH.open() as f:
            contract = yaml.safe_load(f)
        publish_topics = contract["event_bus"]["publish_topics"]
        assert any("sandbox.decisions" in t for t in publish_topics), (
            f"sandbox.decisions snapshot topic missing from publish_topics: {publish_topics}"
        )

    def test_db_io_declared(self) -> None:
        """Contract db_io.db_tables declares sandbox_decisions table."""
        with CONTRACT_PATH.open() as f:
            contract = yaml.safe_load(f)
        tables = contract["db_io"]["db_tables"]
        table_names = [t["name"] for t in tables]
        assert "sandbox_decisions" in table_names

    def test_result_model_immutable(self) -> None:
        """ModelProjectionResult is frozen (immutable)."""
        from pydantic import ValidationError

        result = ModelProjectionResult(rows_inserted=1)
        with pytest.raises((ValidationError, TypeError)):
            result.rows_inserted = 2  # type: ignore[misc]

    def test_handle_shim_requires_db_adapter(self) -> None:
        """handle() raises TypeError when _db is missing or wrong type."""
        handler = HandlerProjectionSandboxDecisions()
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            handler.handle(
                {
                    "correlation_id": "x",
                    "node_name": "y",
                    "status": "completed",
                    "_runtime_backend": "sandbox",
                }
            )
