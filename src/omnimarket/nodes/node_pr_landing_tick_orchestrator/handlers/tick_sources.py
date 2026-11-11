# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot sources of the landing tick orchestrator: in-memory, and Redpanda behind a topic reader.

The overlay names the backend and its settings. The in-memory snapshots are injected; the Redpanda
topic reader factory is injected in tests and defaults to the bus reader of tick_topic_reader
(OMN-23458), which opens the overlay's declared bus lane. The overlay's publish_backend likewise
selects the compared and compare-failed events' publisher (OMN-23539): the in-memory one by default,
the bus publisher of tick_bus_publisher for redpanda. This module holds no Kafka client.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickSkipReason,
    ModelTickRequest,
    ModelTickResult,
    ModelTickSkippedRecord,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    OVERLAY_ID,
    EnumTickBackend,
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
    resolved_consumer_group,
)


class ProtocolTickSnapshotSource(Protocol):
    def snapshots(self) -> tuple[ModelTickSnapshot, ...]: ...

    def skipped(self) -> tuple[ModelTickSkippedRecord, ...]: ...


class ProtocolTickTopicReader(Protocol):
    """Reads raw JSON tick records from a topic, up to a count, within a timeout."""

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]: ...


TopicReaderFactory = Callable[[ModelTickTopicSelection], ProtocolTickTopicReader]

PublisherFactory = Callable[[ModelTickOrchestratorOverlay], ProtocolTickPublisher]


class InMemorySnapshotSource:
    def __init__(self, snapshots: Iterable[ModelTickSnapshot]) -> None:
        self._snapshots = tuple(snapshots)

    def snapshots(self) -> tuple[ModelTickSnapshot, ...]:
        return self._snapshots

    def skipped(self) -> tuple[ModelTickSkippedRecord, ...]:
        return ()


class RedpandaSnapshotSource:
    """The landing snapshot step's tick records, read from the overlay's topic through the injected reader.

    Each record is the shared ModelLandingTickRecord the snapshot step publishes on its record_topic
    (OMN-23409); its facts become one snapshot with no recorded decision. The topic is shared, so a
    record this reader cannot parse is skipped with a typed row (skipped()) and the read goes on
    (OMN-23529); only the reader itself raising fails the read.
    """

    def __init__(
        self,
        reader: ProtocolTickTopicReader,
        *,
        max_snapshots: int,
        poll_timeout_s: float,
    ) -> None:
        self._reader = reader
        self._max = max_snapshots
        self._timeout = poll_timeout_s
        self._skipped: tuple[ModelTickSkippedRecord, ...] = ()

    def snapshots(self) -> tuple[ModelTickSnapshot, ...]:
        out: list[ModelTickSnapshot] = []
        skipped: list[ModelTickSkippedRecord] = []
        for position, raw in enumerate(self._reader.read(self._max, self._timeout)):
            try:
                record = ModelLandingTickRecord.model_validate_json(raw)
            except ValidationError as exc:
                skipped.append(
                    ModelTickSkippedRecord(
                        position=position,
                        reason=EnumTickSkipReason.UNPARSEABLE_RECORD,
                        detail=_validation_detail(exc),
                    )
                )
                continue
            out.append(
                ModelTickSnapshot(
                    facts=record.facts,
                    provenance=record.provenance,
                    controller=record.controller,
                )
            )
        self._skipped = tuple(skipped)
        return tuple(out)

    def skipped(self) -> tuple[ModelTickSkippedRecord, ...]:
        """The records the last snapshots() call skipped, each with its place and reason."""
        return self._skipped


_DETAIL_ERRORS = 3


def _validation_detail(exc: ValidationError) -> str:
    """The first errors as location: type, e.g. facts.prs.0.liveness: extra_forbidden."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<record>'}: {error['type']}"
        for error in exc.errors()[:_DETAIL_ERRORS]
    )


_REQUIRED_FIELDS: dict[EnumTickBackend, tuple[str, ...]] = {
    EnumTickBackend.INMEMORY: (),
    EnumTickBackend.REDPANDA: (
        "consumer_group",
        "host_env",
        "snapshot_topic",
        "poll_timeout_s",
        "emission_journal",
    ),
}


def load_tick_overlay(
    *, explicit: object | None = None
) -> ModelTickOrchestratorOverlay:
    """Read the overlay; a missing or invalid one refuses naming the fields it lacks."""
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelTickOrchestratorOverlay, explicit=explicit
        )
    except NodeOverlayError as exc:
        raise ValueError(str(exc)) from None
    missing = [
        name for name in _REQUIRED_FIELDS[overlay.backend] if not getattr(overlay, name)
    ]
    if (
        overlay.backend is EnumTickBackend.REDPANDA
        and not overlay.bus_lane
        and not overlay.bootstrap_servers
    ):
        missing.append("bus_lane or bootstrap_servers")
    if missing:
        raise ValueError(
            f"backend {overlay.backend.value} needs overlay fields {missing}"
        )
    return overlay


def build_snapshot_source(
    overlay: ModelTickOrchestratorOverlay,
    *,
    inmemory: InMemorySnapshotSource | None = None,
    topic_reader_factory: TopicReaderFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProtocolTickSnapshotSource:
    if overlay.backend is EnumTickBackend.INMEMORY:
        if inmemory is None:
            raise ValueError("backend inmemory needs the in-memory snapshots injected")
        return inmemory
    if not (
        (overlay.bus_lane or overlay.bootstrap_servers)
        and overlay.snapshot_topic
        and overlay.consumer_group
        and overlay.poll_timeout_s
    ):
        raise ValueError("backend redpanda needs its overlay fields")
    assert (
        overlay.join_timeout_s is not None
    )  # the overlay validator requires it for redpanda
    selection = ModelTickTopicSelection(
        topic=overlay.snapshot_topic,
        consumer_group=resolved_consumer_group(
            overlay, os.environ if environ is None else environ
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
    return RedpandaSnapshotSource(
        topic_reader_factory(selection),
        max_snapshots=overlay.max_snapshots,
        poll_timeout_s=overlay.poll_timeout_s,
    )


def build_emission(
    overlay: ModelTickOrchestratorOverlay, *, journal_root: Path | None = None
) -> tuple[ProtocolEmissionSink, str]:
    """The floor emission sink the overlay selects (in memory, or the journal) and its backend label."""
    if overlay.backend is EnumTickBackend.INMEMORY:
        return InMemoryEmissionSink(), "in_memory"
    if overlay.emission_journal is None:
        raise ValueError("backend redpanda needs overlay field emission_journal")
    path = Path(overlay.emission_journal)
    return JsonlEmissionSink(journal_root / path if journal_root else path), "real"


def build_publisher(
    overlay: ModelTickOrchestratorOverlay,
    *,
    publisher_factory: PublisherFactory | None = None,
) -> ProtocolTickPublisher:
    """The overlay's publish_backend (OMN-23539): in-memory by default, the bus publisher for redpanda."""
    if overlay.publish_backend is EnumTickBackend.INMEMORY:
        return InMemoryTickPublisher()
    if publisher_factory is not None:
        return publisher_factory(overlay)
    from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_bus_publisher import (
        BusTickPublisher,
    )

    return BusTickPublisher(
        bus_lane=overlay.bus_lane,
        workspace_root=overlay.workspace_root,
        bootstrap_servers=overlay.bootstrap_servers,
    )


def run_snapshots(
    handler: HandlerPrLandingTickOrchestrator,
    source: ProtocolTickSnapshotSource,
    mode: EnumTickMode,
) -> tuple[ModelTickResult, ...]:
    """Run every snapshot of the source through the handler in the given mode."""
    return tuple(
        handler.handle(
            ModelTickRequest(
                correlation_id=uuid4(),
                mode=mode,
                facts=snapshot.facts,
                recorded_decision=snapshot.decision,
                fair_share=snapshot.fair_share,
                controller=snapshot.controller,
            )
        )
        for snapshot in source.snapshots()
    )


__all__: list[str] = [
    "InMemorySnapshotSource",
    "ProtocolTickSnapshotSource",
    "ProtocolTickTopicReader",
    "PublisherFactory",
    "RedpandaSnapshotSource",
    "TopicReaderFactory",
    "build_emission",
    "build_publisher",
    "build_snapshot_source",
    "load_tick_overlay",
    "run_snapshots",
]
