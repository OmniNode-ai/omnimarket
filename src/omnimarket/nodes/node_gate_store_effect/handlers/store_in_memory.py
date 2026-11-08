# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory twin of the gate store (plan section 7.5: ``timer_store``'s CI backend).

One lock is the one transaction. A request is applied to a copy of the state and the copy
replaces the state only after the commit stage, so a crash at any stage leaves the state as
it was. The fault hook is the kill point of the contract suite: it is called at each stage
and a backend that cannot be killed at a stage cannot pass the suite.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)

#: The stages a kill can land on, in the order a request passes them.
STAGES: tuple[str, ...] = ("before-apply", "after-apply", "before-commit")

_MUTATING = frozenset(EnumGateStoreOperation) - {EnumGateStoreOperation.READ}
_SAFETY_STATES = frozenset({"stop", "clear"})


@dataclass
class _Reservation:
    piece: str
    expires_at: float
    consumed: bool = False


@dataclass
class _State:
    epoch: int = 0
    holder: str | None = None
    order_position: int = 0
    reservations: dict[str, _Reservation] = field(default_factory=dict)
    safety: dict[str, str] = field(default_factory=dict)
    steps: dict[str, ModelGateStoreResult] = field(default_factory=dict)

    def counted(self, piece: str) -> int:
        return sum(1 for r in self.reservations.values() if r.piece == piece)


class InMemoryGateStore:
    """A serializable store held in memory; safe to call from many threads."""

    def __init__(
        self,
        policy: ModelGateStorePolicy,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._policy = policy
        self._fault = fault_hook
        self._lock = threading.Lock()
        self._state = _State()

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        """Apply one request in one transaction; a kill at any stage leaves no trace."""
        with self._lock:
            self._at("before-apply")
            work = copy.deepcopy(self._state)
            result = self._apply(work, request)
            self._at("after-apply")
            self._at("before-commit")
            self._state = work
            return result

    def _at(self, stage: str) -> None:
        if self._fault is not None:
            self._fault(stage)

    def _answer(
        self,
        state: _State,
        request: ModelGateStoreRequest,
        outcome: EnumGateStoreOutcome,
        detail: str = "",
        piece: str | None = None,
    ) -> ModelGateStoreResult:
        return ModelGateStoreResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=outcome,
            epoch=state.epoch,
            counted=state.counted(piece) if piece is not None else 0,
            holder=state.holder,
            order_position=state.order_position,
            detail=detail,
        )

    def _apply(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        op = request.operation
        if op is EnumGateStoreOperation.READ:
            return self._answer(
                state, request, EnumGateStoreOutcome.OK, piece=request.piece
            )
        if request.step_key is None:
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "a mutating request carries a step key",
            )
        first = state.steps.get(request.step_key)
        if first is not None:
            return first.model_copy(
                update={
                    "correlation_id": request.correlation_id,
                    "outcome": EnumGateStoreOutcome.DUPLICATE,
                    "detail": f"replay of {request.step_key}: first answer {first.outcome}",
                }
            )
        handlers = {
            EnumGateStoreOperation.RESERVE: self._reserve,
            EnumGateStoreOperation.CONSUME: self._consume,
            EnumGateStoreOperation.RELEASE: self._release,
            EnumGateStoreOperation.RECORD_SAFETY: self._record_safety,
            EnumGateStoreOperation.HANDOVER: self._handover,
            EnumGateStoreOperation.ADVANCE_ORDER: self._advance_order,
        }
        result = handlers[op](state, request)
        state.steps[request.step_key] = result
        return result

    def _write(
        self,
        state: _State,
        request: ModelGateStoreRequest,
        piece: str | None = None,
    ) -> ModelGateStoreResult:
        state.epoch += 1
        return self._answer(state, request, EnumGateStoreOutcome.OK, piece=piece)

    def _sweep(self, state: _State, now: float) -> None:
        for key in [
            k
            for k, r in state.reservations.items()
            if not r.consumed and r.expires_at <= now
        ]:
            del state.reservations[key]

    def _reserve(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        piece, rid = request.piece, request.reservation_id
        if piece is None or rid is None:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_INVALID, "piece and id"
            )
        limit = self._policy.limits.get(piece)
        if limit is None:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_PIECE, piece
            )
        if state.safety.get(piece) == "stop":
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_STOP, piece, piece
            )
        self._sweep(state, request.now)
        if rid in state.reservations:
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "reservation id in use",
                piece,
            )
        if state.counted(piece) >= limit:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_LIMIT, piece, piece
            )
        state.reservations[rid] = _Reservation(
            piece=piece,
            expires_at=request.now + self._policy.reservation_ttl_seconds,
        )
        return self._write(state, request, piece)

    def _consume(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        res = state.reservations.get(request.reservation_id or "")
        if res is None:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_RESERVATION
            )
        if res.consumed:
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_CONSUMED,
                piece=res.piece,
            )
        if res.expires_at <= request.now:
            self._sweep(state, request.now)
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_EXPIRED, piece=res.piece
            )
        if state.safety.get(res.piece) == "stop":
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_STOP, piece=res.piece
            )
        res.consumed = True
        return self._write(state, request, res.piece)

    def _release(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        rid = request.reservation_id or ""
        res = state.reservations.get(rid)
        if res is None:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_RESERVATION
            )
        if res.consumed and not request.process_gone:
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_PROCESS_LIVE,
                "a consumed reservation counts until its process is read as gone",
                res.piece,
            )
        del state.reservations[rid]
        return self._write(state, request, res.piece)

    def _record_safety(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        piece = request.piece
        if (
            piece is None
            or piece not in self._policy.limits
            or request.safety_state not in _SAFETY_STATES
        ):
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "a known piece and a state of stop or clear",
            )
        state.safety[piece] = request.safety_state or "clear"
        return self._write(state, request, piece)

    def _handover(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        if request.holder is None or request.expected_epoch is None:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_INVALID, "holder and epoch"
            )
        if request.expected_epoch != state.epoch:
            return self._answer(
                state, request, EnumGateStoreOutcome.REFUSED_FENCE, "stale epoch"
            )
        state.holder = request.holder
        return self._write(state, request)

    def _advance_order(
        self, state: _State, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        if (
            request.holder is None
            or request.expected_epoch is None
            or request.order_position is None
        ):
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "holder, epoch and position",
            )
        if request.holder != state.holder or request.expected_epoch != state.epoch:
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_FENCE,
                "not the holder at this epoch",
            )
        if (
            request.order_position != state.order_position + 1
            or request.order_position > len(self._policy.order)
        ):
            return self._answer(
                state,
                request,
                EnumGateStoreOutcome.REFUSED_ORDER,
                "the order advances one position at a time within its length",
            )
        state.order_position = request.order_position
        return self._write(state, request)
