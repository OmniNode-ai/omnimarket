# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory twin of the piece records (plan section 7.5: ``timer_store``'s CI backend).

One lock is the one transaction. A request is applied to a copy of the state and the copy replaces
the state only on commit, so a request that raises leaves the state as it was. The fence is the
newest token adopted: a write is accepted only from the holder whose adopted token is that newest
one, so a writer that lost the handover cannot write a record after the new holder adopted.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from omnimarket.models.model_order_advance import (
    EnumPieceRecordOperation,
    EnumPieceRecordOutcome,
    EnumPieceState,
    ModelPieceRecord,
    ModelPieceRecordRequest,
    ModelPieceRecordResult,
)

#: Each state's one legal successor set; a piece with no record starts at ``started``.
TRANSITIONS: dict[EnumPieceState | None, frozenset[EnumPieceState]] = {
    None: frozenset({EnumPieceState.STARTED}),
    EnumPieceState.STARTED: frozenset({EnumPieceState.LIVE}),
    EnumPieceState.LIVE: frozenset({EnumPieceState.IN_COMPARISON}),
    EnumPieceState.IN_COMPARISON: frozenset(
        {EnumPieceState.KEPT, EnumPieceState.ROLLED_BACK}
    ),
    EnumPieceState.KEPT: frozenset(),
    EnumPieceState.ROLLED_BACK: frozenset(),
}


@dataclass
class _State:
    pieces: dict[str, ModelPieceRecord] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    high_water: int = 0
    steps: dict[str, ModelPieceRecordResult] = field(default_factory=dict)


class InMemoryPieceRecords:
    """Piece records held in memory; safe to call from many threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _State()

    def transact(self, request: ModelPieceRecordRequest) -> ModelPieceRecordResult:
        """Apply one request in one transaction."""
        with self._lock:
            work = copy.deepcopy(self._state)
            result = self._apply(work, request)
            self._state = work
            return result

    def _answer(
        self,
        state: _State,
        request: ModelPieceRecordRequest,
        outcome: EnumPieceRecordOutcome,
        detail: str = "",
    ) -> ModelPieceRecordResult:
        piece = state.pieces.get(request.piece) if request.piece else None
        return ModelPieceRecordResult(
            operation=request.operation,
            outcome=outcome,
            record=piece,
            high_water=state.high_water,
            holder_token=state.tokens.get(request.holder or ""),
            detail=detail,
        )

    def _apply(
        self, state: _State, request: ModelPieceRecordRequest
    ) -> ModelPieceRecordResult:
        if request.operation is EnumPieceRecordOperation.READ:
            return self._answer(state, request, EnumPieceRecordOutcome.OK)
        if request.step_key is None:
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_INVALID,
                "a mutating request carries a step key",
            )
        first = state.steps.get(request.step_key)
        if first is not None:
            return first.model_copy(
                update={
                    "outcome": EnumPieceRecordOutcome.DUPLICATE,
                    "detail": f"replay of {request.step_key}",
                }
            )
        if request.operation is EnumPieceRecordOperation.ADOPT:
            result = self._adopt(state, request)
        else:
            result = self._write(state, request)
        # Only a committed answer is the key's first answer: a refusal leaves the key free, so a
        # writer that adopts a newer token can retry the same step.
        if result.outcome is EnumPieceRecordOutcome.OK:
            state.steps[request.step_key] = result
        return result

    def _adopt(
        self, state: _State, request: ModelPieceRecordRequest
    ) -> ModelPieceRecordResult:
        if request.holder is None or request.fence_token is None:
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_INVALID,
                "holder and token",
            )
        if request.fence_token < state.high_water:
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_FENCE,
                "a newer token was adopted",
            )
        state.high_water = request.fence_token
        state.tokens[request.holder] = request.fence_token
        return self._answer(state, request, EnumPieceRecordOutcome.OK)

    def _write(
        self, state: _State, request: ModelPieceRecordRequest
    ) -> ModelPieceRecordResult:
        if (
            request.piece is None
            or request.to_state is None
            or request.holder is None
            or request.fence_token is None
        ):
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_INVALID,
                "piece, state, holder and token",
            )
        if (
            request.fence_token != state.high_water
            or state.tokens.get(request.holder) != request.fence_token
        ):
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_FENCE,
                "not the holder of the newest token",
            )
        current = state.pieces.get(request.piece)
        before = current.state if current is not None else None
        if request.to_state not in TRANSITIONS[before]:
            return self._answer(
                state,
                request,
                EnumPieceRecordOutcome.REFUSED_TRANSITION,
                f"{before.value if before else 'none'} to {request.to_state.value}",
            )
        live_at = current.live_at if current is not None else None
        if request.to_state is EnumPieceState.LIVE:
            live_at = request.now
        state.pieces[request.piece] = ModelPieceRecord(
            piece=request.piece,
            state=request.to_state,
            live_at=live_at,
            updated_at=request.now,
        )
        return self._answer(state, request, EnumPieceRecordOutcome.OK)
