# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Register the keyless local model server that the ``onex setup`` model step names.

``add_local_model`` lives here, outside the setup node, so that the node imports no
model class from the model setup node's models package (tests/test_no_cross_node_reach_in.py
scans the node directories only).
"""

from __future__ import annotations

from omnimarket.handlers.handler_model_keys import model_handler
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelTestResult,
)


def add_local_model(url: str | None, model: str) -> ModelModelTestResult | None:
    """Register a keyless local model server at ``url`` with ``model`` and return its first test."""
    tests = (
        model_handler()
        .handle(ModelModelSetupRequest(operation="add_local", url=url, models=(model,)))
        .tests
    )
    return tests[0] if tests else None


__all__ = [
    "add_local_model",
]
