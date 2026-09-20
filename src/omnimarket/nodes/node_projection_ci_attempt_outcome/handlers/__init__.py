# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for the per-attempt CI outcome projection (OMN-18903).

A rule-7a pair: the pure fold, and the effect-class writer the runtime calls.
"""

from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers.handler_ci_attempt_outcome_writer import (
    CiAttemptOutcomeProjectionWriter,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers.handler_projection_ci_attempt_outcome import (
    HandlerProjectionCiAttemptOutcome,
)

__all__ = [
    "CiAttemptOutcomeProjectionWriter",
    "HandlerProjectionCiAttemptOutcome",
]
