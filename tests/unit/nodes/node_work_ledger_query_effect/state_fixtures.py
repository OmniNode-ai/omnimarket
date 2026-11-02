# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures shared by the state-backed point query tests (OMN-21948)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerLaneActivity,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
    ModelWorkLedgerStateEntity,
    ModelWorkLedgerStatePage,
    ModelWorkLedgerStateQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)

NOW = datetime(2026, 11, 2, 3, 0, 0, tzinfo=UTC)
WATERMARK = datetime(2026, 11, 2, 2, 59, 40, tzinfo=UTC)
CORRELATION: UUID = uuid4()


def record(raw: str, row_id: str) -> ModelWorkLedgerRowRecord:
    stamp = datetime.strptime(raw[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return ModelWorkLedgerRowRecord(
        row_id=row_id,
        row_ts=stamp,
        row_type=raw.split(" | ")[1].strip(),
        row_lane=None,
        text=raw.split("\n", 1)[0],
        raw_row=raw,
        source="test",
        projected_at=stamp,
    )


HOLD_RAW = (
    "2026-11-01T10:00:00Z | HOLD | lane=orch-1 | id=hold-a | to=lane-x,lane-y "
    "| repo=omnimarket | pr=omnimarket#7 | surface=surface-a "
    "| until=2026-11-03T10:00:00Z | pause the surface"
)
MSG_RAW = (
    "2026-11-01T11:00:00Z | MSG | from=orch-1 | to=lane-x | id=msg-a "
    "| ticket=OMN-1 | please answer the question about the plan"
)
CLAIM_RAW = (
    "2026-11-01T09:00:00Z | CLAIM | lane=lane-x | ticket=OMN-21948 | host=h051 "
    "| run=r1 | scope=anything"
)
RULING_RAW = (
    "2026-11-02T01:00:00Z | RULING | lane=orch-1 | ticket=OMN-19609 "
    '| "Build now." | verbatim'
)


def hold_entity() -> ModelWorkLedgerStateEntity:
    return ModelWorkLedgerStateEntity(
        entity_key="hold:hold-a",
        kind="hold",
        lane="orch-1",
        repo="omnimarket",
        pr="omnimarket#7",
        scope_to="lane-x,lane-y",
        scope_surface="surface-a",
        until_at=datetime(2026, 11, 3, 10, tzinfo=UTC),
        opened_at=datetime(2026, 11, 1, 10, tzinfo=UTC),
        opened_row=record(HOLD_RAW, "row-hold-a"),
    )


def msg_entity() -> ModelWorkLedgerStateEntity:
    return ModelWorkLedgerStateEntity(
        entity_key="msg:msg-a",
        kind="msg",
        lane="orch-1",
        ticket="OMN-1",
        scope_to="lane-x",
        opened_at=datetime(2026, 11, 1, 11, tzinfo=UTC),
        opened_row=record(MSG_RAW, "row-msg-a"),
    )


def claim_entity() -> ModelWorkLedgerStateEntity:
    return ModelWorkLedgerStateEntity(
        entity_key="claim:lane-x",
        kind="claim",
        lane="lane-x",
        ticket="OMN-21948",
        opened_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
        opened_row=record(CLAIM_RAW, "row-claim-x"),
        activity=ModelWorkLedgerLaneActivity(
            rows=4,
            newest_row_ts=datetime(2026, 11, 2, 2, 30, tzinfo=UTC),
            newest_row_kind="STATUS",
            leases=("lease-1",),
        ),
    )


def ruling_entity() -> ModelWorkLedgerStateEntity:
    return ModelWorkLedgerStateEntity(
        entity_key="ruling:2026-11-02T01:00:00Z:orch-1",
        kind="ruling",
        lane="orch-1",
        ticket="OMN-19609",
        opened_at=datetime(2026, 11, 2, 1, tzinfo=UTC),
        opened_row=record(RULING_RAW, "row-ruling"),
    )


class FakeStateReader:
    """A state reader that serves one page, records its queries, or fails."""

    def __init__(
        self,
        entities: tuple[ModelWorkLedgerStateEntity, ...] = (),
        *,
        has_more: bool = False,
        total: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.entities = entities
        self.has_more = has_more
        self.total = total
        self.error = error
        self.queries: list[ModelWorkLedgerStateQuery] = []

    def read_state_page(
        self, *, query: ModelWorkLedgerStateQuery
    ) -> ModelWorkLedgerStatePage:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return ModelWorkLedgerStatePage(
            entities=self.entities,
            watermark=WATERMARK,
            has_more=self.has_more,
            total=len(self.entities) if self.total is None else self.total,
            ledger_newest=datetime(2026, 11, 2, 2, 59, 30, tzinfo=UTC),
        )

    def close(self) -> None:
        return None


def node(reader: FakeStateReader) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(
        reader=cast(ProtocolWorkLedgerQueryReader, reader), now=lambda: NOW
    )


def request(
    kind: EnumWorkLedgerQueryKind, **fields: object
) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {"correlation_id": CORRELATION, "query": kind, **fields}
    )
