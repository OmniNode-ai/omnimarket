# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of omnimarket#5245 on the stale-hold rule (OMN-23069).

Finding 1: a reader without ``read_state_page`` (the row-replay path, the in-memory reader among
them) dropped stale holds from ``holds`` and ``inbox`` without listing them or saying how many
were left out. Finding 2: the state-backed partition of live and stale holds compared Pydantic
models pairwise, so an all-stale page cost quadratic equality calls.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerHoldInForce,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    node,
    request,
)
from tests.unit.nodes.node_work_ledger_query_effect.test_stale_holds import (
    STALE_AFTER,
    open_hold,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
FIFTEEN_DAYS = timedelta(days=15)


def _stamp(age: timedelta) -> str:
    return (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ")


def _replay_node(*raw_rows: str) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: NOW)
    for number, raw in enumerate(raw_rows):
        assert reader.ingest({"raw_row": raw, "row_id": f"row-{number}"})
    return HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, reader), now=lambda: NOW
    )


def _old_hold_row() -> str:
    return (
        f"{_stamp(FIFTEEN_DAYS)} | HOLD | lane=orch-1 | id=hold-old | to=lane-x "
        "| repo=omnimarket | hold the lane"
    )


def _young_hold_row() -> str:
    return (
        f"{_stamp(timedelta(days=1))} | HOLD | lane=orch-1 | id=hold-young | to=lane-x "
        "| repo=omnimarket | hold the lane"
    )


def _msg_row() -> str:
    return (
        f"{_stamp(timedelta(hours=2))} | MSG | from=orch-1 | to=lane-x | id=msg-a "
        "| ticket=OMN-1 | please answer the question about the plan"
    )


def test_row_replay_holds_lists_the_stale_hold_with_its_age() -> None:
    result = _replay_node(_old_hold_row()).handle(request(K.HOLDS))
    assert result.status == "ok"
    assert result.holds == ()
    assert result.lines[0].startswith("HOLD none checked=")
    assert result.lines[1] == (
        "HOLD stale=1: until=open, no RELEASE, older than 14d; not in force"
    )
    assert result.lines[2] == "  STALE hold-old by=orch-1 age=360h00m db:row-0"


def test_row_replay_holds_keeps_the_live_hold_beside_the_stale_one() -> None:
    result = _replay_node(_old_hold_row(), _young_hold_row()).handle(request(K.HOLDS))
    assert [h.hold_id for h in result.holds] == ["hold-young"]
    assert result.matched == 1
    assert result.lines[0].startswith("HOLD hold-young by=orch-1")
    assert "stale=1" in result.lines[1]
    assert "STALE hold-old" in result.lines[2]


def test_row_replay_inbox_says_how_many_stale_holds_it_left_out() -> None:
    result = _replay_node(_old_hold_row(), _msg_row()).handle(
        request(K.INBOX, lanes=("lane-x",))
    )
    assert result.matched == 1
    assert result.lines[0].startswith(
        "inbox lane-x: 1 open, 1 need action (HOLD:0 ask:1)"
    )
    assert result.lines[-1] == (
        "  HOLD stale=1: until=open, no RELEASE, older than 14d; not in force"
    )


def test_state_and_row_replay_paths_answer_the_stale_hold_alike() -> None:
    replayed = _replay_node(_old_hold_row()).handle(request(K.HOLDS))
    stated = node(FakeStateReader((open_hold("hold-old", FIFTEEN_DAYS),))).handle(
        request(K.HOLDS)
    )
    assert FIFTEEN_DAYS > STALE_AFTER
    assert replayed.lines[1] == stated.lines[1]
    assert replayed.lines[2].split(" db:")[0] == stated.lines[2].split(" db:")[0]


def test_state_hold_partition_makes_no_pairwise_model_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons = 0
    original = ModelWorkLedgerHoldInForce.__eq__

    def counting(self: ModelWorkLedgerHoldInForce, other: object) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(self, other)

    monkeypatch.setattr(ModelWorkLedgerHoldInForce, "__eq__", counting)
    page = tuple(open_hold(f"hold-{n}", FIFTEEN_DAYS) for n in range(200))
    result = node(FakeStateReader(page)).handle(request(K.HOLDS, page_size=200))
    assert result.status == "ok"
    assert result.holds == ()
    assert comparisons == 0
