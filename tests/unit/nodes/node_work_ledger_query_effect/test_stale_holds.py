# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale HOLD is not in force (cutover plan section 13, answer 2; OMN-22478 piece 6).

An ``until=open`` HOLD with no RELEASE older than the contract's hold_stale_after_days is printed
under a stale heading with its age, and neither holds nor inbox counts it. Measured 2026-11-06:
the dev query node answered 70 holds in force, open-ended holds from 2026-09-28 among them, where
the file verb read none, so moving the hold and inbox readers to the node would have turned every
lane's CLEAR into HELD.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerStateEntity,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_state_answer import (
    load_hold_stale_after,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    msg_entity,
    node,
    record,
    request,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
STALE_AFTER = load_hold_stale_after(CONTRACT)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_hold(
    key: str, age: timedelta, until: str | None = None
) -> ModelWorkLedgerStateEntity:
    opened = NOW - age
    until_cell = f" | until={until}" if until else ""
    raw = (
        f"{_stamp(opened)} | HOLD | lane=orch-1 | id={key} | to=lane-x | repo=omnimarket"
        f"{until_cell} | hold the lane"
    )
    return ModelWorkLedgerStateEntity(
        entity_key=f"hold:{key}",
        kind="hold",
        lane="orch-1",
        repo="omnimarket",
        scope_to="lane-x",
        until_at=datetime.fromisoformat(until.replace("Z", "+00:00"))
        if until
        else None,
        opened_at=opened,
        opened_row=record(raw, f"row-{key}"),
    )


def test_the_contract_bound_is_fourteen_days() -> None:
    assert timedelta(days=14) == STALE_AFTER


def test_failing_control_an_open_hold_past_the_bound_is_stale_not_in_force() -> None:
    old = open_hold("hold-old", STALE_AFTER + timedelta(days=1))
    young = open_hold("hold-young", STALE_AFTER - timedelta(days=1))
    result = node(FakeStateReader((old, young))).handle(request(K.HOLDS))
    assert result.status == "ok"
    assert [h.hold_id for h in result.holds] == ["hold-young"]
    assert result.matched == 1
    assert result.lines[0].startswith("HOLD hold-young by=orch-1")
    assert result.lines[1] == (
        "HOLD stale=1: until=open, no RELEASE, older than 14d; not in force"
    )
    assert result.lines[2] == "  STALE hold-old by=orch-1 age=360h00m db:row-hold-old"


def test_only_stale_holds_read_none_in_force_with_the_stale_listed() -> None:
    old = open_hold("hold-old", timedelta(days=39))
    result = node(FakeStateReader((old,))).handle(request(K.HOLDS))
    assert result.holds == ()
    assert result.lines[0].startswith("HOLD none checked=")
    assert "stale=1" in result.lines[1]


def test_a_hold_with_an_end_stamp_is_never_stale_by_age() -> None:
    until = _stamp(NOW + timedelta(days=3))
    dated = open_hold("hold-dated", timedelta(days=20), until=until)
    result = node(FakeStateReader((dated,))).handle(request(K.HOLDS))
    assert [h.hold_id for h in result.holds] == ["hold-dated"]
    assert len(result.lines) == 1


def test_inbox_does_not_count_a_stale_hold_and_says_how_many_it_left_out() -> None:
    old = open_hold("hold-old", timedelta(days=39))
    result = node(FakeStateReader((old, msg_entity()))).handle(
        request(K.INBOX, lanes=("lane-x",))
    )
    assert result.matched == 1
    assert result.lines[0].startswith(
        "inbox lane-x: 1 open, 1 need action (HOLD:0 ask:1)"
    )
    assert result.lines[-1] == (
        "  HOLD stale=1: until=open, no RELEASE, older than 14d; not in force"
    )
    assert [e.row.row_id for e in result.inbox[0].entries] == ["row-msg-a"]


def test_a_contract_without_the_bound_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "contract.yaml"
    broken.write_text("work_ledger_query: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hold_stale_after_days"):
        load_hold_stale_after(broken)
