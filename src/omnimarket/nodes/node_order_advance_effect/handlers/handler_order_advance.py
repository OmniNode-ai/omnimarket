# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer: ``handle(ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult``.

One tick reads the gate store, claims the holder record when it is the writer's to claim (a
fenced handover at the epoch it read), and advances the order by one position when the piece
at the current position reads ready. Every write carries a step key, ``run_key/advance/<n>``
or ``run_key/handover/<epoch>``, so a replayed tick writes nothing twice. A tick that finds
itself fenced reports it and writes nothing; the next tick reads again. The store and the
readiness read are injected; the in-memory readiness is the default and the overlay selects it.
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
    EnumOrderAdvanceOutcome,
    EnumOrderReadinessBackend,
    ModelOrderAdvanceFailure,
    ModelOrderAdvanceRequest,
    ModelOrderAdvanceResult,
    ModelOrderWriterPolicy,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_order_readiness import ProtocolOrderReadiness

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
OP = EnumGateStoreOperation
OUT = EnumOrderAdvanceOutcome


class OrderReadinessNotBuiltError(RuntimeError):
    """The overlay selected a readiness read that is not built; nothing answers in its place."""


class InMemoryOrderReadiness:
    """The readiness the overlay lists; the twin the tests and the first install use."""

    def __init__(self, ready: tuple[str, ...]) -> None:
        self._ready = frozenset(ready)

    def is_ready(self, piece: str) -> bool:
        return piece in self._ready


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def build_readiness(policy: ModelOrderWriterPolicy) -> ProtocolOrderReadiness:
    """The readiness read the overlay names; refuses one that is not built."""
    if policy.readiness_backend is EnumOrderReadinessBackend.IN_MEMORY:
        return InMemoryOrderReadiness(policy.ready_pieces)
    raise OrderReadinessNotBuiltError(
        f"order readiness backend {policy.readiness_backend.value} is not built"
    )


class HandlerOrderAdvance:
    """One tick of the writer on the injected gate store and readiness."""

    def __init__(
        self,
        store: ProtocolGateStore,
        readiness: ProtocolOrderReadiness,
        policy: ModelOrderWriterPolicy,
        order: tuple[str, ...],
    ) -> None:
        self._store = store
        self._readiness = readiness
        self._policy = policy
        self._order = order

    def _write(
        self, request: ModelOrderAdvanceRequest, op: OP, key: str, **fields: object
    ) -> ModelGateStoreResult:
        return self._store.transact(
            ModelGateStoreRequest.model_validate(
                {
                    "correlation_id": request.correlation_id,
                    "operation": op,
                    "step_key": key,
                    "now": request.now,
                    **fields,
                }
            )
        )

    def _read(self, request: ModelOrderAdvanceRequest) -> ModelGateStoreResult:
        return self._store.transact(
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=OP.READ,
                now=request.now,
            )
        )

    @staticmethod
    def _result(
        request: ModelOrderAdvanceRequest,
        outcome: EnumOrderAdvanceOutcome,
        state: ModelGateStoreResult,
        next_piece: str | None = None,
        detail: str = "",
    ) -> ModelOrderAdvanceResult:
        return ModelOrderAdvanceResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            order_position=state.order_position,
            epoch=state.epoch,
            holder=state.holder,
            next_piece=next_piece,
            detail=detail,
        )

    def handle(self, request: ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult:
        """Read, claim the holder record if it is ours to claim, advance one position if ready."""
        me, run_key = self._policy.holder, self._policy.run_key
        state = self._read(request)
        if state.holder != me:
            if (
                state.holder is not None
                and not self._policy.takes_over_from_other_holder
            ):
                return self._result(
                    request,
                    OUT.REFUSED,
                    state,
                    detail=f"{state.holder} holds the record",
                )
            claim = self._write(
                request,
                OP.HANDOVER,
                f"{run_key}/handover/{state.epoch}",
                holder=me,
                expected_epoch=state.epoch,
            )
            if claim.outcome not in (
                EnumGateStoreOutcome.OK,
                EnumGateStoreOutcome.DUPLICATE,
            ):
                return self._result(
                    request, OUT.HANDOVER_REFUSED, claim, detail=claim.outcome.value
                )
            state = self._read(request)
            if state.holder != me:
                return self._result(
                    request,
                    OUT.FENCED,
                    state,
                    detail="the holder changed after the claim",
                )
        position = state.order_position
        if position >= len(self._order):
            return self._result(request, OUT.COMPLETE, state)
        piece = self._order[position]
        if not self._readiness.is_ready(piece):
            return self._result(request, OUT.WAITING, state, piece, "piece not ready")
        advanced = self._write(
            request,
            OP.ADVANCE_ORDER,
            f"{run_key}/advance/{position + 1}",
            holder=me,
            expected_epoch=state.epoch,
            order_position=position + 1,
        )
        if advanced.outcome in (
            EnumGateStoreOutcome.OK,
            EnumGateStoreOutcome.DUPLICATE,
        ):
            return self._result(request, OUT.ADVANCED, advanced, piece)
        if advanced.outcome is EnumGateStoreOutcome.REFUSED_FENCE:
            return self._result(request, OUT.FENCED, advanced, piece, advanced.detail)
        return self._result(
            request, OUT.REFUSED, advanced, piece, advanced.outcome.value
        )


def answer_command(
    payload: bytes, handler: HandlerOrderAdvance, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One tick command in, the event it produces out, as (topic, JSON bytes).

    A payload that is not a request, or a store that cannot answer, is a typed failure event
    naming the failure, never dropped and never retried silently.
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
            topics["order_advanced"],
            json.dumps(result.model_dump(mode="json"), sort_keys=True).encode(),
        )
    ]
