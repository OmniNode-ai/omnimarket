# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host boundary of the service principal mint effect (dependency injection)."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import SecretStr

from ..models.model_service_principal_mint import (
    ModelServicePrincipalDelivery,
    ModelServicePrincipalDeliveryReading,
)


class BrokerLoginAdminError(RuntimeError):
    """Raised by the broker admin boundary on any failure. Its message never includes a password."""


class PrincipalVaultError(RuntimeError):
    """Raised by the principal vault boundary when the store is unreachable or refuses."""


class ProtocolBrokerLoginAdmin(Protocol):
    """The broker login administration boundary.

    ``list_logins`` returns the names of all existing logins. ``create_login`` creates a new
    login with the given password; the password is never logged or included in any error.
    """

    async def list_logins(self) -> tuple[str, ...]:
        """The names of all existing broker logins."""
        ...

    async def create_login(self, name: str, password: SecretStr) -> None:
        """Create a new login with the given password. Raises BrokerLoginAdminError on failure."""
        ...


class ProtocolPrincipalVault(Protocol):
    """The principal secret store boundary.

    ``read`` returns the stored secret or ``None`` when the key is absent. ``create`` stores a
    new secret create-only; returns ``"exists"`` when the key is already present.
    """

    async def read(self, folder: str, key: str) -> SecretStr | None:
        """The stored secret, or ``None`` when the key is absent."""
        ...

    async def create(
        self, folder: str, key: str, value: SecretStr
    ) -> Literal["created", "exists"]:
        """Store a new secret create-only. Raises PrincipalVaultError when the store is unreachable."""
        ...


class ProtocolCredentialDelivery(Protocol):
    """The credential file delivery boundary.

    ``deliver`` writes the credential env file to the target host. Failures are reported as
    state ``"failed"`` with a detail that never contains the content.
    """

    async def deliver(
        self,
        target: ModelServicePrincipalDelivery,
        content: SecretStr,
        *,
        apply: bool,
    ) -> ModelServicePrincipalDeliveryReading:
        """Deliver the credential file to the target host."""
        ...
