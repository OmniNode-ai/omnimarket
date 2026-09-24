# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sample-size sizing for a pass-rate range line (unified plan section 2b, rule 3).

The line is met when the one-sided lower bound of the pass rate clears the
floor. The power analysis sizes n so that the decision separates the two rates
it has to separate at the stated error rates:

* H0, the true rate equals the floor: the line is met with probability at most
  ``1 - confidence``;
* H1, the true rate equals the floor plus the stated margin: the line is met
  with probability at least ``power``.

One-sample proportion, one-sided, normal approximation (Chow, Shao and Wang,
*Sample Size Calculations in Clinical Research*, section 4.1)::

    n = ceil( ( z_conf * sqrt(p0 (1 - p0)) + z_power * sqrt(p1 (1 - p1)) )^2 / margin^2 )

with p0 = floor and p1 = floor + margin. Standard library only.
"""

from __future__ import annotations

import math
from statistics import NormalDist


def required_sample_size(
    *, floor: float, margin: float, confidence: float, power: float
) -> int:
    """The n a pass-rate line needs, from its floor, margin, confidence and power."""
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
    standard_normal = NormalDist()
    z_confidence = standard_normal.inv_cdf(confidence)
    z_power = standard_normal.inv_cdf(power)
    p0 = floor
    p1 = floor + margin
    numerator = z_confidence * math.sqrt(p0 * (1.0 - p0)) + z_power * math.sqrt(
        p1 * (1.0 - p1)
    )
    # A tiny epsilon keeps an exact integer result from rounding up on float noise.
    return max(1, math.ceil((numerator / margin) ** 2 - 1e-9))


__all__ = ["required_sample_size"]
