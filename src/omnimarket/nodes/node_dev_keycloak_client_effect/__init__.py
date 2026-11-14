# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_keycloak_client_effect: one dev Keycloak service client, two realm-management roles (OMN-24237)."""

from omnimarket.nodes.node_dev_keycloak_client_effect.handlers.handler_dev_keycloak_client import (
    HandlerDevKeycloakClient,
)

__all__ = ["HandlerDevKeycloakClient"]
