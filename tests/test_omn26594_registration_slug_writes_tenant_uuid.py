# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A key registered under a tenant SLUG is stored and read under the tenant UUID.

``onex secret register-tenant-key --tenant cli-run-a`` publishes
``credential-registered`` carrying the slug. The projection used to write the
catalog row and the routing-overlay row under that slug, while the delegation
router reads the overlay by the caller's tenant UUID: the customer's own row was
never read and an older UUID row answered. The projection now resolves the slug
through ``tenant_registry_mirror`` and writes under the UUID, so there is one
identity form in both tables.

The exemplar is ``test_omn17372_byok_routing_overlay_bridge.py``: same runner,
same fake overlay reader, same real resolver.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)
from omnimarket.routing.tenant_overlay_resolver import (
    BYOK_ALL_TASK_TYPES,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"

SLUG = "cli-run-a"
TENANT_UUID = UUID("8044e4eb-0e41-4f94-ad61-ab63064f4198")
OTHER_SLUG = "other-tenant"
OTHER_UUID = UUID("11111111-2222-4333-8444-555555555555")
MIRROR = {SLUG: TENANT_UUID, OTHER_SLUG: OTHER_UUID}
PROVIDER = "openrouter"


def _meta() -> MessageMeta:
    return MessageMeta(partition=0, offset=0, fallback_id="fallback-omn26594")


class _FakeOverlayReader:
    """The overlay table as ``resolve_tenant_overlay`` reads it."""

    def __init__(self, rows: dict[tuple[str, str], dict[str, object]]) -> None:
        self._rows = rows

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        assert table == TENANT_OVERLAY_TABLE
        return [
            row
            for row in self._rows.values()
            if all(row.get(key) == value for key, value in (filters or {}).items())
        ]


class _World:
    """A mirror that answers slug lookups, and an overlay that upserts on
    ``(tenant_id, task_type)`` as the real statement's ON CONFLICT does."""

    def __init__(self) -> None:
        self.overlay: dict[tuple[str, str], dict[str, object]] = {}
        self.credentials: dict[str, str] = {}
        self.db = AsyncMock()
        self.db.fetchval = AsyncMock(side_effect=self._fetchval)
        self.db.execute = AsyncMock(side_effect=self._execute)

    async def _fetchval(self, sql: str, param: object) -> UUID | None:
        assert "tenant_registry_mirror" in sql
        if isinstance(param, UUID):
            return param if param in MIRROR.values() else None
        return MIRROR.get(str(param))

    async def _execute(self, sql: str, *args: Any, **_: Any) -> list[dict[str, Any]]:
        if "INSERT INTO delegation_routing_tenant_overlay" in sql:
            row: dict[str, object] = {
                "tenant_id": args[0],
                "task_type": args[1],
                "backend_id": args[2],
                "provider": args[3],
                "endpoint_url": args[4],
                "model_name": args[5],
                "secret_ref": args[6],
                "timeout_ms": args[7],
                "max_tokens": args[8],
            }
            self.overlay[(str(args[0]), str(args[1]))] = row
            return [row]
        if "INSERT INTO tenant_inference_credentials" in sql:
            self.credentials[str(args[0])] = str(args[1])
        return []


def _runner(world: _World) -> HandlerTenantCredentialsProjectionRunner:
    runner = HandlerTenantCredentialsProjectionRunner()
    runner._db = world.db
    return runner


def _event(tenant: str, ref: str, model: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "provider": PROVIDER,
        "name": "my-key",
        "api_key_ref": ref,
        "metadata": {"model": model},
    }


async def _register(
    runner: HandlerTenantCredentialsProjectionRunner, event: dict[str, Any]
) -> None:
    assert await runner.project_event(TOPIC_REGISTERED, event, _meta()) is True


@pytest.mark.asyncio
async def test_a_registration_by_slug_is_read_by_the_resolver_for_that_tenant() -> None:
    world = _World()
    await _register(
        _runner(world),
        _event(SLUG, f"cred_{SLUG}_{PROVIDER}_aa", "qwen/qwen3-coder"),
    )

    overlay = resolve_tenant_overlay(
        _FakeOverlayReader(world.overlay),
        tenant_id=str(TENANT_UUID),
        task_type="code_generation",
    )

    assert overlay is not None
    assert overlay.model_name == "qwen/qwen3-coder"
    assert overlay.secret_ref == f"cred_{SLUG}_{PROVIDER}_aa"
    assert set(world.credentials.values()) == {str(TENANT_UUID)}
    assert {tenant for tenant, _ in world.overlay} == {str(TENANT_UUID)}


@pytest.mark.asyncio
async def test_a_second_registration_replaces_the_first() -> None:
    world = _World()
    runner = _runner(world)
    await _register(runner, _event(SLUG, f"cred_{SLUG}_{PROVIDER}_aa", "model/first"))
    await _register(runner, _event(SLUG, f"cred_{SLUG}_{PROVIDER}_bb", "model/second"))

    assert list(world.overlay) == [(str(TENANT_UUID), BYOK_ALL_TASK_TYPES)]
    overlay = resolve_tenant_overlay(
        _FakeOverlayReader(world.overlay),
        tenant_id=str(TENANT_UUID),
        task_type="code_generation",
    )
    assert overlay is not None
    assert overlay.model_name == "model/second"
    assert overlay.secret_ref == f"cred_{SLUG}_{PROVIDER}_bb"


@pytest.mark.asyncio
async def test_another_tenants_rows_are_never_read() -> None:
    world = _World()
    runner = _runner(world)
    await _register(runner, _event(OTHER_SLUG, f"cred_{OTHER_SLUG}_x", "other/model"))
    await _register(runner, _event(SLUG, f"cred_{SLUG}_{PROVIDER}_aa", "mine/model"))

    reader = _FakeOverlayReader(world.overlay)
    mine = resolve_tenant_overlay(
        reader, tenant_id=str(TENANT_UUID), task_type="code_generation"
    )
    theirs = resolve_tenant_overlay(
        reader, tenant_id=str(OTHER_UUID), task_type="code_generation"
    )
    assert mine is not None
    assert mine.model_name == "mine/model"
    assert theirs is not None
    assert theirs.model_name == "other/model"


@pytest.mark.asyncio
async def test_a_uuid_identity_is_written_as_given() -> None:
    world = _World()
    await _register(
        _runner(world),
        _event(str(TENANT_UUID), f"cred_{SLUG}_{PROVIDER}_aa", "mine/model"),
    )
    assert {tenant for tenant, _ in world.overlay} == {str(TENANT_UUID)}


@pytest.mark.asyncio
async def test_a_slug_the_registry_does_not_know_writes_nothing() -> None:
    world = _World()
    with pytest.raises(TenantRegistryResolutionError):
        await _runner(world).project_event(
            TOPIC_REGISTERED,
            _event("not-in-registry", "cred_not-in-registry_x", "m/m"),
            _meta(),
        )
    assert world.overlay == {}
    assert world.credentials == {}
