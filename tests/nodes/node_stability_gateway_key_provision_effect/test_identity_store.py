# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key row the node writes is the row the gateway resolves (OMN-26586, the AC3 proxy that needs no lab).

``TenantIdentityPostgres.ensure_api_key`` runs against a fake asyncpg pool that enforces the two unique constraints of
``tenant_api_keys``; the hash it stores is then handed to the gateway's own ``PostgresApiKeyTenantIdentity``, which must
resolve the plaintext key to the tenant. A key stored under any other hash scheme would answer ``None`` (a 401).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from omnimarket.nodes.node_stability_gateway_key_provision_effect.adapters import (
    GatewayIdentityPostgres,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumApiKeyEnsure,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)

from .fakes import KEY_VALUE

pytestmark = pytest.mark.unit

TENANT = UUID(int=1)
OTHER_TENANT = UUID(int=2)


class _KeyTable:
    """``tenant_api_keys`` as a table: UNIQUE (key_hash) and a unique active (tenant_id, name)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        if "INSERT INTO tenant_api_keys" in sql:
            tenant_id, name, key_hash = args
            clash = any(
                row["key_hash"] == key_hash
                or (
                    row["tenant_id"] == tenant_id
                    and row["name"] == name
                    and row["is_active"]
                )
                for row in self.rows
            )
            if clash:
                return None
            self.rows.append(
                {
                    "id": UUID(int=100 + len(self.rows)),
                    "tenant_id": tenant_id,
                    "name": name,
                    "key_hash": key_hash,
                    "is_active": True,
                }
            )
            return {"id": self.rows[-1]["id"]}
        if "SELECT key_hash FROM tenant_api_keys" in sql:
            tenant_id, name = args
            return next(
                (
                    {"key_hash": r["key_hash"]}
                    for r in self.rows
                    if r["tenant_id"] == tenant_id
                    and r["name"] == name
                    and r["is_active"]
                ),
                None,
            )
        if "FROM tenant_api_keys k JOIN tenants t" in sql:
            (key_hash,) = args
            row = next((r for r in self.rows if r["key_hash"] == key_hash), None)
            return (
                None
                if row is None
                else (row["tenant_id"], "stability-canary", row["id"])
            )
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append(sql)
        return "UPDATE 1"

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_KeyTable]:
        yield self

    def transaction(self) -> _Transaction:
        return _Transaction()


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def _store(table: _KeyTable) -> TenantIdentityPostgres:
    return TenantIdentityPostgres(pool=table)  # type: ignore[arg-type]


def test_first_insert_creates_and_the_gateway_resolves_the_plaintext_key() -> None:
    table = _KeyTable()
    key_hash = hashlib.sha256(KEY_VALUE.encode("utf-8")).hexdigest()
    adapter = GatewayIdentityPostgres(_store(table))
    assert (
        asyncio.run(
            adapter.ensure_api_key(tenant_id=TENANT, name="k", key_hash=key_hash)
        )
        is EnumApiKeyEnsure.CREATED
    )
    identity = PostgresApiKeyTenantIdentity(
        pool=table,
        header="x-api-key",
        prefix="onxk_",  # type: ignore[arg-type]
    )
    resolved = asyncio.run(identity.resolve({"x-api-key": KEY_VALUE}))
    assert resolved is not None
    assert resolved.tenant_id == TENANT


def test_failing_control_a_key_stored_under_another_scheme_does_not_resolve() -> None:
    table = _KeyTable()
    wrong = hashlib.sha256(KEY_VALUE.removeprefix("onxk_").encode("utf-8")).hexdigest()
    asyncio.run(
        _store(table).ensure_api_key(tenant_id=TENANT, name="k", key_hash=wrong)
    )
    identity = PostgresApiKeyTenantIdentity(
        pool=table,
        header="x-api-key",
        prefix="onxk_",  # type: ignore[arg-type]
    )
    assert asyncio.run(identity.resolve({"x-api-key": KEY_VALUE})) is None


def test_same_hash_again_is_exists_and_inserts_no_second_row() -> None:
    table = _KeyTable()
    store = _store(table)
    asyncio.run(store.ensure_api_key(tenant_id=TENANT, name="k", key_hash="a" * 64))
    again = asyncio.run(
        store.ensure_api_key(tenant_id=TENANT, name="k", key_hash="a" * 64)
    )
    assert again.value == "exists"
    assert len(table.rows) == 1


def test_another_hash_under_the_active_name_is_a_name_conflict_and_replaces_nothing() -> (
    None
):
    table = _KeyTable()
    store = _store(table)
    asyncio.run(store.ensure_api_key(tenant_id=TENANT, name="k", key_hash="a" * 64))
    other = asyncio.run(
        store.ensure_api_key(tenant_id=TENANT, name="k", key_hash="b" * 64)
    )
    assert other.value == "name_conflict"
    assert [row["key_hash"] for row in table.rows] == ["a" * 64]


def test_a_hash_held_by_another_tenant_is_hash_taken() -> None:
    table = _KeyTable()
    store = _store(table)
    asyncio.run(store.ensure_api_key(tenant_id=TENANT, name="k", key_hash="a" * 64))
    taken = asyncio.run(
        store.ensure_api_key(tenant_id=OTHER_TENANT, name="k2", key_hash="a" * 64)
    )
    assert taken.value == "hash_taken"
