# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The router's interpreter is a declared setting resolved from the running environment (OMN-25501).

On dev every fire raised FileNotFoundError on a hard-coded ``.venv/bin/python`` in the overlay's commands;
the missing interpreter must refuse by name, once, never a traceback each tick.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    CommandPorts,
    HandlerFailureAlarmRouterScheduled,
    InterpreterMissingError,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
)
from tests.nodes.node_failure_alarm_router_effect.helpers import NOW

pytestmark = pytest.mark.unit

MISSING = "/nonexistent/venv/bin/python"


def _request(**extra: object) -> ModelFailureAlarmRequest:
    return ModelFailureAlarmRequest(
        now=NOW,
        alert_command="true",
        ledger_rows_command="{python} -c 'print(1)'",
        **extra,
    )


def test_interpreter_defaults_to_the_running_environment() -> None:
    ports = CommandPorts(_request())
    assert ports.read_rows() == ["1"]
    assert ports.interpreter == sys.executable


def test_declared_interpreter_setting_is_used() -> None:
    ports = CommandPorts(_request(python_interpreter=sys.executable))
    assert ports.interpreter == sys.executable


def test_missing_declared_interpreter_refuses_by_name() -> None:
    with pytest.raises(InterpreterMissingError, match=MISSING):
        CommandPorts(_request(python_interpreter=MISSING))


def test_hard_coded_missing_command_refuses_by_name_not_file_not_found() -> None:
    request = ModelFailureAlarmRequest(
        now=NOW,
        alert_command="true",
        ledger_rows_command=f"{MISSING} -m x",
    )
    with pytest.raises(InterpreterMissingError, match=MISSING):
        CommandPorts(request).read_rows()


def test_scheduled_fire_with_missing_interpreter_refuses_once_not_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduled = HandlerFailureAlarmRouterScheduled(
        request=_request(python_interpreter=MISSING)
    )

    def tick(when: datetime) -> ModelRuntimeTick:
        return ModelRuntimeTick(
            now=when,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=when,
            correlation_id=uuid4(),
            scheduler_id="test",
            tick_interval_ms=1000,
        )

    with caplog.at_level(logging.ERROR):
        for i in range(3):
            asyncio.run(scheduled.handle(tick(NOW + timedelta(days=i))))
    named = [r for r in caplog.records if MISSING in r.getMessage()]
    assert len(named) <= 1
