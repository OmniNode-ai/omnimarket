"""Control: the recorded 73 MB case against the node as it stood (no fix)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from tests.test_golden_chain_work_ledger_query import FakeReader

NOW = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)


def _records(lanes: int, chars: int) -> tuple[ModelWorkLedgerRowRecord, ...]:
    out = []
    for i in range(lanes):
        ts = NOW - timedelta(seconds=lanes - i)
        raw = (
            f"{ts:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=l-{i:05d} | ticket=OMN-22478 | "
            + "x" * chars
        )
        out.append(
            ModelWorkLedgerRowRecord(
                row_id=work_ledger_row_id(raw),
                row_ts=ts,
                row_type="STATUS",
                row_lane=f"l-{i:05d}",
                text=raw,
                raw_row=raw,
                source="s",
                projected_at=ts,
            )
        )
    return tuple(out)


def _ask(**f: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "query": EnumWorkLedgerQueryKind.NEWEST_PER_LANE,
            "now": NOW,
            **f,
        }
    )


def test_limit_is_honoured() -> None:
    node = HandlerWorkLedgerQuery(reader=FakeReader(_records(10, 10)), now=lambda: NOW)
    assert len(node.handle(_ask(limit=3)).rows) == 3


def test_recorded_case_answer_stays_under_the_broker_limit() -> None:
    node = HandlerWorkLedgerQuery(
        reader=FakeReader(_records(3000, 12000)), now=lambda: NOW
    )
    size = len(node.handle(_ask()).model_dump_json().encode())
    assert size < 1_048_588, size
