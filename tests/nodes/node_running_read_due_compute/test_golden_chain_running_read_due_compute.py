# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the running-read due node: merge, due row, alarm, run line (OMN-26491)."""

from datetime import UTC, datetime

from omnimarket.events.running_read_due import (
    ModelMergedPrFact,
    ModelRunningReadDueRequest,
)
from omnimarket.nodes.node_running_read_due_compute import NodeRunningReadDueCompute


def test_merge_to_due_row_to_alarm_to_run_line() -> None:
    node = NodeRunningReadDueCompute()
    merged_at = datetime(2026, 11, 23, 0, 30, tzinfo=UTC)
    written = node.handle(
        ModelRunningReadDueRequest(
            operation="write_due_row",
            merged=ModelMergedPrFact(
                pr="OmniNode-ai/omnimarket#6700",
                ticket="OMN-26491",
                owner="orchestrator-31d9-swarm",
                merged_at=merged_at,
                runtime_affecting=True,
                pr_body="Running read: unit=example-unit host=example-host",
            ),
        )
    )
    assert written.append_request is not None
    ledger = written.append_request.rows + "\n"

    late = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)
    owed = node.handle(
        ModelRunningReadDueRequest(
            operation="read_due_rows", ledger_text=ledger, now=late
        )
    )
    assert [alarm.due_id for alarm in owed.alarms] == [written.due_id]
    assert owed.blocked_tickets == ["OMN-26491"]

    ledger += owed.alarms[0].append_request.rows + "\n"
    ledger += (
        "2026-11-23T02:05:00Z | STATUS | lane=orchestrator-31d9-swarm | ticket=OMN-26491 | "
        f"due_id={written.due_id} | observed=unit example-unit ran on example-host | "
        "read_at=2026-11-23T02:05:00Z | read the running change\n"
    )
    closed = node.handle(
        ModelRunningReadDueRequest(
            operation="read_due_rows", ledger_text=ledger, now=late
        )
    )
    assert closed.alarms == []
    assert closed.blocked_tickets == []
    assert closed.open_due_ids == []
