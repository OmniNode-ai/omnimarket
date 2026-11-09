# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5347:3, 5347:4, 5347:6 and 5347:8 on the work-state projection (OMN-23013); each failed before the fix."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.work_state_projection import (
    EnumWorkItemState,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_effect import runtime_work_state

pytestmark = pytest.mark.unit


def _project(rules, now, rows, tickets, scope_tickets=()):
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=now,
            rules=rules,
            scope=ModelWorkScope(in_scope_tickets=tuple(scope_tickets)),
            tickets=tuple(tickets),
            ledger_rows=tuple(rows),
        )
    )


def _item(result, key):
    return next(i for i in result.items if i.key == key)


def test_5347_3_a_reused_lane_is_attributed_to_its_current_run_only(
    rules, now, row
) -> None:
    result = _project(
        rules,
        now,
        [
            row("CLAIM", "lane=reused", "ticket=ZEP-1", minutes_ago=300),
            row("TERMINAL", "lane=reused", "outcome=done", minutes_ago=200),
            row("CLAIM", "lane=reused", "ticket=ZEP-2", minutes_ago=10),
        ],
        [
            ModelTicketSource(ticket_id="ZEP-1", state_type="started"),
            ModelTicketSource(ticket_id="ZEP-2", state_type="started"),
        ],
        scope_tickets=("ZEP-1", "ZEP-2"),
    )
    finished = _item(result, "ticket:ZEP-1")
    assert finished.lanes == ()
    assert finished.state is EnumWorkItemState.AVAILABLE, finished.reason
    current = _item(result, "ticket:ZEP-2")
    assert current.lanes == ("reused",)
    assert current.state is EnumWorkItemState.IN_PROGRESS


def test_5347_3_failing_control_a_child_claim_of_the_open_run_stays_attributed(
    rules, now, row
) -> None:
    result = _project(
        rules,
        now,
        [
            row("CLAIM", "lane=parent-run", "ticket=ZEP-1", minutes_ago=60),
            row("CLAIM", "lane=parent-run", "ticket=ZEP-2", minutes_ago=10),
        ],
        [
            ModelTicketSource(ticket_id="ZEP-1", state_type="started"),
            ModelTicketSource(ticket_id="ZEP-2", state_type="started"),
        ],
        scope_tickets=("ZEP-1", "ZEP-2"),
    )
    assert _item(result, "ticket:ZEP-1").lanes == ("parent-run",)
    assert _item(result, "ticket:ZEP-2").lanes == ("parent-run",)


def test_5347_4_a_stopped_item_with_an_unmet_start_criterion_is_not_ready(
    rules, now, row
) -> None:
    result = _project(
        rules,
        now,
        [
            row("CLAIM", "lane=fix-s", "ticket=ZEP-S", minutes_ago=90),
            row("TERMINAL", "lane=fix-s", "outcome=partial", minutes_ago=20),
        ],
        [
            ModelTicketSource(
                ticket_id="ZEP-S", state_type="started", requires=("ticket:ZEP-X",)
            ),
            ModelTicketSource(ticket_id="ZEP-X", state_type="started"),
        ],
        scope_tickets=("ZEP-S", "ZEP-X"),
    )
    stopped = _item(result, "ticket:ZEP-S")
    assert stopped.state is EnumWorkItemState.STOPPED
    assert stopped.criterion.value == "false"
    assert "ticket:ZEP-S" not in [i.key for i in result.ready()]


def test_5347_4_failing_control_a_stopped_item_whose_criterion_reads_true_is_ready(
    rules, now, row
) -> None:
    result = _project(
        rules,
        now,
        [
            row("CLAIM", "lane=fix-s", "ticket=ZEP-S", minutes_ago=90),
            row("TERMINAL", "lane=fix-s", "outcome=partial", minutes_ago=20),
        ],
        [
            ModelTicketSource(
                ticket_id="ZEP-S", state_type="started", requires=("ticket:ZEP-X",)
            ),
            ModelTicketSource(ticket_id="ZEP-X", state_type="completed"),
        ],
        scope_tickets=("ZEP-S", "ZEP-X"),
    )
    assert "ticket:ZEP-S" in [i.key for i in result.ready()]


def test_5347_6_nothing_is_ready_while_an_ownership_source_is_unread(
    rules, now
) -> None:
    free = ModelTicketSource(ticket_id="ZEP-F", state_type="started")
    for unread_source in ("ledger:CLAIM read timed out", "pr-watcher:OSError"):
        result = HandlerWorkStateProjection().handle(
            ModelWorkStateRequest(
                now=now,
                rules=rules,
                scope=ModelWorkScope(in_scope_tickets=("ZEP-F",)),
                tickets=(free,),
                unread=(unread_source,),
            )
        )
        assert result.ready() == (), unread_source


def test_5347_6_failing_control_an_unrelated_unread_source_leaves_ready_alone(
    rules, now
) -> None:
    result = HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=now,
            rules=rules,
            scope=ModelWorkScope(in_scope_tickets=("ZEP-F",)),
            tickets=(ModelTicketSource(ticket_id="ZEP-F", state_type="started"),),
            unread=("receipts:dir absent",),
        )
    )
    assert [i.key for i in result.ready()] == ["ticket:ZEP-F"]


def test_5347_8_a_publication_never_touches_a_temp_file_another_publisher_holds(
    tmp_path: Path,
) -> None:
    from tests.nodes.node_work_state_projection_compute.conftest import EXAMPLE_OVERLAY

    rt = runtime_work_state._runtime()
    out = tmp_path / "work-state.json"
    other_writer = out.with_suffix(".tmp")
    other_writer.write_text("the other publisher's half-written snapshot")
    env = {
        rt["home_env"]: str(tmp_path),
        rt["overlay_file_env"]: str(EXAMPLE_OVERLAY),
    }
    runtime_work_state.run(env, datetime(2026, 11, 7, 13, 0, tzinfo=UTC), out)
    assert out.is_file()
    assert other_writer.exists(), "the publication replaced the other publisher's file"
    assert other_writer.read_text() == "the other publisher's half-written snapshot"
