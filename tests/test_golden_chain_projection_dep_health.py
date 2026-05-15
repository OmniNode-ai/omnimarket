# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_dep_health."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
    EnumDepHealthSeverity,
    ModelDepHealthFinding,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_completed_event import (
    ModelDepHealthSweepCompletedEvent,
)
from omnimarket.nodes.node_projection_dep_health.handlers.handler_projection_dep_health import (
    HandlerProjectionDepHealth,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDepHealth()

_NOW = datetime(2025, 5, 15, 12, 0, 0, tzinfo=UTC)

_FINDING_A = ModelDepHealthFinding(
    finding_type=EnumDepHealthFindingType.MISSING_TOPIC_EDGE,
    severity=EnumDepHealthSeverity.CRITICAL,
    repo="omnimarket",
    file_path="src/omnimarket/nodes/node_foo/contract.yaml",
    symbol=None,
    detail="Command topic onex.cmd.omnimarket.foo.v1 has no consumer.",
    rule_id="MISSING_TOPIC_EDGE",
    rule_version="v1",
)

_FINDING_B = ModelDepHealthFinding(
    finding_type=EnumDepHealthFindingType.UNTESTED_HANDLER,
    severity=EnumDepHealthSeverity.MAJOR,
    repo="omnimarket",
    file_path="src/omnimarket/nodes/node_foo/handlers/handler_foo.py",
    symbol="HandlerFoo",
    detail="Handler referenced in contract has no test coverage.",
    rule_id="UNTESTED_HANDLER",
    rule_version="v1",
)


def _make_event(
    findings: list[ModelDepHealthFinding],
    run_id: str = "run-001",
) -> ModelDepHealthSweepCompletedEvent:
    return ModelDepHealthSweepCompletedEvent(
        run_id=run_id,
        findings=findings,
        summary={str(f.finding_type): 1 for f in findings},
        captured_at=_NOW,
    )


class TestProjectionDepHealth:
    def test_project_two_findings(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _make_event([_FINDING_A, _FINDING_B])
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 2
        rows = db.query("dep_health_findings")
        assert len(rows) == 2

    def test_row_fields_correct(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _make_event([_FINDING_A])
        HANDLER.project(event, db)
        rows = db.query("dep_health_findings")
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == "run-001"
        assert row["finding_type"] == EnumDepHealthFindingType.MISSING_TOPIC_EDGE
        assert row["severity"] == EnumDepHealthSeverity.CRITICAL
        assert row["repo"] == "omnimarket"
        assert row["file_path"] == "src/omnimarket/nodes/node_foo/contract.yaml"
        assert row["symbol"] == ""
        assert row["rule_id"] == "MISSING_TOPIC_EDGE"
        assert row["rule_version"] == "v1"
        assert row["captured_at"] == _NOW.isoformat()

    def test_idempotent_upsert(self) -> None:
        """Projecting the same event twice leaves exactly N rows, not 2N."""
        db = InmemoryDatabaseAdapter()
        event = _make_event([_FINDING_A, _FINDING_B])
        HANDLER.project(event, db)
        HANDLER.project(event, db)
        rows = db.query("dep_health_findings")
        assert len(rows) == 2

    def test_empty_findings(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _make_event([])
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 0
        assert db.query("dep_health_findings") == []

    def test_different_run_ids_produce_separate_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        event_a = _make_event([_FINDING_A], run_id="run-001")
        event_b = _make_event([_FINDING_A], run_id="run-002")
        HANDLER.project(event_a, db)
        HANDLER.project(event_b, db)
        rows = db.query("dep_health_findings")
        assert len(rows) == 2

    def test_contract_topic_wired(self) -> None:
        import yaml

        contract_path = "src/omnimarket/nodes/node_projection_dep_health/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnimarket.dep-health-sweep-completed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert contract["node_type"] == "REDUCER"
