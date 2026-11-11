# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23717: the J5 projection read through the real read command and the real dev broker.

The seam is the one this change made: ``SourceProjectedRows.read_row`` tells a read that failed (it raises
``ProjectionReadFailedError`` with the last read error) from a row the projection has not received yet (it answers ``None``).
The in-memory tests (``tests/unit/nodes/node_work_ledger_append_effect/test_join_j5_read_failure.py``) script the source; here
the source is the command the deployment overlay names (``onex ledger query --stdin``), so a change to the query node's answer
shape or to the way the command reports a refusal turns this red.

It runs where a lab host names that command in ``JOIN_J5_LIVE_READ_COMMAND`` (the command resolves the bus login itself and
this file never reads, builds or prints a credential). It SKIPS without the variable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    SourceProjectedRows,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    ProjectionReadFailedError,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

LIVE_COMMAND_ENV = "JOIN_J5_LIVE_READ_COMMAND"
FAILING_COMMAND_ENV = "JOIN_J5_LIVE_FAILING_READ_COMMAND"
UNKNOWN_BUS_LANE = "nosuchlane"
LEDGER_ID = "seam-test-ledger"
READ_TIMEOUT_SECONDS = 90
ROW_LIMIT = 5000
LOOKBACK = timedelta(days=1)
JOINED_LANE = "join-j5"
PROBE_LANE = "join-j5-seam-probe"


def _route(command_env: str) -> ModelCommandLedgerReadRoute:
    return ModelCommandLedgerReadRoute(
        route="command",
        command_env=command_env,
        timeout_seconds=READ_TIMEOUT_SECONDS,
        row_limit=ROW_LIMIT,
    )


def _projected(command_env: str) -> SourceProjectedRows:
    return SourceProjectedRows(
        build_row_source(_route(command_env), os.environ),
        attempts=1,
        interval_seconds=1,
        slack_seconds=120,
    )


@pytest.fixture(autouse=True)
def _live_commands(monkeypatch: pytest.MonkeyPatch) -> str:
    command = os.environ.get(LIVE_COMMAND_ENV)
    if not command:
        pytest.skip(f"{LIVE_COMMAND_ENV} names no live read command on this host")
    monkeypatch.setenv(FAILING_COMMAND_ENV, f"{command} --bus-lane {UNKNOWN_BUS_LANE}")
    return command


def _a_real_row() -> str:
    now = datetime.now(UTC)
    read = build_row_source(_route(LIVE_COMMAND_ENV), os.environ).read(
        ModelLedgerQuery(
            since=now - LOOKBACK,
            until=now,
            row_type="STATUS",
            lane=JOINED_LANE,
            correlation_id=str(uuid4()),
        )
    )
    assert read.error is None, read.error
    if not read.rows:
        pytest.skip(f"the projection holds no {JOINED_LANE} row in the last day")
    return read.rows[-1].raw


def _never_appended_row() -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | STATUS | lane={PROBE_LANE} | src={PROBE_LANE} | seam probe {uuid4()}"


def test_golden_a_row_the_projection_holds_is_found_through_the_real_command() -> None:
    raw = _a_real_row()

    record = _projected(LIVE_COMMAND_ENV).read_row(
        ledger_id=LEDGER_ID, ledger_seq=1, raw_row=raw
    )

    assert record is not None
    assert record.raw_row.strip() == raw.strip()


def test_error_a_row_never_appended_is_not_yet_projected_not_a_read_failure() -> None:
    record = _projected(LIVE_COMMAND_ENV).read_row(
        ledger_id=LEDGER_ID, ledger_seq=1, raw_row=_never_appended_row()
    )

    assert record is None


def test_error_a_read_the_command_refuses_raises_with_its_error() -> None:
    with pytest.raises(ProjectionReadFailedError) as raised:
        _projected(FAILING_COMMAND_ENV).read_row(
            ledger_id=LEDGER_ID, ledger_seq=1, raw_row=_never_appended_row()
        )

    assert raised.value.error
    assert raised.value.attempts == 1


def test_control_the_same_unprojected_row_ends_differently_when_the_read_fails() -> (
    None
):
    raw = _never_appended_row()

    answered = _projected(LIVE_COMMAND_ENV).read_row(
        ledger_id=LEDGER_ID, ledger_seq=1, raw_row=raw
    )
    with pytest.raises(ProjectionReadFailedError):
        _projected(FAILING_COMMAND_ENV).read_row(
            ledger_id=LEDGER_ID, ledger_seq=1, raw_row=raw
        )

    assert answered is None
