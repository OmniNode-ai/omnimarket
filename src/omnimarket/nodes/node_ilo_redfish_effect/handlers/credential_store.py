# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credential source of the iLO 4 Redfish effect: the lab secret store, and nothing else (OMN-21835).

The secret resolver falls back to the process environment for some references. An iLO account must never
come from the environment, so this adapter accepts a value only when the resolver reports that the lab
secret store answered, and raises otherwise.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import SecretStr

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    CredentialSourceNotStoreError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloRefusal,
)

SecretResolverFn = Callable[[str], tuple[SecretStr | None, EnumSecretSource | None]]


def _default_resolver(key: str) -> tuple[SecretStr | None, EnumSecretSource | None]:
    from omnimarket.inference.secret_store_resolver import resolve_api_key_with_source

    return resolve_api_key_with_source(key, required=False)


class StoreCredentialSource:
    """``ProtocolCredentialSource`` over the secret resolver, accepting only the lane secret store."""

    def __init__(self, resolver: SecretResolverFn = _default_resolver) -> None:
        self._resolver = resolver

    def get(self, key: str) -> SecretStr | None:
        try:
            value, source = self._resolver(key)
        except (
            Exception
        ) as exc:  # any store failure is a missing credential, never a fallback
            raise IloRefusalError(
                EnumIloRefusal.CREDENTIAL_MISSING,
                f"secret store error {type(exc).__name__}",
            ) from None
        if value is None:
            return None
        if source is not EnumSecretSource.LANE_SECRET_STORE:
            raise CredentialSourceNotStoreError
        return value
