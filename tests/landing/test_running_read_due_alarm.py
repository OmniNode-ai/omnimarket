# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-26491): a due row with no run line by its due time alarms the owner once and holds the ticket.

The falsifier: a fixture due row older than 60 minutes with no STATUS row carrying observed= and read_at=
that raises no alarm fails this file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_running_read_due_compute.handlers.handler_running_read_due import (
    HandlerRunningReadDue,
)
from omnimarket.nodes.node_running_read_due_compute.models.model_running_read_due import (
    ModelMergedPrFact,
    ModelRunningReadDueRequest,
)

pytestmark = pytest.mark.unit

MERGED_AT = datetime(2026, 11, 23, 0, 30, 0, tzinfo=UTC)
DUE_ID = "rrd:OmniNode-ai/omnimarket:6700"


def _due_row() -> str:
    result = HandlerRunningReadDue().handle(
        ModelRunningReadDueRequest(
            operation="write_due_row",
            merged=ModelMergedPrFact(
                pr="OmniNode-ai/omnimarket#6700",
                ticket="OMN-26491",
                owner="orchestrator-31d9-swarm",
                merged_at=MERGED_AT,
                runtime_affecting=True,
                pr_body="Running read: node=node_floor_fill_tick topic=onex.evt.floor.fill.v1",
            ),
        )
    )
    assert result.append_request is not None
    assert result.due_id == DUE_ID
    return result.append_request.rows


def _read(ledger: str, now: datetime):
    return HandlerRunningReadDue().handle(
        ModelRunningReadDueRequest(
            operation="read_due_rows", ledger_text=ledger, now=now
        )
    )


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 23, hour, minute, 0, tzinfo=UTC)


def test_a_due_row_past_sixty_minutes_with_no_run_line_alarms_the_owner_once() -> None:
    ledger = _due_row() + "\n"
    result = _read(ledger, _at(1, 31))

    assert len(result.alarms) == 1, "a due row past its due time raised no alarm"
    alarm = result.alarms[0]
    row = grammar.parse_row(alarm.append_request.rows)
    assert row is not None
    assert row.row_type == "MSG"
    assert row.value("to") == "orchestrator-31d9-swarm"
    assert row.value("ticket") == "OMN-26491"
    assert row.value("re") == DUE_ID
    assert row.value("kind") == "running-read-alarm"
    assert grammar.row_refusals(alarm.append_request.rows) == []

    # Exactly one: once the alarm row is on the ledger, the next read raises none.
    again = _read(ledger + alarm.append_request.rows + "\n", _at(1, 40))
    assert again.alarms == []


def test_no_alarm_before_the_due_time() -> None:
    assert _read(_due_row() + "\n", _at(1, 29)).alarms == []


def test_a_status_row_with_observed_and_read_at_closes_the_due_row() -> None:
    observed = (
        "2026-11-23T01:10:00Z | STATUS | lane=orchestrator-31d9-swarm | ticket=OMN-26491 | "
        f"due_id={DUE_ID} | observed=node_floor_fill_tick consumed onex.evt.floor.fill.v1 | "
        "read_at=2026-11-23T01:10:00Z | the change is running"
    )
    result = _read(_due_row() + "\n" + observed + "\n", _at(2, 0))
    assert result.alarms == []
    assert result.blocked_tickets == []


def test_an_observation_missing_read_at_does_not_close_the_due_row() -> None:
    half = (
        "2026-11-23T01:10:00Z | STATUS | lane=x | ticket=OMN-26491 | "
        f"due_id={DUE_ID} | observed=looks fine | a half observation"
    )
    result = _read(_due_row() + "\n" + half + "\n", _at(2, 0))
    assert len(result.alarms) == 1
    assert result.blocked_tickets == ["OMN-26491"]


def test_the_ticket_cannot_move_to_done_while_the_due_row_is_open() -> None:
    ledger = _due_row() + "\n"
    before_due = _read(ledger, _at(1, 0))
    assert before_due.blocked_tickets == ["OMN-26491"]
    after_alarm = _read(ledger, _at(3, 0))
    assert after_alarm.blocked_tickets == ["OMN-26491"]
    assert "OMN-26491" in after_alarm.blocked_reason
