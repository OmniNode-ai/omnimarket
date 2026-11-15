# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters for the service principal mint effect."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import SecretStr

from ..models.model_service_principal_mint import (
    ModelServicePrincipalDelivery,
    ModelServicePrincipalDeliveryReading,
)
from ..protocols.protocol_service_principal_mint import (
    BrokerLoginAdminError,
    PrincipalVaultError,
)


class MemoryBrokerLoginAdmin:
    """In-memory broker login admin: a dict of logins; records create calls."""

    def __init__(
        self,
        logins: Iterable[str] = (),
        *,
        fail_list: bool = False,
        fail_create: bool = False,
    ) -> None:
        self._logins: dict[str, SecretStr] = {
            name: SecretStr(f"pw-{name}") for name in logins
        }
        self._fail_list = fail_list
        self._fail_create = fail_create
        self.created: list[tuple[str, SecretStr]] = []

    async def list_logins(self) -> tuple[str, ...]:
        if self._fail_list:
            raise BrokerLoginAdminError("broker unreachable")
        return tuple(self._logins.keys())

    async def create_login(self, name: str, password: SecretStr) -> None:
        if self._fail_create:
            raise BrokerLoginAdminError("broker create failed")
        if name in self._logins:
            raise BrokerLoginAdminError(f"login {name!r} already exists")
        self._logins[name] = password
        self.created.append((name, password))

    def _peek_logins(self) -> dict[str, SecretStr]:
        return dict(self._logins)

    def _peek_created(self) -> list[tuple[str, SecretStr]]:
        return list(self.created)


class MemoryPrincipalVault:
    """In-memory principal vault: dict keyed (folder, key); create is create-only."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self._store: dict[tuple[str, str], SecretStr] = {}
        self._unavailable = unavailable

    async def read(self, folder: str, key: str) -> SecretStr | None:
        if self._unavailable:
            raise PrincipalVaultError("vault unreachable")
        return self._store.get((folder, key))

    async def create(
        self, folder: str, key: str, value: SecretStr
    ) -> Literal["created", "exists"]:
        if self._unavailable:
            raise PrincipalVaultError("vault unreachable")
        k = (folder, key)
        if k in self._store:
            return "exists"
        self._store[k] = value
        return "created"

    def _peek_store(self) -> dict[tuple[str, str], SecretStr]:
        return dict(self._store)


class MemoryCredentialDelivery:
    """In-memory credential delivery: dict keyed (host, folder, filename) -> content."""

    def __init__(self, fail_hosts: Iterable[str] = ()) -> None:
        self._files: dict[tuple[str, str, str], SecretStr] = {}
        self._fail_hosts = frozenset(fail_hosts)

    async def deliver(
        self,
        target: ModelServicePrincipalDelivery,
        content: SecretStr,
        *,
        apply: bool,
    ) -> ModelServicePrincipalDeliveryReading:
        if target.host in self._fail_hosts:
            return ModelServicePrincipalDeliveryReading(
                host=target.host,
                state="failed",
                owner=target.owner,
                mode=target.mode,
                detail="delivery failed",
            )
        k = (target.host, target.folder, target.filename)
        if (
            k in self._files
            and self._files[k].get_secret_value() == content.get_secret_value()
        ):
            return ModelServicePrincipalDeliveryReading(
                host=target.host,
                state="unchanged",
                owner=target.owner,
                mode=target.mode,
            )
        if not apply:
            return ModelServicePrincipalDeliveryReading(
                host=target.host,
                state="would-write",
                owner=target.owner,
                mode=target.mode,
            )
        self._files[k] = content
        return ModelServicePrincipalDeliveryReading(
            host=target.host,
            state="written",
            owner=target.owner,
            mode=target.mode,
        )

    def _peek_files(self) -> dict[tuple[str, str, str], SecretStr]:
        return dict(self._files)
