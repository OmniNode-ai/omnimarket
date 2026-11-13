# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the lab state alarm on the runtime schedule (OMN-19998)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.floor_17a import ModelLabSourceVerdict
from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.handlers.handler_lab_state_alarm_scheduled import (
    HandlerLabStateAlarmScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_lab_state_alarm_scheduled_effect"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)


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


def verdict(state: str) -> ModelLabSourceVerdict:
    return ModelLabSourceVerdict.model_validate(
        {
            "id": "src_a",
            "title": "Source a",
            "kind": "hosts",
            "state": state,
            "probed_at": T0,
            "published_at": T0,
            "age_s": 0,
            "max_age_s": 900,
            "reason": "" if state == "WORKING" else "test runner offline",
            "summary": "",
            "subjects": (),
        }
    )


class _Source:
    def __init__(self, state: str) -> None:
        self._state = state

    def read(self, now: datetime) -> tuple[ModelLabSourceVerdict, ...]:
        return (verdict(self._state),)


def environ(tmp_path: Path, *, with_alarm: bool = True) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "lab_state_alarm",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "alarm_overlay_id": "node_lab_state_alarm_effect",
            }
        ),
        encoding="utf-8",
    )
    if with_alarm:
        (tmp_path / "node_lab_state_alarm_effect").mkdir()
        (tmp_path / "node_lab_state_alarm_effect" / "overlay.yaml").write_text(
            yaml.safe_dump(
                {
                    "readback_s": 300,
                    "renotify_s": 3600,
                    "heartbeat_max_age_s": 900,
                    "channels": [
                        {"id": "primary", "kind": "in_memory", "role": "primary"},
                        {"id": "second", "kind": "in_memory", "role": "second"},
                    ],
                }
            ),
            encoding="utf-8",
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_golden_a_working_lab_is_a_completed_run_with_nothing_raised(
    tmp_path: Path,
) -> None:
    h = HandlerLabStateAlarmScheduled(
        environ=environ(tmp_path), source=_Source("WORKING")
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "lab_state_alarm",
        NODE,
        "completed",
    )
    assert event.detail.startswith("raised=0 ")


def test_golden_an_alarm_is_raised_once_and_the_state_is_kept(tmp_path: Path) -> None:
    h = HandlerLabStateAlarmScheduled(
        environ=environ(tmp_path), source=_Source("NOT WORKING")
    )
    first = h.handle(tick(T0))
    second = h.handle(tick(T0 + timedelta(seconds=300)))
    assert first is not None
    assert second is not None
    assert first.detail.startswith("raised=1 ")
    assert second.detail.startswith("raised=0 ")


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerLabStateAlarmScheduled(
        environ=environ(tmp_path), source=_Source("WORKING")
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None


def test_error_the_unbound_source_is_a_failed_run_by_name(tmp_path: Path) -> None:
    event = HandlerLabStateAlarmScheduled(environ=environ(tmp_path)).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "no lab verdict source is bound in the runtime"


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerLabStateAlarmScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_error_a_missing_alarm_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerLabStateAlarmScheduled(
        environ=environ(tmp_path, with_alarm=False), source=_Source("WORKING")
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "node_lab_state_alarm_effect" in event.detail


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerLabStateAlarmScheduled(environ=environ(tmp_path)).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("lab_state_alarm")
    assert held is not None
    assert held.last_outcome == "failed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]
