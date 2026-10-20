# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work ledger on the in-memory event bus, for a customer with no lab (OMN-17427).

RULING 2026-10-19T21:47:42Z: a customer running OmniNode locally has no rolling
markdown ledger and no lab Postgres, and that is no reason to have no ledger --
the ledger is a dependency like any other and the in-memory event bus stands in
for the lab bus. The ledger's row events already travel as typed events; this
module holds two pieces, the reader and ``attach``/``ledger_row_topics``, that
work with the emitter to close the loop without a broker or a database:

- ``InMemoryBusEmitter`` in
  ``omnimarket.nodes.node_event_emit_effect.handlers.inmemory_bus_emitter`` is
  the ``ProtocolEventEmitter`` that ``HandlerWorkLedgerEmit`` takes: it publishes
  a row's event to the bus topic the event registry names, instead of the Kafka spool.
- ``InMemoryWorkLedgerQueryReader`` is the ``ProtocolWorkLedgerQueryReader`` that
  ``HandlerWorkLedgerQuery`` takes: it folds the row events it hears into the
  records the Postgres reader would return, so every query verb (rows, holds,
  inbox, open claims, rulings) answers the same way.
- ``attach`` subscribes the reader to every ledger row topic on a bus.

``raw_row`` is the truth of an event (see ``model_ledger_row_event``), so the
reader rebuilds a record from ``raw_row`` alone and ignores the typed fields.
A redelivered row (same ``row_id``) is kept once.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.handlers.work_ledger_text import ledger_row_stamp
from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord

_DEFAULT_GROUP = "work-ledger-inmemory-reader"


class ProtocolLedgerBus(Protocol):
    """The slice of the event bus the local ledger uses (``EventBusInmemory`` has it)."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]: ...


def ledger_row_topics() -> tuple[str, ...]:
    """One topic per canonical row type, as the event registry declares them."""
    return tuple(row_type.topic for row_type in EnumLedgerRowType)


def _lane_of(raw_row: str) -> str | None:
    for cell in raw_row.split("\n", 1)[0].split(" | ")[2:]:
        if cell.startswith("lane="):
            return cell[len("lane=") :].split()[0]
    return None


class InMemoryWorkLedgerQueryReader:
    """Serves the work ledger from the row events heard on an in-memory bus."""

    def __init__(
        self,
        *,
        parity_receipt_lane: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._records: dict[str, ModelWorkLedgerRowRecord] = {}
        self._parity_receipt_lane = parity_receipt_lane
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def __len__(self) -> int:
        return len(self._records)

    def ingest(self, payload: Mapping[str, object]) -> bool:
        """Fold one row event payload in; False for a redelivery or a payload with no row."""
        raw_row = payload.get("raw_row")
        row_id = payload.get("row_id")
        if not isinstance(raw_row, str) or not raw_row.strip():
            return False
        if not isinstance(row_id, str) or row_id in self._records:
            return False
        raw_row = raw_row.strip()
        row_type = payload.get("row_type")
        _, _, rest = raw_row.partition(" | ")
        self._records[row_id] = ModelWorkLedgerRowRecord(
            row_id=row_id,
            row_ts=ledger_row_stamp(raw_row),
            row_type=str(row_type)
            if isinstance(row_type, str)
            else rest.split(" | ", 1)[0].strip(),
            row_lane=_lane_of(raw_row),
            text=raw_row.split("\n", 1)[0],
            raw_row=raw_row,
            source=str(payload.get("source") or "onex-ledger"),
            projected_at=self._now(),
        )
        return True

    async def on_message(self, message: Any) -> None:
        """The bus callback: decode the event's JSON value and fold it in."""
        decoded = json.loads(message.value)
        if isinstance(decoded, dict):
            payload = decoded.get("payload")
            self.ingest(payload if isinstance(payload, dict) else decoded)

    async def attach(
        self, bus: ProtocolLedgerBus, *, group_id: str = _DEFAULT_GROUP
    ) -> Callable[[], Awaitable[None]]:
        """Subscribe to every ledger row topic; the returned call unsubscribes all."""
        unsubscribers = [
            await bus.subscribe(topic, on_message=self.on_message, group_id=group_id)
            for topic in ledger_row_topics()
        ]

        async def unsubscribe() -> None:
            for undo in unsubscribers:
                await undo()

        return unsubscribe

    def _ordered(self) -> list[ModelWorkLedgerRowRecord]:
        return sorted(
            self._records.values(),
            key=lambda r: (r.row_ts, r.projected_at or r.row_ts, r.row_id),
        )

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            r
            for r in self._ordered()
            if (since is None or r.row_ts >= since) and r.row_ts <= until
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        """No parity: the bus is the only copy, so there is no markdown ledger to measure against."""
        if self._parity_receipt_lane is None:
            return ()
        return tuple(
            r
            for r in self._ordered()
            if r.row_type == "STATUS"
            and r.row_lane == self._parity_receipt_lane
            and since <= r.row_ts <= until
        )

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        shown = [r for r in self._records.values() if r.row_ts <= until]
        if not shown:
            return None, None
        return max(r.row_ts for r in shown), max(
            r.projected_at for r in shown if r.projected_at is not None
        )


__all__: list[str] = [
    "InMemoryWorkLedgerQueryReader",
    "ProtocolLedgerBus",
    "ledger_row_topics",
]
