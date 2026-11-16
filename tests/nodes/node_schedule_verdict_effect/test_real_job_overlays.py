# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real job overlays: the extra keys that ModelScheduleDeclaration forbids (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleBlock,
    ModelScheduleDeclaration,
    ModelScheduleRunEvent,
    ModelScheduleVerdictOverlay,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.models.model_floor_fill_schedule_overlay import (
    ModelFloorFillScheduleOverlay,
)
from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.models.model_lab_state_alarm_schedule_overlay import (
    ModelLabStateAlarmScheduleOverlay,
)
from omnimarket.nodes.node_schedule_verdict_effect.handlers.handler_schedule_verdict_scheduled import (
    HandlerScheduleVerdictScheduled,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)

LAB_STATE_ALARM_OVERLAY = """job_id: lab_state_alarm
run_interval_seconds: 300
grace_seconds: 300
alarm_overlay_id: node_lab_state_alarm_effect
"""

FLOOR_FILL_OVERLAY = """job_id: floor_fill
run_interval_seconds: 300
grace_seconds: 600
fill_overlay_id: node_floor_fill_orchestrator
profile: inmemory
"""


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


def _run_event(
    job_id: str,
    fired_at: datetime,
) -> ModelScheduleRunEvent:
    return ModelScheduleRunEvent(
        job_id=job_id,
        node_name=f"node_{job_id}",
        tick_id=f"tick-{job_id}",
        scheduled_for=fired_at,
        fired_at=fired_at,
        outcome="completed",
    )


def _write_overlay(tmp_path: Path, overlay_id: str, text: str) -> None:
    overlay_dir = tmp_path / overlay_id
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(text, encoding="utf-8")


def test_golden_real_job_overlays_with_extra_keys_are_judged(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path, "node_lab_state_alarm_scheduled_effect", LAB_STATE_ALARM_OVERLAY
    )
    _write_overlay(tmp_path, "node_floor_fill_scheduled_effect", FLOOR_FILL_OVERLAY)
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    store = InMemoryScheduleRunStore()
    store.apply(_run_event("lab_state_alarm", T0))
    store.apply(_run_event("floor_fill", T0))
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(
            job_overlay_ids=(
                "node_lab_state_alarm_scheduled_effect",
                "node_floor_fill_scheduled_effect",
            )
        ),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    result = h.handle(tick(T0 + timedelta(seconds=60)))
    assert result is not None
    assert result.red is False
    assert len(result.verdicts) == 2
    assert all(v.state == "FIRING" for v in result.verdicts)


def test_control_real_overlay_text_raises_with_declaration_but_handler_judges(
    tmp_path: Path,
) -> None:
    _write_overlay(
        tmp_path, "node_lab_state_alarm_scheduled_effect", LAB_STATE_ALARM_OVERLAY
    )
    _write_overlay(tmp_path, "node_floor_fill_scheduled_effect", FLOOR_FILL_OVERLAY)
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    with pytest.raises(NodeOverlayError):
        load_structured_overlay(
            "node_floor_fill_scheduled_effect",
            ModelScheduleDeclaration,
            environ,
        )
    store = InMemoryScheduleRunStore()
    store.apply(_run_event("floor_fill", T0))
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(
            job_overlay_ids=("node_floor_fill_scheduled_effect",)
        ),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    result = h.handle(tick(T0 + timedelta(seconds=60)))
    assert result is not None
    assert result.verdicts[0].state == "FIRING"


def test_error_chain_one_missing_and_one_invalid_reads_unreadable(
    tmp_path: Path,
) -> None:
    _write_overlay(
        tmp_path, "node_lab_state_alarm_scheduled_effect", LAB_STATE_ALARM_OVERLAY
    )
    _write_overlay(
        tmp_path,
        "node_floor_fill_scheduled_effect",
        "job_id: floor_fill\nrun_interval_seconds: 0\ngrace_seconds: 600\n",
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    store = InMemoryScheduleRunStore()
    store.apply(_run_event("lab_state_alarm", T0))
    gate = ScheduleGate(
        "node_schedule_verdict_effect",
        declaration=_verdict_overlay(
            job_overlay_ids=(
                "node_lab_state_alarm_scheduled_effect",
                "node_floor_fill_scheduled_effect",
                "node_missing_job",
            )
        ),
    )
    h = HandlerScheduleVerdictScheduled(gate=gate, store=store, environ=environ)
    result = h.handle(tick(T0 + timedelta(seconds=60)))
    assert result is not None
    assert result.red is True
    by_job = {v.job_id: v for v in result.verdicts}
    assert by_job["lab_state_alarm"].state == "FIRING"
    assert by_job["node_floor_fill_scheduled_effect"].state == "UNREADABLE"
    assert by_job["node_missing_job"].state == "UNREADABLE"


@pytest.mark.parametrize(
    ("text", "job_model"),
    [
        (LAB_STATE_ALARM_OVERLAY, ModelLabStateAlarmScheduleOverlay),
        (FLOOR_FILL_OVERLAY, ModelFloorFillScheduleOverlay),
    ],
)
def test_seam_the_job_node_model_and_the_verdict_schedule_block_read_one_schedule(
    text: str, job_model: type[ModelScheduleDeclaration]
) -> None:
    document = yaml.safe_load(text)
    job = job_model.model_validate(document)
    block = ModelScheduleBlock.model_validate(document)
    assert (job.job_id, job.run_interval_seconds, job.grace_seconds) == (
        block.job_id,
        block.run_interval_seconds,
        block.grace_seconds,
    )
