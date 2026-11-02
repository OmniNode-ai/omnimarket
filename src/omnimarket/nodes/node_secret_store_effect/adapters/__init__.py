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

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_secret_store_effect.adapters import (
    handler_infisical_http,
    local_database_store,
)
from omnimarket.nodes.node_secret_store_effect.adapters.http_exchange import (
    HttpExchange,
)
from omnimarket.nodes.node_secret_store_effect.adapters.identity_file import (
    IdentityFileStore,
)

#: The provider a machine with no overlay block is read as: its own database.
LOCAL_PROVIDER = local_database_store.PROVIDER


def identity_needed(provider: str) -> bool:
    """Whether the provider's adapter is handed a machine identity (all but the local one)."""
    return provider != LOCAL_PROVIDER


def missing_overlay_fields(overlay: ModelSecretStoreOverlay) -> tuple[str, ...]:
    """The overlay fields the provider's adapter needs and the overlay leaves out."""
    if overlay.provider != handler_infisical_http.PROVIDER:
        return ()
    wanted = (
        "address",
        "project_id",
        "environment",
        "client_id_ref",
        "client_secret_ref",
    )
    return tuple(name for name in wanted if getattr(overlay, name) is None)


def secret_store_for(
    overlay: ModelSecretStoreOverlay,
    *,
    client_id: SecretStr | None,
    client_secret: SecretStr | None,
    exchange: HttpExchange,
) -> ProtocolSecretStore | None:
    """The store the overlay names, as its identity, over the handler's HTTP exchange.

    ``None`` for an unknown provider, and for a provider whose overlay lacks a
    field or identity it needs (see :func:`missing_overlay_fields`).
    """
    if overlay.provider == LOCAL_PROVIDER:
        return local_database_store.LocalDatabaseStore()
    if (
        overlay.provider == handler_infisical_http.PROVIDER
        and overlay.address is not None
        and overlay.project_id is not None
        and overlay.environment is not None
        and client_id is not None
        and client_secret is not None
    ):
        return handler_infisical_http.HandlerInfisicalHttp(
            address=overlay.address,
            project_id=str(overlay.project_id),
            environment=overlay.environment,
            client_id=client_id,
            client_secret=client_secret,
            exchange=exchange,
            timeout_seconds=overlay.timeout_seconds,
        )
    return None


__all__ = [
    "LOCAL_PROVIDER",
    "HttpExchange",
    "IdentityFileStore",
    "identity_needed",
    "missing_overlay_fields",
    "secret_store_for",
]
