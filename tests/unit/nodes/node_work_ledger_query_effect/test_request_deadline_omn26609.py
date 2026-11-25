# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answerer drops a request whose caller's deadline has passed (OMN-26609).

The incident: on 2026-11-23 the dev answerer's consumer group held thousands of read requests
(open-claims, holds and rows reads from the pr-create guard, the create gate and the pool
snapshot) whose callers had stopped waiting about 121 s after sending. The answerer worked every
request in order, whatever its age, at about one per second, so each new read waited behind a
backlog of dead ones for as long as the backlog took to drain. A sample of the queue read 700
requests, every one a read, every one with ``timeout_seconds: null`` on its envelope and no
deadline anywhere in its payload.

The fixture ``incident_stale_query_requests.jsonl`` holds twelve of those recorded requests
(payload and envelope time, as read from the topic at offsets 225221 and after).

* an expired request never reaches the query work (the reader is not touched);
* a live request still answers;
* a request with no deadline keeps today's behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    CORRELATION,
    NOW,
    FakeStateReader,
    claim_entity,
    request,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "incident_stale_query_requests.jsonl"
# What the caller (the claims reader) waited: the bus read's timeout in the dev overlay.
CALLER_WAIT_S = 121


class UntouchableReader:
    """Any read through this reader means the answerer did the query work."""

    reads = 0

    def __getattr__(self, name: str) -> Any:
        UntouchableReader.reads += 1
        raise AssertionError(f"the query work reached the reader: {name}")


def _node_over(reader: object) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, reader), now=lambda: NOW
    )


def _recorded() -> list[tuple[datetime, dict[str, Any]]]:
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line]
    assert rows, "the recorded incident fixture must not be empty"
    return [
        (
            datetime.fromisoformat(r["envelope_timestamp"].replace("Z", "+00:00")),
            r["payload"],
        )
        for r in rows
    ]


def test_expired_request_does_not_reach_the_query_work() -> None:
    reader = FakeStateReader((claim_entity(),))
    node = _node_over(reader)
    expired = request(
        EnumWorkLedgerQueryKind.OPEN_CLAIMS, deadline=NOW - timedelta(seconds=1)
    )
    answer = node.handle(expired)
    assert reader.queries == []
    assert answer.correlation_id == CORRELATION
    assert answer.status == "error"
    assert answer.error is not None
    assert answer.error.startswith("deadline_expired")
    assert answer.open_claims == ()


def test_request_due_exactly_now_is_expired() -> None:
    reader = FakeStateReader((claim_entity(),))
    answer = _node_over(reader).handle(
        request(EnumWorkLedgerQueryKind.OPEN_CLAIMS, deadline=NOW)
    )
    assert reader.queries == []
    assert answer.status == "error"


def test_live_request_still_answers() -> None:
    reader = FakeStateReader((claim_entity(),))
    answer = _node_over(reader).handle(
        request(
            EnumWorkLedgerQueryKind.OPEN_CLAIMS, deadline=NOW + timedelta(seconds=30)
        )
    )
    assert len(reader.queries) == 1
    assert answer.status == "ok"
    assert answer.error is None


def test_request_with_no_deadline_keeps_todays_behaviour() -> None:
    reader = FakeStateReader((claim_entity(),))
    answer = _node_over(reader).handle(request(EnumWorkLedgerQueryKind.OPEN_CLAIMS))
    assert len(reader.queries) == 1
    assert answer.status == "ok"


def test_a_deadline_without_a_utc_offset_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ModelWorkLedgerQueryRequest.model_validate(
            {
                "correlation_id": str(CORRELATION),
                "query": "open-claims",
                "deadline": "2026-11-02T03:00:00",
            }
        )


def test_recorded_incident_backlog_never_reaches_the_query_work() -> None:
    """Each recorded request, stamped with the deadline its caller waited to, read 40 minutes late."""
    recorded = _recorded()
    last_sent = max(sent for sent, _ in recorded)
    late = _node_over_clock(UntouchableReader(), last_sent + timedelta(minutes=40))
    for sent, payload in recorded:
        stamped = ModelWorkLedgerQueryRequest.model_validate(
            {
                **payload,
                "deadline": (sent + timedelta(seconds=CALLER_WAIT_S)).isoformat(),
            }
        )
        answer = late.handle(stamped)
        assert answer.status == "error"
        assert answer.error is not None
        assert answer.error.startswith("deadline_expired")
    assert UntouchableReader.reads == 0


def test_recorded_incident_requests_answer_while_their_callers_still_wait() -> None:
    """The control: the same recorded requests, read two seconds after sending, are worked."""
    recorded = _recorded()
    for sent, payload in recorded:
        reader = FakeStateReader((claim_entity(),))
        node = _node_over_clock(reader, sent + timedelta(seconds=2))
        stamped = ModelWorkLedgerQueryRequest.model_validate(
            {
                **payload,
                "deadline": (sent + timedelta(seconds=CALLER_WAIT_S)).isoformat(),
            }
        )
        answer = node.handle(stamped)
        assert not (answer.error or "").startswith("deadline_expired"), payload["query"]


def _node_over_clock(reader: object, at: datetime) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, reader),
        now=lambda: at.astimezone(UTC),
    )
