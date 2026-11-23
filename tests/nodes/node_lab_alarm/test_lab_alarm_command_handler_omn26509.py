# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command on the alarm's topic runs broker-lag once and writes one run line (OMN-26509)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_lab_alarm.handlers import handler_broker_lag_command as mod
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag_command import (
    HandlerBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
    ModelBrokerLagResult,
)

pytestmark = pytest.mark.unit

FINISHED = datetime(2026, 11, 23, 7, 4, 9, tzinfo=UTC)


class _Lag:
    def __init__(self, outcome: EnumBrokerLagOutcome) -> None:
        self.outcome = outcome
        self.calls: list[ModelBrokerLagCommand] = []

    def handle(self, request: ModelBrokerLagCommand) -> ModelBrokerLagResult:
        self.calls.append(request)
        return ModelBrokerLagResult(
            correlation_id=request.correlation_id,
            outcome=self.outcome,
            fault=EnumLabAlarmFault.NONE,
            declared_groups=("g1",),
            readings=(),
            alarms=("g1 grew",) if self.outcome is EnumBrokerLagOutcome.ALARM else (),
            reasons=(),
            sample_kept=True,
            evidence="",
        )


def _handler(lag: _Lag, monkeypatch: pytest.MonkeyPatch) -> HandlerBrokerLagCommand:
    seen: list[Any] = []

    def load(*args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "overlay"

    monkeypatch.setattr(mod, "load_structured_overlay", load)

    def build(_overlay: Any) -> _Lag:
        return lag

    h = HandlerBrokerLagCommand(
        clock=lambda: FINISHED,
        build_handler=build,  # type: ignore[arg-type]
    )
    h.seen = seen  # type: ignore[attr-defined]
    return h


def test_the_handler_builds_with_no_argument_the_way_auto_wiring_builds_it() -> None:
    assert HandlerBrokerLagCommand().handle is not None


@pytest.mark.parametrize(
    ("outcome", "alarms"),
    [(EnumBrokerLagOutcome.OK, 0), (EnumBrokerLagOutcome.ALARM, 1)],
)
def test_a_command_runs_the_alarm_once_and_writes_one_run_line(
    outcome: EnumBrokerLagOutcome,
    alarms: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lag = _Lag(outcome)
    h = _handler(lag, monkeypatch)
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        result = h.handle(ModelBrokerLagCommand(correlation_id="c-1"))
    assert result.outcome is outcome
    assert len(lag.calls) == 1
    lines = [r.getMessage() for r in caplog.records if r.name == mod.__name__]
    assert lines == [
        f"node_lab_alarm: broker-lag run outcome={outcome.value} "
        f"finished_at=2026-11-23T07:04:09Z alarms={alarms} correlation_id=c-1"
    ]


def test_the_handler_reads_the_runtime_alarm_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _handler(_Lag(EnumBrokerLagOutcome.OK), monkeypatch)
    h.handle(ModelBrokerLagCommand(correlation_id="c-2"))
    (args, kwargs) = h.seen[0]  # type: ignore[attr-defined]
    assert args[0] == "node_lab_alarm_runtime"
    assert kwargs["file_env"] == "ONEX_LAB_ALARM_OVERLAY"
