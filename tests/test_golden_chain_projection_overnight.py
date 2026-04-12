# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_overnight.

Validates the full projection flow:
  phase-start → overnight_sessions INSERT
  phase-completed → overnight_session_phases INSERT
  session-completed → overnight_sessions UPDATE to terminal status

OMN-8455 TDD requirement: these tests must pass before handler changes merge.
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_overnight.handlers.handler_projection_overnight import (
    HandlerProjectionOvernightPhaseEnd,
    HandlerProjectionOvernightSessionComplete,
    HandlerProjectionOvernightSessionStart,
    ModelOvernightPhaseEndEvent,
    ModelOvernightSessionCompleteEvent,
    ModelOvernightSessionStartEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

SESSION_START_HANDLER = HandlerProjectionOvernightSessionStart()
PHASE_END_HANDLER = HandlerProjectionOvernightPhaseEnd()
SESSION_COMPLETE_HANDLER = HandlerProjectionOvernightSessionComplete()


class TestOvernightSessionStartProjection:
    def test_project_session_start_creates_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelOvernightSessionStartEvent(
            correlation_id="sess-001",
            phase="build_loop_orchestrator",
            timestamp="2026-04-12T02:00:00Z",
        )
        result = SESSION_START_HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("overnight_sessions")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-001"
        assert rows[0]["session_status"] == "in_progress"

    def test_duplicate_start_idempotent(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelOvernightSessionStartEvent(
            correlation_id="sess-002",
            phase="nightly_loop_controller",
        )
        SESSION_START_HANDLER.project(event, db)
        SESSION_START_HANDLER.project(event, db)
        rows = db.query("overnight_sessions")
        assert len(rows) == 1

    def test_dry_run_flag_stored(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelOvernightSessionStartEvent(
            correlation_id="sess-003",
            phase="platform_readiness",
            dry_run=True,
        )
        SESSION_START_HANDLER.project(event, db)
        rows = db.query("overnight_sessions")
        assert rows[0]["dry_run"] is True


class TestOvernightPhaseEndProjection:
    def test_project_phase_end_creates_phase_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightPhaseEnd()
        event = ModelOvernightPhaseEndEvent(
            correlation_id="sess-010",
            phase="build_loop_orchestrator",
            phase_status="success",
            duration_ms=45000,
            timestamp="2026-04-12T02:10:00Z",
        )
        result = handler.project(event, db)
        assert result.rows_upserted == 1
        assert result.table == "overnight_session_phases"
        phase_rows = db.query("overnight_session_phases")
        assert len(phase_rows) == 1
        assert phase_rows[0]["phase_name"] == "build_loop_orchestrator"
        assert phase_rows[0]["phase_status"] == "success"
        assert phase_rows[0]["duration_ms"] == 45000

    def test_phase_end_ensures_parent_session_row(self) -> None:
        """Out-of-order: phase-end arrives before session-start."""
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightPhaseEnd()
        event = ModelOvernightPhaseEndEvent(
            correlation_id="sess-011",
            phase="platform_readiness",
            phase_status="success",
            duration_ms=3200,
        )
        handler.project(event, db)
        # Parent row must exist
        session_rows = db.query("overnight_sessions")
        assert len(session_rows) == 1
        assert session_rows[0]["session_id"] == "sess-011"

    def test_skipped_phase_stored_correctly(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightPhaseEnd()
        event = ModelOvernightPhaseEndEvent(
            correlation_id="sess-012",
            phase="ci_watch",
            phase_status="skipped",
            error_message="SKIPPED: no PR refs",
            duration_ms=0,
        )
        handler.project(event, db)
        phase_rows = db.query("overnight_session_phases")
        assert phase_rows[0]["phase_status"] == "skipped"
        assert phase_rows[0]["error_message"] == "SKIPPED: no PR refs"

    def test_unknown_phase_status_defaults_to_failed(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightPhaseEnd()
        event = ModelOvernightPhaseEndEvent(
            correlation_id="sess-013",
            phase="merge_sweep",
            phase_status="unknown_garbage",
            duration_ms=100,
        )
        handler.project(event, db)
        phase_rows = db.query("overnight_session_phases")
        assert phase_rows[0]["phase_status"] == "failed"


class TestOvernightSessionCompleteProjection:
    def test_project_complete_updates_terminal_status(self) -> None:
        db = InmemoryDatabaseAdapter()
        # Seed in-progress row
        SESSION_START_HANDLER.project(
            ModelOvernightSessionStartEvent(
                correlation_id="sess-020", phase="build_loop_orchestrator"
            ),
            db,
        )
        handler = HandlerProjectionOvernightSessionComplete()
        event = ModelOvernightSessionCompleteEvent(
            correlation_id="sess-020",
            session_status="completed",
            phases_run=["nightly_loop_controller", "build_loop_orchestrator"],
            phases_failed=[],
            phases_skipped=["ci_watch"],
            accumulated_cost_usd=0.15,
            completed_at="2026-04-12T04:00:00Z",
        )
        result = handler.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("overnight_sessions")
        assert rows[0]["session_status"] == "completed"
        assert rows[0]["phases_skipped"] == ["ci_watch"]
        assert rows[0]["accumulated_cost_usd"] == 0.15

    def test_complete_without_prior_start_creates_row(self) -> None:
        """Out-of-order: session-completed arrives before any phase-start."""
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightSessionComplete()
        event = ModelOvernightSessionCompleteEvent(
            correlation_id="sess-021",
            session_status="partial",
            phases_run=["build_loop_orchestrator"],
            phases_failed=["merge_sweep"],
            phases_skipped=[],
        )
        handler.project(event, db)
        rows = db.query("overnight_sessions")
        assert len(rows) == 1
        assert rows[0]["session_status"] == "partial"

    def test_full_session_lifecycle(self) -> None:
        """Full lifecycle: start → 2 phase-ends → complete."""
        db = InmemoryDatabaseAdapter()
        phase_handler = HandlerProjectionOvernightPhaseEnd()
        complete_handler = HandlerProjectionOvernightSessionComplete()

        SESSION_START_HANDLER.project(
            ModelOvernightSessionStartEvent(
                correlation_id="sess-030", phase="nightly_loop_controller"
            ),
            db,
        )

        phase_handler.project(
            ModelOvernightPhaseEndEvent(
                correlation_id="sess-030",
                phase="nightly_loop_controller",
                phase_status="success",
                duration_ms=1200,
            ),
            db,
        )
        phase_handler.project(
            ModelOvernightPhaseEndEvent(
                correlation_id="sess-030",
                phase="build_loop_orchestrator",
                phase_status="success",
                duration_ms=45000,
            ),
            db,
        )

        complete_handler.project(
            ModelOvernightSessionCompleteEvent(
                correlation_id="sess-030",
                session_status="completed",
                phases_run=["nightly_loop_controller", "build_loop_orchestrator"],
                phases_failed=[],
                phases_skipped=["ci_watch", "merge_sweep"],
            ),
            db,
        )

        session_rows = db.query("overnight_sessions")
        assert len(session_rows) == 1
        assert session_rows[0]["session_status"] == "completed"

        phase_rows = db.query("overnight_session_phases")
        assert len(phase_rows) == 2
        phase_names = {r["phase_name"] for r in phase_rows}
        assert phase_names == {"nightly_loop_controller", "build_loop_orchestrator"}


class TestOvernightProjectionPhaseNormalized:
    def test_overnight_session_phases_normalized_not_jsonb(self) -> None:
        """OMN-8455 TDD requirement: phases stored in separate table, not JSONB."""
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionOvernightPhaseEnd()
        for phase in [
            "nightly_loop_controller",
            "build_loop_orchestrator",
            "platform_readiness",
        ]:
            handler.project(
                ModelOvernightPhaseEndEvent(
                    correlation_id="sess-040",
                    phase=phase,
                    phase_status="success",
                    duration_ms=1000,
                ),
                db,
            )
        # Phases must be in separate table, not embedded in overnight_sessions row
        session_rows = db.query("overnight_sessions")
        assert len(session_rows) == 1
        # No JSONB phase_results field on the session row
        assert "phase_results" not in session_rows[0]
        assert "phases_json" not in session_rows[0]

        # Phases are queryable individually from separate table
        phase_rows = db.query("overnight_session_phases")
        assert len(phase_rows) == 3


class TestOvernightProjectionContractWiring:
    def _contract_path(self) -> str:
        import pathlib

        return str(
            pathlib.Path(__file__).parent.parent
            / "src/omnimarket/nodes/node_projection_overnight/contract.yaml"
        )

    def test_contract_subscribe_topics(self) -> None:
        with open(self._contract_path()) as f:
            contract = yaml.safe_load(f)
        subscribe = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omnimarket.overnight.phase-start.v1" in subscribe
        assert "onex.evt.omnimarket.overnight.phase-completed.v1" in subscribe
        assert "onex.evt.omnimarket.overnight.session-completed.v1" in subscribe

    def test_contract_publish_topics(self) -> None:
        with open(self._contract_path()) as f:
            contract = yaml.safe_load(f)
        assert len(contract["event_bus"]["publish_topics"]) >= 1
