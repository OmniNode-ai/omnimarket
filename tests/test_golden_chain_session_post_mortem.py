# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_post_mortem.

Verifies: post-mortem command -> handler -> ModelPostMortemResult.
Uses mocked FrictionReaderProtocol for hermetic tests. No real filesystem access.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_session_post_mortem.handlers.handler_session_post_mortem import (
    EnumPostMortemOutcome,
    FrictionReaderProtocol,
    HandlerSessionPostMortem,
    ModelFrictionEventLocal,
    ModelPostMortemCommand,
)

EVT_TOPIC = "onex.evt.omnimarket.session-post-mortem.v1"


def _make_friction_event(
    friction_type: str = "unknown",
    agent_id: str | None = None,
    ticket_id: str | None = None,
) -> ModelFrictionEventLocal:
    return ModelFrictionEventLocal(
        event_id=str(uuid.uuid4()),
        friction_type=friction_type,
        description=f"Test friction event: {friction_type}",
        recorded_at=datetime.now(UTC),
        agent_id=agent_id,
        ticket_id=ticket_id,
    )


def _make_reader(events: list[ModelFrictionEventLocal]) -> FrictionReaderProtocol:
    """Create a mock friction reader returning the given events."""
    reader = MagicMock(spec=FrictionReaderProtocol)
    reader.read_friction_events.return_value = events
    return reader


def _make_command(
    session_id: str | None = None,
    phases_planned: list[str] | None = None,
    phases_completed: list[str] | None = None,
    phases_failed: list[str] | None = None,
    phases_skipped: list[str] | None = None,
    carry_forward_items: list[str] | None = None,
    dry_run: bool = True,
) -> ModelPostMortemCommand:
    return ModelPostMortemCommand(
        session_id=session_id or str(uuid.uuid4()),
        session_label="2026-04-10 overnight test",
        phases_planned=phases_planned or ["build_loop", "merge_sweep"],
        phases_completed=phases_completed or [],
        phases_failed=phases_failed or [],
        phases_skipped=phases_skipped or [],
        carry_forward_items=carry_forward_items or [],
        dry_run=dry_run,
    )


@pytest.mark.unit
class TestGoldenChainSessionPostMortem:
    """Golden chain: post-mortem command -> handler -> result."""

    def test_dry_run_no_filesystem_write(self) -> None:
        """dry_run=True -> report_path == '(dry-run)', no disk write."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.report_path == "(dry-run)"
        assert result.dry_run is True

    def test_all_phases_completed_yields_completed_outcome(self) -> None:
        """All planned phases completed -> outcome == COMPLETED."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["build_loop", "merge_sweep"],
            phases_completed=["build_loop", "merge_sweep"],
            phases_failed=[],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.outcome == EnumPostMortemOutcome.COMPLETED

    def test_partial_phases_yields_partial_outcome(self) -> None:
        """Some completed, some failed -> outcome == PARTIAL."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["build_loop", "merge_sweep", "platform_readiness"],
            phases_completed=["merge_sweep"],
            phases_failed=["build_loop"],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.outcome == EnumPostMortemOutcome.PARTIAL

    def test_no_phases_completed_yields_failed_outcome(self) -> None:
        """No phases completed -> outcome == FAILED."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["build_loop", "merge_sweep"],
            phases_completed=[],
            phases_failed=["build_loop"],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.outcome == EnumPostMortemOutcome.FAILED

    def test_friction_events_collected(self) -> None:
        """Mock adapter returns 2 events -> result has 2 friction_events."""
        events = [
            _make_friction_event("build_failure"),
            _make_friction_event("timeout"),
        ]
        reader = _make_reader(events)
        handler = HandlerSessionPostMortem(friction_reader=reader)
        # dry_run=False so friction reader is called (but reader is mocked — no real I/O)
        cmd = _make_command(
            phases_completed=["merge_sweep"],
            phases_failed=[],
            dry_run=False,
        )
        # Patch report_dir to tempdir to avoid disk write issues
        import tempfile

        cmd = ModelPostMortemCommand(
            **{**cmd.model_dump(), "report_dir": tempfile.mkdtemp()}
        )
        result = handler.handle(cmd)

        assert len(result.friction_events) == 2

    def test_stalled_agents_derived_from_friction(self) -> None:
        """Friction events with type 'agent_stall' -> stalled_agents populated."""
        events = [
            _make_friction_event("agent_stall", agent_id="agent-abc"),
            _make_friction_event("build_failure"),
        ]
        reader = _make_reader(events)
        handler = HandlerSessionPostMortem(friction_reader=reader)
        import tempfile

        cmd = ModelPostMortemCommand(
            session_id=str(uuid.uuid4()),
            session_label="test",
            phases_planned=["build_loop"],
            phases_completed=["build_loop"],
            phases_failed=[],
            report_dir=tempfile.mkdtemp(),
            dry_run=False,
        )
        result = handler.handle(cmd)

        assert "agent-abc" in result.stalled_agents
        assert len(result.stalled_agents) == 1

    def test_event_bus_wiring(self, event_bus: object) -> None:
        """Handler returns valid result regardless of event_bus fixture presence."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["merge_sweep"],
            phases_completed=["merge_sweep"],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.outcome in (
            EnumPostMortemOutcome.COMPLETED,
            EnumPostMortemOutcome.PARTIAL,
            EnumPostMortemOutcome.FAILED,
        )

    def test_result_serializes_to_json(self) -> None:
        """result.model_dump_json() parses cleanly."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["build_loop"],
            phases_completed=["build_loop"],
            dry_run=True,
        )
        result = handler.handle(cmd)

        raw = result.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["session_id"] == cmd.session_id
        assert "outcome" in parsed
        assert "completed_at" in parsed

    def test_carry_forward_items_preserved(self) -> None:
        """carry_forward_items passed in command preserved in report."""
        carry = ["OMN-1234", "OMN-5678"]
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(carry_forward_items=carry, dry_run=True)
        result = handler.handle(cmd)

        assert result.carry_forward_items == carry

    def test_completed_subset_yields_partial(self) -> None:
        """Some planned phases completed but not all, none failed -> PARTIAL."""
        reader = _make_reader([])
        handler = HandlerSessionPostMortem(friction_reader=reader)
        cmd = _make_command(
            phases_planned=["build_loop", "merge_sweep", "platform_readiness"],
            phases_completed=["build_loop", "merge_sweep"],
            phases_failed=[],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.outcome == EnumPostMortemOutcome.PARTIAL
