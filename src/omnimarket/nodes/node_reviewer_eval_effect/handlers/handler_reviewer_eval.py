# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Score the hostile reviewer against its eval set (OMN-17427).

Recall is the share of true catches the reviewer blocks. The false-block rate
is the share of clean heads (merged with no revert, or blocks later shown
wrong) the reviewer blocks. A degraded or missing verdict scores nothing, so a
run that could not review every case is a regression, never a pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_reviewer_eval_effect.models import (
    EnumReviewerVerdict,
    ModelReviewerEvalCaseOutcome,
    ModelReviewerEvalCaseSet,
    ModelReviewerEvalRequest,
    ModelReviewerEvalScored,
)

# Scores are ratios of small counts; compare them with a tolerance well below
# one case's weight so float rounding never reads as a drop.
_EPSILON = 1e-9


def verdict_from_review_result(document: Mapping[str, Any]) -> EnumReviewerVerdict:
    """The gate's verdict on one cli_review result document.

    The same reading as the verdict parse in .github/workflows/hostile-reviewer.yml:
    the reviewer's quorum verdict decides; a quorum block of any other verdict is
    degraded; a document with no quorum block passes only when a model succeeded.
    """
    quorum = document.get("quorum") or {}
    quorum_verdict = (
        str(quorum.get("verdict", "")) if isinstance(quorum, Mapping) else ""
    )
    if quorum_verdict == "blocked":
        return EnumReviewerVerdict.BLOCKED
    if quorum_verdict == "passed":
        return EnumReviewerVerdict.PASSED
    if quorum_verdict or not list(document.get("models_succeeded") or []):
        return EnumReviewerVerdict.DEGRADED
    return EnumReviewerVerdict.PASSED


def _ratchet_reasons(
    prior: ModelReviewerEvalCaseSet, current: ModelReviewerEvalCaseSet
) -> list[str]:
    reasons: list[str] = []
    labels = {case.case_id: case.label for case in current.cases}
    for case in prior.cases:
        if case.case_id not in labels:
            reasons.append(f"case {case.case_id} was removed from the eval set")
        elif labels[case.case_id] is not case.label:
            reasons.append(
                f"case {case.case_id} was relabelled {case.label} -> {labels[case.case_id]}"
            )
    if current.baseline.recall < prior.baseline.recall - _EPSILON:
        reasons.append(
            f"baseline recall lowered {prior.baseline.recall:.4f} -> "
            f"{current.baseline.recall:.4f}"
        )
    if current.baseline.false_block_rate > prior.baseline.false_block_rate + _EPSILON:
        reasons.append(
            f"baseline false-block rate raised {prior.baseline.false_block_rate:.4f} -> "
            f"{current.baseline.false_block_rate:.4f}"
        )
    return reasons


class HandlerReviewerEval:
    """Pure scorer: verdicts in, recall, false-block rate and regression out."""

    def handle(self, request: ModelReviewerEvalRequest) -> ModelReviewerEvalScored:
        case_set = request.case_set
        known = {case.case_id for case in case_set.cases}
        observed: dict[str, EnumReviewerVerdict] = {}
        reasons: list[str] = []
        for observation in request.observations:
            if observation.case_id not in known:
                reasons.append(f"verdict for unknown case {observation.case_id}")
            elif observation.case_id in observed:
                reasons.append(f"two verdicts for case {observation.case_id}")
            else:
                observed[observation.case_id] = observation.verdict

        outcomes: list[ModelReviewerEvalCaseOutcome] = []
        for case in case_set.cases:
            verdict = observed.get(case.case_id, EnumReviewerVerdict.DEGRADED)
            blocked = verdict is EnumReviewerVerdict.BLOCKED
            outcomes.append(
                ModelReviewerEvalCaseOutcome(
                    case_id=case.case_id,
                    label=case.label,
                    should_block=case.should_block,
                    observed=verdict,
                    correct=verdict is not EnumReviewerVerdict.DEGRADED
                    and blocked == case.should_block,
                )
            )

        catches = [o for o in outcomes if o.should_block]
        clean = [o for o in outcomes if not o.should_block]
        caught = sum(o.observed is EnumReviewerVerdict.BLOCKED for o in catches)
        false_blocks = sum(o.observed is EnumReviewerVerdict.BLOCKED for o in clean)
        degraded = [
            o.case_id for o in outcomes if o.observed is EnumReviewerVerdict.DEGRADED
        ]
        recall = caught / len(catches)
        false_block_rate = false_blocks / len(clean)

        baseline = case_set.baseline
        if degraded:
            reasons.append(
                f"{len(degraded)} case(s) have no verdict, so the score is incomplete: "
                + ", ".join(degraded)
            )
        if recall < baseline.recall - _EPSILON:
            reasons.append(
                f"recall {recall:.4f} fell below the baseline {baseline.recall:.4f}"
            )
        if false_block_rate > baseline.false_block_rate + _EPSILON:
            reasons.append(
                f"false-block rate {false_block_rate:.4f} rose above the baseline "
                f"{baseline.false_block_rate:.4f}"
            )
        if request.prior_case_set is not None:
            reasons.extend(_ratchet_reasons(request.prior_case_set, case_set))

        return ModelReviewerEvalScored(
            correlation_id=request.correlation_id,
            reviewer_ref=request.reviewer_ref,
            source=request.source,
            true_catch_cases=len(catches),
            clean_cases=len(clean),
            caught=caught,
            false_blocks=false_blocks,
            degraded=len(degraded),
            recall=recall,
            false_block_rate=false_block_rate,
            baseline=baseline,
            regressed=bool(reasons),
            reasons=tuple(reasons),
            outcomes=tuple(outcomes),
        )


__all__ = ["HandlerReviewerEval", "verdict_from_review_result"]
