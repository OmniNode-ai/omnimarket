# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paired comparison of two rungs on the same prompts (unified plan row G3).

Each prompt is answered by rung A and rung B and both answers are graded by the
same grader, so the data are pairs. Only the discordant pairs, where exactly one
rung passed, carry information about the difference. The verdict is the exact
two-sided McNemar test on them: a binomial test of the discordant split against
one half. It never exceeds its nominal false-difference rate, at any n.

SIZING. The number of prompts n is sized against the worst case for that test.
With discordance psi (the share of pairs where the rungs disagree) and a true
difference delta in pass rate, the normal approximation needs

    n = (z_{1-alpha/2} * sqrt(psi) + z_power * sqrt(psi - delta**2))**2 / delta**2

which grows with psi, so psi = 1 (every pair discordant) is the worst case and
the approximation there is the starting point. n is then found by exact search:
the smallest n, from that start, whose exact power at psi = 1 is at least the
stated power. The tests assert the exact power at other discordances and the
exact false-difference rate at the sized n.

The reported interval is a paired bootstrap interval of the difference at the
same confidence, with the evaluator's own fixed resampling seed (never a model
sampling seed). It is reported beside the verdict; the verdict is the exact test.

Standard library only.
"""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Sequence
from statistics import NormalDist

from omnimarket.models.ranges import (
    EnumComparisonVerdict,
    EnumRangeSampleOutcome,
    ModelComparisonMethod,
    ModelComparisonPair,
    ModelComparisonResult,
)
from omnimarket.ranges.bootstrap import BOOTSTRAP_RESAMPLES
from omnimarket.ranges.power import binomial_upper_tail

_EVALUATOR_BOOTSTRAP_SEED = 20260924
_SEARCH_LIMIT_FACTOR = 10


def _validate(*, margin: float, confidence: float, power: float) -> None:
    if not 0.0 < margin < 1.0:
        raise ValueError(f"margin must be strictly between 0 and 1, got {margin}")
    if not 0.5 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0.5, 1), got {confidence}")
    if not 0.0 < power < 1.0:
        raise ValueError(f"power must be in (0, 1), got {power}")


def _binomial_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    return math.exp(
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def mcnemar_exact_p_value(*, a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts."""
    if a_only < 0 or b_only < 0:
        raise ValueError("discordant counts are never negative")
    discordant = a_only + b_only
    if discordant == 0:
        return 1.0
    smaller = min(a_only, b_only)
    # P(X <= smaller) for X ~ Bin(m, 1/2) equals P(X >= m - smaller) by symmetry.
    return min(1.0, 2.0 * binomial_upper_tail(discordant - smaller, discordant, 0.5))


@functools.cache
def _critical_count(discordant: int, confidence: float) -> int:
    """The largest k whose two-sided p-value is at most alpha, or -1."""
    alpha = 1.0 - confidence
    critical = -1
    for k in range(discordant // 2 + 1):
        if mcnemar_exact_p_value(a_only=k, b_only=discordant - k) <= alpha:
            critical = k
        else:
            break
    return critical


def _rejection_probability(discordant: int, share_b: float, confidence: float) -> float:
    """P(the exact test rejects | m discordant pairs, each favouring B w.p. share_b)."""
    critical = _critical_count(discordant, confidence)
    if critical < 0:
        return 0.0
    low = sum(_binomial_pmf(k, discordant, share_b) for k in range(critical + 1))
    high = sum(
        _binomial_pmf(k, discordant, share_b)
        for k in range(discordant - critical, discordant + 1)
    )
    return min(1.0, low + high)


def _decision_probability(
    *, n: int, discordance: float, share_b: float, confidence: float
) -> float:
    return math.fsum(
        _binomial_pmf(m, n, discordance)
        * _rejection_probability(m, share_b, confidence)
        for m in range(n + 1)
    )


def exact_comparison_power(
    *, n: int, discordance: float, difference: float, confidence: float
) -> float:
    """Exact probability of a DIFFERENCE verdict when B's rate exceeds A's by
    ``difference`` and the rungs disagree on a share ``discordance`` of prompts."""
    if not 0.0 < difference <= discordance <= 1.0:
        raise ValueError(
            f"need 0 < difference {difference} <= discordance {discordance} <= 1"
        )
    share_b = (discordance + difference) / (2.0 * discordance)
    return _decision_probability(
        n=n, discordance=discordance, share_b=share_b, confidence=confidence
    )


def exact_false_difference_rate(
    *, n: int, discordance: float, confidence: float
) -> float:
    """Exact probability of a DIFFERENCE verdict when the rungs are equal."""
    if not 0.0 < discordance <= 1.0:
        raise ValueError(f"discordance must be in (0, 1], got {discordance}")
    return _decision_probability(
        n=n, discordance=discordance, share_b=0.5, confidence=confidence
    )


def normal_approximation_comparison_size(
    *, margin: float, confidence: float, power: float
) -> int:
    """The worst-case (every pair discordant) normal approximation; the start of
    the exact search, not the answer."""
    _validate(margin=margin, confidence=confidence, power=power)
    standard_normal = NormalDist()
    z_alpha = standard_normal.inv_cdf(1.0 - (1.0 - confidence) / 2.0)
    z_power = standard_normal.inv_cdf(power)
    root = z_alpha + z_power * math.sqrt(1.0 - margin**2)
    return max(1, math.ceil((root / margin) ** 2 - 1e-9))


@functools.cache
def required_comparison_size(*, margin: float, confidence: float, power: float) -> int:
    """Prompts a comparison needs: exact power at every pair discordant >= power."""
    start = normal_approximation_comparison_size(
        margin=margin, confidence=confidence, power=power
    )
    for n in range(start, start * _SEARCH_LIMIT_FACTOR + 1):
        if (
            exact_comparison_power(
                n=n, discordance=1.0, difference=margin, confidence=confidence
            )
            >= power
        ):
            return n
    raise ValueError(
        f"no n up to {start * _SEARCH_LIMIT_FACTOR} reaches power {power} at "
        f"margin {margin}; widen the margin"
    )


def _bootstrap_difference_interval(
    *, n: int, a_only: int, b_only: int, confidence: float
) -> tuple[float, float]:
    rng = random.Random(_EVALUATOR_BOOTSTRAP_SEED)
    share_a = a_only / n
    rest = n - a_only
    share_b_of_rest = b_only / rest if rest else 0.0
    draws: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        resampled_a = rng.binomialvariate(n, share_a)
        resampled_b = rng.binomialvariate(n - resampled_a, share_b_of_rest)
        draws.append((resampled_b - resampled_a) / n)
    draws.sort()
    alpha = 1.0 - confidence

    def quantile(q: float) -> float:
        return draws[min(len(draws) - 1, max(0, int(q * len(draws))))]

    return quantile(alpha / 2.0), quantile(1.0 - alpha / 2.0)


def compare_paired_outcomes(
    comparison_id: str,
    pairs: Sequence[ModelComparisonPair],
    method: ModelComparisonMethod,
) -> ModelComparisonResult:
    """Judge rung B against rung A over the same prompts."""
    seen: set[str] = set()
    for pair in pairs:
        if pair.case_id in seen:
            raise ValueError(f"duplicate case_id {pair.case_id!r}")
        seen.add(pair.case_id)

    power_n = required_comparison_size(
        margin=method.margin, confidence=method.confidence, power=method.power
    )
    required_n = max(power_n, method.sample_size)
    observed_n = len(pairs)
    passed_a = [pair.outcome_a is EnumRangeSampleOutcome.PASS for pair in pairs]
    passed_b = [pair.outcome_b is EnumRangeSampleOutcome.PASS for pair in pairs]
    a_only = sum(a and not b for a, b in zip(passed_a, passed_b, strict=True))
    b_only = sum(b and not a for a, b in zip(passed_a, passed_b, strict=True))

    reasons: list[str] = []
    if method.sample_size < power_n:
        reasons.append(
            f"declared n={method.sample_size} was not sized by the power analysis: "
            f"required n={power_n} for margin {method.margin}, confidence "
            f"{method.confidence}, power {method.power}"
        )
    if observed_n == 0:
        reasons.append("no pairs: an empty comparison is refused, never passed")
    elif observed_n < required_n:
        reasons.append(
            f"n={observed_n} prompts is below the power-analysed size: "
            f"required n={required_n}"
        )

    base: dict[str, object] = {
        "comparison_id": comparison_id,
        "confidence": method.confidence,
        "margin": method.margin,
        "required_n": required_n,
        "declared_n": method.sample_size,
        "observed_n": observed_n,
        "passes_a": sum(passed_a),
        "passes_b": sum(passed_b),
        "incomplete_a": sum(
            pair.outcome_a is EnumRangeSampleOutcome.INCOMPLETE for pair in pairs
        ),
        "incomplete_b": sum(
            pair.outcome_b is EnumRangeSampleOutcome.INCOMPLETE for pair in pairs
        ),
        "a_only": a_only,
        "b_only": b_only,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
    }
    if observed_n == 0:
        return ModelComparisonResult.model_validate(
            {
                **base,
                "verdict": EnumComparisonVerdict.REFUSED,
                "reasons": tuple(reasons),
            }
        )

    low, high = _bootstrap_difference_interval(
        n=observed_n, a_only=a_only, b_only=b_only, confidence=method.confidence
    )
    p_value = mcnemar_exact_p_value(a_only=a_only, b_only=b_only)
    statistics = {
        "rate_a": sum(passed_a) / observed_n,
        "rate_b": sum(passed_b) / observed_n,
        "difference": (b_only - a_only) / observed_n,
        "interval_low": low,
        "interval_high": high,
        "p_value": p_value,
    }
    if reasons:
        verdict = EnumComparisonVerdict.REFUSED
    elif p_value <= 1.0 - method.confidence:
        verdict = EnumComparisonVerdict.DIFFERENCE
        reasons.append(
            f"exact McNemar p={p_value:.4g} on {a_only + b_only} discordant pairs "
            f"({a_only} A only, {b_only} B only) is at most {1.0 - method.confidence:.4g}"
        )
    else:
        verdict = EnumComparisonVerdict.NO_DIFFERENCE
    return ModelComparisonResult.model_validate(
        {**base, **statistics, "verdict": verdict, "reasons": tuple(reasons)}
    )


__all__ = [
    "compare_paired_outcomes",
    "exact_comparison_power",
    "exact_false_difference_rate",
    "mcnemar_exact_p_value",
    "normal_approximation_comparison_size",
    "required_comparison_size",
]
