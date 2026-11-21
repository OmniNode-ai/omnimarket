# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rollback command: replay the accepted-row topic into the 22 old row topics (OMN-25630, L12).

Until L15 the old topics are still published, so a rollback has live publishers; this command refills any
gap, or the whole family after a broker loss. Each distinct row goes to the one old topic of its row type
and version, once: a row first emitted typed goes to the ``.v2`` topic of its type with its typed envelope,
every other row to the ``.v1`` topic with the row envelope the projection reads. A row seen twice on the
accepted-row topic (a bootstrap row and its live twin) is one row, and a row an old topic already holds is not
written again, so a consumer of an old topic sees every row once however often the command runs. The 22 topic
names are read from the node contract's rollback block and spelled nowhere in this file.
"""

from __future__ import annotations

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import work_ledger_event_id
from omnimarket.handlers.work_ledger_fold import WorkLedgerFoldError, fold_row
from omnimarket.models.model_work_ledger_accepted_row import ModelAcceptedRowEvent
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    ROLLBACK_ROW_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_accepted_row_relay import (
    ModelRepublishedRow,
    ModelRepublishRequest,
    ModelRepublishResult,
)
from omnimarket.protocols.protocol_work_ledger_accepted_row_partition import (
    ProtocolOldRowTopics,
)


class RepublishRefusedError(ValueError):
    """An accepted row that cannot be written to any old topic."""


def old_topic_of(event: ModelAcceptedRowEvent) -> str:
    """The one old row topic this accepted row belongs on, from its row type and its first emission."""
    try:
        folded = fold_row(
            ModelWorkLedgerFoldRequest(
                raw_row=event.raw_row,
                ledger_id=event.ledger_id,
                source=event.source.value,
                ledger_seq=event.ledger_seq,
            )
        )
        row_type = EnumLedgerRowType(folded.row.row_type)
    except (WorkLedgerFoldError, ValueError) as exc:
        raise RepublishRefusedError(
            f"accepted row {event.row_id} has no old row topic: {exc}"
        ) from exc
    topic = row_type.typed_topic if event.typed is not None else row_type.topic
    if topic not in ROLLBACK_ROW_TOPICS:
        raise RepublishRefusedError(
            f"topic {topic} of row {event.row_id} is not one of the contract's rollback topics"
        )
    return topic


def old_topic_payload(event: ModelAcceptedRowEvent) -> dict[str, object]:
    if event.typed is not None:
        return event.typed.model_dump(mode="json")
    payload: dict[str, object] = {
        "ledger_id": event.ledger_id,
        "row_id": event.row_id,
        "raw_row": event.raw_row,
        "source": event.source.value,
        "event_id": str(work_ledger_event_id(event.ledger_id, event.row_id)),
    }
    if event.ledger_seq is not None:
        payload["ledger_seq"] = event.ledger_seq
    return payload


class HandlerWorkLedgerRepublishOldTopics:
    """Replays accepted-row events, in topic order, into the old row topics; once per row and topic."""

    def __init__(self, old_topics: ProtocolOldRowTopics) -> None:
        self._old_topics = old_topics

    def handle(self, request: ModelRepublishRequest) -> ModelRepublishResult:
        distinct: dict[str, ModelAcceptedRowEvent] = {}
        duplicates = 0
        for event in request.events:
            held = distinct.get(event.row_id)
            if held is None:
                distinct[event.row_id] = event
                continue
            duplicates += 1
            if held.ledger_seq is None and event.ledger_seq is not None:
                # The sequenced live twin of an unsequenced bootstrap row carries the sequence old consumers need.
                distinct[event.row_id] = event.model_copy(
                    update={"typed": event.typed or held.typed}
                )
        present: dict[str, frozenset[str]] = {}
        published: list[ModelRepublishedRow] = []
        already = 0
        for event in distinct.values():
            topic = old_topic_of(event)
            if topic not in present:
                present[topic] = self._old_topics.row_ids_on(topic)
            if event.row_id in present[topic]:
                already += 1
                continue
            self._old_topics.publish(topic, event.row_id, old_topic_payload(event))
            present[topic] = present[topic] | {event.row_id}
            published.append(ModelRepublishedRow(topic=topic, row_id=event.row_id))
        return ModelRepublishResult(
            published=tuple(published),
            duplicates_dropped=duplicates,
            already_on_old_topics=already,
        )


__all__: list[str] = [
    "HandlerWorkLedgerRepublishOldTopics",
    "RepublishRefusedError",
    "old_topic_of",
    "old_topic_payload",
]
