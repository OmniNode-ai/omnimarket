# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the question guard, bound to its handler by contract."""

from omnimarket.nodes.node_question_guard_compute.handlers.handler_question_guard import (
    HandlerQuestionGuard,
)


class NodeQuestionGuardCompute(HandlerQuestionGuard):
    """Pure compute: a drafted reply and the rulings in force in, a typed verdict out."""


__all__ = ["NodeQuestionGuardCompute"]
