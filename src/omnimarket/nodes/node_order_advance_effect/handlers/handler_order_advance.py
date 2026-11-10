# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer: ``handle(ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult`` (OMN-22938).

One tick of one writer (rebuild plan section 7.5, the holder):

* read the gate store's holder, epoch and order position; a writer that is not the holder writes nothing.
* make at most one change to the piece at the order position: ``started`` when it has no record, ``live`` when its
  golden and error tests, its join tests and one live readback pass, ``in_comparison`` on the next tick, then
  ``kept`` once the comparison window has run and read kept, or ``rolled_back`` the moment it reads rolled back.
* advance the gate store's order by one position only when the piece is ``kept``; a rolled-back piece holds the order.

Every write is keyed ``run_key/...`` and fenced: a record write carries the writer's adopted token and the order
advance carries the epoch it was decided at, so a second writer with a stale fence is refused at both. A restart
rebuilds the next change from the stores alone, never from memory, so no change is repeated; a kill between the
``kept`` record and the order advance is finished by the next tick under the same key.

A fallback writer takes over with ``take_over``, a fenced handover written in the gate store. It is refused when
the current holder runs on the fallback's own host: the holder and its fallback never share a host.

The gate store and the records are injected; the backend and the comparison window are the ``order_advance`` overlay.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_order_advance import (
    EnumComparisonReading,
    EnumOrderAdvanceOperation,
    EnumOrderAdvanceOutcome,
    EnumPieceRecordOperation,
    EnumPieceRecordOutcome,
    EnumPieceState,
    EnumRunEngineBackend,
    ModelOrderAdvanceFailure,
    ModelOrderAdvancePolicy,
    ModelOrderAdvanceRequest,
    ModelOrderAdvanceResult,
    ModelPieceEvidence,
    ModelPieceRecord,
    ModelPieceRecordRequest,
    holder_host,
    holder_id,
)
from omnimarket.nodes.node_order_advance_effect.handlers.records_in_memory import (
    InMemoryPieceRecords,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_piece_records import ProtocolPieceRecords

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_OK = frozenset({EnumPieceRecordOutcome.OK, EnumPieceRecordOutcome.DUPLICATE})


class OrderWriterBackendNotBuiltError(RuntimeError):
    """The overlay selected a backend that is not built; nothing answers in its place."""


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def build_records(policy: ModelOrderAdvancePolicy) -> ProtocolPieceRecords:
    """The records backend the overlay names; refuses one that is not built."""
    if policy.run_engine_backend is EnumRunEngineBackend.TIMER_STORE:
        return InMemoryPieceRecords()
    raise OrderWriterBackendNotBuiltError(
        f"order writer backend {policy.run_engine_backend.value} is not built"
    )


def plan_transition(
    record: ModelPieceRecord | None,
    evidence: ModelPieceEvidence,
    now: float,
    policy: ModelOrderAdvancePolicy,
) -> EnumPieceState | None:
    """The one change this tick makes to the piece, or none."""
    state = record.state if record is not None else None
    if state is None:
        return EnumPieceState.STARTED
    if state is EnumPieceState.STARTED:
        shown = evidence.tests_pass and evidence.joins_pass and evidence.readback_pass
        return EnumPieceState.LIVE if shown else None
    if state is EnumPieceState.LIVE:
        return EnumPieceState.IN_COMPARISON
    if state is EnumPieceState.IN_COMPARISON:
        if evidence.comparison is EnumComparisonReading.ROLLED_BACK:
            return EnumPieceState.ROLLED_BACK
        if evidence.comparison is EnumComparisonReading.KEPT:
            live_at = record.live_at if record is not None else None
            if live_at is not None and now - live_at >= policy.comparison_seconds:
                return EnumPieceState.KEPT
    return None


class HandlerOrderAdvance:
    """One writer's tick or take-over over the injected gate store and piece records."""

    def __init__(
        self,
        store: ProtocolGateStore,
        records: ProtocolPieceRecords,
        policy: ModelOrderAdvancePolicy,
        order: tuple[str, ...],
    ) -> None:
        if policy.run_engine_backend is not EnumRunEngineBackend.TIMER_STORE:
            raise OrderWriterBackendNotBuiltError(
                f"order writer backend {policy.run_engine_backend.value} is not built"
            )
        self._store = store
        self._records = records
        self._policy = policy
        self._order = order

    def handle(self, request: ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult:
        """Run the request; a refusal is an answer, a store that cannot answer raises."""
        if request.operation is EnumOrderAdvanceOperation.TAKE_OVER:
            return self._take_over(request)
        return self._tick(request)

    # ---- reads and answers ----

    def _read(self, request: ModelOrderAdvanceRequest) -> ModelGateStoreResult:
        return self._store.transact(
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.READ,
            )
        )

    def _answer(
        self,
        request: ModelOrderAdvanceRequest,
        outcome: EnumOrderAdvanceOutcome,
        snap: ModelGateStoreResult,
        detail: str = "",
        piece: str | None = None,
        piece_state: EnumPieceState | None = None,
        order_position: int | None = None,
    ) -> ModelOrderAdvanceResult:
        return ModelOrderAdvanceResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=outcome,
            epoch=snap.epoch,
            order_position=(
                snap.order_position if order_position is None else order_position
            ),
            holder=snap.holder,
            piece=piece,
            piece_state=piece_state,
            detail=detail,
        )

    def _holder_token(self, holder: str) -> int | None:
        return self._records.transact(
            ModelPieceRecordRequest(
                operation=EnumPieceRecordOperation.READ, holder=holder
            )
        ).holder_token

    def _ensure_token(
        self, request: ModelOrderAdvanceRequest, me: str, epoch: int
    ) -> int | None:
        """The writer's adopted fence token; adopts the store's epoch when the holder has none."""
        token = self._holder_token(me)
        if token is not None:
            return token
        adopted = self._records.transact(
            ModelPieceRecordRequest(
                operation=EnumPieceRecordOperation.ADOPT,
                step_key=f"{request.run_key}/adopt/{me}/{epoch}",
                holder=me,
                fence_token=epoch,
            )
        )
        return epoch if adopted.outcome in _OK else None

    # ---- take over ----

    def _take_over(self, request: ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult:
        me = holder_id(request.host, request.writer_id)
        snap = self._read(request)
        if snap.holder == me:
            token = self._ensure_token(request, me, snap.epoch)
            if token is None:
                return self._answer(
                    request,
                    EnumOrderAdvanceOutcome.REFUSED_FENCE,
                    snap,
                    "a newer token was adopted",
                )
            return self._answer(
                request, EnumOrderAdvanceOutcome.IDLE, snap, "already the holder"
            )
        if snap.holder is not None and holder_host(snap.holder) == request.host:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_PLACEMENT,
                snap,
                "the holder and its fallback never run on one host",
            )
        if request.expected_epoch is None:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_INVALID,
                snap,
                "a take-over carries the epoch it was decided at",
            )
        handed = self._store.transact(
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.HANDOVER,
                step_key=f"{request.run_key}/takeover/{request.expected_epoch}/{me}",
                holder=me,
                expected_epoch=request.expected_epoch,
            )
        )
        if handed.outcome is EnumGateStoreOutcome.DUPLICATE:
            handed = self._read(request)
            won = handed.holder == me
        else:
            won = handed.outcome is EnumGateStoreOutcome.OK
        if not won:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_FENCE,
                handed,
                f"the store answered {handed.outcome.value}: {handed.detail}",
            )
        if self._ensure_token(request, me, handed.epoch) is None:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_FENCE,
                handed,
                "a newer token was adopted",
            )
        return self._answer(request, EnumOrderAdvanceOutcome.TAKEN_OVER, handed)

    # ---- tick ----

    def _tick(self, request: ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult:
        me = holder_id(request.host, request.writer_id)
        snap = self._read(request)
        if snap.holder != me:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_NOT_HOLDER,
                snap,
                f"the holder is {snap.holder}",
            )
        token = self._ensure_token(request, me, snap.epoch)
        if token is None:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_FENCE,
                snap,
                "a newer token was adopted",
            )
        position = snap.order_position
        if position >= len(self._order):
            return self._answer(request, EnumOrderAdvanceOutcome.COMPLETE, snap)
        piece = self._order[position]
        record = self._records.transact(
            ModelPieceRecordRequest(
                operation=EnumPieceRecordOperation.READ, piece=piece, holder=me
            )
        ).record
        planned = plan_transition(record, request.evidence, request.now, self._policy)
        state = record.state if record is not None else None
        if planned is not None:
            written = self._records.transact(
                ModelPieceRecordRequest(
                    operation=EnumPieceRecordOperation.WRITE,
                    step_key=f"{request.run_key}/{piece}/{planned.value}",
                    now=request.now,
                    piece=piece,
                    to_state=planned,
                    holder=me,
                    fence_token=token,
                )
            )
            if written.outcome not in _OK:
                outcome = (
                    EnumOrderAdvanceOutcome.REFUSED_FENCE
                    if written.outcome is EnumPieceRecordOutcome.REFUSED_FENCE
                    else EnumOrderAdvanceOutcome.REFUSED_INVALID
                )
                return self._answer(
                    request, outcome, snap, written.detail, piece, state
                )
            state = planned
        if state is not EnumPieceState.KEPT:
            outcome = (
                EnumOrderAdvanceOutcome.ADVANCED
                if planned is not None
                else EnumOrderAdvanceOutcome.IDLE
            )
            return self._answer(request, outcome, snap, "", piece, state)
        return self._advance(request, me, snap, piece)

    def _advance(
        self,
        request: ModelOrderAdvanceRequest,
        me: str,
        snap: ModelGateStoreResult,
        piece: str,
    ) -> ModelOrderAdvanceResult:
        target = snap.order_position + 1
        moved = self._store.transact(
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.ADVANCE_ORDER,
                step_key=self._advance_key(request, target, snap.epoch),
                holder=me,
                expected_epoch=snap.epoch,
                order_position=target,
            )
        )
        if moved.outcome is EnumGateStoreOutcome.DUPLICATE:
            moved = self._read(request)
            done = moved.order_position == target
        else:
            done = moved.outcome is EnumGateStoreOutcome.OK
        if not done:
            return self._answer(
                request,
                EnumOrderAdvanceOutcome.REFUSED_FENCE,
                moved,
                f"the store answered {moved.outcome.value}: {moved.detail}",
                piece,
                EnumPieceState.KEPT,
            )
        return self._answer(
            request,
            EnumOrderAdvanceOutcome.ADVANCED,
            moved,
            "",
            piece,
            EnumPieceState.KEPT,
            order_position=target,
        )

    @staticmethod
    def _advance_key(request: ModelOrderAdvanceRequest, target: int, epoch: int) -> str:
        """One advance to a position at an epoch; a replay at the same epoch is the same step."""
        return f"{request.run_key}/advance/{target}@{epoch}"


def answer_command(
    payload: bytes, handler: HandlerOrderAdvance, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request, or a store that cannot answer, is answered with a typed
    failure event naming the failure, never dropped and never retried silently.
    """
    try:
        request = ModelOrderAdvanceRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = ModelOrderAdvanceFailure(
            correlation_id="unknown", failure=f"bad-payload:{fields}"
        )
        return [(topics["order_advance_failed"], failure.model_dump_json().encode())]
    try:
        result = handler.handle(request)
    except Exception as exc:  # the store's own fault is an answer, not a crash
        failure = ModelOrderAdvanceFailure(
            correlation_id=request.correlation_id,
            failure=f"store-fault:{type(exc).__name__}",
        )
        return [(topics["order_advance_failed"], failure.model_dump_json().encode())]
    return [
        (
            topics["order_advance_answered"],
            json.dumps(result.model_dump(mode="json"), sort_keys=True).encode(),
        )
    ]
