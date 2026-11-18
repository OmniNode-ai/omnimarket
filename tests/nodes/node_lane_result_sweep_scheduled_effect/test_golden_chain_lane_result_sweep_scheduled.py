# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the lane result sweep on the runtime schedule (OMN-24262)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lane_result_sweep_scheduled_tick import (
    LedgerRouteError,
    ModelLaneResultSweepTickOutcome,
    run_lane_result_sweep_tick,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_lane_result_sweep_scheduled_effect.handlers.handler_lane_result_sweep_scheduled import (
    HandlerLaneResultSweepScheduled,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)
from tests.nodes.node_lane_result_sweep_effect.lane_result_sweep_support import (
    terminal,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_lane_result_sweep_scheduled_effect"
SWEEP_NODE = "node_lane_result_sweep_effect"
SWEEP_EXAMPLE = REPO / f"src/omnimarket/nodes/{SWEEP_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelLaneResultSweepTickOutcome(
    False, "verdict=WORKING receipts=1 rows_written=1 reasons=0"
)


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
                "job_id": "lane_result_sweep",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "window_seconds": 600,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def sweep_environ(tmp_path: Path) -> dict[str, str]:
    """The sweep's own overlay, from the checked-in example."""
    (tmp_path / SWEEP_NODE).mkdir(exist_ok=True)
    (tmp_path / SWEEP_NODE / "overlay.yaml").write_text(
        SWEEP_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingTick:
    """The injected tick: returns the fixed outcome and records the window it was given."""

    def __init__(self, outcome: ModelLaneResultSweepTickOutcome) -> None:
        self._outcome = outcome
        self.calls: list[int] = []

    def __call__(
        self,
        *,
        window_seconds: int,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelLaneResultSweepTickOutcome:
        self.calls.append(window_seconds)
        return self._outcome


def _raising(error: Exception) -> Any:
    def tick(
        *,
        window_seconds: int,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelLaneResultSweepTickOutcome:
        raise error

    return tick


def test_golden_a_due_tick_runs_the_sweep_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    recorded = _RecordingTick(DONE)
    h = HandlerLaneResultSweepScheduled(environ=environ(tmp_path), tick=recorded)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "lane_result_sweep",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail
    assert recorded.calls == [600]


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerLaneResultSweepScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None


def test_error_an_unreadable_sweep_overlay_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerLaneResultSweepScheduled(
        environ=environ(tmp_path),
        tick=_raising(NodeOverlayError("boom: the overlay is invalid")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the lane result sweep overlay could not be read",
    )


def test_error_an_unbound_ledger_route_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerLaneResultSweepScheduled(
        environ=environ(tmp_path),
        tick=_raising(LedgerRouteError("ONEX_LANE_RESULT_SWEEP_LEDGER is not set")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the ledger route of the lane result sweep is not bound in the runtime",
    )


def test_error_a_failed_run_outcome_carries_its_detail(tmp_path: Path) -> None:
    failed = ModelLaneResultSweepTickOutcome(
        True, "the lane result sweep failed: ledger-unreadable:down"
    )
    h = HandlerLaneResultSweepScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(failed)
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == failed.detail


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerLaneResultSweepScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerLaneResultSweepScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("lane_result_sweep")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


def test_real_chain_a_sweep_with_an_in_memory_ledger_is_not_failed(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([])
    outcome = run_lane_result_sweep_tick(
        window_seconds=600,
        environ=sweep_environ(tmp_path),
        clock=lambda: T0,
        ledger=ledger,
        appender=ledger,
    )
    assert outcome.failed is False
    assert ledger.lines != []


def test_control_a_row_outside_the_window_is_not_swept_and_one_inside_is(
    tmp_path: Path,
) -> None:
    """The failing control of the window: a tick that ignored window_seconds would sweep the old receipt on the narrow run."""
    ledger = InMemoryLedger(
        [terminal("lane-old", T0 - timedelta(seconds=120), "done", result="done")]
    )
    narrow = run_lane_result_sweep_tick(
        window_seconds=60,
        environ=sweep_environ(tmp_path),
        clock=lambda: T0,
        ledger=ledger,
        appender=ledger,
    )
    assert narrow.failed is False
    assert "receipts=0" in narrow.detail
    wide = run_lane_result_sweep_tick(
        window_seconds=600,
        environ=sweep_environ(tmp_path),
        clock=lambda: T0,
        ledger=ledger,
        appender=ledger,
    )
    assert wide.failed is False
    assert "receipts=1" in wide.detail
