# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the order-writer tests: a two-piece test order, its stores and request builders."""

from __future__ import annotations

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_order_advance import (
    EnumComparisonReading,
    EnumOrderAdvanceOperation,
    EnumRunEngineBackend,
    ModelOrderAdvancePolicy,
    ModelOrderAdvanceRequest,
    ModelPieceEvidence,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
)
from omnimarket.nodes.node_order_advance_effect.handlers.records_in_memory import (
    InMemoryPieceRecords,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_piece_records import ProtocolPieceRecords

ORDER = ("alpha", "beta")
GATE_POLICY = ModelGateStorePolicy(
    backend=EnumGateStoreBackend.IN_MEMORY,
    limits={"alpha": 1, "beta": 1},
    reservation_ttl_seconds=60,
    order=ORDER,
)
POLICY = ModelOrderAdvancePolicy(
    run_engine_backend=EnumRunEngineBackend.TIMER_STORE, comparison_seconds=100
)
SHOWN = ModelPieceEvidence(tests_pass=True, joins_pass=True, readback_pass=True)
KEPT_READING = ModelPieceEvidence(comparison=EnumComparisonReading.KEPT)
ROLLED_BACK_READING = ModelPieceEvidence(comparison=EnumComparisonReading.ROLLED_BACK)

HOST_A = "host-a.test"
HOST_B = "host-b.test"


def tick(
    writer: str = "w1",
    host: str = HOST_A,
    now: float = 0.0,
    evidence: ModelPieceEvidence | None = None,
    corr: str | None = None,
) -> ModelOrderAdvanceRequest:
    return ModelOrderAdvanceRequest(
        correlation_id=corr or f"c-tick-{writer}-{now}",
        operation=EnumOrderAdvanceOperation.TICK,
        run_key="run1",
        writer_id=writer,
        host=host,
        now=now,
        evidence=evidence or ModelPieceEvidence(),
    )


def take_over(
    writer: str,
    host: str,
    expected_epoch: int,
    corr: str | None = None,
) -> ModelOrderAdvanceRequest:
    return ModelOrderAdvanceRequest(
        correlation_id=corr or f"c-take-{writer}-{expected_epoch}",
        operation=EnumOrderAdvanceOperation.TAKE_OVER,
        run_key="run1",
        writer_id=writer,
        host=host,
        expected_epoch=expected_epoch,
    )


def make_handler(
    store: ProtocolGateStore | None = None,
    records: ProtocolPieceRecords | None = None,
) -> HandlerOrderAdvance:
    return HandlerOrderAdvance(
        store if store is not None else InMemoryGateStore(GATE_POLICY),
        records if records is not None else InMemoryPieceRecords(),
        POLICY,
        ORDER,
    )


class SpyStore:
    """Counts what the gate store committed; can be told to fail one advance before it applies."""

    def __init__(self, inner: ProtocolGateStore, fail_first_advance: bool = False):
        self.inner = inner
        self.fail_first_advance = fail_first_advance
        self.committed_advances = 0

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if (
            request.operation is EnumGateStoreOperation.ADVANCE_ORDER
            and self.fail_first_advance
        ):
            self.fail_first_advance = False
            raise RuntimeError("killed before the advance applied")
        result = self.inner.transact(request)
        if (
            request.operation is EnumGateStoreOperation.ADVANCE_ORDER
            and result.outcome is EnumGateStoreOutcome.OK
        ):
            self.committed_advances += 1
        return result


class StaleReadStore:
    """Serves a fixed read to the correlation ids it was given: a writer that read before a handover."""

    def __init__(self, inner: ProtocolGateStore) -> None:
        self.inner = inner
        self.stale: dict[str, ModelGateStoreResult] = {}

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if (
            request.operation is EnumGateStoreOperation.READ
            and request.correlation_id in self.stale
        ):
            return self.stale[request.correlation_id]
        return self.inner.transact(request)


def stale_read(
    corr: str, holder: str, epoch: int, position: int = 0
) -> ModelGateStoreResult:
    return ModelGateStoreResult(
        correlation_id=corr,
        operation=EnumGateStoreOperation.READ,
        outcome=EnumGateStoreOutcome.OK,
        epoch=epoch,
        holder=holder,
        order_position=position,
    )
