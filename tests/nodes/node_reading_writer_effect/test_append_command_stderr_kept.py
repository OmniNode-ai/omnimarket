# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The error exit of the append command keeps the stderr tail in its message (OMN-23454).

A scheduled unit that appends through the bus used to log only ``append-command-exit-69`` when
the append command died on an unexpected exit: the command's stderr, which says why (the bus
could not be opened), was thrown away. The outcome's message now carries the tail of that stderr
next to the exit code, and the scenario R2 handler, which raises on the outcome's message, carries
it further to whoever runs the unit. The refusal (65), pending (75), timeout, OSError and receipt
paths are unchanged; a command that writes no stderr still names the exit code alone.
"""

from __future__ import annotations

import sys

import pytest

from omnimarket.handlers.delegation_scenario_runtime import bus_appender
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

pytestmark = pytest.mark.unit

REQUEST_ID = "6f2c1c0e-7d52-5a3e-9b0b-2f7c1d0a4e11"
ROW = (
    "2026-11-09T13:06:30Z | STATUS | lane=node_delegation_scenario_r2_effect | "
    "scenario=R2 | conclusion=failure\n"
)
LANE_DECLARATION = (
    "bus: the lane declaration config/lab/ci_bus_lanes.yaml does not exist"
)


def _command(code: int, stderr: str = "", stdout: str = "") -> list[str]:
    """A stand-in append command: it writes what it is told, ignores stdin, exits with ``code``."""
    script = (
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({code})\n"
    )
    return [sys.executable, "-c", script]


def _appender(code: int, stderr: str = "", stdout: str = "") -> CommandLedgerAppender:
    return CommandLedgerAppender(
        _command(code, stderr=stderr, stdout=stdout), "reading-writer", "h-test", 60
    )


def test_the_golden_chain_a_zero_exit_with_an_accepted_receipt_is_accepted() -> None:
    outcome = _appender(0, stdout='{"status": "accepted", "message": "ok"}\n').append(
        ROW, REQUEST_ID
    )
    assert outcome.status == "accepted"
    assert outcome.message == "ok"


def test_an_unexpected_exit_keeps_the_stderr_tail_next_to_the_code() -> None:
    outcome = _appender(
        69, stderr=f"bus: opening the bus failed\n{LANE_DECLARATION}\n"
    ).append(ROW, REQUEST_ID)
    assert outcome.status == "error"
    assert "append-command-exit-69" in outcome.message
    assert LANE_DECLARATION in outcome.message
    # The two stderr lines arrive as one flattened line of the message.
    assert f"bus: opening the bus failed | {LANE_DECLARATION}" in outcome.message


def test_an_unexpected_exit_without_stderr_names_the_exit_code_alone() -> None:
    outcome = _appender(69).append(ROW, REQUEST_ID)
    assert outcome.status == "error"
    assert outcome.message == "append-command-exit-69"


def test_a_long_stderr_keeps_only_its_tail_within_the_model_limit() -> None:
    stderr = "S" * 4000 + "E" * 999 + "\n"
    assert len(stderr) == 5000
    outcome = _appender(70, stderr=stderr).append(ROW, REQUEST_ID)
    assert outcome.status == "error"
    assert len(outcome.message) < 2000
    assert "E" * 300 in outcome.message  # the end of the stderr
    assert "S" not in outcome.message  # never the start


def test_the_scenario_r2_handler_path_surfaces_the_stderr_tail() -> None:
    # The wiring of build_runner in handler_scenario_r2.py: bus_appender around
    # CommandLedgerAppender raises "the ledger append was error: <the message>".
    append = bus_appender(
        CommandLedgerAppender(
            _command(69, stderr=f"{LANE_DECLARATION}\n"),
            "node_delegation_scenario_r2_effect",
            "h-test",
            60,
        )
    )
    with pytest.raises(RuntimeError) as raised:
        append(ROW)
    assert "the ledger append was error:" in str(raised.value)
    assert "append-command-exit-69" in str(raised.value)
    assert LANE_DECLARATION in str(raised.value)
