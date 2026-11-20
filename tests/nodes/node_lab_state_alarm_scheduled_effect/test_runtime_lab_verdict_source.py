# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab state alarm's scheduled run on the real overlays binds a lab verdict source (OMN-25461).

``fixtures_real_overlays`` holds the three overlay documents the dev runtime loads for this job, copied from the overlays
repository's main branch: the schedule overlay (``lab_state_alarm``), the alarm overlay and the floor's lab-state overlay.
Two things differ from the copy: the deploy agent's address, which the hygiene gate refuses as a literal, reads a
reserved name, and the runner_plan rows (host names the gate refuses) are left out because this source never reads them. The tests answer the HTTP call themselves. Nothing is injected into the handler but the clock-free tick and the deploy agent's HTTP answer, so a run goes the way the
runtime's default constructor builds it.
"""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.handlers.handler_lab_state_alarm_scheduled import (
    HandlerLabStateAlarmScheduled,
)

pytestmark = pytest.mark.unit

REAL_OVERLAYS = Path(__file__).parent / "fixtures_real_overlays"
T0 = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)
UNBOUND = "hosts,runner_groups,disk,queue_age,deploy_lag,broker_lag,overlay_store"


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


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def agent_answers(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | None) -> None:
    def urlopen(url: str, timeout: float | None = None) -> _Response:
        if body is None:
            raise OSError("connection refused")
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def real_environ() -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(REAL_OVERLAYS)}


def test_real_overlays_a_healthy_agent_is_a_completed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_answers(
        monkeypatch,
        {
            "state": "idle",
            "last_result": {
                "correlation_id": "c-1",
                "status": "success",
                "completed_at": "2026-11-17T17:50:00Z",
            },
        },
    )
    event = HandlerLabStateAlarmScheduled(environ=real_environ()).handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.outcome) == ("lab_state_alarm", "completed")
    assert event.detail.startswith("raised=0 escalated=0 cleared=0 undelivered=0")
    assert event.detail.endswith(f"read=1 unbound={UNBOUND}")


def test_real_overlays_an_unreadable_agent_is_an_alarm_not_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_answers(monkeypatch, None)
    event = HandlerLabStateAlarmScheduled(environ=real_environ()).handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert event.detail.startswith("raised=1 ")


def test_error_a_missing_lab_state_overlay_fails_by_name(tmp_path: Path) -> None:
    for name in (
        "node_lab_state_alarm_scheduled_effect",
        "node_lab_state_alarm_effect",
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "overlay.yaml").write_bytes(
            (REAL_OVERLAYS / name / "overlay.yaml").read_bytes()
        )
    event = HandlerLabStateAlarmScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    ).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith("the lab verdict source could not be bound: ")
    assert "node_floor_simple_effect" in event.detail
