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


class HandlerEndpointCatalog:
    """Expose the discovery owner through a contract-bound cross-node interface."""

    def handle(self, request: ModelEndpointModelsRequest) -> ModelEndpointModelsResult:
        return HandlerEndpointModels().handle(request)

    def discover(
        self, *, url: str, key: SecretStr | None = None, provider: str | None = None
    ) -> ModelEndpointModelsResult:
        """Read this connection's catalog without inference or persistent changes."""
        return self.handle(
            ModelEndpointModelsRequest(url=url, key=key, provider=provider)
        )
