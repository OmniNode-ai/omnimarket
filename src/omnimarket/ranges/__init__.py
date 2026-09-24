# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Range evaluation and the gate-or-range check register (unified plan row G1).

Pure functions over the models in :mod:`omnimarket.models.ranges`: the power
analysis, the bootstrap evaluator, and the register check. Callers (the
shadow-comparison harness and the response-quality baseline) compose these;
the CLI is ``python -m omnimarket.ranges``.
"""

from omnimarket.ranges.bootstrap import (
    BOOTSTRAP_RESAMPLES,
    bootstrap_pass_rate_bounds,
)
from omnimarket.ranges.evaluate import evaluate_range_line
from omnimarket.ranges.power import (
    binomial_upper_tail,
    exact_lower_bound,
    minimum_passes_to_meet,
    normal_approximation_sample_size,
    required_sample_size,
)
from omnimarket.ranges.register import (
    DEFAULT_CHECK_REGISTER_PATH,
    validate_check_register,
)

__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "DEFAULT_CHECK_REGISTER_PATH",
    "binomial_upper_tail",
    "bootstrap_pass_rate_bounds",
    "evaluate_range_line",
    "exact_lower_bound",
    "minimum_passes_to_meet",
    "normal_approximation_sample_size",
    "required_sample_size",
    "validate_check_register",
]
