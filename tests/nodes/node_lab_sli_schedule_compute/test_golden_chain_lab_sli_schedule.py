# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the schedule turns a boundary tick into the collect request, and nothing else."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.lab_sli.model_lab_sli_collect import (
    ModelLabSliCollectRequest,
)
from omnimarket.models.model_runtime_tick_schedule import ModelRuntimeTickScheduleConfig
from omnimarket.nodes.node_lab_sli_schedule_compute import (
    HandlerLabSliSchedule,
    ModelLabSliScheduleOverlay,
)

_INTERVAL = 300
_OFFSET = 0
_HOST = "host-a"
_STATE = "/state/pr.json"
_DISK = ("/",)


def _overlay() -> ModelLabSliScheduleOverlay:
    return ModelLabSliScheduleOverlay(
        schedule=ModelRuntimeTickScheduleConfig(
            workflow="lab_sli", interval_seconds=_INTERVAL, offset_seconds=_OFFSET
        ),
        collector_host=_HOST,
        pr_watcher_state_path=_STATE,
        disk_paths=_DISK,
    )


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        scheduled_at=now,
        tick_interval_ms=1000,
        tick_id=uuid4(),
        sequence_number=1,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
    )


def _boundary_now() -> dt.datetime:
    return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def test_boundary_tick_returns_overlay_values() -> None:
    handler = HandlerLabSliSchedule(overlay=_overlay())
    result = handler.handle(_tick(_boundary_now()))
    assert result is not None
    assert result.collector_host == _HOST
    assert result.pr_watcher_state_path == _STATE
    assert result.disk_paths == _DISK
    assert result.interval_start == _boundary_now() - dt.timedelta(seconds=_INTERVAL)
    assert result.now == _boundary_now()


def test_mid_window_tick_returns_none() -> None:
    handler = HandlerLabSliSchedule(overlay=_overlay())
    assert handler.handle(_tick(_boundary_now() + dt.timedelta(seconds=150))) is None


def test_same_boundary_tick_yields_equal_requests() -> None:
    handler = HandlerLabSliSchedule(overlay=_overlay())
    first = handler.handle(_tick(_boundary_now()))
    second = handler.handle(_tick(_boundary_now()))
    assert first is not None
    assert first == second


def test_result_validates_as_collect_input() -> None:
    handler = HandlerLabSliSchedule(overlay=_overlay())
    result = handler.handle(_tick(_boundary_now()))
    assert result is not None
    assert ModelLabSliCollectRequest.model_validate(result.model_dump()) == result


def test_pack_gate_failing_control(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "lab_sli_pack"
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "overlay.yaml").write_text("status: disabled\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    disabled = HandlerLabSliSchedule(
        overlay=_overlay(), environ={OVERLAY_ROOTS_ENV: str(tmp_path)}
    )
    assert disabled.handle(_tick(_boundary_now())) is None

    enabled_root = tmp_path / "enabled"
    (enabled_root / "lab_sli_pack").mkdir(parents=True)
    (enabled_root / "lab_sli_pack" / "overlay.yaml").write_text(
        "status: enabled\n", encoding="utf-8"
    )
    enabled = HandlerLabSliSchedule(
        overlay=_overlay(), environ={OVERLAY_ROOTS_ENV: str(enabled_root)}
    )
    assert enabled.handle(_tick(_boundary_now())) is not None
