# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing stall fold: which transitions open, refresh and answer a stall."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.liveness.model_landing_stall import (
    EnumLandingStallSubject,
    EnumLandingStallTransition,
    ModelLandingStallTransition,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.handlers.handler_landing_stall_expectation import (
    HandlerLandingStallExpectation,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_fold import (
    ModelLandingStallFoldRequest,
    ModelLandingStallFoldResult,
    ModelLandingStallRoute,
)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_S = EnumLandingStallSubject
_T = EnumLandingStallTransition
ROUTES = (
    ModelLandingStallRoute(subject=_S.CAUSE, process_id="h/cause", host="h"),
    ModelLandingStallRoute(subject=_S.BASE_BRANCH, process_id="h/cause", host="h"),
    ModelLandingStallRoute(subject=_S.VERSION_BUMP, process_id="h/bump", host="h"),
)


def _ev(
    subject: EnumLandingStallSubject,
    transition: EnumLandingStallTransition,
    minute: int,
    key: str = "k",
) -> ModelLandingStallTransition:
    return ModelLandingStallTransition(
        subject=subject,
        transition=transition,
        repo="o/r",
        subject_key=key,
        decision_text=f"{transition.value} at {minute}",
        observed_at=T0 + timedelta(minutes=minute),
    )


def _fold(
    *events: ModelLandingStallTransition,
    routes: tuple[ModelLandingStallRoute, ...] = ROUTES,
) -> ModelLandingStallFoldResult:
    return HandlerLandingStallExpectation().handle(
        ModelLandingStallFoldRequest(transitions=events, routes=routes)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stall", [_T.HELD, _T.CLAIM_REFUSED, _T.ATTEMPT_NEVER_RAN], ids=str
)
def test_landing_stall_fold_opens_at_the_first_stall_and_keeps_it(
    stall: EnumLandingStallTransition,
) -> None:
    result = _fold(_ev(_S.CAUSE, stall, 0), _ev(_S.CAUSE, _T.HELD, 5))
    (e,) = result.expectations
    assert e.process_id == "h/cause"
    assert e.match_key == "o/r:cause:k"
    assert e.triggered_at == T0
    assert e.answered_at is None
    assert e.detail == "held: held at 5"


@pytest.mark.unit
@pytest.mark.parametrize("move", [_T.DISPATCHED, _T.RELEASED, _T.MERGED], ids=str)
def test_landing_stall_fold_a_move_answers_a_held_base(
    move: EnumLandingStallTransition,
) -> None:
    result = _fold(_ev(_S.BASE_BRANCH, _T.HELD, 0), _ev(_S.BASE_BRANCH, move, 30))
    (e,) = result.expectations
    assert e.answered_at == T0 + timedelta(minutes=30)


@pytest.mark.unit
def test_landing_stall_fold_a_hold_after_a_move_opens_a_new_stall() -> None:
    result = _fold(
        _ev(_S.CAUSE, _T.HELD, 0),
        _ev(_S.CAUSE, _T.DISPATCHED, 10),
        _ev(_S.CAUSE, _T.ATTEMPT_NEVER_RAN, 20),
    )
    (e,) = result.expectations
    assert e.triggered_at == T0 + timedelta(minutes=20)
    assert e.answered_at is None


@pytest.mark.unit
def test_landing_stall_fold_a_version_bump_moves_only_on_merge_or_release() -> None:
    open_bump = _fold(
        _ev(_S.VERSION_BUMP, _T.DISPATCHED, 0), _ev(_S.VERSION_BUMP, _T.HELD, 40)
    )
    (e,) = open_bump.expectations
    assert e.process_id == "h/bump"
    assert e.triggered_at == T0
    assert e.answered_at is None
    merged = _fold(
        _ev(_S.VERSION_BUMP, _T.DISPATCHED, 0), _ev(_S.VERSION_BUMP, _T.MERGED, 95)
    )
    assert merged.expectations[0].answered_at == T0 + timedelta(minutes=95)


@pytest.mark.unit
def test_landing_stall_fold_a_move_with_nothing_open_opens_nothing() -> None:
    assert _fold(_ev(_S.CAUSE, _T.DISPATCHED, 0)).expectations == ()


@pytest.mark.unit
def test_landing_stall_fold_reports_an_unrouted_subject() -> None:
    result = _fold(_ev(_S.VERSION_BUMP, _T.HELD, 0), routes=ROUTES[:1])
    assert result.expectations == ()
    assert result.unrouted == ("o/r:version_bump:k",)


@pytest.mark.unit
def test_landing_stall_fold_orders_by_observed_time() -> None:
    late_first = _fold(_ev(_S.CAUSE, _T.HELD, 10), _ev(_S.CAUSE, _T.HELD, 0))
    assert late_first.expectations[0].triggered_at == T0
