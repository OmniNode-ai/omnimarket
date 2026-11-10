# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step of the composed landing workflow (OMN-23409, rebuild piece 9).

One tick: read the tick's facts from the declared facts source, read each PR's GraphQL node id and
the head's failed run ids from the declared write-facts source in one batched read, fill them into
the PR facts the act-mode writer reads (pr_node_id, failed_run_ids), and record the filled facts of
the tick, appended to the recorder and published on the overlay's record topic. A write fact read
at another head than the facts' fills nothing. A tick that is unread, mismatched or already
recorded ends in the typed failed event and replaces no earlier record.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.landing_decision import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingFactsSource,
    InMemoryLandingSnapshotPublisher,
    InMemoryLandingTickRecorder,
    InMemoryLandingWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
    ModelLandingSnapshotResult,
    ModelLandingTickRecord,
    ModelPrWriteFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingFactsSource,
    ProtocolLandingSnapshotPublisher,
    ProtocolLandingTickRecorder,
    ProtocolLandingWriteFactsSource,
)

_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "snapshot.overlay.example.yaml"


def default_overlay() -> ModelSnapshotOverlay:
    """The node's shipped example overlay: the in-memory default when none is injected."""
    return ModelSnapshotOverlay.model_validate(
        yaml.safe_load(_OVERLAY_PATH.read_text(encoding="utf-8"))
    )


def fill_write_facts(
    facts: ModelLandingFacts, write: dict[str, ModelPrWriteFacts]
) -> tuple[ModelLandingFacts, int, int]:
    """The facts with each PR's node id and failed run ids filled; and how many of each were filled."""
    node_ids = 0
    run_ids = 0
    prs: list[ModelLandingPrFacts] = []
    for pr in facts.prs:
        read = write.get(pr.pr)
        if read is None or read.head_sha != pr.head_sha:
            prs.append(pr)
            continue
        update: dict[str, object] = {"failed_run_ids": read.failed_run_ids}
        if read.pr_node_id is not None:
            update["pr_node_id"] = read.pr_node_id
            node_ids += 1
        if read.failed_run_ids:
            run_ids += 1
        prs.append(pr.model_copy(update=update))
    return facts.model_copy(update={"prs": tuple(prs)}), node_ids, run_ids


class HandlerPrLandingSnapshot:
    """Fill one tick's facts from the declared sources and record them on the bus.

    Every port is injected in tests and by the composed workflow. Without one,
    the handler builds the in-memory default: the shipped example overlay and
    the empty in-memory ports.
    """

    def __init__(
        self,
        *,
        overlay: ModelSnapshotOverlay | None = None,
        facts_source: ProtocolLandingFactsSource | None = None,
        write_source: ProtocolLandingWriteFactsSource | None = None,
        recorder: ProtocolLandingTickRecorder | None = None,
        publisher: ProtocolLandingSnapshotPublisher | None = None,
    ) -> None:
        self._overlay = default_overlay() if overlay is None else overlay
        self._facts_source = (
            InMemoryLandingFactsSource({}) if facts_source is None else facts_source
        )
        self._write_source = (
            InMemoryLandingWriteFactsSource(())
            if write_source is None
            else write_source
        )
        self._recorder = InMemoryLandingTickRecorder() if recorder is None else recorder
        self._publisher = (
            InMemoryLandingSnapshotPublisher() if publisher is None else publisher
        )

    def handle(
        self, request: ModelLandingSnapshotRequest
    ) -> ModelLandingSnapshotResult:
        try:
            facts = self._facts_source.read_facts(request.tick)
        except SnapshotSourceError as exc:
            return self._fail(request, EnumSnapshotFailureReason.FACTS_UNREAD, str(exc))
        if facts is None:
            return self._fail(
                request,
                EnumSnapshotFailureReason.FACTS_UNREAD,
                f"{self._facts_source.name} holds no facts for tick {request.tick}",
            )
        if facts.tick != request.tick:
            return self._fail(
                request,
                EnumSnapshotFailureReason.TICK_MISMATCH,
                f"{self._facts_source.name} returned tick {facts.tick} for tick {request.tick}",
            )
        if any(r.tick == request.tick for r in self._recorder.recorded()):
            return self._fail(
                request,
                EnumSnapshotFailureReason.TICK_ALREADY_RECORDED,
                f"tick {request.tick} is recorded; a record is never replaced",
            )
        try:
            write = self._write_source.read_write_facts(
                {p.pr: p.head_sha for p in facts.prs}
            )
        except SnapshotSourceError as exc:
            return self._fail(
                request, EnumSnapshotFailureReason.WRITE_FACTS_UNREAD, str(exc)
            )
        filled, node_ids, run_ids = fill_write_facts(facts, write)
        record = ModelLandingTickRecord(
            correlation_id=request.correlation_id,
            tick=request.tick,
            facts=filled,
            provenance=f"{self._facts_source.name}+{self._write_source.name}",
        )
        self._recorder.record(record)
        self._publisher.publish(self._overlay.record_topic, record)
        return ModelLandingSnapshotResult(
            correlation_id=request.correlation_id,
            tick=request.tick,
            status=EnumSnapshotStatus.RECORDED,
            record=record,
            node_ids_filled=node_ids,
            run_ids_filled=run_ids,
        )

    def _fail(
        self,
        request: ModelLandingSnapshotRequest,
        reason: EnumSnapshotFailureReason,
        detail: str,
    ) -> ModelLandingSnapshotResult:
        self._publisher.publish(
            self._overlay.failed_topic,
            ModelLandingSnapshotFailedEvent(
                correlation_id=request.correlation_id,
                tick=request.tick,
                reason=reason,
                detail=detail,
            ),
        )
        return ModelLandingSnapshotResult(
            correlation_id=request.correlation_id,
            tick=request.tick,
            status=EnumSnapshotStatus.FAILED,
            failure_reason=reason,
            failure_detail=detail,
        )


__all__: list[str] = [
    "HandlerPrLandingSnapshot",
    "default_overlay",
    "fill_write_facts",
]
