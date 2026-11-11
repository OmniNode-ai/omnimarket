# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rerun-request sources of the landing tick orchestrator: in-memory, and Redpanda behind a topic reader.

Each raw record is either the bare ModelLandingRerunRequest or an event envelope whose "payload"
object is the request (the onex scm verb publishes an envelope with a "payload" key). The topic is
shared, so a record this source cannot parse is skipped with a typed row (skipped()) and the read
goes on; only the reader itself raising fails the read. This module holds no Kafka client.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_rerun_ledger import (
    FileRerunLedger,
    InMemoryRerunLedger,
    ProtocolRerunLedger,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    ProtocolRerunRequestSource,
    ProtocolTickTopicReader,
    TopicReaderFactory,
    _validation_detail,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickSkipReason,
    ModelTickSkippedRecord,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    EnumTickBackend,
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
    resolved_consumer_group,
)


class RedpandaRerunRequestSource:
    """The rerun-request topic's records, read through the injected reader.

    Each record is the bare ModelLandingRerunRequest, or an event envelope whose "payload" object
    is the request. A record that is not JSON, not an object, or fails validation is skipped with a
    typed row (skipped()) and the read goes on; only the reader itself raising fails the read.
    """

    def __init__(
        self,
        reader: ProtocolTickTopicReader,
        *,
        max_requests: int,
        poll_timeout_s: float,
    ) -> None:
        self._reader = reader
        self._max = max_requests
        self._timeout = poll_timeout_s
        self._skipped: tuple[ModelTickSkippedRecord, ...] = ()

    def requests(self) -> tuple[ModelLandingRerunRequest, ...]:
        out: list[ModelLandingRerunRequest] = []
        skipped: list[ModelTickSkippedRecord] = []
        for position, raw in enumerate(self._reader.read(self._max, self._timeout)):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                skipped.append(
                    ModelTickSkippedRecord(
                        position=position,
                        reason=EnumTickSkipReason.UNPARSEABLE_RECORD,
                        detail="not_json",
                    )
                )
                continue
            if not isinstance(value, dict):
                skipped.append(
                    ModelTickSkippedRecord(
                        position=position,
                        reason=EnumTickSkipReason.UNPARSEABLE_RECORD,
                        detail="not_an_object",
                    )
                )
                continue
            payload = value.get("payload")
            try:
                request = ModelLandingRerunRequest.model_validate(
                    payload if isinstance(payload, dict) else value
                )
            except ValidationError as exc:
                skipped.append(
                    ModelTickSkippedRecord(
                        position=position,
                        reason=EnumTickSkipReason.UNPARSEABLE_RECORD,
                        detail=_validation_detail(exc),
                    )
                )
                continue
            out.append(request)
        self._skipped = tuple(skipped)
        return tuple(out)

    def skipped(self) -> tuple[ModelTickSkippedRecord, ...]:
        """The records the last requests() call skipped, each with its place and reason."""
        return self._skipped


class InMemoryRerunRequestSource:
    def __init__(self, requests: Iterable[ModelLandingRerunRequest]) -> None:
        self._requests = tuple(requests)

    def requests(self) -> tuple[ModelLandingRerunRequest, ...]:
        return self._requests

    def skipped(self) -> tuple[ModelTickSkippedRecord, ...]:
        return ()


def build_rerun_request_source(
    overlay: ModelTickOrchestratorOverlay,
    *,
    inmemory: InMemoryRerunRequestSource | None = None,
    topic_reader_factory: TopicReaderFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProtocolRerunRequestSource | None:
    """The overlay's rerun-request source (OMN-23830): None when the overlay declares no section."""
    section = overlay.rerun_requests
    if section is None:
        return None
    if overlay.backend is EnumTickBackend.INMEMORY:
        return inmemory if inmemory is not None else InMemoryRerunRequestSource(())
    # the overlay validator requires them for redpanda
    assert section.request_topic
    assert section.consumer_group
    assert section.poll_timeout_s
    assert overlay.join_timeout_s is not None
    selection = ModelTickTopicSelection(
        topic=section.request_topic,
        consumer_group=resolved_consumer_group(
            overlay.model_copy(update={"consumer_group": section.consumer_group}),
            os.environ if environ is None else environ,
        ),
        join_timeout_s=overlay.join_timeout_s,
        bus_lane=overlay.bus_lane,
        workspace_root=overlay.workspace_root,
        bootstrap_servers=overlay.bootstrap_servers,
    )
    if topic_reader_factory is None:
        from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_topic_reader import (
            BusTickTopicReader,
        )

        topic_reader_factory = BusTickTopicReader
    return RedpandaRerunRequestSource(
        topic_reader_factory(selection),
        max_requests=section.max_requests,
        poll_timeout_s=section.poll_timeout_s,
    )


def build_rerun_ledger(overlay: ModelTickOrchestratorOverlay) -> ProtocolRerunLedger:
    """The ledger the overlay selects: a file when it names one, memory otherwise (OMN-23830)."""
    section = overlay.rerun_requests
    if section is not None and section.ledger_path:
        return FileRerunLedger(Path(os.path.expandvars(section.ledger_path)))
    return InMemoryRerunLedger()


__all__: list[str] = [
    "InMemoryRerunRequestSource",
    "RedpandaRerunRequestSource",
    "build_rerun_ledger",
    "build_rerun_request_source",
]
