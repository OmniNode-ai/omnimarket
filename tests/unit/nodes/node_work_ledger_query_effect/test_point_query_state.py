# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State-backed point queries of the work ledger read node (OMN-21948)."""

from __future__ import annotations

import base64
import json

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    CORRELATION,
    HOLD_RAW,
    NOW,
    WATERMARK,
    FakeStateReader,
    claim_entity,
    hold_entity,
    msg_entity,
    node,
    record,
    request,
    ruling_entity,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
POINT_KINDS = (K.HOLDS, K.INBOX, K.OPEN_CLAIMS, K.OPEN_RULINGS)


def _extras(kind: EnumWorkLedgerQueryKind) -> dict[str, object]:
    if kind is K.INBOX:
        return {"lanes": ("lane-x",)}
    if kind is K.OPEN_RULINGS:
        return {"ticket": "OMN-19609"}
    return {}


def _entities(kind: EnumWorkLedgerQueryKind):
    return {
        K.HOLDS: (hold_entity(),),
        K.INBOX: (hold_entity(), msg_entity()),
        K.OPEN_CLAIMS: (claim_entity(),),
        K.OPEN_RULINGS: (ruling_entity(),),
    }[kind]


def test_point_query_holds_answers_the_verb_line_from_state() -> None:
    reader = FakeStateReader((hold_entity(),))
    result = node(reader).handle(request(K.HOLDS))
    assert result.status == "ok"
    assert result.error is None
    assert result.matched == 1
    assert result.holds[0].hold_id == "hold-a"
    assert result.holds[0].by == "orch-1"
    assert result.lines == (
        "HOLD hold-a by=orch-1 scope=<to=lane-x,lane-y;repo=omnimarket;"
        "pr=omnimarket#7;surface=surface-a> until=2026-11-03T10:00:00Z "
        "db:row-hold-a",
    )


def test_point_query_holds_empty_set_prints_the_none_line_with_counts() -> None:
    reader = FakeStateReader(())
    result = node(reader).handle(request(K.HOLDS))
    assert result.status == "ok"
    assert result.matched == 0
    assert result.lines == ("HOLD none checked=0 released=0 expired=0 legacy-no-id=0",)


def test_point_query_holds_pass_the_filters_to_the_state_read() -> None:
    reader = FakeStateReader((hold_entity(),))
    node(reader).handle(
        request(K.HOLDS, pr="omnimarket#7", repo="omnimarket", surface="s", to="lane-x")
    )
    query = reader.queries[0]
    assert query.point is K.HOLDS
    assert (query.pr, query.repo, query.surface, query.to) == (
        "omnimarket#7",
        "omnimarket",
        "s",
        "lane-x",
    )
    assert query.now == NOW


def test_point_query_inbox_prints_the_summary_and_the_action_lines() -> None:
    reader = FakeStateReader((hold_entity(), msg_entity()))
    result = node(reader).handle(request(K.INBOX, lanes=("lane-x",)))
    assert result.status == "ok"
    assert result.matched == 2
    assert result.inbox[0].lane == "lane-x"
    assert result.lines == (
        "inbox lane-x: 2 open, 2 need action (HOLD:1 ask:1), 0 FYI, "
        "newest=2026-11-01T11:00:00Z",
        "  HOLD id=hold-a from=orch-1 db:row-hold-a: pause the surface",
        "  MSG id=msg-a from=orch-1 db:row-msg-a: "
        "please answer the question about the plan",
    )
    assert reader.queries[0].lanes == ("lane-x",)


def test_point_query_inbox_for_a_lane_with_nothing_open_says_so() -> None:
    reader = FakeStateReader(())
    result = node(reader).handle(request(K.INBOX, lane="lane-z"))
    assert result.status == "ok"
    assert result.lines == (
        "inbox lane-z: 0 open, 0 need action (HOLD:0 ask:0), 0 FYI, newest=none",
    )


def test_point_query_open_claims_prints_the_header_and_the_claim_line() -> None:
    reader = FakeStateReader((claim_entity(),), total=3)
    result = node(reader).handle(request(K.OPEN_CLAIMS))
    assert result.status == "ok"
    assert result.matched == 1
    assert result.open_claims[0].lane == "lane-x"
    assert result.open_claims[0].rows == 4
    assert result.open_claims[0].leases == ("lease-1",)
    assert result.lines == (
        "open-claims since=beginning claims=3 open=1 "
        "ledger-newest=2026-11-02T02:59:30Z now=2026-11-02T03:00:00Z",
        "OPEN-CLAIM lane=lane-x claim=2026-11-01T09:00:00Z db:row-claim-x "
        "ticket=OMN-21948 newest-row=2026-11-02T02:30:00Z kind=STATUS "
        "age=0h30m rows=4 host=h051 run=r1 leases=lease-1 liveness=unknown",
    )


def test_point_query_open_rulings_prints_the_count_and_stamps() -> None:
    reader = FakeStateReader((ruling_entity(),))
    result = node(reader).handle(request(K.OPEN_RULINGS, ticket="OMN-19609"))
    assert result.status == "ok"
    assert result.matched == 1
    assert result.lines == (
        "OPEN-RULINGS ticket=OMN-19609 n=1 stamps=2026-11-02T01:00:00Z",
    )
    assert reader.queries[0].ticket == "OMN-19609"


def test_point_query_open_rulings_none_prints_none() -> None:
    result = node(FakeStateReader(())).handle(
        request(K.OPEN_RULINGS, ticket="OMN-19609")
    )
    assert result.lines == ("OPEN-RULINGS ticket=OMN-19609 n=0 stamps=none",)


def test_point_query_open_rulings_requires_a_ticket() -> None:
    with pytest.raises(ValueError, match="ticket"):
        ModelWorkLedgerQueryRequest(correlation_id=CORRELATION, query=K.OPEN_RULINGS)


# ---- AC2: a database fault is status=error, never an empty page ----


@pytest.mark.parametrize("kind", POINT_KINDS)
@pytest.mark.parametrize(
    "fault",
    [
        WorkLedgerReadError("OperationalError: connection refused"),
        RuntimeError("database offline"),
    ],
    ids=["read-error", "unexpected"],
)
def test_point_query_fault_answers_status_error_never_an_empty_page(
    kind: EnumWorkLedgerQueryKind, fault: Exception
) -> None:
    reader = FakeStateReader(_entities(kind), error=fault)
    result = node(reader).handle(request(kind, **_extras(kind)))
    assert result.status == "error"
    assert result.error
    assert result.matched == 0
    assert result.lines == ()
    assert result.holds == ()
    assert result.inbox == ()
    assert result.open_claims == ()
    assert result.rows == ()
    assert result.next_cursor is None
    assert result.watermark is None


def test_point_query_fault_message_carries_the_exception_type() -> None:
    reader = FakeStateReader((), error=RuntimeError("database offline"))
    result = node(reader).handle(request(K.HOLDS))
    assert result.error == "RuntimeError: database offline"


def test_fault_malformed_cursor_is_an_error_not_a_first_page() -> None:
    reader = FakeStateReader((hold_entity(),))
    result = node(reader).handle(request(K.HOLDS, cursor="not-a-cursor"))
    assert result.status == "error"
    assert result.lines == ()
    assert reader.queries == []


def test_fault_invalid_claims_since_is_an_error() -> None:
    reader = FakeStateReader((claim_entity(),))
    result = node(reader).handle(request(K.OPEN_CLAIMS, claims_since="yesterday"))
    assert result.status == "error"
    assert reader.queries == []


def test_fault_the_database_error_path_closes_the_reader() -> None:
    closed: list[bool] = []

    class Closing(FakeStateReader):
        def close(self) -> None:
            closed.append(True)

    reader = Closing((), error=RuntimeError("down"))
    node(reader).handle(request(K.HOLDS))
    assert closed == [True]


def test_fault_status_is_ok_only_without_an_error() -> None:
    result = node(FakeStateReader(())).handle(request(K.HOLDS))
    assert (result.status, result.error) == ("ok", None)


# ---- AC3: every point-query answer carries a page size and a watermark ----


@pytest.mark.parametrize("kind", POINT_KINDS)
def test_page_and_watermark_ride_every_point_query_answer(
    kind: EnumWorkLedgerQueryKind,
) -> None:
    reader = FakeStateReader(_entities(kind))
    result = node(reader).handle(request(kind, **_extras(kind)))
    assert result.page_size == 500
    assert result.watermark == WATERMARK
    assert reader.queries[0].limit == 500


@pytest.mark.parametrize("kind", POINT_KINDS)
def test_page_and_watermark_ride_an_empty_answer_too(
    kind: EnumWorkLedgerQueryKind,
) -> None:
    result = node(FakeStateReader(())).handle(request(kind, **_extras(kind)))
    assert result.status == "ok"
    assert result.page_size == 500
    assert result.watermark == WATERMARK
    assert result.next_cursor is None


def test_page_size_is_taken_from_the_request() -> None:
    reader = FakeStateReader((hold_entity(),))
    result = node(reader).handle(request(K.HOLDS, page_size=25))
    assert result.page_size == 25
    assert reader.queries[0].limit == 25


def test_page_size_above_the_cap_is_refused_by_the_request_model() -> None:
    with pytest.raises(ValueError, match="page_size"):
        request(K.HOLDS, page_size=5001)
    with pytest.raises(ValueError, match="page_size"):
        request(K.HOLDS, page_size=0)


def test_page_more_rows_yield_a_cursor_that_resumes_after_the_last_entity() -> None:
    reader = FakeStateReader((hold_entity(),), has_more=True)
    first = node(reader).handle(request(K.HOLDS, page_size=1))
    assert first.next_cursor
    decoded = json.loads(base64.urlsafe_b64decode(first.next_cursor + "=="))
    assert decoded["k"] == "hold:hold-a"
    second_reader = FakeStateReader(())
    node(second_reader).handle(request(K.HOLDS, page_size=1, cursor=first.next_cursor))
    after = second_reader.queries[0].after
    assert after is not None
    assert (after.entity_key, after.opened_at) == (
        "hold:hold-a",
        hold_entity().opened_at,
    )


def test_page_last_page_has_no_cursor() -> None:
    result = node(FakeStateReader((hold_entity(),), has_more=False)).handle(
        request(K.HOLDS, page_size=1)
    )
    assert result.next_cursor is None


def test_page_cursor_is_refused_on_a_cursor_for_another_query_kind() -> None:
    reader = FakeStateReader((hold_entity(),), has_more=True)
    cursor = node(reader).handle(request(K.HOLDS, page_size=1)).next_cursor
    result = node(FakeStateReader(())).handle(request(K.OPEN_CLAIMS, cursor=cursor))
    assert result.status == "error"


def test_watermark_is_the_state_projection_time_not_the_wall_clock() -> None:
    result = node(FakeStateReader(())).handle(request(K.HOLDS))
    assert result.watermark == WATERMARK
    assert result.watermark != NOW
    assert result.freshness is None


def test_point_query_inbox_requires_a_lane() -> None:
    with pytest.raises(ValueError, match="lane"):
        ModelWorkLedgerQueryRequest(correlation_id=CORRELATION, query=K.INBOX)


def test_point_query_open_rulings_without_a_state_reader_is_an_error() -> None:
    from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
        InMemoryWorkLedgerQueryReader,
    )

    handler = HandlerWorkLedgerQuery(
        reader=InMemoryWorkLedgerQueryReader(), now=lambda: NOW
    )
    result = handler.handle(request(K.OPEN_RULINGS, ticket="OMN-19609"))
    assert result.status == "error"
    assert result.lines == ()


def test_point_query_an_opening_row_that_is_not_a_ledger_row_is_an_error_not_a_short_page() -> (
    None
):
    good = record(HOLD_RAW, "row-bad")
    broken = hold_entity().model_copy(
        update={
            "opened_row": good.model_copy(
                update={"text": "not a ledger row", "raw_row": "not a ledger row"}
            ),
        }
    )
    result = node(FakeStateReader((broken,))).handle(request(K.HOLDS))
    assert result.status == "error"
    assert "hold:hold-a" in (result.error or "")
    assert result.holds == ()


def test_point_query_page_size_rides_the_error_answer_but_no_watermark() -> None:
    result = node(FakeStateReader((), error=RuntimeError("down"))).handle(
        request(K.HOLDS, page_size=7)
    )
    assert result.page_size == 7
    assert result.watermark is None
