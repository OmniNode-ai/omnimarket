# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The vendor boundary of node_secret_store_effect.

Each module here implements the omnibase_spi ``ProtocolSecretStore`` for one
store product, and this package is the only place in the node that names one.
The node's handler asks :func:`secret_store_for` for the store the overlay's
``provider`` names and never imports a vendor module itself.
"""

from __future__ import annotations

from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.adapters import handler_infisical_http
from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)


def secret_store_for(
    overlay: ModelSecretStoreOverlay,
    *,
    client_id: SecretStr,
    client_secret: SecretStr,
) -> ProtocolSecretStore | None:
    """The store the overlay names, as its identity; ``None`` for an unknown provider."""
    if overlay.provider == handler_infisical_http.PROVIDER:
        return handler_infisical_http.HandlerInfisicalHttp(
            address=overlay.address,
            project_id=str(overlay.project_id),
            environment=overlay.environment,
            client_id=client_id,
            client_secret=client_secret,
            timeout_seconds=overlay.timeout_seconds,
        )
    return None


__all__ = ["secret_store_for"]
