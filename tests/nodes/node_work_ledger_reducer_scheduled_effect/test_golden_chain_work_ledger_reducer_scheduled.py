# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the work-ledger reducer on the runtime schedule (OMN-24305)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.handlers.work_ledger_reducer_scheduled_tick import (
    InMemoryWorkLedgerRowsReader,
    ModelWorkLedgerReducerTickOutcome,
    ProtocolWorkLedgerRowsReader,
    run_work_ledger_reducer_tick,
)
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelGuardReplayResult,
    ModelJudgeVerdict,
    ModelReplayDifference,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_reducer import (
    ModelWorkLedgerReducerRequest,
    ModelWorkLedgerReducerResult,
)
from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.handlers.handler_work_ledger_reducer_scheduled import (
    HandlerWorkLedgerReducerScheduled,
)
from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.models import (
    ModelWorkLedgerReducerScheduleOverlay,
)

pytestmark = pytest.mark.unit

LEDGER_NAME = next(iter(GOVERNED_LEDGER_NAMES))

REPO = Path(__file__).resolve().parents[3]
NODE = "node_work_ledger_reducer_scheduled_effect"
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


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "work_ledger_reducer",
                "run_interval_seconds": 900,
                "grace_seconds": 300,
                "ledger_name": LEDGER_NAME,
                "ledger_id": "ledger-test",
                "read_command_env": "LEDGER_READ_TEST_COMMAND",
                "read_timeout_seconds": 30,
                "window_seconds": 900,
                "row_limit": 500,
                "prior_lookback_seconds": 86400,
                "prior_kinds": ["CLAIM", "HOLD", "RELEASE"],
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _row(
    ts: datetime,
    row_type: str,
    lane: str,
    text: str,
    ledger_seq: int | None = None,
) -> ModelWorkLedgerRowRecord:
    raw = (
        f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')} | {row_type} | lane={lane} | "
        f"principal=dev-test | ticket=OMN-24305 | {text}"
    )
    return ModelWorkLedgerRowRecord(
        row_id=work_ledger_row_id(raw),
        row_ts=ts,
        row_type=row_type,
        row_lane=lane,
        text=text,
        raw_row=raw,
        source="test",
        projected_at=ts,
        ledger_seq=ledger_seq,
        ingested_at=ts,
    )


def _three_rows(ledger_seq: bool = False) -> list[ModelWorkLedgerRowRecord]:
    base = T0 - timedelta(minutes=14)
    return [
        _row(base, "STATUS", "alpha", "started", 1 if ledger_seq else None),
        _row(
            base + timedelta(minutes=4),
            "STATUS",
            "alpha",
            "in progress",
            2 if ledger_seq else None,
        ),
        _row(
            base + timedelta(minutes=8),
            "STATUS",
            "beta",
            "finished",
            3 if ledger_seq else None,
        ),
    ]


def test_golden_a_due_tick_with_three_rows_completes(tmp_path: Path) -> None:
    reader = InMemoryWorkLedgerRowsReader(_three_rows())
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "work_ledger_reducer",
        NODE,
        "completed",
    )
    assert "window_rows=3" in event.detail
    assert "differences=0" in event.detail
    assert "rebuild=not-run" in event.detail
    assert "unsequenced=3" in event.detail


def test_golden_sequenced_rows_run_the_rebuild(tmp_path: Path) -> None:
    reader = InMemoryWorkLedgerRowsReader(_three_rows(ledger_seq=True))
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert "rebuild_events=3" in event.detail
    assert "rebuild=ran" in event.detail


def test_error_an_unbound_read_command_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerWorkLedgerReducerScheduled(environ=environ(tmp_path), clock=lambda: T0)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "LEDGER_READ_TEST_COMMAND is not set" in event.detail


def test_error_a_read_error_from_the_reader_is_a_failed_run(tmp_path: Path) -> None:
    reader = InMemoryWorkLedgerRowsReader([])
    reader.read_error = "read-command-failed:OSError"
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "window read failed: read-command-failed:OSError" in event.detail


def test_error_an_empty_window_is_a_failed_run(tmp_path: Path) -> None:
    reader = InMemoryWorkLedgerRowsReader([])
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "window held no rows" in event.detail


def test_error_a_release_naming_an_unopened_hold_is_refused(tmp_path: Path) -> None:
    release = _row(
        T0 - timedelta(minutes=5),
        "RELEASE",
        "beta",
        "re=2026-11-13T17:00:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done",
    )
    reader = InMemoryWorkLedgerRowsReader([release])
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "guard_replay refused: WorkLedgerGuardReplayError" in event.detail


def test_error_two_rows_claiming_the_same_ledger_seq_are_refused(
    tmp_path: Path,
) -> None:
    rows = [
        _row(T0 - timedelta(minutes=10), "STATUS", "alpha", "a", ledger_seq=1),
        _row(T0 - timedelta(minutes=5), "STATUS", "alpha", "b", ledger_seq=1),
    ]
    reader = InMemoryWorkLedgerRowsReader(rows)
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "rebuild refused" in event.detail


def test_error_a_reducer_with_one_difference_fails(tmp_path: Path) -> None:
    def fake_reducer(
        request: ModelWorkLedgerReducerRequest,
    ) -> ModelWorkLedgerReducerResult:
        accept = ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT)
        refuse = ModelJudgeVerdict(
            verdict=EnumJudgeVerdict.REFUSE, rule="injected-rule", reason="injected"
        )
        return ModelWorkLedgerReducerResult(
            operation="guard_replay",
            replay=ModelGuardReplayResult(
                judged=3,
                differences=(
                    ModelReplayDifference(
                        position=1,
                        row_ref="abc123def456abc",
                        recorded=accept,
                        replayed=refuse,
                        difference="recorded accepted; replayed refused under injected-rule",
                    ),
                ),
                equivalent=False,
            ),
        )

    def broken_tick(
        *,
        overlay: ModelWorkLedgerReducerScheduleOverlay,
        reader: ProtocolWorkLedgerRowsReader,
        clock: Callable[[], datetime],
    ) -> ModelWorkLedgerReducerTickOutcome:
        return run_work_ledger_reducer_tick(
            overlay=overlay, reader=reader, clock=clock, reducer=fake_reducer
        )

    reader = InMemoryWorkLedgerRowsReader(_three_rows())
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0, tick=broken_tick
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "guard replay differences=1 first=1:abc123def456" in event.detail


def test_failing_control_the_same_rows_with_the_real_reducer_complete(
    tmp_path: Path,
) -> None:
    reader = InMemoryWorkLedgerRowsReader(_three_rows())
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"


def test_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    reader = InMemoryWorkLedgerRowsReader(_three_rows())
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=899))) is None


def test_a_refused_overlay_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerWorkLedgerReducerScheduled(environ={}, clock=lambda: T0)
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_error_a_row_the_real_guards_refuse_is_a_difference(tmp_path: Path) -> None:
    """A TERMINAL with no CLAIM before it: recorded accepted, the real guards refuse it, so the run fails by name."""
    rows = [_row(T0 - timedelta(minutes=5), "TERMINAL", "alpha", "done")]
    reader = InMemoryWorkLedgerRowsReader(rows)
    h = HandlerWorkLedgerReducerScheduled(
        environ=environ(tmp_path), reader=reader, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "guard replay differences=1 first=1:" in event.detail
