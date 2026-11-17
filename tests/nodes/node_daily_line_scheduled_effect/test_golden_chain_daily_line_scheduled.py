# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the daily line on the runtime schedule (OMN-25163)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.model_daily_line import (
    DailyLinePartName,
    ModelDailyLinePart,
    ModelDailyLinePublication,
)
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
)
from omnimarket.nodes.node_daily_line_scheduled_effect.handlers.handler_daily_line_scheduled import (
    HandlerDailyLineScheduled,
)
from omnimarket.nodes.node_daily_line_scheduled_effect.models import (
    ModelDailyLineScheduleOverlay,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_daily_line_scheduled_effect"


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
        yaml.safe_dump(
            {
                "job_id": "daily_line",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _Runner:
    def __init__(
        self,
        line: str = "Daily line published.",
        unknown_parts: tuple[DailyLinePartName, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.calls = 0
        self._error = error
        self._publication = ModelDailyLinePublication(
            generated_at=T0,
            line=line,
            parts=tuple(
                ModelDailyLinePart(
                    name=name,
                    text="UNKNOWN" if name in unknown_parts else "read",
                    read=name not in unknown_parts,
                )
                for name in cast(
                    tuple[DailyLinePartName, ...], ("piece1", "j1", "claims")
                )
            ),
            unknown_parts=unknown_parts,
        )

    def run(self) -> ModelDailyLinePublication:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._publication


def test_golden_a_due_tick_runs_and_returns_a_completed_event(tmp_path: Path) -> None:
    runner = _Runner("Daily line published.\nmore")
    builds = 0

    def build() -> HandlerDailyLineRun:
        nonlocal builds
        builds += 1
        return cast(HandlerDailyLineRun, runner)

    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path), build_runner=build, clock=lambda: T0
    )
    request = tick(T0)
    event = h.handle(request)
    assert event is not None
    assert (event.job_id, event.node_name) == ("daily_line", NODE)
    assert (event.outcome, event.detail) == ("completed", "Daily line published.")
    assert event.tick_id == str(request.tick_id)
    assert event.scheduled_for == T0
    assert event.fired_at == T0
    assert builds == runner.calls == 1


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    runner = _Runner()
    builds = 0

    def build() -> HandlerDailyLineRun:
        nonlocal builds
        builds += 1
        return cast(HandlerDailyLineRun, runner)

    gate = ScheduleGate(
        NODE,
        declaration=ModelDailyLineScheduleOverlay(
            job_id="daily_line", run_interval_seconds=300, grace_seconds=60
        ),
    )
    h = HandlerDailyLineScheduled(gate=gate, build_runner=build)
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert builds == runner.calls == 1
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert builds == runner.calls == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def build() -> HandlerDailyLineRun:
        pytest.fail("a missing overlay must not build a runner")

    h = HandlerDailyLineScheduled(environ={}, build_runner=build)
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_build_key_error_is_a_failed_run_naming_omni_home(
    tmp_path: Path,
) -> None:
    def build() -> HandlerDailyLineRun:
        raise KeyError("OMNI_HOME")

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", "KeyError: 'OMNI_HOME'")
    assert h.handle(tick(T0 + timedelta(seconds=1))) is None


@pytest.mark.parametrize("error_type", [KeyError, RuntimeError, OSError, ValueError])
@pytest.mark.parametrize("during_build", [True, False])
def test_error_build_or_run_failures_are_named_and_truncated(
    tmp_path: Path, error_type: type[Exception], during_build: bool
) -> None:
    message = "the ledger append failed: " + "x" * 400
    error = error_type(message)
    runner = _Runner(error=error)

    def build() -> HandlerDailyLineRun:
        if during_build:
            raise error
        return cast(HandlerDailyLineRun, runner)

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == f"{error_type.__name__}: {error}"[:300]
    assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert runner.calls == (0 if during_build else 1)


def test_golden_unknown_parts_appear_after_the_truncated_first_line(
    tmp_path: Path,
) -> None:
    runner = _Runner("x" * 400 + "\nmore", unknown_parts=("piece1", "claims"))
    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "completed",
        "x" * 300 + " (unknown: piece1,claims)",
    )


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    event = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("daily_line")
    assert held is not None
    assert held.last_outcome == "completed"
    assert (held.fire_count, held.failure_count) == (1, 0)
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]
