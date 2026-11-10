# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot sources of the landing tick orchestrator: in-memory, and Redpanda behind an injected reader.

The overlay names the backend and its settings. The in-memory snapshots and the Redpanda topic
reader factory are injected; this module holds no Kafka client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

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
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    ModelTickRequest,
    ModelTickResult,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    OVERLAY_ID,
    EnumTickBackend,
    ModelTickOrchestratorOverlay,
)


class ProtocolTickSnapshotSource(Protocol):
    def snapshots(self) -> tuple[ModelTickSnapshot, ...]: ...


class ProtocolTickTopicReader(Protocol):
    """Reads raw JSON tick records from a topic, up to a count, within a timeout."""

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]: ...


TopicReaderFactory = Callable[[str, str, str], ProtocolTickTopicReader]


class InMemorySnapshotSource:
    def __init__(self, snapshots: Iterable[ModelTickSnapshot]) -> None:
        self._snapshots = tuple(snapshots)

    def snapshots(self) -> tuple[ModelTickSnapshot, ...]:
        return self._snapshots


class RedpandaSnapshotSource:
    """The landing snapshot step's tick records, read from the overlay's topic through the injected reader.

    Each record is the shared ModelLandingTickRecord the snapshot step publishes on its record_topic
    (OMN-23409); its facts become one snapshot with no recorded decision.
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

    def snapshots(self) -> tuple[ModelTickSnapshot, ...]:
        out: list[ModelTickSnapshot] = []
        for raw in self._reader.read(self._max, self._timeout):
            record = ModelLandingTickRecord.model_validate_json(raw)
            out.append(
                ModelTickSnapshot(facts=record.facts, provenance=record.provenance)
            )
        return tuple(out)


_REQUIRED_FIELDS: dict[EnumTickBackend, tuple[str, ...]] = {
    EnumTickBackend.INMEMORY: (),
    EnumTickBackend.REDPANDA: (
        "bootstrap_servers",
        "consumer_group",
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
) -> ProtocolTickSnapshotSource:
    if overlay.backend is EnumTickBackend.INMEMORY:
        if inmemory is None:
            raise ValueError("backend inmemory needs the in-memory snapshots injected")
        return inmemory
    if topic_reader_factory is None:
        raise ValueError("backend redpanda needs a topic reader factory")
    if not (
        overlay.bootstrap_servers
        and overlay.snapshot_topic
        and overlay.consumer_group
        and overlay.poll_timeout_s
    ):
        raise ValueError("backend redpanda needs its overlay fields")
    return RedpandaSnapshotSource(
        topic_reader_factory(
            overlay.bootstrap_servers, overlay.snapshot_topic, overlay.consumer_group
        ),
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
            )
        )
        for snapshot in source.snapshots()
    )


__all__: list[str] = [
    "InMemorySnapshotSource",
    "ProtocolTickSnapshotSource",
    "ProtocolTickTopicReader",
    "RedpandaSnapshotSource",
    "build_emission",
    "build_snapshot_source",
    "load_tick_overlay",
    "run_snapshots",
]
