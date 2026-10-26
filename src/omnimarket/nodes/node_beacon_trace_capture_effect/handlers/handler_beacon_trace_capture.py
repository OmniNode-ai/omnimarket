# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a byte-offset batch, map metadata, and publish session-keyed events."""

import json
from collections import Counter
from uuid import UUID

from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.adapter_io import (
    publish_topics,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.handler_beacon_event_mapping import (
    map_beacon_line,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_trace_capture import (
    ModelBeaconTraceCaptureCommand,
    ModelBeaconTraceCaptureResult,
)
from omnimarket.nodes.node_beacon_trace_capture_effect.protocols import (
    ProtocolBeaconLogReader,
    ProtocolHookEventPublisher,
)


class HandlerBeaconTraceCapture:
    """Capture one batch; the caller persists the returned offset after flush."""

    def __init__(
        self, *, reader: ProtocolBeaconLogReader, publisher: ProtocolHookEventPublisher
    ) -> None:
        self._reader = reader
        self._publisher = publisher

    def handle(
        self, command: ModelBeaconTraceCaptureCommand
    ) -> ModelBeaconTraceCaptureResult:
        lines, next_offset = self._reader.read_from(command.log_path, command.offset)
        topic = publish_topics()[0]
        skipped = malformed = published = 0
        event_ids: list[UUID] = []
        counts: Counter[str] = Counter()
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            event = map_beacon_line(record, lane=command.lane, host=command.host)
            if event is None:
                skipped += 1
                continue
            event_ids.append(event.event_id)
            counts[event.hook_event_name] += 1
            if not command.dry_run:
                self._publisher.publish(topic, event.lineage.session_id, event)
                published += 1
        return ModelBeaconTraceCaptureResult(
            next_offset=next_offset,
            lines_read=len(lines),
            events_published=published,
            skipped=skipped,
            malformed=malformed,
            event_ids=tuple(event_ids),
            hook_event_counts=dict(counts),
        )
