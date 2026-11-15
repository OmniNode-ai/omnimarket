# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The next move of a plan's review ladder, from its history alone.

``check_plan_review_ladder(ModelPlanReviewLadderRequest) ->
ModelPlanReviewLadderResult`` decides the plan's next move, in this order: a
first-slice verdict other than ``ok`` returns the plan to its author; with no
round yet the first whole-plan round starts; whole-plan rounds repeat while an
open blocking finding remains and stop in favour of the first slice once none
does (non-blocking open findings never hold a whole-plan round open); a clean
first-slice round hands over to the first pending later rung, an open finding
in the first slice - blocking or not - keeps it under review; a later rung
round with an open finding repeats for that rung, a clean one moves to the
next pending rung or completes the plan. A rung is pending until its LATEST
later_rung round closed with no open finding. Pure: no clock, no I/O, no
model. Every setting comes from the request, the injected settings or the
overlay.
"""

from __future__ import annotations

from omnimarket.nodes.node_plan_first_slice_compute.models.model_plan_first_slice import (
    EnumPlanFirstSliceVerdict,
)
from omnimarket.nodes.node_plan_review_ladder_compute.models.model_plan_review_ladder import (
    EnumPlanReviewMove,
    EnumPlanReviewScope,
    ModelPlanReviewFinding,
    ModelPlanReviewLadderRequest,
    ModelPlanReviewLadderResult,
    ModelPlanReviewLadderSettings,
    ModelPlanReviewRound,
)
from omnimarket.nodes.node_plan_review_ladder_compute.overlay import load_settings


def _count_open_blocking(
    findings: tuple[ModelPlanReviewFinding, ...], blocking_kinds: tuple[str, ...]
) -> int:
    """How many of ``findings`` are open and of a blocking kind."""
    return sum(
        1 for finding in findings if finding.open and finding.kind in blocking_kinds
    )


def _clean_rungs(rounds: tuple[ModelPlanReviewRound, ...]) -> frozenset[str]:
    """The rungs whose LATEST later_rung round closed with no open finding."""
    latest: dict[str, ModelPlanReviewRound] = {}
    for round_ in rounds:
        if round_.scope is EnumPlanReviewScope.LATER_RUNG:
            latest[round_.rung or ""] = round_
    return frozenset(
        rung
        for rung, round_ in latest.items()
        if not any(finding.open for finding in round_.findings)
    )


def _pending_rungs(request: ModelPlanReviewLadderRequest) -> tuple[str, ...]:
    """The plan's later rungs not yet closed by a clean later_rung round."""
    clean = _clean_rungs(request.rounds)
    return tuple(rung for rung in request.later_rungs if rung not in clean)


def check_plan_review_ladder(
    request: ModelPlanReviewLadderRequest,
    settings: ModelPlanReviewLadderSettings,
) -> ModelPlanReviewLadderResult:
    """The plan's next review move under ``settings``: the move, the round it starts,
    the rung it reviews, the last round's open blocking count, and the rule."""
    if request.first_slice_verdict is not EnumPlanFirstSliceVerdict.OK:
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.RETURN_TO_AUTHOR,
            next_scope=None,
            next_rung=None,
            open_blocking=0,
            rule="first_slice_not_ok",
            detail=(
                f"The plan's first slice verdict is {request.first_slice_verdict.value}; "
                "the plan returns to its author."
            ),
        )
    last = request.rounds[-1] if request.rounds else None
    if last is None:
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.FULL_PLAN_ROUND,
            next_scope=EnumPlanReviewScope.FULL_PLAN,
            next_rung=None,
            open_blocking=0,
            rule="no_round_yet",
            detail="The plan has no review round yet; the first whole-plan round starts.",
        )
    open_blocking = _count_open_blocking(last.findings, settings.blocking_kinds)
    if last.scope is EnumPlanReviewScope.FULL_PLAN:
        if open_blocking > 0:
            return ModelPlanReviewLadderResult(
                move=EnumPlanReviewMove.FULL_PLAN_ROUND,
                next_scope=EnumPlanReviewScope.FULL_PLAN,
                next_rung=None,
                open_blocking=open_blocking,
                rule="blocking_open",
                detail=(
                    f"The last whole-plan round leaves {open_blocking} open blocking "
                    "finding(s); another whole-plan round runs."
                ),
            )
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE,
            next_scope=EnumPlanReviewScope.FIRST_SLICE,
            next_rung=None,
            open_blocking=open_blocking,
            rule="no_blocking_left",
            detail=(
                "No blocking finding is open on the whole plan; the review moves to "
                "the first slice alone."
            ),
        )
    if last.scope is EnumPlanReviewScope.FIRST_SLICE:
        if any(finding.open for finding in last.findings):
            return ModelPlanReviewLadderResult(
                move=EnumPlanReviewMove.FIRST_SLICE_ROUND,
                next_scope=EnumPlanReviewScope.FIRST_SLICE,
                next_rung=None,
                open_blocking=open_blocking,
                rule="first_slice_open",
                detail=(
                    "The first slice still carries an open finding; another "
                    "first-slice round runs."
                ),
            )
        pending = _pending_rungs(request)
        if pending:
            detail = (
                "The first slice round is clean; the first slice is built and the "
                f"next rung {pending[0]!r} is next."
            )
        else:
            detail = "The first slice round is clean; no later rung is pending."
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.BUILD_FIRST_SLICE,
            next_scope=None,
            next_rung=pending[0] if pending else None,
            open_blocking=open_blocking,
            rule="first_slice_clean",
            detail=detail,
        )
    # The last round covered a later rung.
    if any(finding.open for finding in last.findings):
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.RUNG_ROUND,
            next_scope=EnumPlanReviewScope.LATER_RUNG,
            next_rung=last.rung,
            open_blocking=open_blocking,
            rule="rung_open",
            detail=(
                f"The rung {last.rung!r} still carries an open finding; another "
                "round of the same rung runs."
            ),
        )
    pending = _pending_rungs(request)
    if not pending:
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.PLAN_COMPLETE,
            next_scope=None,
            next_rung=None,
            open_blocking=open_blocking,
            rule="all_rungs_clean",
            detail=(
                f"The rung {last.rung!r} closed clean and no later rung is pending; "
                "the plan is complete."
            ),
        )
    return ModelPlanReviewLadderResult(
        move=EnumPlanReviewMove.RUNG_ROUND,
        next_scope=EnumPlanReviewScope.LATER_RUNG,
        next_rung=pending[0],
        open_blocking=open_blocking,
        rule="next_rung",
        detail=(
            f"The rung {last.rung!r} closed clean; the next pending rung "
            f"{pending[0]!r} is next."
        ),
    )


class HandlerPlanReviewLadder:
    """The plan review ladder: definition-B compute.

    The settings come from the request, else from the ones injected at construction,
    else from the overlay, read once at the first call that needs them.
    """

    def __init__(self, settings: ModelPlanReviewLadderSettings | None = None) -> None:
        self._settings = settings

    def handle(
        self, request: ModelPlanReviewLadderRequest
    ) -> ModelPlanReviewLadderResult:
        settings = request.settings or self._settings
        if settings is None:
            settings = self._settings = load_settings()
        return check_plan_review_ladder(request, settings)


__all__: list[str] = [
    "HandlerPlanReviewLadder",
    "check_plan_review_ladder",
]
