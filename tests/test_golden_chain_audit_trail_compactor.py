# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_audit_trail_compactor.

All tests use injectable stub adapters so no filesystem I/O occurs.
Verifies failure mode aggregation, recurring ticket detection, stall heatmap,
empty input, dry-run, and rollup content generation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_audit_trail_compactor.handlers.handler_audit_trail_compactor import (
    AuditReader,
    HandlerAuditTrailCompactor,
    RollupWriter,
)
from omnimarket.nodes.node_audit_trail_compactor.models.model_audit_trail_input import (
    ModelAuditEntry,
    ModelCompactorCommand,
)


def _make_entry(
    entry_type: str = "friction",
    ticket_id: str | None = None,
    agent_id: str | None = None,
    description: str = "",
    recorded_at: str | None = None,
) -> ModelAuditEntry:
    return ModelAuditEntry(
        entry_type=entry_type,
        ticket_id=ticket_id,
        agent_id=agent_id,
        description=description,
        recorded_at=recorded_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


def _stub_reader(entries: list[ModelAuditEntry] | None = None) -> MagicMock:
    reader = MagicMock(spec=AuditReader)
    reader.read_friction_entries.return_value = entries or []
    reader.read_dispatch_entries.return_value = []
    return reader


def _stub_writer() -> MagicMock:
    writer = MagicMock(spec=RollupWriter)
    writer.write_rollup.return_value = "docs/audit-rollups/audit-rollup-2026-04-21.md"
    return writer


@pytest.mark.unit
class TestAuditTrailCompactorGoldenChain:
    def test_empty_input(self) -> None:
        reader = _stub_reader([])
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert result.total_entries == 0
        assert result.failure_modes == []
        assert result.recurring_tickets == []
        assert result.stall_heatmap == []

    def test_failure_modes_aggregation(self) -> None:
        entries = [
            _make_entry(description="CI timeout on pytest"),
            _make_entry(description="CI timeout on pytest"),
            _make_entry(description="import resolution failure"),
            _make_entry(description="CI timeout on pytest"),
        ]
        reader = _stub_reader(entries)
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert len(result.failure_modes) == 2
        assert result.failure_modes[0].description == "CI timeout on pytest"
        assert result.failure_modes[0].count == 3
        assert result.failure_modes[1].description == "import resolution failure"
        assert result.failure_modes[1].count == 1

    def test_failure_modes_with_ticket_ids(self) -> None:
        entries = [
            _make_entry(ticket_id="OMN-100", description="build failed"),
            _make_entry(ticket_id="OMN-200", description="build failed"),
            _make_entry(ticket_id="OMN-100", description="build failed"),
        ]
        reader = _stub_reader(entries)
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert result.failure_modes[0].count == 3
        assert "OMN-100" in result.failure_modes[0].ticket_ids
        assert "OMN-200" in result.failure_modes[0].ticket_ids

    def test_recurring_tickets(self) -> None:
        entries = [
            _make_entry(ticket_id="OMN-100", description="error A"),
            _make_entry(ticket_id="OMN-100", description="error B"),
            _make_entry(ticket_id="OMN-200", description="error C"),
        ]
        reader = _stub_reader(entries)
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert len(result.recurring_tickets) == 1
        assert result.recurring_tickets[0].ticket_id == "OMN-100"
        assert result.recurring_tickets[0].failure_count == 2

    def test_stall_heatmap(self) -> None:
        entries = [
            _make_entry(
                agent_id="merge-sweep",
                ticket_id="OMN-100",
                description="stall detected",
            ),
            _make_entry(
                agent_id="merge-sweep",
                ticket_id="OMN-200",
                description="stall detected",
            ),
            _make_entry(
                agent_id="dispatch-engine",
                ticket_id="OMN-300",
                description="agent_stall",
            ),
            _make_entry(
                agent_id="merge-sweep",
                ticket_id="OMN-400",
                description="merge-sweep stall timeout",
            ),
        ]
        reader = _stub_reader(entries)
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert len(result.stall_heatmap) == 2
        merge_sweep = next(
            s for s in result.stall_heatmap if s.agent_id == "merge-sweep"
        )
        assert merge_sweep.stall_count == 3
        assert "OMN-100" in merge_sweep.affected_tickets
        assert "OMN-200" in merge_sweep.affected_tickets
        assert "OMN-400" in merge_sweep.affected_tickets

    def test_dispatch_entries_filtered_by_lookback(self) -> None:
        now = datetime.now(UTC)
        old_entry = _make_entry(
            description="old error",
            recorded_at=(now - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        )
        new_entry = _make_entry(
            description="new error",
            recorded_at=now.isoformat().replace("+00:00", "Z"),
        )
        reader = _stub_reader([old_entry, new_entry])
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand(lookback_days=7))

        assert result.total_entries == 1
        assert result.failure_modes[0].description == "new error"

    def test_rollup_written_when_writer_provided(self) -> None:
        entries = [_make_entry(description="some failure")]
        reader = _stub_reader(entries)
        writer = _stub_writer()
        handler = HandlerAuditTrailCompactor(
            audit_reader=reader,
            rollup_writer=writer,
        )
        result = handler.handle(ModelCompactorCommand())

        assert result.rollup_path == "docs/audit-rollups/audit-rollup-2026-04-21.md"
        writer.write_rollup.assert_called_once()

    def test_dry_run_no_rollup_file(self) -> None:
        entries = [_make_entry(description="some failure")]
        reader = _stub_reader(entries)
        writer = _stub_writer()
        handler = HandlerAuditTrailCompactor(
            audit_reader=reader,
            rollup_writer=writer,
        )
        result = handler.handle(ModelCompactorCommand(dry_run=True))

        assert result.dry_run is True
        writer.write_rollup.assert_called_once()
        call_kwargs = writer.write_rollup.call_args
        assert call_kwargs[1]["dry_run"] is True
        assert "some failure" in call_kwargs[0][0]

    def test_success_entries_excluded_from_failures(self) -> None:
        entries = [
            _make_entry(entry_type="dispatch_success", description=""),
            _make_entry(entry_type="dispatch_failure", description="build error"),
        ]
        reader = _stub_reader(entries)
        handler = HandlerAuditTrailCompactor(audit_reader=reader)
        result = handler.handle(ModelCompactorCommand())

        assert result.total_entries == 2
        assert len(result.failure_modes) == 1
        assert result.failure_modes[0].description == "build error"
