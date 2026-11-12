# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paired comparison of the two arms of the context injection A/B (OMN-20017).

``handle`` takes the per-task outcomes of two arms at one size band and one
attempt budget and answers the exact paired test already in
``omnimarket.ranges.compare``: no second statistical test and no second verdict
enum live here. The arms are paired by ``task_key``, never by position; a task
one arm did not answer is INCOMPLETE for that arm, because a missing run counts
as a failure and is never excluded.

The margin, confidence and power are not literals in this file: the handler
reads them from the overlay key ``delegation.capability_map.line`` (the
capability map's own class line) on each call, unless the caller injected one.
A negative control that reproduces the arm's gain cancels it: the verdict reads
NO_DIFFERENCE and ``control_cancelled_gain`` is true, so a gain the unchanged
comparison also produces is not counted. A malformed request ends as a typed
failure and never a verdict. No clock, no file read other than the overlay, no
model call.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.delegation_arm_compare.enum_arm_compare_failure import (
    EnumArmCompareFailure,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_overlay import (
    ModelArmCompareOverlay,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_result import (
    ModelArmCompareResult,
)
from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)
from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.models.ranges.enum_incomplete_run_treatment import (
    EnumIncompleteRunTreatment,
)
from omnimarket.models.ranges.enum_range_sample_outcome import EnumRangeSampleOutcome
from omnimarket.models.ranges.model_comparison_method import ModelComparisonMethod
from omnimarket.models.ranges.model_comparison_pair import ModelComparisonPair
from omnimarket.models.ranges.model_comparison_result import ModelComparisonResult
from omnimarket.ranges.compare import (
    compare_paired_outcomes,
    required_comparison_size,
)

_OVERLAY_ID = "node_delegation_arm_compare_compute"


def _failure(
    comparison_id: str, failure: EnumArmCompareFailure, detail: str
) -> ModelArmCompareResult:
    return ModelArmCompareResult(
        comparison_id=comparison_id, failure=failure, detail=detail
    )


def _first_repeated_task_key(
    outcomes: Sequence[ModelArmTaskOutcome],
) -> str | None:
    seen: set[str] = set()
    for outcome in outcomes:
        if outcome.task_key in seen:
            return outcome.task_key
        seen.add(outcome.task_key)
    return None


class HandlerDelegationArmCompare:
    """Measure one arm pair by the exact paired test, with its negative control."""

    def __init__(self, overlay: ModelArmCompareOverlay | None = None) -> None:
        self._overlay = overlay

    def handle(self, request: ModelArmCompareRequest) -> ModelArmCompareResult:
        if request.arm_a_model == request.arm_b_model:
            return _failure(
                request.comparison_id,
                EnumArmCompareFailure.SAME_ARM,
                f"arm_a_model and arm_b_model are both {request.arm_a_model!r}: "
                "an arm is never compared with itself",
            )
        for label, outcomes in (
            ("outcomes_a", request.outcomes_a),
            ("outcomes_b", request.outcomes_b),
            ("control_outcomes_a", request.control_outcomes_a),
            ("control_outcomes_b", request.control_outcomes_b),
        ):
            repeated = _first_repeated_task_key(outcomes)
            if repeated is not None:
                return _failure(
                    request.comparison_id,
                    EnumArmCompareFailure.DUPLICATE_TASK,
                    f"{label} repeats task_key {repeated!r}",
                )
        if not request.outcomes_a and not request.outcomes_b:
            return _failure(
                request.comparison_id,
                EnumArmCompareFailure.NO_TASKS,
                "outcomes_a and outcomes_b are both empty: no tasks to pair",
            )

        overlay = self._overlay
        if overlay is None:
            try:
                overlay = load_structured_overlay(_OVERLAY_ID, ModelArmCompareOverlay)
            except NodeOverlayError as exc:
                return _failure(
                    request.comparison_id,
                    EnumArmCompareFailure.OVERLAY_UNAVAILABLE,
                    str(exc),
                )
        try:
            sample_size = required_comparison_size(
                margin=overlay.line.margin,
                confidence=overlay.line.confidence,
                power=overlay.line.power,
            )
        except ValueError as exc:
            return _failure(
                request.comparison_id,
                EnumArmCompareFailure.OVERLAY_UNAVAILABLE,
                str(exc),
            )
        method = ModelComparisonMethod(
            sample_size=sample_size,
            confidence=overlay.line.confidence,
            power=overlay.line.power,
            margin=overlay.line.margin,
            incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
        )

        pairs = self._pair(request.outcomes_a, request.outcomes_b)
        result = compare_paired_outcomes(request.comparison_id, pairs, method)
        control: ModelComparisonResult | None = None
        control_cancelled_gain = False
        if request.control_outcomes_a or request.control_outcomes_b:
            control = compare_paired_outcomes(
                f"{request.comparison_id}:control",
                self._pair(request.control_outcomes_a, request.control_outcomes_b),
                method,
            )
            if self._control_reproduces_the_gain(result, control):
                result = result.model_copy(
                    update={
                        "verdict": EnumComparisonVerdict.NO_DIFFERENCE,
                        "reasons": (
                            *result.reasons,
                            "negative control reproduced the difference: exact "
                            f"McNemar p={control.p_value:.4g} on "
                            f"{control.a_only + control.b_only} discordant pairs; "
                            "the gain is not counted",
                        ),
                    }
                )
                control_cancelled_gain = True
        return ModelArmCompareResult(
            comparison_id=request.comparison_id,
            result=result,
            control=control,
            control_cancelled_gain=control_cancelled_gain,
        )

    @staticmethod
    def _control_reproduces_the_gain(
        result: ModelComparisonResult, control: ModelComparisonResult
    ) -> bool:
        """True when both the arms and the unchanged control found a signed gain."""
        if (
            result.verdict is not EnumComparisonVerdict.DIFFERENCE
            or control.verdict is not EnumComparisonVerdict.DIFFERENCE
            or result.difference is None
            or control.difference is None
            or control.p_value is None
        ):
            return False
        same_sign = (result.difference > 0 and control.difference > 0) or (
            result.difference < 0 and control.difference < 0
        )
        return same_sign

    def _pair(
        self,
        outcomes_a: Sequence[ModelArmTaskOutcome],
        outcomes_b: Sequence[ModelArmTaskOutcome],
    ) -> tuple[ModelComparisonPair, ...]:
        """Pair the two arms by task key over the union of their tasks, sorted.

        A task one arm did not answer is INCOMPLETE for that arm: a missing run
        counts as a failure and is never excluded, as ranges/compare.py states.
        Pairing is never by position.
        """
        answers_a = {outcome.task_key: outcome.outcome for outcome in outcomes_a}
        answers_b = {outcome.task_key: outcome.outcome for outcome in outcomes_b}
        return tuple(
            ModelComparisonPair(
                case_id=task_key,
                outcome_a=answers_a.get(task_key, EnumRangeSampleOutcome.INCOMPLETE),
                outcome_b=answers_b.get(task_key, EnumRangeSampleOutcome.INCOMPLETE),
            )
            for task_key in sorted(answers_a.keys() | answers_b.keys())
        )
