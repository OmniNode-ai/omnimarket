# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models for the PR landing reducer's input and output, which the orchestrator builds and reads.

Promoted out of the owning node's private models package so a sibling node
imports them from here, never by reaching into that node (OMN-9263).
"""

from omnimarket.events.pr_landing_reduce.model_pr_landing_reduce_input import (
    ModelPrLandingReduceInput,
)
from omnimarket.events.pr_landing_reduce.model_pr_landing_reduce_output import (
    ModelPrLandingReduceOutput,
)

__all__: list[str] = ["ModelPrLandingReduceInput", "ModelPrLandingReduceOutput"]
