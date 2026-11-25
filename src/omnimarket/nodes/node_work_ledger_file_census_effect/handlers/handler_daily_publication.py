# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census's daily publication: ``handle(ModelCensusDailyPublicationRequest) -> ModelCensusDailyPublicationResult``.

One daily run's record is read against the previous day's kept record, put on the declared topic as one typed event (a
partial or a failed record too: the shrink is read from the bus whether the run passed or not) and kept as its day's record.
The topic, the host name and the history are given by the caller; nothing here names a topic, a path or a broker.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_daily_change import (
    HandlerDailyChange,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_daily_change import (
    EVENT_TYPE,
    ModelCensusDailyEventRequest,
    ModelCensusDailyPublicationRequest,
    ModelCensusDailyPublicationResult,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusDailyRecord,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.protocols.protocol_daily_publication import (
    ProtocolDailyEventPublisher,
    ProtocolDailyRecordHistory,
)


class HandlerDailyPublication:
    def __init__(
        self,
        history: ProtocolDailyRecordHistory,
        publisher: ProtocolDailyEventPublisher,
        topic: str,
        host: str,
    ) -> None:
        self._history = history
        self._publisher = publisher
        self._topic = topic
        self._host = host

    def handle(
        self, request: ModelCensusDailyPublicationRequest
    ) -> ModelCensusDailyPublicationResult:
        record = request.record
        previous: ModelCensusDailyRecord | None = None
        unreadable = False
        try:
            previous = self._history.previous_before(record.recorded_on)
        except (OSError, ValueError):
            unreadable = True
        event = HandlerDailyChange().handle(
            ModelCensusDailyEventRequest(
                record=record,
                previous=previous,
                host=self._host,
                previous_unreadable=unreadable,
            )
        )
        published = self._publisher.publish(
            topic=self._topic,
            event_type=EVENT_TYPE,
            payload=event.model_dump(mode="json"),
            event_id=record.correlation_id,
            correlation_id=record.correlation_id,
        )
        try:
            self._history.keep(record)
            kept = True
        except OSError:
            kept = False
        return ModelCensusDailyPublicationResult(
            event=event, published=published, history_kept=kept
        )
