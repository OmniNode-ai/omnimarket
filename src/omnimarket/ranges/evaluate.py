# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evaluate a range acceptance line over a set of runs (unified plan row G1).

Pure and deterministic: the same line and runs give the same evaluation.

The line is met when the decision lower bound clears the floor. That bound is
the more conservative of the one-sided bootstrap bound and the exact
Clopper-Pearson bound (see :mod:`omnimarket.ranges.power` for the measurement
that made the exact bound necessary). Both bounds are reported, together with
the two-sided bootstrap interval, so the result is an interval and never a bare
mean.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.ranges import (
    EnumRangeSampleOutcome,
    EnumRangeVerdict,
    ModelRangeAcceptanceLine,
    ModelRangeEvaluation,
    ModelRangeRun,
)
from omnimarket.ranges.bootstrap import (
    BOOTSTRAP_RESAMPLES,
    bootstrap_pass_rate_bounds,
)
from omnimarket.ranges.power import exact_lower_bound, required_sample_size


def _counted_outcomes(
    line: ModelRangeAcceptanceLine, runs: Sequence[ModelRangeRun]
) -> list[EnumRangeSampleOutcome]:
    """Every sample, plus an INCOMPLETE for each declared case a run lacks."""
    outcomes: list[EnumRangeSampleOutcome] = []
    declared = set(line.declared_case_ids)
    for run in runs:
        outcomes.extend(sample.outcome for sample in run.samples)
        if declared:
            present = {sample.case_id for sample in run.samples}
            outcomes.extend(
                EnumRangeSampleOutcome.INCOMPLETE for _ in sorted(declared - present)
            )
    return outcomes


def _pinned_run_reasons(runs: Sequence[ModelRangeRun]) -> list[str]:
    reasons: list[str] = []
    for run in runs:
        if run.sampling_seed is not None:
            reasons.append(
                f"run {run.run_id} pinned the sampling seed ({run.sampling_seed}): "
                "a seed-pinned run measures the sampling budget, not the system"
            )
        if run.temperature_forced:
            reasons.append(
                f"run {run.run_id} forced the sampling temperature: not a range result"
            )
        if run.retried_until_pass:
            reasons.append(
                f"run {run.run_id} retried cases until they passed: "
                "retry-until-green is not a range result"
            )
    return reasons


def evaluate_range_line(
    line: ModelRangeAcceptanceLine, runs: Sequence[ModelRangeRun]
) -> ModelRangeEvaluation:
    """Judge ``runs`` against ``line``. MET passes; MISSED and REFUSED block."""
    method = line.method
    power_n = required_sample_size(
        floor=line.floor,
        margin=method.margin,
        confidence=method.confidence,
        power=method.power,
    )
    required_n = max(power_n, method.sample_size)
    outcomes = _counted_outcomes(line, runs)
    observed_n = len(outcomes)
    passes = outcomes.count(EnumRangeSampleOutcome.PASS)
    incomplete = outcomes.count(EnumRangeSampleOutcome.INCOMPLETE)
    # count_as_failure: an incomplete sample is a failure in the rate, and is
    # still reported by its own count.
    failures = observed_n - passes

    reasons: list[str] = []
    if method.sample_size < power_n:
        reasons.append(
            f"declared n={method.sample_size} was not sized by the power analysis: "
            f"required n={power_n} for floor {line.floor}, margin {method.margin}, "
            f"confidence {method.confidence}, power {method.power}"
        )
    if observed_n == 0:
        reasons.append("no samples: an empty evaluation is refused, never passed")
    elif observed_n < required_n:
        reasons.append(
            f"n={observed_n} is below the power-analysed size: required n={required_n}"
        )
    reasons.extend(_pinned_run_reasons(runs))

    base: dict[str, object] = {
        "check_id": line.check_id,
        "floor": line.floor,
        "confidence": method.confidence,
        "required_n": required_n,
        "declared_n": method.sample_size,
        "observed_n": observed_n,
        "passes": passes,
        "failures": failures,
        "incomplete": incomplete,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
    }
    if observed_n == 0:
        return ModelRangeEvaluation.model_validate(
            {**base, "verdict": EnumRangeVerdict.REFUSED, "reasons": tuple(reasons)}
        )

    bootstrap_lower, low, high = bootstrap_pass_rate_bounds(
        passes=passes, n=observed_n, confidence=method.confidence
    )
    exact_lower = exact_lower_bound(
        passes=passes, n=observed_n, confidence=method.confidence
    )
    lower = min(bootstrap_lower, exact_lower)
    statistics = {
        "point_estimate": passes / observed_n,
        "lower_bound": lower,
        "bootstrap_lower_bound": bootstrap_lower,
        "exact_lower_bound": exact_lower,
        "interval_low": low,
        "interval_high": high,
    }
    if reasons:
        verdict = EnumRangeVerdict.REFUSED
    elif lower >= line.floor:
        verdict = EnumRangeVerdict.MET
    else:
        verdict = EnumRangeVerdict.MISSED
        reasons.append(
            f"one-sided {method.confidence:.0%} lower bound {lower:.4f} "
            f"is below the floor {line.floor}"
        )
    return ModelRangeEvaluation.model_validate(
        {**base, **statistics, "verdict": verdict, "reasons": tuple(reasons)}
    )


__all__ = ["evaluate_range_line"]
