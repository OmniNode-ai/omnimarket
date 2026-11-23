# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the stability gateway key provision node tests (OMN-26586).

``FakeIdentityStore`` is a stateful stand-in for the identity database: it keeps tenants and key rows, answers
idempotently like the real transaction, and records every write so a test can prove a refusal wrote nothing.
``FakeKeyGenerator`` and ``FakeSettingStore`` are the secret store seams; the generator holds the value it made in memory
so a test can prove the value never appears in a result, a repr or a log.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import SecretStr

from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumApiKeyEnsure,
    EnumTenantEnsure,
    ModelTenantEnsured,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantStoreUnavailableError,
)

KEY_VALUE = "onxk_fake-generated-gateway-key-value"
FOLDER = "/shared/env"
KEY_NAME = "STABILITY_GATEWAY_API_KEY"


class FakeSettingStore:
    """A settings store that answers the secrets a test seeds, and records what it was asked."""

    def __init__(self) -> None:
        self._secrets: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    def set(self, folder: str, key: str, value: str) -> None:
        self._secrets[(folder, key)] = value

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.calls.append((folder, name))
        value = self._secrets.get((folder, name))
        return SecretStr(value) if value is not None else None


class FakeKeyGenerator:
    """The seed node's generate-key operation in memory: the value is held by the test, never returned."""

    def __init__(self, outcome: str = "created") -> None:
        self.outcome = EnumSecretStoreOutcome(outcome)
        self.calls: list[str] = []

    async def generate(self, name: str) -> ModelGenerateKeyResult:
        self.calls.append(name)
        return ModelGenerateKeyResult(
            name=name,
            reference=f"secret:{FOLDER}/{KEY_NAME}",
            outcome=self.outcome,
            key_length=len(KEY_VALUE)
            if self.outcome is EnumSecretStoreOutcome.CREATED
            else None,
            folder=FOLDER,
            key=KEY_NAME,
        )


class FakeIdentityStore:
    """The identity database in memory: tenants by slug, active keys by (tenant id, name)."""

    def __init__(
        self,
        *,
        slug_taken: bool = False,
        unavailable: bool = False,
        hashes_held_by_others: frozenset[str] = frozenset(),
    ) -> None:
        self.tenants: dict[str, UUID] = {}
        self.keys: dict[tuple[UUID, str], str] = {}
        self.calls: list[str] = []
        self._slug_taken = slug_taken
        self._unavailable = unavailable
        self._hashes_held_by_others = hashes_held_by_others

    @property
    def writes(self) -> int:
        return len(self.tenants) + len(self.keys)

    async def ensure_tenant(self, *, tenant_slug: str, name: str) -> ModelTenantEnsured:
        self.calls.append("ensure_tenant")
        if self._unavailable:
            raise TenantStoreUnavailableError("ConnectionRefusedError")
        if self._slug_taken:
            return ModelTenantEnsured(
                kind=EnumTenantEnsure.REFUSED, detail="slug belongs to another owner"
            )
        if tenant_slug in self.tenants:
            return ModelTenantEnsured(
                kind=EnumTenantEnsure.EXISTS, tenant_id=self.tenants[tenant_slug]
            )
        self.tenants[tenant_slug] = uuid4()
        return ModelTenantEnsured(
            kind=EnumTenantEnsure.CREATED, tenant_id=self.tenants[tenant_slug]
        )

    async def ensure_api_key(
        self, *, tenant_id: UUID, name: str, key_hash: str
    ) -> EnumApiKeyEnsure:
        self.calls.append("ensure_api_key")
        if key_hash in self._hashes_held_by_others:
            return EnumApiKeyEnsure.HASH_TAKEN
        held = self.keys.get((tenant_id, name))
        if held is None:
            self.keys[(tenant_id, name)] = key_hash
            return EnumApiKeyEnsure.CREATED
        if held == key_hash:
            return EnumApiKeyEnsure.EXISTS
        return EnumApiKeyEnsure.NAME_CONFLICT
