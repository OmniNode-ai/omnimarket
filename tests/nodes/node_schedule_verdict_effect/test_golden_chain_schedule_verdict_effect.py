# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the schedule verdict effect (OMN-19998): the gate, the verdict, the refusals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleRunEvent,
    ModelScheduleVerdictOverlay,
)
from omnimarket.nodes.node_schedule_verdict_effect.handlers.handler_schedule_verdict_scheduled import (
    HandlerScheduleVerdictScheduled,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def _verdict_overlay(
    job_overlay_ids: tuple[str, ...] = ("job_a",),
    interval: int = 60,
    grace: int = 10,
) -> ModelScheduleVerdictOverlay:
    return ModelScheduleVerdictOverlay(
        job_id="node_schedule_verdict_effect",
        run_interval_seconds=interval,
        grace_seconds=grace,
        job_overlay_ids=job_overlay_ids,
    )


def _job_overlay(
    job_id: str, interval: int = 60, grace: int = 10
) -> ModelScheduleDeclaration:
    return ModelScheduleDeclaration(
        job_id=job_id, run_interval_seconds=interval, grace_seconds=grace
    )


def _run_event(
    job_id: str,
    fired_at: datetime,
    *,
    outcome: Literal["completed", "failed"] = "completed",
) -> ModelScheduleRunEvent:
    return ModelScheduleRunEvent(
        job_id=job_id,
        node_name=f"node_{job_id}",
        tick_id=f"tick-{job_id}",
        scheduled_for=fired_at,
        fired_at=fired_at,
        outcome=outcome,
    )


def _store_with_fresh_run(job_id: str = "job_a") -> InMemoryScheduleRunStore:
    store = InMemoryScheduleRunStore()
    store.apply(_run_event(job_id, T0))
    return store


def test_golden_a_due_tick_with_a_fresh_completed_run_is_not_red(
    tmp_path: Path,
) -> None:
    overlay_dir = tmp_path / "job_a"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "job_a",
                "run_interval_seconds": 60,
                "grace_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    store = _store_with_fresh_run()
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    result = h.handle(tick(T0 + timedelta(seconds=60)))
    assert result is not None
    assert result.red is False
    assert result.verdicts[0].state == "FIRING"


def test_golden_a_tick_inside_the_interval_returns_none() -> None:
    store = _store_with_fresh_run()
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store)
    h.handle(tick(T0))
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None


def test_error_empty_store_past_interval_plus_grace_is_never_fired_and_red(
    tmp_path: Path,
) -> None:
    overlay_dir = tmp_path / "job_a"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "job_a",
                "run_interval_seconds": 60,
                "grace_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    store = InMemoryScheduleRunStore()
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    h.handle(tick(T0))
    result = h.handle(tick(T0 + timedelta(seconds=80)))
    assert result is not None
    assert result.red is True
    assert result.verdicts[0].state == "NEVER_FIRED"


def test_error_no_overlay_refuses_naming_the_overlay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerScheduleVerdictScheduled(environ={})
    with caplog.at_level("ERROR"):
        assert h.handle(tick(T0)) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "node_schedule_verdict_effect" in errors[0].message


def test_error_a_missing_declared_job_overlay_returns_none(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "node_schedule_verdict_effect"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "node_schedule_verdict_effect",
                "run_interval_seconds": 60,
                "grace_seconds": 10,
                "job_overlay_ids": ["job_missing"],
            }
        ),
        encoding="utf-8",
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    store = InMemoryScheduleRunStore()
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(job_overlay_ids=("job_missing",)),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    assert h.handle(tick(T0)) is None
