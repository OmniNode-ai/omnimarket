# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed provider model catalogue effect."""

from omnimarket.nodes.node_provider_model_catalogue_effect.handlers.handler_provider_model_catalogue import (
    HandlerProviderModelCatalogue,
)


class NodeProviderModelCatalogueEffect(HandlerProviderModelCatalogue):
    """ONEX entry point."""


__all__ = ["HandlerProviderModelCatalogue", "NodeProviderModelCatalogueEffect"]
