# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-bound cross-node model availability interface for guided setup."""

from pydantic import SecretStr

from omnimarket.models.model_endpoint_probe import (
    ModelEndpointProbeRequest,
    ModelEndpointProbeResult,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_probe import (
    HandlerEndpointProbe,
)


class HandlerModelProbe:
    def handle(self, request: ModelEndpointProbeRequest) -> ModelEndpointProbeResult:
        return HandlerEndpointProbe().handle(request)

    def probe(
        self,
        *,
        url: str,
        models: tuple[str, ...],
        key: SecretStr | None = None,
        provider: str | None = None,
    ) -> ModelEndpointProbeResult:
        return self.handle(
            ModelEndpointProbeRequest(
                url=url, models=models, key=key, provider=provider
            )
        )
