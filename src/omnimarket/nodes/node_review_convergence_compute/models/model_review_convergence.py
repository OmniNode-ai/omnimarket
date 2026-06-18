# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-referenced I/O for node_review_convergence_compute (OMN-13210 / B1).

The convergence models + pure ``compute_convergence`` function are OWNED by the
shared ``omnimarket.review`` package so review nodes and the orchestrator import
them from one place without a cross-node reach-in. This module re-exports them at
the contract-declared path.
"""

from __future__ import annotations

from omnimarket.review.convergence import compute_convergence
from omnimarket.review.node_io import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
)

__all__: list[str] = [
    "ModelConvergenceInput",
    "ModelConvergenceOutput",
    "ModelFindingLabel",
    "compute_convergence",
]
