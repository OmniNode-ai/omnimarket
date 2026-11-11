# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared endpoint-discovery facade for model management and guided setup."""

from pydantic import SecretStr

from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_models import (
    HandlerEndpointModels,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
    ModelEndpointModelsResult,
)


def discover_models(
    *, url: str, key: SecretStr | None = None
) -> ModelEndpointModelsResult:
    """Read this connection's catalog without inference or persistent changes."""
    return HandlerEndpointModels().handle(ModelEndpointModelsRequest(url=url, key=key))
