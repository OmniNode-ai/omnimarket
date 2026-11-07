# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection lists every open commitment as a work item (OMN-23013 AC2).

An operator ask recorded as a commitment is work until its check passes: available, blocked on its start criterion or
stopped with what its lane left, and the fill dispatcher reads it through ``ready()`` like any other work. The failing
controls drop the commitment cell from the overlay's attribution cells and turn the commitment's source off, and show the
reading fail.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_commitment_ledger_compute.handlers.commitment_rows import (
    fold_commitments,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.models import (
    EnumScopeStatus,
    EnumWorkItemKind,
    EnumWorkItemState,
    ModelCommitmentSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    build_request,
)

pytestmark = pytest.mark.unit

ENV = {
    "ONEX_COMMITMENT_OVERLAY": str(
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
    )
}
CID = "2026-11-07T09:00:00Z-orchestrator-31d9"
KEY = f"commitment:{CID}"


def _commitment(now, status="open", **overrides: object) -> ModelCommitmentSource:
    fields: dict[str, object] = {
        "commitment_id": CID,
        "ask": "Switch h053 to the NVMe and tell me when the host reads it",
        "owner": "host-bringup-1",
        "check": "uv run onex host-read --host h053 --expect nvme-root",
        "status": status,
        "opened_at": now - timedelta(hours=7),
    }
    fields.update(overrides)
    return ModelCommitmentSource.model_validate(fields)


def _project(**fields: object) -> ModelWorkStateResult:
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest.model_validate(fields)
    )


def _item(result: ModelWorkStateResult, key: str):
    found = [i for i in result.items if i.key == key]
    assert len(found) == 1, (key, [i.key for i in result.items])
    return found[0]


def test_an_open_commitment_is_available_work_in_scope(rules, now) -> None:
    result = _project(
        now=now, rules=rules, scope=ModelWorkScope(), commitments=[_commitment(now)]
    )
    item = _item(result, KEY)
    assert item.kind is EnumWorkItemKind.COMMITMENT
    assert item.state is EnumWorkItemState.AVAILABLE
    assert item.scope is EnumScopeStatus.IN_SCOPE
    assert item.owner == "host-bringup-1"
    assert "Switch h053" in item.title
    assert item in result.ready(), "the fill dispatches it like any other ready work"


def test_failing_control_with_the_commitment_source_off_no_item_appears(
    rules, now
) -> None:
    result = _project(now=now, rules=rules, scope=ModelWorkScope(), commitments=[])
    assert [i for i in result.items if i.kind is EnumWorkItemKind.COMMITMENT] == []
    assert result.ready() == ()


def test_a_verified_commitment_is_done_and_not_dispatched(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        commitments=[_commitment(now, "verified", last_checked_at=now, last_exit=0)],
    )
    item = _item(result, KEY)
    assert item.state is EnumWorkItemState.DONE
    assert item not in result.ready()


def test_a_regressed_commitment_is_available_again_and_says_so(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        commitments=[_commitment(now, "regressed", last_checked_at=now, last_exit=1)],
    )
    item = _item(result, KEY)
    assert item.state is EnumWorkItemState.AVAILABLE
    assert "regressed" in item.reason
    assert "exit 1" in item.reason
    assert item in result.ready()


def test_a_commitment_waits_on_its_start_criterion(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="HOST-9", state_type="started")],
        commitments=[_commitment(now, requires=("ticket:HOST-9",))],
    )
    item = _item(result, KEY)
    assert item.state is EnumWorkItemState.BLOCKED
    assert item.blocking == ("ticket:HOST-9",)
    assert item not in result.ready()


def test_a_live_lane_claiming_the_commitment_makes_it_in_progress(
    rules, now, row
) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        commitments=[_commitment(now)],
        ledger_rows=[
            row("CLAIM", "lane=host-bringup-2", f"commitment={CID}", minutes_ago=20)
        ],
    )
    item = _item(result, KEY)
    assert item.state is EnumWorkItemState.IN_PROGRESS
    assert item.live_lane == "host-bringup-2"


def test_a_lane_that_stopped_leaves_the_commitment_stopped_with_what_it_left(
    rules, now, row
) -> None:
    rows = [
        row("CLAIM", "lane=host-bringup-2", f"commitment={CID}", minutes_ago=90),
        row(
            "STATUS",
            "lane=host-bringup-2",
            "branch=omn-9-nvme",
            "head=abc123",
            minutes_ago=60,
        ),
        row("TERMINAL", "lane=host-bringup-2", "outcome=partial", minutes_ago=30),
    ]
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        commitments=[_commitment(now)],
        ledger_rows=rows,
    )
    item = _item(result, KEY)
    assert item.state is EnumWorkItemState.STOPPED
    assert item.left is not None
    assert "branch=omn-9-nvme" in item.left
    assert item in result.ready(), "a stopped lane's work is dispatched again"


def test_injected_break_without_the_commitment_cell_the_stopped_lane_is_not_seen(
    rules, now, row
) -> None:
    """The failing control of the attribution: the overlay with the commitment cell removed leaves the item available."""
    cells = {k: v for k, v in rules.attribution_cells.items() if k != "commitment"}
    broken = rules.model_copy(update={"attribution_cells": cells})
    rows = [
        row("CLAIM", "lane=host-bringup-2", f"commitment={CID}", minutes_ago=90),
        row("TERMINAL", "lane=host-bringup-2", "outcome=partial", minutes_ago=30),
    ]
    result = _project(
        now=now,
        rules=broken,
        scope=ModelWorkScope(),
        commitments=[_commitment(now)],
        ledger_rows=rows,
    )
    assert _item(result, KEY).state is EnumWorkItemState.AVAILABLE
    assert _item(result, KEY).lanes == ()


def test_a_commitment_of_a_paused_ticket_is_projected_paused(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(paused_tickets=("HOST-9",)),
        commitments=[_commitment(now, ticket="HOST-9")],
    )
    item = _item(result, KEY)
    assert item.scope is EnumScopeStatus.PAUSED
    assert item not in result.ready()


def test_the_effect_reads_a_commitment_older_than_the_ledger_window(
    rules, now, tmp_path
) -> None:
    """A projection read: a nine day old ask still shows as open work, though the lane rows window is 72 hours."""
    from tests.nodes.node_commitment_check_runner_effect.support import (
        ledger_rules,
        open_commitment_line,
    )

    old = open_commitment_line(minutes_ago=9 * 24 * 60)
    ledger = InMemoryLedger([old])
    request = build_request(
        now,
        tmp_path,
        {**ENV, "ONEX_WORK_STATE_PLANS_ROOT": str(tmp_path)},
        ledger,
        rules,
    )
    assert len(request.commitments) == 1
    result = HandlerWorkStateProjection().handle(request)
    (item,) = [i for i in result.items if i.kind is EnumWorkItemKind.COMMITMENT]
    assert item.state is EnumWorkItemState.AVAILABLE
    assert item in result.ready()
    # The same ledger folded directly names the same commitment: the source is the ledger, not a copy.
    rows = ledger.read(
        ModelLedgerQuery(
            since=now - timedelta(days=30),
            until=now + timedelta(days=1),
            row_type="STATUS",
        )
    ).rows
    folded, _ = fold_commitments(rows, ledger_rules())
    assert item.key == f"commitment:{folded[0].commitment_id}"


def test_an_unreadable_commitment_source_is_carried_as_unread_not_empty(
    rules, now, tmp_path
) -> None:
    ledger = InMemoryLedger([])
    ledger.read_error = "route-down"
    request = build_request(
        now,
        tmp_path,
        {**ENV, "ONEX_WORK_STATE_PLANS_ROOT": str(tmp_path)},
        ledger,
        rules,
    )
    assert any(u.startswith("ledger:") for u in request.unread)
    assert request.commitments == ()
