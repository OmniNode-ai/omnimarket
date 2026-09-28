# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bootstrap interval of a pass rate (unified plan section 2b, rule 2).

Deterministic: the resampling uses a fixed evaluator seed, so an interval is
reproducible. That seed belongs to the EVALUATOR; it is not a run's model
sampling seed, which rule 4 forbids pinning and the evaluator refuses.

A pass-rate sample is a Bernoulli draw, so a nonparametric bootstrap resample
of n samples has exactly a Binomial(n, p_hat) pass count; each resample is drawn
that way rather than by resampling the list: the same distribution, far cheaper.
"""

from __future__ import annotations

import random

#: Resamples per bootstrap. Fixed, so intervals are comparable between runs.
BOOTSTRAP_RESAMPLES = 10_000

_EVALUATOR_BOOTSTRAP_SEED = 20260923


def bootstrap_pass_rate_bounds(
    *, passes: int, n: int, confidence: float
) -> tuple[float, float, float]:
    """(one-sided lower bound, two-sided low, two-sided high) at ``confidence``."""
    if n <= 0:
        raise ValueError("a bootstrap needs at least one sample")
    if not 0 <= passes <= n:
        raise ValueError(f"passes={passes} is outside 0..n={n}")
    rate = passes / n
    rng = random.Random(_EVALUATOR_BOOTSTRAP_SEED)
    draws = sorted(rng.binomialvariate(n, rate) / n for _ in range(BOOTSTRAP_RESAMPLES))
    alpha = 1.0 - confidence

    def quantile(q: float) -> float:
        return draws[min(len(draws) - 1, max(0, int(q * len(draws))))]

    return quantile(alpha), quantile(alpha / 2.0), quantile(1.0 - alpha / 2.0)


__all__ = ["BOOTSTRAP_RESAMPLES", "bootstrap_pass_rate_bounds"]
