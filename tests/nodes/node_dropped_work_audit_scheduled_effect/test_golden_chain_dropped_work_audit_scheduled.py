# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the dropped-work audit on the runtime schedule (OMN-26205): a due tick runs the audit, a refused one fails by name."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableSet
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.dropped_work_audit_scheduled_tick import (
    DroppedWorkAuditSourceError,
    ModelDroppedWorkAuditTickOutcome,
)
from omnimarket.models.dropped_work_audit import ModelDroppedWorkAuditScheduleOverlay
from omnimarket.nodes.node_dropped_work_audit_scheduled_effect.handlers.handler_dropped_work_audit_scheduled import (
    HandlerDroppedWorkAuditScheduled,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_dropped_work_audit_scheduled_effect"
EXAMPLE = REPO / f"src/omnimarket/nodes/{NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 20, 20, 20, tzinfo=UTC)


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


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _tick_returning(
    outcome: ModelDroppedWorkAuditTickOutcome,
) -> Callable[..., ModelDroppedWorkAuditTickOutcome]:
    def run(
        *,
        schedule: ModelDroppedWorkAuditScheduleOverlay,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        evented: MutableSet[str],
        publisher: object,
    ) -> ModelDroppedWorkAuditTickOutcome:
        return outcome

    return run


def test_golden_a_due_tick_runs_the_audit_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    done = ModelDroppedWorkAuditTickOutcome(
        False, "items=6 unowned=6 events=6 alarms=6"
    )
    handler = HandlerDroppedWorkAuditScheduled(
        environ=environ(tmp_path), tick=_tick_returning(done), clock=lambda: T0
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "dropped_work_audit",
        NODE,
        "completed",
    )
    assert event.detail == done.detail
    assert (
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["run_interval_seconds"]
        == 3600
    )


def test_error_an_unbound_source_is_a_failed_run_event(tmp_path: Path) -> None:
    def unbound(**_: object) -> ModelDroppedWorkAuditTickOutcome:
        raise DroppedWorkAuditSourceError("ONEX_PR_WATCHER_STATE is not set")

    handler = HandlerDroppedWorkAuditScheduled(
        environ=environ(tmp_path), tick=unbound, clock=lambda: T0
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "ONEX_PR_WATCHER_STATE" in event.detail
