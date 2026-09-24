# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sample sizing and the exact lower bound for a pass-rate range line.

Section 2b sizes n so the decision separates the floor from the floor plus
the stated margin at the stated error rates:

* H0, the true rate equals the floor: the line is met with probability at most
  ``1 - confidence``;
* H1, the true rate equals the floor plus the margin: the line is met with
  probability at least ``power``.

WHY NOT THE NORMAL APPROXIMATION ALONE. Measured when this module was written:
at floor 0.8, margin 0.1, confidence 0.95 and power 0.8, the normal
approximation gives n=83. At that n, the percentile bootstrap lower bound
meets the line from 72 passes, and a system truly AT the floor reaches 72 passes
with probability 0.076, not 0.05. A Monte Carlo of 2,000 runs read 0.0705. The
percentile bootstrap is liberal for a proportion at small n, so a line that
claims 95 percent delivered about 92.

The decision therefore uses the more conservative of the bootstrap bound and
the exact one-sided Clopper-Pearson bound, which never covers below its
nominal level. n is sized by exact search against that decision: the smallest
n, starting from the normal approximation, at which the exact probability of
meeting the line at floor + margin is at least ``power``. The exact false-pass
probability at the floor is then at most ``1 - confidence`` by construction.
For the example above, that is n=88: false-pass 0.046, power 0.833.

Standard library only.
"""

from __future__ import annotations

import functools
import math
from statistics import NormalDist

from omnimarket.ranges.bootstrap import bootstrap_pass_rate_bounds

#: The exact search gives up past this multiple of the normal approximation.
_SEARCH_LIMIT_FACTOR = 10


def _validate(*, floor: float, margin: float, confidence: float, power: float) -> None:
    if not 0.0 < floor < 1.0:
        raise ValueError(f"floor must be a rate strictly between 0 and 1, got {floor}")
    if not 0.0 < margin < 1.0:
        raise ValueError(f"margin must be strictly between 0 and 1, got {margin}")
    if floor + margin > 1.0:
        raise ValueError(f"floor {floor} + margin {margin} exceeds 1.0")
    if not 0.5 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0.5, 1), got {confidence}")
    if not 0.0 < power < 1.0:
        raise ValueError(f"power must be in (0, 1), got {power}")


def normal_approximation_sample_size(
    *, floor: float, margin: float, confidence: float, power: float
) -> int:
    """One-sample proportion, one-sided, normal approximation (Chow, Shao and
    Wang, *Sample Size Calculations in Clinical Research*, section 4.1).

    The starting point of the exact search, not the answer.
    """
    _validate(floor=floor, margin=margin, confidence=confidence, power=power)
    standard_normal = NormalDist()
    p0, p1 = floor, floor + margin
    numerator = standard_normal.inv_cdf(confidence) * math.sqrt(
        p0 * (1.0 - p0)
    ) + standard_normal.inv_cdf(power) * math.sqrt(p1 * (1.0 - p1))
    return max(1, math.ceil((numerator / margin) ** 2 - 1e-9))


def binomial_upper_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed in log space."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n_factorial = math.lgamma(n + 1)
    terms = [
        log_n_factorial
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * log_p
        + (n - i) * log_q
        for i in range(k, n + 1)
    ]
    peak = max(terms)
    return min(1.0, math.exp(peak) * math.fsum(math.exp(t - peak) for t in terms))


def exact_lower_bound(*, passes: int, n: int, confidence: float) -> float:
    """One-sided Clopper-Pearson lower bound of a pass rate at ``confidence``."""
    if n <= 0:
        raise ValueError("an exact bound needs at least one sample")
    if not 0 <= passes <= n:
        raise ValueError(f"passes={passes} is outside 0..n={n}")
    if passes == 0:
        return 0.0
    alpha = 1.0 - confidence
    low, high = 0.0, passes / n
    for _ in range(100):
        middle = (low + high) / 2.0
        if binomial_upper_tail(passes, n, middle) < alpha:
            low = middle
        else:
            high = middle
    return low


def decision_lower_bound(*, passes: int, n: int, confidence: float) -> float:
    """The bound the floor is compared against: the more conservative of the
    bootstrap bound and the exact bound."""
    bootstrap_lower, _, _ = bootstrap_pass_rate_bounds(
        passes=passes, n=n, confidence=confidence
    )
    return min(
        bootstrap_lower,
        exact_lower_bound(passes=passes, n=n, confidence=confidence),
    )


@functools.cache
def minimum_passes_to_meet(*, n: int, floor: float, confidence: float) -> int | None:
    """The fewest passes out of n whose decision bound clears the floor."""
    low, high = 0, n
    if decision_lower_bound(passes=n, n=n, confidence=confidence) < floor:
        return None
    while low < high:
        middle = (low + high) // 2
        if decision_lower_bound(passes=middle, n=n, confidence=confidence) >= floor:
            high = middle
        else:
            low = middle + 1
    return low


@functools.cache
def required_sample_size(
    *, floor: float, margin: float, confidence: float, power: float
) -> int:
    """The n a pass-rate line needs: exact power at floor + margin >= ``power``."""
    start = normal_approximation_sample_size(
        floor=floor, margin=margin, confidence=confidence, power=power
    )
    for n in range(start, start * _SEARCH_LIMIT_FACTOR + 1):
        threshold = minimum_passes_to_meet(n=n, floor=floor, confidence=confidence)
        if threshold is None:
            continue
        if binomial_upper_tail(threshold, n, floor + margin) >= power:
            return n
    raise ValueError(
        f"no n up to {start * _SEARCH_LIMIT_FACTOR} reaches power {power} at "
        f"floor {floor} + margin {margin}; widen the margin"
    )


__all__ = [
    "binomial_upper_tail",
    "decision_lower_bound",
    "exact_lower_bound",
    "minimum_passes_to_meet",
    "normal_approximation_sample_size",
    "required_sample_size",
]
