# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one Wilson score interval shared by the comparison nodes. Pure.

Promoted from the tier-comparison node so a second node can use the same
function without importing that node's private handler package.
"""

from __future__ import annotations

import math

_Z = 1.959963984540054

#: The two-sided confidence of :func:`wilson_interval`.
WILSON_CONFIDENCE = 0.95


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


__all__ = ["WILSON_CONFIDENCE", "wilson_interval"]
