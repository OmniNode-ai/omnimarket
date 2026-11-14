# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: missing, broken and invalid overlays are refused or raise by name."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.models.model_runtime_tick_schedule import ModelRuntimeTickScheduleConfig
from omnimarket.nodes.node_lab_sli_schedule_compute import (
    HandlerLabSliSchedule,
    ModelLabSliScheduleOverlay,
)

NODE = "node_lab_sli_schedule_compute"


def _tick() -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        scheduled_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        tick_interval_ms=1000,
        tick_id=uuid4(),
        sequence_number=1,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
    )


def test_missing_overlay_refused_once_across_two_ticks(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    handler = HandlerLabSliSchedule(environ={OVERLAY_ROOTS_ENV: str(tmp_path)})
    with caplog.at_level("ERROR"):
        assert handler.handle(_tick()) is None
        assert handler.handle(_tick()) is None
    error_lines = [
        record
        for record in caplog.records
        if record.levelname == "ERROR" and NODE in record.getMessage()
    ]
    assert len(error_lines) == 1


def test_broken_overlay_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_dir = tmp_path / NODE
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        "schedule: {workflow: lab_sli, interval_seconds: 300, offset_seconds: 0}\n"
        "collector_host: host-a\n"
        "pr_watcher_state_path: /state/pr.json\n"
        'disk_paths: ["/"]\n'
        "bogus: extra\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    handler = HandlerLabSliSchedule(environ={OVERLAY_ROOTS_ENV: str(tmp_path)})
    with pytest.raises(NodeOverlayError):
        handler.handle(_tick())


def test_pack_status_bogus_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "lab_sli_pack"
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "overlay.yaml").write_text("status: bogus\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    handler = HandlerLabSliSchedule(environ={OVERLAY_ROOTS_ENV: str(tmp_path)})
    with pytest.raises(NodeOverlayError):
        handler.handle(_tick())


def test_interval_below_minimum_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLabSliScheduleOverlay(
            schedule=ModelRuntimeTickScheduleConfig(
                workflow="lab_sli", interval_seconds=30, offset_seconds=0
            ),
            collector_host="host-a",
            pr_watcher_state_path="/state/pr.json",
            disk_paths=("/",),
        )
