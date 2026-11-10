# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold validator outcomes into a pull request verdict, one-way ratchets and a canonical count.

``fold`` is pure: state and request in, result out. ``HandlerValidatorVerdictReducer``
loads the state from the injected store, folds, and saves the new state.

A ratchet reading that would move its baseline against the declared direction is refused:
the baseline stays where it was and the pull request's verdict is ``fail``. A reading for a
key the overlay does not declare is refused the same way. The first reading of a declared
key seeds its baseline.
"""

from __future__ import annotations

from omnimarket.models.model_validator_verdict import (
    EnumPrVerdict,
    EnumRatchetDirection,
    ModelPrVerdict,
    ModelRatchetBaseline,
    ModelRatchetMove,
    ModelRatchetReading,
    ModelRatchetRefusal,
    ModelVerdictFoldRequest,
    ModelVerdictFoldResult,
    ModelVerdictState,
    pr_key,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    ModelVerdictReducerOverlay,
)
from omnimarket.nodes.node_validator_verdict_reducer.protocols import (
    ProtocolVerdictStore,
)

REASON_AGAINST_DIRECTION = "against-direction"
REASON_UNDECLARED = "undeclared-ratchet"


def allowed(direction: EnumRatchetDirection, before: int, after: int) -> bool:
    if direction is EnumRatchetDirection.SHRINK_ONLY:
        return after <= before
    return after >= before


def _ratchet(
    baselines: dict[str, ModelRatchetBaseline],
    reading: ModelRatchetReading,
    overlay: ModelVerdictReducerOverlay,
) -> ModelRatchetMove | ModelRatchetRefusal | None:
    declaration = overlay.declaration(reading.key)
    current = baselines.get(reading.key)
    if declaration is None:
        return ModelRatchetRefusal(
            key=reading.key,
            reason=REASON_UNDECLARED,
            baseline=None if current is None else current.value,
            proposed=reading.value,
        )
    if current is None:
        baselines[reading.key] = ModelRatchetBaseline(
            key=reading.key, direction=declaration.direction, value=reading.value
        )
        return ModelRatchetMove(key=reading.key, before=None, after=reading.value)
    if not allowed(declaration.direction, current.value, reading.value):
        return ModelRatchetRefusal(
            key=reading.key,
            reason=REASON_AGAINST_DIRECTION,
            baseline=current.value,
            proposed=reading.value,
        )
    if reading.value == current.value:
        return None
    baselines[reading.key] = current.model_copy(update={"value": reading.value})
    return ModelRatchetMove(key=reading.key, before=current.value, after=reading.value)


def fold(
    state: ModelVerdictState,
    request: ModelVerdictFoldRequest,
    overlay: ModelVerdictReducerOverlay,
) -> ModelVerdictFoldResult:
    baselines = dict(state.baselines)
    moves: list[ModelRatchetMove] = []
    refusals: list[ModelRatchetRefusal] = []
    for reading in request.ratchet_readings:
        outcome = _ratchet(baselines, reading, overlay)
        if isinstance(outcome, ModelRatchetMove):
            moves.append(outcome)
        elif isinstance(outcome, ModelRatchetRefusal):
            refusals.append(outcome)
    failing = tuple(
        sorted(o.validator_id for o in request.outcomes if o.failing_count > 0)
    )
    errored = tuple(
        sorted(o.validator_id for o in request.outcomes if o.error is not None)
    )
    verdict = ModelPrVerdict(
        repo=request.repo,
        pr=request.pr,
        head=request.head,
        verdict=(
            EnumPrVerdict.FAIL if failing or errored or refusals else EnumPrVerdict.PASS
        ),
        failing_validators=failing,
        errored_validators=errored,
        refused_ratchets=tuple(refusals),
    )
    canonical = tuple(
        sorted(
            set(state.canonical_validators)
            | {
                o.validator_id
                for o in request.outcomes
                if o.canonical and o.error is None
            }
        )
    )
    new_state = ModelVerdictState(
        verdicts={**state.verdicts, pr_key(request.repo, request.pr): verdict},
        baselines=baselines,
        canonical_validators=canonical,
    )
    return ModelVerdictFoldResult(
        correlation_id=request.correlation_id,
        verdict=verdict,
        moves=tuple(moves),
        canonical_validator_count=new_state.canonical_validator_count,
        state=new_state,
    )


class HandlerValidatorVerdictReducer:
    """Load the state from the store, fold one request, save the new state."""

    def __init__(
        self, *, overlay: ModelVerdictReducerOverlay, store: ProtocolVerdictStore
    ) -> None:
        self._overlay = overlay
        self._store = store

    def handle(self, request: ModelVerdictFoldRequest) -> ModelVerdictFoldResult:
        result = fold(self._store.load(), request, self._overlay)
        self._store.save(result.state)
        return result
