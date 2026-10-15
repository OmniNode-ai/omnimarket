# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wilson interval, Cohen's kappa, nearest-rank percentile and the paired bootstrap. Pure."""

from __future__ import annotations

import math
import random

_Z = 1.959963984540054


def wilson_interval(accepted: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95 percent interval for an accept proportion."""
    if not 0 <= accepted <= n:
        raise ValueError("Wilson counts must satisfy 0 <= accepted <= n")
    if n == 0:
        return 0.0, 1.0
    p = accepted / n
    denominator = 1 + _Z**2 / n
    center = (p + _Z**2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + _Z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    """Cohen's kappa over (judge A, judge B) accepts; 1.0 when chance agreement is certain."""
    n = len(pairs)
    if n == 0:
        raise ValueError("kappa needs at least one pair")
    observed = sum(a == b for a, b in pairs) / n
    a_yes = sum(a for a, _ in pairs) / n
    b_yes = sum(b for _, b in pairs) / n
    expected = a_yes * b_yes + (1 - a_yes) * (1 - b_yes)
    if expected >= 1.0:
        return 1.0
    return max(-1.0, min(1.0, (observed - expected) / (1 - expected)))


def nearest_rank_percentile(values: list[int], fraction: float) -> float | None:
    """The nearest-rank percentile: the ceil(fraction * n)th smallest value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[rank - 1])


def paired_bootstrap_interval(
    differences: list[int], *, resamples: int, seed: int
) -> tuple[float, float]:
    """95 percent percentile interval of the mean of paired differences, from a fixed seed."""
    n = len(differences)
    rng = random.Random(seed)
    means = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(resamples)
    )
    low = means[int(0.025 * resamples)]
    high = means[min(resamples - 1, int(0.975 * resamples))]
    return low, high
