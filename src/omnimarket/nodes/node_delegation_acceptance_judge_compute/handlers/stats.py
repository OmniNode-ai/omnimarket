# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wilson interval and Cohen's kappa for binary verdicts. Pure."""

from __future__ import annotations

import math

_Z = 1.959963984540054


def wilson_accept_interval(accepted: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95 percent interval for an accept proportion.

    The gate-eval node holds the same interval for error proportions, but a node may not
    import another node's handler, so this is the accept-side counterpart.
    """
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
    """Cohen's kappa over (judge A accept, judge B accept) pairs.

    When both judges used a single identical label throughout, chance agreement is 1 and
    kappa is undefined; the pairs then agree perfectly, so 1.0 is returned.
    """
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
