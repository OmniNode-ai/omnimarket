# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing snapshot step: fill one tick's write facts from the watcher and record the tick on the bus (OMN-23409).

handle(ModelLandingSnapshotRequest) -> ModelLandingSnapshotResult. Each pull request of the tick's
facts gets its GraphQL node id and, when the watcher's CI record is of the same head, its head's
failed workflow run ids (the red checks whose conclusion the overlay names as failed). The filled
facts and the tick's decision are published as one ModelTickSnapshot on the recorded topic, one
record per tick: the tick orchestrator's replay source reads that topic, so every tick's facts stay
recoverable after the next tick. A watcher that cannot be read, or a record the bus refuses, ends
the tick with the typed failed event instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    InMemoryWatcherSource,
    ProtocolWatcherSource,
    watcher_key,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
    ModelLandingSnapshotResult,
    ModelWatcherPrRead,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def node_name() -> str:
    """The node's name as the contract declares it: what the floor's emission chains are filed under."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return str(contract["name"])


def contract_topics() -> tuple[str, str]:
    """The recorded and the failed topic, as the contract declares them."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    terminal = contract["runtime_dispatch"]["terminal_events"]
    return str(terminal["success"]), str(terminal["failure"])


def _fill(
    pr: ModelLandingPrFacts,
    read: ModelWatcherPrRead,
    failed_conclusions: frozenset[str],
) -> tuple[ModelLandingPrFacts, bool]:
    """The PR with its node id and, at the same head, its failed run ids; whether the head matched."""
    update: dict[str, object] = {}
    if read.node_id:
        update["pr_node_id"] = read.node_id
    same_head = read.ci_head == pr.head_sha
    if same_head:
        update["failed_run_ids"] = tuple(
            sorted(
                {r.run_id for r in read.red_runs if r.conclusion in failed_conclusions}
            )
        )
    return pr.model_copy(update=update), same_head


class HandlerPrLandingSnapshot:
    """One tick: read the watcher, fill the write facts, publish the tick's snapshot record."""

    def __init__(
        self,
        *,
        failed_conclusions: Sequence[str],
        watcher: ProtocolWatcherSource | None = None,
        publisher: ProtocolTickPublisher | None = None,
        emission_sink: ProtocolEmissionSink | None = None,
        floor_piece: int | None = None,
        emission_backend: str = "in_memory",
        drill: bool = False,
    ) -> None:
        if not failed_conclusions:
            raise ValueError(
                "failed_conclusions comes from the overlay and is never empty"
            )
        self._failed_conclusions = frozenset(failed_conclusions)
        self._watcher: ProtocolWatcherSource = watcher or InMemoryWatcherSource({})
        self._publisher: ProtocolTickPublisher = publisher or InMemoryTickPublisher()
        self._emission_sink = emission_sink
        self._floor_piece = floor_piece
        self._emission_backend = emission_backend
        self._drill = drill
        self._recorded_topic, self._failed_topic = contract_topics()

    def handle(
        self, request: ModelLandingSnapshotRequest
    ) -> ModelLandingSnapshotResult:
        chain = self._chain(request)
        if chain is not None:
            chain.start()
        result = self._record(request)
        if chain is not None:
            if result.status is EnumSnapshotStatus.RECORDED:
                chain.success(
                    {
                        "prs": len(request.facts.prs),
                        "node_ids_filled": result.node_ids_filled,
                        "prs_with_failed_runs": result.prs_with_failed_runs,
                        "unread_prs": len(result.unread_prs),
                    }
                )
            else:
                chain.failure(
                    result.failure_reason.value if result.failure_reason else "failed",
                    result.failure_detail,
                )
        return result

    def _chain(self, request: ModelLandingSnapshotRequest) -> ChainEmitter | None:
        if self._emission_sink is None or self._floor_piece is None:
            return None
        return ChainEmitter(
            self._emission_sink,
            node=node_name(),
            piece=self._floor_piece,
            correlation_id=request.correlation_id.hex,
            backend=self._emission_backend,
            drill=self._drill,
        )

    def _failed(
        self,
        request: ModelLandingSnapshotRequest,
        reason: EnumSnapshotFailureReason,
        detail: str,
    ) -> ModelLandingSnapshotResult:
        self._publisher.publish(
            self._failed_topic,
            ModelLandingSnapshotFailedEvent(
                correlation_id=request.correlation_id,
                tick=request.facts.tick,
                reason=reason,
                detail=detail,
            ),
        )
        return ModelLandingSnapshotResult(
            correlation_id=request.correlation_id,
            status=EnumSnapshotStatus.FAILED,
            tick=request.facts.tick,
            failure_reason=reason,
            failure_detail=detail,
        )

    def _record(
        self, request: ModelLandingSnapshotRequest
    ) -> ModelLandingSnapshotResult:
        facts = request.facts
        keys = {pr.pr: watcher_key(pr.pr) for pr in facts.prs}
        try:
            reads = self._watcher.read(sorted(set(keys.values())))
        except Exception as exc:
            return self._failed(
                request,
                EnumSnapshotFailureReason.WATCHER_UNREADABLE,
                type(exc).__name__,
            )
        prs: list[ModelLandingPrFacts] = []
        unread: list[str] = []
        other_head: list[str] = []
        for pr in facts.prs:
            read = reads.get(keys[pr.pr])
            if read is None:
                unread.append(pr.pr)
                prs.append(pr)
                continue
            filled, same_head = _fill(pr, read, self._failed_conclusions)
            if not same_head:
                other_head.append(pr.pr)
            prs.append(filled)
        filled_facts = ModelLandingFacts.model_validate(
            {**facts.model_dump(), "prs": [p.model_dump() for p in prs]}
        )
        snapshot = ModelTickSnapshot(
            facts=filled_facts,
            decision=request.decision,
            fair_share=request.fair_share,
            provenance=(
                f"{node_name()} tick={facts.tick} correlation={request.correlation_id}"
            ),
        )
        try:
            self._publisher.publish(self._recorded_topic, snapshot)
        except Exception as exc:
            return self._failed(
                request, EnumSnapshotFailureReason.RECORD_REFUSED, type(exc).__name__
            )
        return ModelLandingSnapshotResult(
            correlation_id=request.correlation_id,
            status=EnumSnapshotStatus.RECORDED,
            tick=facts.tick,
            snapshot=snapshot,
            node_ids_filled=sum(1 for p in prs if p.pr_node_id),
            prs_with_failed_runs=sum(1 for p in prs if p.failed_run_ids),
            unread_prs=tuple(unread),
            other_head_prs=tuple(other_head),
        )


__all__: list[str] = [
    "HandlerPrLandingSnapshot",
    "contract_topics",
    "node_name",
]
