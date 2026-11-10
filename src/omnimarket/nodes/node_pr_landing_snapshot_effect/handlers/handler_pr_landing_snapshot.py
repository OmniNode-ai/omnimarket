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

from omnimarket.models.landing_decision import (
    ModelLandingFacts,
    ModelLandingPrFacts,
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
    """Fill one tick's facts from the declared sources and record them on the bus."""

    def __init__(
        self,
        *,
        overlay: ModelSnapshotOverlay,
        facts_source: ProtocolLandingFactsSource,
        write_source: ProtocolLandingWriteFactsSource,
        recorder: ProtocolLandingTickRecorder,
        publisher: ProtocolLandingSnapshotPublisher,
    ) -> None:
        self._overlay = overlay
        self._facts_source = facts_source
        self._write_source = write_source
        self._recorder = recorder
        self._publisher = publisher

    def handle(
        self, request: ModelLandingSnapshotRequest
    ) -> ModelLandingSnapshotResult:
        facts = self._facts_source.read_facts(request.tick)
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
        write = self._write_source.read_write_facts(
            {p.pr: p.head_sha for p in facts.prs}
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


__all__: list[str] = ["HandlerPrLandingSnapshot", "fill_write_facts"]
