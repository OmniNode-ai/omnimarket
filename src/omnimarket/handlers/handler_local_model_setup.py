# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Register the keyless local model server that the ``onex setup`` model step names.

``add_local_model`` lives here, outside the setup node, so that the node imports no
model class from the model setup node's models package (tests/test_no_cross_node_reach_in.py
scans the node directories only).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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


class ModelLocalModelSetupRequest(BaseModel):
    """One local model server to register: its base ``url`` and a model name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str | None = None
    model: str = Field(min_length=1)


class HandlerLocalModelSetup:
    """Register a keyless local model server from a typed request."""

    def handle(
        self, request: ModelLocalModelSetupRequest
    ) -> ModelModelTestResult | None:
        """Register the request's model server and return its first test."""
        return add_local_model(request.url, request.model)


__all__ = [
    "HandlerLocalModelSetup",
    "ModelLocalModelSetupRequest",
    "add_local_model",
]
