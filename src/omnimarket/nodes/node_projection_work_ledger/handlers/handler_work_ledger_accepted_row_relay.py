# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox relay for the accepted-row topic: history once, live events held behind it (OMN-25630, L12).

The history up to the watermark ``H`` (the highest ``ledger_seq`` when the bootstrap starts) is published
once, marked ``bootstrap``, in the L3 ordering contract: rows without a sequence first by ``legacy_ordinal``,
then sequenced rows by ``ledger_seq``. Live events (judged after ``H``) are held until every bootstrap row is
on the partition, so no live event precedes a bootstrap event. The partition is the only state: a pass that
crashed in the middle is resumed by the next one, which reads what the topic holds and publishes only what is
missing, and a row already on the topic is never published again (consumers dedupe on ``row_id`` as a second
defence).
"""

from __future__ import annotations

from omnimarket.models.model_work_ledger_accepted_row import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_accepted_row_relay import (
    ModelAcceptedRowRelayRequest,
    ModelAcceptedRowRelayResult,
)
from omnimarket.protocols.protocol_work_ledger_accepted_row_partition import (
    ProtocolAcceptedRowPartition,
)


class AcceptedRowRelayError(ValueError):
    """A history or live row the relay refuses to put on the partition."""


def bootstrap_order_key(event: ModelAcceptedRowEvent) -> tuple[int, int]:
    """Unsequenced history first by ``legacy_ordinal``, then sequenced rows by ``ledger_seq``."""
    if event.ledger_seq is None:
        if event.legacy_ordinal is None:
            raise AcceptedRowRelayError(
                f"history row {event.row_id} carries neither ledger_seq nor legacy_ordinal"
            )
        return (0, event.legacy_ordinal)
    return (1, event.ledger_seq)


class HandlerWorkLedgerAcceptedRowRelay:
    """Publishes the bootstrap once, then releases the held live events in ``ledger_seq`` order."""

    def __init__(self, partition: ProtocolAcceptedRowPartition) -> None:
        self._partition = partition

    def handle(
        self, request: ModelAcceptedRowRelayRequest
    ) -> ModelAcceptedRowRelayResult:
        history, above = self._history_to_bootstrap(request)
        on_topic = {event.row_id: event.ledger_seq for event in self._partition.read()}

        published = already = 0
        for event in history:
            if event.row_id in on_topic:
                already += 1
                continue
            self._partition.publish(event)
            on_topic[event.row_id] = event.ledger_seq
            published += 1
        # Completeness is read back from the partition, never assumed from the publishes that returned.
        on_topic = {event.row_id: event.ledger_seq for event in self._partition.read()}
        complete = all(event.row_id in on_topic for event in history)

        live_published = duplicates = 0
        held: list[str] = []
        for event in self._live_in_order(request):
            if not complete:
                held.append(event.row_id)
            elif event.row_id in on_topic and (
                on_topic[event.row_id] is not None or event.ledger_seq is None
            ):
                duplicates += 1
            else:
                # A live twin of an unsequenced bootstrap row is the one live event that carries a sequence
                # the topic lacks: it is published, and the projection promotes the NULL sequence.
                self._partition.publish(event)
                on_topic[event.row_id] = event.ledger_seq
                live_published += 1
        return ModelAcceptedRowRelayResult(
            bootstrap_published=published,
            bootstrap_already_on_topic=already,
            bootstrap_complete=complete,
            live_published=live_published,
            live_duplicates_dropped=duplicates,
            live_held_row_ids=tuple(held),
            above_watermark_history_skipped=above,
        )

    @staticmethod
    def _history_to_bootstrap(
        request: ModelAcceptedRowRelayRequest,
    ) -> tuple[list[ModelAcceptedRowEvent], int]:
        kept: dict[str, ModelAcceptedRowEvent] = {}
        above = 0
        for event in request.history:
            if event.source is not EnumAcceptedRowSource.BOOTSTRAP:
                raise AcceptedRowRelayError(
                    f"history row {event.row_id} is not marked source=bootstrap"
                )
            if event.ledger_seq is not None and event.ledger_seq > request.watermark:
                # Written after the watermark was read: it is a live event and arrives through the live leg.
                above += 1
                continue
            kept.setdefault(event.row_id, event)
        return sorted(kept.values(), key=bootstrap_order_key), above

    @staticmethod
    def _live_in_order(
        request: ModelAcceptedRowRelayRequest,
    ) -> list[ModelAcceptedRowEvent]:
        unique: dict[str, ModelAcceptedRowEvent] = {}
        for event in request.live:
            if event.source is not EnumAcceptedRowSource.LIVE:
                raise AcceptedRowRelayError(
                    f"live row {event.row_id} is marked source={event.source.value}"
                )
            if event.ledger_seq is None:
                raise AcceptedRowRelayError(
                    f"live row {event.row_id} has no ledger_seq: it was never judged"
                )
            unique.setdefault(event.row_id, event)
        return sorted(unique.values(), key=lambda e: (e.ledger_seq or 0, e.row_id))


__all__: list[str] = [
    "AcceptedRowRelayError",
    "HandlerWorkLedgerAcceptedRowRelay",
    "bootstrap_order_key",
]
