# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26476: a dev-lane entry for the gateway key node, and an expiry the gateway's key lookup reads.

The contract declares a ``dev_lane`` block (own tenant slug, key row name, generated key ids). A dev command with a dev overlay
runs the whole flow under those names and never the stability ones; a stability overlay refuses a dev command and a dev overlay
refuses a stability command. An entry that declares an expiry writes ``expires_at`` on the key row; the gateway lookup then
refuses the key once the time has passed. prod and the cloud are never declared.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_stability_gateway_key_provision_effect.adapters import (
    GatewayIdentityPostgres,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.handlers.handler_stability_gateway_key_provision import (
    HandlerStabilityGatewayKeyProvision,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumStabilityKeyOutcome,
    ModelStabilityKeyProvisionCommand,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)

from .fakes import FakeIdentityStore, FakeSecretStore

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACT_PATH = ROOT / "node_stability_gateway_key_provision_effect/contract.yaml"
STORE_CONTRACT = ROOT / "node_secret_store_effect/contract.yaml"
NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
TENANT = UUID(int=7)


def _block() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    block: dict[str, Any] = document["dev_lane"]
    return block


def _handler(
    identity: FakeIdentityStore, store: FakeSecretStore, overlay_lane: str = "dev"
) -> HandlerStabilityGatewayKeyProvision:
    return HandlerStabilityGatewayKeyProvision(
        overlay_lane=overlay_lane,
        identity_store=identity,
        settings_store=store.reader,
        key_generator=store.generator,
        contract_path=CONTRACT_PATH,
        clock=lambda: NOW,
    )


def _run(
    handler: HandlerStabilityGatewayKeyProvision, lane: str, check: str | None = None
) -> Any:
    return asyncio.run(
        handler.handle(ModelStabilityKeyProvisionCommand(lane=lane, check=check))
    )


def test_dev_command_with_dev_overlay_provisions_the_dev_tenant_and_key() -> None:
    block = _block()
    identity, store = FakeIdentityStore(), FakeSecretStore()
    result = _run(_handler(identity, store), "dev")
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert result.tenant_slug == block["tenant_slug"] != "stability-canary"
    assert result.key_name == block["api_key_name"]
    assert store.generator.calls == [block["generated_key_id"]]


def test_dev_default_key_carries_the_declared_expiry() -> None:
    block = _block()
    identity, store = FakeIdentityStore(), FakeSecretStore()
    result = _run(_handler(identity, store), "dev")
    expected = NOW + timedelta(seconds=block["expires_in_seconds"])
    assert list(identity.expiries.values()) == [expected]
    assert result.expires_at == expected


def test_dev_expiry_check_is_two_minutes_under_its_own_key_id_and_name() -> None:
    block = _block()
    entry = block["checks"]["expiry"]
    assert entry["expires_in_seconds"] == 120
    assert entry["generated_key_id"] != block["generated_key_id"]
    assert entry["api_key_name"] != block["api_key_name"]
    identity, store = FakeIdentityStore(), FakeSecretStore()
    result = _run(_handler(identity, store), "dev", "expiry")
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert result.key_name == entry["api_key_name"]
    assert list(identity.expiries.values()) == [NOW + timedelta(seconds=120)]


def test_every_dev_generated_key_id_is_declared_by_the_secret_store() -> None:
    declared = yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))[
        "generated_keys"
    ]
    block = _block()
    ids = [block["generated_key_id"]] + [
        e["generated_key_id"] for e in block["checks"].values()
    ]
    for key_id in ids:
        assert key_id in declared
        assert declared[key_id]["prefix"] == "onxk_"
    assert len({declared[i]["key"] for i in ids}) == len(ids)


def test_stability_default_run_writes_no_expiry() -> None:
    identity, store = FakeIdentityStore(), FakeSecretStore()
    result = _run(_handler(identity, store, "stability"), "stability")
    assert result.ok is True
    assert list(identity.expiries.values()) == [None]
    assert result.expires_at is None


@pytest.mark.parametrize(
    ("command_lane", "overlay_lane"),
    [("dev", "stability"), ("stability", "dev"), ("dev", "prod"), ("dev", "cloud")],
)
def test_a_lane_pair_that_is_not_one_declared_lane_is_refused_with_no_side_effect(
    command_lane: str, overlay_lane: str
) -> None:
    identity, store = FakeIdentityStore(), FakeSecretStore()
    result = _run(_handler(identity, store, overlay_lane), command_lane)
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED
    assert store.generator.calls == []
    assert identity.calls == []


def test_the_contract_declares_dev_and_neither_prod_nor_cloud() -> None:
    assert _block()["lanes"] == ["dev"]


# ---- the key table and the gateway lookup ----------------------------------------------------------------------------


class _KeyTable:
    """``tenant_api_keys`` that honours the expiry predicate the way the database evaluates it, with a settable clock."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        if "INSERT INTO tenant_api_keys" in sql:
            expires_at = args[3] if "expires_at" in sql else None
            tenant_id, name, key_hash = args[:3]
            self.rows.append(
                {
                    "id": UUID(int=100 + len(self.rows)),
                    "tenant_id": tenant_id,
                    "name": name,
                    "key_hash": key_hash,
                    "expires_at": expires_at,
                }
            )
            return {"id": self.rows[-1]["id"]}
        if "FROM tenant_api_keys k JOIN tenants t" in sql:
            (key_hash,) = args
            for row in self.rows:
                if row["key_hash"] != key_hash:
                    continue
                if (
                    "k.expires_at > now()" in sql
                    and row["expires_at"] is not None
                    and row["expires_at"] <= self.now
                ):
                    return None
                return (row["tenant_id"], "dev-slug", row["id"])
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        return "UPDATE 1"

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_KeyTable]:
        yield self

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield None


def _identity(table: _KeyTable) -> PostgresApiKeyTenantIdentity:
    return PostgresApiKeyTenantIdentity(
        pool=cast(Any, table), header="x-api-key", prefix="onxk_"
    )


def test_store_writes_expires_at_and_the_gateway_refuses_the_key_after_it() -> None:
    table = _KeyTable(NOW)
    value = "onxk_dev-key-with-an-expiry"
    adapter = GatewayIdentityPostgres(TenantIdentityPostgres(pool=cast(Any, table)))
    asyncio.run(
        adapter.ensure_api_key(
            tenant_id=TENANT,
            name="k",
            key_hash=hashlib.sha256(value.encode()).hexdigest(),
            expires_at=NOW + timedelta(minutes=2),
        )
    )
    assert table.rows[0]["expires_at"] == NOW + timedelta(minutes=2)
    identity = _identity(table)
    assert asyncio.run(identity.resolve({"x-api-key": value})) is not None
    table.now = NOW + timedelta(minutes=3)
    assert asyncio.run(identity.resolve({"x-api-key": value})) is None


def test_a_key_with_no_expiry_still_resolves_and_its_insert_names_no_expiry_column() -> (
    None
):
    table = _KeyTable(NOW)
    value = "onxk_dev-key-without-expiry"
    adapter = GatewayIdentityPostgres(TenantIdentityPostgres(pool=cast(Any, table)))
    asyncio.run(
        adapter.ensure_api_key(
            tenant_id=TENANT,
            name="k",
            key_hash=hashlib.sha256(value.encode()).hexdigest(),
        )
    )
    insert = next(s for s in table.statements if "INSERT INTO tenant_api_keys" in s)
    assert "expires_at" not in insert
    table.now = NOW + timedelta(days=400)
    assert asyncio.run(_identity(table).resolve({"x-api-key": value})) is not None
