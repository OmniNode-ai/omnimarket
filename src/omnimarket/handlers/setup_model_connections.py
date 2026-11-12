# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model-setup requests ``onex setup`` sends (OMN-23413).

Setup reads one provider or endpoint's status without writing anything (its
verify step never edits), and registers a user-run server's chosen models
through the same handler ``onex models`` uses, so every model it lists was
proven by a pinned delegation.
"""

from __future__ import annotations

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelStatus,
    ModelModelTestResult,
)


def provider_status(
    provider: ModelProvider | None, url: str | None, models: tuple[str, ...]
) -> tuple[ModelModelStatus, ...]:
    """One provider's or endpoint's status, read without creating or migrating anything."""
    request = ModelModelSetupRequest(
        operation="status", provider=provider, url=url, models=models
    )
    return HandlerModelSetup(read_only=True).handle(request).providers


def add_local_endpoint(
    url: str | None, models: tuple[str, ...]
) -> tuple[ModelModelTestResult, ...]:
    """Register a user-run server's models, each proven by its own pinned delegation."""
    request = ModelModelSetupRequest(operation="add_local", url=url, models=models)
    return model_keys.model_handler().handle(request).tests
