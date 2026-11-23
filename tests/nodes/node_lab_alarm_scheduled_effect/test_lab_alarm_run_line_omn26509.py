# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One run of the lab alarm writes one run line to the runtime log (OMN-26509).

node_lab_alarm subscribes to no topic, so before this change the runtime log held only its wiring lines and a
broker-lag run left a trace in broker-lag-runs.jsonl alone. The scheduled node that runs the alarm on the runtime
tick now logs one line per run that names the alarm, the outcome and the time the run finished.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.lab_alarm_scheduled_tick import ModelLabAlarmTickOutcome
from omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled import (
    HandlerLabAlarmScheduled,
)
from tests.nodes.node_lab_alarm_scheduled_effect.test_golden_chain_lab_alarm_scheduled import (
    DONE,
    T0,
    _raising,
    _RecordingTick,
    environ,
    tick,
)

pytestmark = pytest.mark.unit

FINISHED = datetime(2026, 11, 23, 7, 4, 9, tzinfo=UTC)
LOGGER = "omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled"


def _run_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and "broker-lag run" in r.getMessage()
    ]


def test_a_due_tick_writes_one_run_line_with_outcome_and_finished_at(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = HandlerLabAlarmScheduled(
        environ=environ(tmp_path),
        tick=_RecordingTick(DONE),
        clock=lambda: FINISHED,
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert h.handle(tick(T0)) is not None
    assert _run_lines(caplog) == [
        "node_lab_alarm_scheduled_effect: node_lab_alarm broker-lag run "
        "outcome=completed finished_at=2026-11-23T07:04:09Z detail=outcome=ok declared=1"
    ]


def test_a_failed_run_writes_its_run_line_with_the_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    failed = ModelLabAlarmTickOutcome(True, "the lab alarm could not read the lag: x")
    h = HandlerLabAlarmScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(failed), clock=lambda: FINISHED
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        h.handle(tick(T0))
    (line,) = _run_lines(caplog)
    assert "outcome=failed finished_at=2026-11-23T07:04:09Z" in line
    assert "could not read the lag: x" in line


def test_an_unreadable_alarm_overlay_still_writes_a_run_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    h = HandlerLabAlarmScheduled(
        environ=environ(tmp_path),
        tick=_raising(NodeOverlayError("boom")),
        clock=lambda: FINISHED,
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        h.handle(tick(T0))
    (line,) = _run_lines(caplog)
    assert "outcome=failed" in line


def test_a_tick_inside_the_interval_runs_nothing_and_writes_no_second_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Positive control for the one-line count: the due tick wrote one line, the early tick adds none."""
    h = HandlerLabAlarmScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE), clock=lambda: FINISHED
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        h.handle(tick(T0))
        assert len(_run_lines(caplog)) == 1
        assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert len(_run_lines(caplog)) == 1
