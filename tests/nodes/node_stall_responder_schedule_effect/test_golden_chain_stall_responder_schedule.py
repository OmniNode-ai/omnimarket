# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder's producer: due ticks read the stall rows and request the responder (OMN-26438)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.model_stall_cause import ModelStallResponderRequest
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_schedule_effect.handlers.handler_stall_responder_schedule import (
    HandlerStallResponderSchedule,
)
from omnimarket.nodes.node_stall_responder_schedule_effect.models import (
    ModelStallResponderScheduleOverlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
STALL_RAW = (
    "2026-11-22T03:50:00Z | MSG | from=launchd-throughput-tick | principal=p | to=orchestrator-1 | "
    "ticket=OMN-20800 | req=r | via=bus:h200 | throughput-tick is not OK: THROUGHPUT STALL n=1 "
    "unknown=0: floor:omnibase_core. Full output: /tmp/throughput-tick.latest.txt"
)
OVERLAY = ModelStallResponderScheduleOverlay.model_validate(
    {
        "job_id": "stall_responder",
        "run_interval_seconds": 600,
        "grace_seconds": 300,
        "window_seconds": 3600,
        "ledger_read": {
            "route": "command",
            "command_env": "SYNTHETIC_READ",
            "timeout_seconds": 5,
            "row_limit": 10,
        },
    }
)


class _Source:
    def __init__(self, answer: ModelLedgerRead) -> None:
        self.answer = answer
        self.queries: list[ModelLedgerQuery] = []

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self.queries.append(query)
        return self.answer


def _tick(now: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def _handler(source: _Source) -> HandlerStallResponderSchedule:
    gate = ScheduleGate("node_stall_responder_schedule_effect", declaration=OVERLAY)
    return HandlerStallResponderSchedule(gate=gate, source=source, environ={})


def _row() -> ModelLedgerEvidenceRow:
    return ModelLedgerEvidenceRow(
        stamp=NOW - timedelta(minutes=10),
        row_type="MSG",
        lane="launchd-throughput-tick",
        raw=STALL_RAW,
    )


def test_a_due_tick_requests_the_responder_with_the_window_rows() -> None:
    source = _Source(ModelLedgerRead(rows=(_row(),)))
    request = _handler(source).handle(_tick(NOW))
    assert isinstance(request, ModelStallResponderRequest)
    assert request.rows == (STALL_RAW,)
    (query,) = source.queries
    assert (query.row_type, query.term) == ("MSG", "THROUGHPUT STALL")
    assert query.until - query.since == timedelta(seconds=3600)
    result = HandlerStallCauseResponder().handle(request)
    assert [d.cause_key for d in result.dispatches] == ["floor:omnibase_core"]


def test_a_tick_inside_the_interval_requests_nothing() -> None:
    source = _Source(ModelLedgerRead(rows=(_row(),)))
    handler = _handler(source)
    assert handler.handle(_tick(NOW)) is not None
    assert handler.handle(_tick(NOW + timedelta(seconds=60))) is None
    assert len(source.queries) == 1
    assert handler.handle(_tick(NOW + timedelta(seconds=600))) is not None


def test_a_window_with_no_stall_rows_still_requests_an_empty_answer() -> None:
    request = _handler(_Source(ModelLedgerRead())).handle(_tick(NOW))
    assert request is not None
    assert request.rows == ()


def test_an_unreadable_ledger_publishes_nothing_and_retries_on_the_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Source(ModelLedgerRead(error="read command exited 2"))
    handler = _handler(source)
    with caplog.at_level(logging.ERROR):
        assert handler.handle(_tick(NOW)) is None
    assert "read command exited 2" in caplog.text
    source.answer = ModelLedgerRead(rows=(_row(),))
    retried = handler.handle(_tick(NOW + timedelta(seconds=1)))
    assert retried is not None
    assert retried.rows == (STALL_RAW,)


def test_a_missing_overlay_starts_nothing(tmp_path: pytest.TempPathFactory) -> None:
    handler = HandlerStallResponderSchedule(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert handler.handle(_tick(NOW)) is None
