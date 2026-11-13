# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule gate: when a scheduled job is due, and the run event it emits (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleVerdictOverlay,
)

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def _declaration(run_interval_seconds: int = 60) -> ModelScheduleDeclaration:
    return ModelScheduleDeclaration(
        job_id="job_a", run_interval_seconds=run_interval_seconds, grace_seconds=0
    )


def test_due_on_first_call() -> None:
    gate = ScheduleGate("node_a", declaration=_declaration())
    assert gate.due(T0) is True


def test_not_due_inside_the_interval() -> None:
    gate = ScheduleGate("node_a", declaration=_declaration(run_interval_seconds=60))
    gate.mark(T0)
    assert gate.due(T0 + timedelta(seconds=30)) is False


def test_due_at_exactly_the_interval() -> None:
    gate = ScheduleGate("node_a", declaration=_declaration(run_interval_seconds=60))
    gate.mark(T0)
    assert gate.due(T0 + timedelta(seconds=60)) is True


def test_a_skipped_tick_does_not_move_the_last_run() -> None:
    gate = ScheduleGate("node_a", declaration=_declaration(run_interval_seconds=60))
    gate.mark(T0)
    # A tick that is not due is skipped; mark is not called, so the last run stays.
    assert gate.due(T0 + timedelta(seconds=30)) is False
    assert gate.due(T0 + timedelta(seconds=60)) is True


def test_missing_overlay_raises_node_overlay_not_found() -> None:
    gate = ScheduleGate("node_missing", environ={})
    with pytest.raises(NodeOverlayNotFoundError):
        gate.declaration()


def test_a_zero_interval_declaration_is_refused_by_validation() -> None:
    with pytest.raises(ValidationError):
        _declaration(run_interval_seconds=0)


def test_run_event_carries_job_id_and_node_name() -> None:
    gate = ScheduleGate("node_a", declaration=_declaration())
    event = gate.run_event(
        tick_id="tick-1",
        scheduled_for=T0,
        fired_at=T0 + timedelta(seconds=1),
        outcome="completed",
    )
    assert event.job_id == "job_a"
    assert event.node_name == "node_a"


def test_loads_a_verdict_overlay(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "node_x"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "job_x",
                "run_interval_seconds": 60,
                "grace_seconds": 0,
                "job_overlay_ids": ["node_a", "node_b"],
            }
        ),
        encoding="utf-8",
    )
    gate = ScheduleGate(
        "node_x",
        model=ModelScheduleVerdictOverlay,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    decl = gate.declaration()
    assert isinstance(decl, ModelScheduleVerdictOverlay)
    assert decl.job_overlay_ids == ("node_a", "node_b")


def test_rejects_an_empty_job_overlay_ids() -> None:
    with pytest.raises(ValidationError):
        ModelScheduleVerdictOverlay(
            job_id="job_x",
            run_interval_seconds=60,
            grace_seconds=0,
            job_overlay_ids=(),
        )
