# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A canary run whose ledger append fails keeps the append's own cause (OMN-23077).

Recorded failure: the daily unit on the h053 lab host ran at 2026-11-08 04:14Z for 66 seconds against 12 for the
run before it, exited 1, and its out log read ``ledger=append-failed:error`` with an empty error log. The status
alone was kept; the append command's message (here, its 60 second timeout) was dropped, so no one could tell a slow
bus from a refused row. These tests build that failure and assert the cause reaches the result, the failure event
and the unit's error log.
"""

from __future__ import annotations

import json
import sys

import pytest

from omnimarket.nodes.node_landing_canary_effect import runtime_landing_canary
from omnimarket.nodes.node_landing_canary_effect.handlers.handler_landing_canary import (
    HandlerLandingCanary,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.unit.nodes.node_landing_canary_effect.canary_support import World

pytestmark = pytest.mark.unit

TOPICS = load_topics()
TIMEOUT_CAUSE = "append-command-failed:TimeoutExpired"


class _AppendTimesOut:
    """The append the recorded run met: the command did not answer inside its timeout."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        return ModelAppendOutcome(status="error", message=TIMEOUT_CAUSE)


def _command() -> ModelLandingCanaryCommand:
    return ModelLandingCanaryCommand(correlation_id="cause-1", write=True)


def test_error_chain_a_failed_append_keeps_its_cause_on_the_result() -> None:
    result = World().handler(_AppendTimesOut()).handle(_command())
    assert result.ledger == "append-failed:error"
    assert result.ledger_cause == TIMEOUT_CAUSE
    assert result.failed


def test_error_chain_a_failed_append_names_its_cause_in_the_failure_event() -> None:
    handler = World().handler(_AppendTimesOut())
    payload = b'{"correlation_id": "cause-2", "write": true}'
    events = answer_command(payload, handler, TOPICS)
    assert events[0][0] == TOPICS["landing_canary_failed"]
    body = json.loads(events[0][1])
    assert body["ledger"] == "append-failed:error"
    assert body["ledger_cause"] == TIMEOUT_CAUSE


def test_golden_chain_a_written_row_has_no_cause() -> None:
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        InMemoryLedger,
    )

    result = World().handler(InMemoryLedger()).handle(_command())
    assert result.ledger == "written"
    assert result.ledger_cause == ""


def test_the_real_append_command_that_outlives_its_timeout_reaches_the_result() -> None:
    """The recorded failure end to end: a real subprocess past its timeout, through the real appender."""
    appender = CommandLedgerAppender(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        lane="landing-canary",
        host="example-host",
        timeout_seconds=1,
    )
    result = World().handler(appender).handle(_command())
    assert result.ledger == "append-failed:error"
    assert result.ledger_cause == TIMEOUT_CAUSE


def test_the_unit_error_log_names_the_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    handler = world.handler(_AppendTimesOut())
    monkeypatch.setattr(
        runtime_landing_canary, "load_overlay", lambda: handler._overlay
    )

    def build(*_: object) -> tuple[HandlerLandingCanary, str]:
        return handler, "example-host"

    monkeypatch.setattr(runtime_landing_canary, "build_handler", build)
    assert runtime_landing_canary._run(write=True) == 1
    seen = capsys.readouterr()
    assert TIMEOUT_CAUSE in seen.err
    assert "append-failed:error" in seen.err
    assert f"cause={TIMEOUT_CAUSE}" in seen.out
