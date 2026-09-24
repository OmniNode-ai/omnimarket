# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evaluate a range acceptance line over a set of runs (unified plan row G1).

Pure and deterministic: the same line and runs give the same evaluation. The
bootstrap resampling uses a fixed evaluator seed. That seed belongs to the
EVALUATOR, so the confidence interval is reproducible; it is not the model
sampling seed of a run, which rule 4 forbids pinning and which this module
refuses.

A pass-rate sample is a Bernoulli draw, so a nonparametric bootstrap resample
of n samples has exactly a Binomial(n, p_hat) pass count; each resample is
drawn that way rather than by resampling the list, which is the same
distribution at a fraction of the cost.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from omnimarket.models.ranges import (
    EnumRangeSampleOutcome,
    EnumRangeVerdict,
    ModelRangeAcceptanceLine,
    ModelRangeEvaluation,
    ModelRangeRun,
)
from omnimarket.ranges.power import required_sample_size

#: Resamples per bootstrap. Fixed, so an interval is reproducible and
#: comparable between evaluations.
BOOTSTRAP_RESAMPLES = 10_000

#: The evaluator's own resampling seed (see the module docstring).
_EVALUATOR_BOOTSTRAP_SEED = 20260923


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


def bootstrap_pass_rate_bounds(
    *, passes: int, n: int, confidence: float
) -> tuple[float, float, float]:
    """(one-sided lower bound, two-sided low, two-sided high) at ``confidence``."""
    if n <= 0:
        raise ValueError("a bootstrap needs at least one sample")
    rate = passes / n
    rng = random.Random(_EVALUATOR_BOOTSTRAP_SEED)
    draws = sorted(rng.binomialvariate(n, rate) / n for _ in range(BOOTSTRAP_RESAMPLES))
    alpha = 1.0 - confidence

    def quantile(q: float) -> float:
        index = min(len(draws) - 1, max(0, int(q * len(draws))))
        return draws[index]

    return quantile(alpha), quantile(alpha / 2.0), quantile(1.0 - alpha / 2.0)


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

    lower, low, high = bootstrap_pass_rate_bounds(
        passes=passes, n=observed_n, confidence=method.confidence
    )
    statistics = {
        "point_estimate": passes / observed_n,
        "lower_bound": lower,
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


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "bootstrap_pass_rate_bounds",
    "evaluate_range_line",
]
