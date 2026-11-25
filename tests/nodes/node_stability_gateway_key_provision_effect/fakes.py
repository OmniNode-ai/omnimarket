# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the stability gateway key provision node tests (OMN-26586).

``FakeIdentityStore`` is a stateful stand-in for the identity database: it keeps tenants and key rows, answers
idempotently like the real transaction, and records every write so a test can prove a refusal wrote nothing.
``FakeKeyGenerator`` and ``FakeSettingStore`` are the secret store seams; the generator holds the value it made in memory
so a test can prove the value never appears in a result, a repr or a log.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import yaml
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
# The name the stability probes read from the stability Infisical (omnibase_infra's chain-canary and C16 workflows fetch
# CHAIN_CANARY_GATEWAY_API_KEY under this name from /shared/env): spelled here, in this repository, so the node's stored
# name is pinned against it without reading the other repository.
PROBE_READS_KEY_NAME = "STABILITY_TEST_CHAIN_CANARY_GATEWAY_API_KEY"
KEY_NAME = PROBE_READS_KEY_NAME


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


STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)


class FakeSecretStore:
    """One in-memory secret store seen through both seams, resolving key ids by the real secret store contract.

    ``generator`` stands in for the seed node's create-only generate-key operation (an id's folder and key name come from the
    ``generated_keys`` block of ``node_secret_store_effect/contract.yaml``, a value already there is left as it is and answers
    ``exists``); ``reader`` is the settings seam. Every write and every read is recorded by (folder, name), so a test can prove
    which names a run touched and that no stored value changed.
    """

    def __init__(self, seeded: dict[tuple[str, str], str] | None = None) -> None:
        self.values: dict[tuple[str, str], str] = dict(seeded or {})
        self.writes: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self.declared: dict[str, dict[str, object]] = yaml.safe_load(
            STORE_CONTRACT.read_text(encoding="utf-8")
        )["generated_keys"]
        self.generator = _StoreGenerator(self)
        self.reader = _StoreReader(self)

    def touched(self) -> set[tuple[str, str]]:
        return set(self.writes) | set(self.reads)


class _StoreGenerator:
    def __init__(self, store: FakeSecretStore) -> None:
        self._store = store
        self.calls: list[str] = []

    async def generate(self, name: str) -> ModelGenerateKeyResult:
        self.calls.append(name)
        spec = self._store.declared[name]
        folder, key = str(spec["folder"]), str(spec["key"])
        if (folder, key) in self._store.values:
            outcome = EnumSecretStoreOutcome.EXISTS
        else:
            self._store.values[(folder, key)] = f"{spec['prefix']}value-for-{key}"
            self._store.writes.append((folder, key))
            outcome = EnumSecretStoreOutcome.CREATED
        return ModelGenerateKeyResult(
            name=name,
            reference=f"secret:{folder}/{key}",
            outcome=outcome,
            key_length=len(self._store.values[(folder, key)])
            if outcome is EnumSecretStoreOutcome.CREATED
            else None,
            folder=folder,
            key=key,
        )


class _StoreReader:
    def __init__(self, store: FakeSecretStore) -> None:
        self._store = store

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self._store.reads.append((folder, name))
        value = self._store.values.get((folder, name))
        return SecretStr(value) if value is not None else None
