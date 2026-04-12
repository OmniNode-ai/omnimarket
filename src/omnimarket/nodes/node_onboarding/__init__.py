# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_onboarding — Contract-driven progressive onboarding for new users and employees."""

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)


class NodeOnboarding(HandlerOnboarding):
    """ONEX entry-point wrapper for HandlerOnboarding."""
