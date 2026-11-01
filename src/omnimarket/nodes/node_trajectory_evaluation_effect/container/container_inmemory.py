# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory evaluator the effect runs on when none is injected (OMN-21695).

The handler never constructs the evaluator itself: it takes the one this module binds.
"""

from __future__ import annotations

from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_in_memory import (
    EvaluatorInMemory,
)


def bind_evaluator() -> EvaluatorInMemory:
    """Bind a fresh in-memory evaluator."""
    return EvaluatorInMemory()
