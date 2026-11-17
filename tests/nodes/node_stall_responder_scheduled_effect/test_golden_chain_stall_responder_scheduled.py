# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the stall responder on the runtime schedule (OMN-25160).

The ledger rows are the stall responder's own replay fixture of 2026-11-07 (the throughput tick's THROUGHPUT STALL MSG rows).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled import (
    HandlerStallResponderScheduled,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.stall_responder_tick import (
    LedgerRouteError,
    ModelStallResponderTickOutcome,
    run_stall_responder_tick,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.models import (
    ModelStallResponderScheduleOverlay,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_stall_responder_scheduled_effect"
FIXTURE = (
    REPO
    / "tests/nodes/node_stall_responder_compute/fixtures/stall_msg_rows_2026-11-07.txt"
)
STALL_ROWS = tuple(FIXTURE.read_text(encoding="utf-8").splitlines())
T0 = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)
DONE = ModelStallResponderTickOutcome(False, "stall_rows=2 dispatches=3", {"k": "v"})


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


def overlay(window_seconds: int = 3600) -> ModelStallResponderScheduleOverlay:
    return ModelStallResponderScheduleOverlay(
        job_id="stall_responder",
        run_interval_seconds=600,
        grace_seconds=600,
        window_seconds=window_seconds,
        redispatch_after_hours=24,
        ledger_read=ModelCommandLedgerReadRoute(
            route="command",
            command_env="ONEX_LEDGER_BUS_READ_COMMAND",
            timeout_seconds=120,
            row_limit=20000,
        ),
    )


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "stall_responder",
                "run_interval_seconds": 600,
                "grace_seconds": 600,
                "window_seconds": 3600,
                "redispatch_after_hours": 24,
                "ledger_read": {
                    "route": "command",
                    "command_env": "ONEX_LEDGER_BUS_READ_COMMAND",
                    "timeout_seconds": 120,
                    "row_limit": 20000,
                },
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingTick:
    """The injected tick: returns the fixed outcome and records the dispatched map it was given."""

    def __init__(self, outcome: ModelStallResponderTickOutcome) -> None:
        self._outcome = outcome
        self.seen: list[dict[str, str]] = []

    def __call__(self, **kwargs: Any) -> ModelStallResponderTickOutcome:
        self.seen.append(dict(kwargs["dispatched"]))
        return self._outcome


def _raising(error: Exception) -> Any:
    def run(**kwargs: Mapping[str, str]) -> ModelStallResponderTickOutcome:
        raise error

    return run


def test_golden_a_due_tick_runs_the_responder_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    recorded = _RecordingTick(DONE)
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=recorded
    ).handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "stall_responder",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=599))) is None
    assert h.handle(tick(T0 + timedelta(seconds=600))) is not None


def test_golden_the_dispatched_map_of_a_completed_run_reaches_the_next_run(
    tmp_path: Path,
) -> None:
    recorded = _RecordingTick(DONE)
    h = HandlerStallResponderScheduled(environ=environ(tmp_path), tick=recorded)
    h.handle(tick(T0))
    h.handle(tick(T0 + timedelta(seconds=600)))
    assert recorded.seen == [{}, {"k": "v"}]


def test_error_a_failed_run_keeps_the_dispatched_map(tmp_path: Path) -> None:
    failed = ModelStallResponderTickOutcome(True, "the ledger could not be read: x", {})
    recorded = _RecordingTick(DONE)
    h = HandlerStallResponderScheduled(environ=environ(tmp_path), tick=recorded)
    h.handle(tick(T0))
    h._tick = _RecordingTick(failed)
    event = h.handle(tick(T0 + timedelta(seconds=600)))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", failed.detail)
    h._tick = recorded
    h.handle(tick(T0 + timedelta(seconds=1200)))
    assert recorded.seen[-1] == {"k": "v"}


def test_error_an_unreadable_overlay_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path),
        tick=_raising(NodeOverlayError("boom: the overlay is invalid")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the stall responder overlay could not be read",
    )


def test_error_an_unbound_ledger_route_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerStallResponderScheduled(
        environ=environ(tmp_path),
        tick=_raising(LedgerRouteError("ONEX_LEDGER_BUS_READ_COMMAND is not set")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the ledger route of the stall responder is not bound in the runtime",
    )


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerStallResponderScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerStallResponderScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("stall_responder")
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


def test_real_chain_stall_rows_in_the_window_answer_dispatches() -> None:
    outcome = run_stall_responder_tick(
        overlay=overlay(),
        dispatched={},
        environ={},
        now=T0,
        ledger=InMemoryLedger(STALL_ROWS),
    )
    assert outcome.failed is False
    assert "stall_rows=2 " in outcome.detail
    assert "dispatches=0" not in outcome.detail
    assert outcome.dispatched != {}


def test_control_rows_outside_the_window_are_not_read() -> None:
    """The failing control of the window: a tick that ignored window_seconds would answer the old rows on the narrow run."""
    outcome = run_stall_responder_tick(
        overlay=overlay(window_seconds=60),
        dispatched={},
        environ={},
        now=T0,
        ledger=InMemoryLedger(STALL_ROWS),
    )
    assert outcome.failed is False
    assert "stall_rows=0 " in outcome.detail
    assert outcome.dispatched == {}


def test_control_a_key_dispatched_inside_the_bound_is_not_dispatched_again() -> None:
    ledger = InMemoryLedger(STALL_ROWS)
    first = run_stall_responder_tick(
        overlay=overlay(), dispatched={}, environ={}, now=T0, ledger=ledger
    )
    second = run_stall_responder_tick(
        overlay=overlay(),
        dispatched=first.dispatched,
        environ={},
        now=T0,
        ledger=ledger,
    )
    assert "dispatches=0 " in second.detail
    assert second.dispatched == first.dispatched


def test_error_an_unreadable_ledger_is_a_failed_pass_not_an_empty_answer() -> None:
    ledger = InMemoryLedger(STALL_ROWS)
    ledger.read_error = "ledger-unreadable:down"
    outcome = run_stall_responder_tick(
        overlay=overlay(),
        dispatched={"k": "v"},
        environ={},
        now=T0,
        ledger=ledger,
    )
    assert outcome.failed is True
    assert outcome.detail == "the ledger could not be read: ledger-unreadable:down"
    assert outcome.dispatched == {"k": "v"}


def test_control_a_live_lane_covers_its_cause_key() -> None:
    """A CLAIM with no TERMINAL names a live lane; the responder skips the key that lane's stem covers."""
    bare = run_stall_responder_tick(
        overlay=overlay(),
        dispatched={},
        environ={},
        now=T0,
        ledger=InMemoryLedger(STALL_ROWS),
    )
    claim = "2026-11-07T00:09:00Z | CLAIM | lane=stall-floor-omnibase-core-20261107T0009Z | ticket=OMN-20800 | live"
    covered = run_stall_responder_tick(
        overlay=overlay(),
        dispatched={},
        environ={},
        now=T0,
        ledger=InMemoryLedger((claim, *STALL_ROWS)),
    )
    assert covered.failed is False
    assert "skipped=0 " in bare.detail
    assert "skipped=1 " in covered.detail
    assert "dispatches=7 " in covered.detail
