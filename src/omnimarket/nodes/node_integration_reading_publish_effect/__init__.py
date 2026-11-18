# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes the integration readings of one pass as the ``integration`` floor projection (OMN-23899)."""

from omnimarket.nodes.node_integration_reading_publish_effect.handlers import (
    HandlerIntegrationReadingPublish,
)


class NodeIntegrationReadingPublishEffect(HandlerIntegrationReadingPublish):
    """ONEX entry point."""


__all__ = ["HandlerIntegrationReadingPublish", "NodeIntegrationReadingPublishEffect"]
