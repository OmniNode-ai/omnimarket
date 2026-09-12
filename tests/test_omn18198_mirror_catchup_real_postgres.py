# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18198 against a real Postgres: the mirror-catchup race, end to end.

WHY THIS EXISTS BESIDE THE UNIT TESTS. The unit suite
(``test_omn18198_mirror_catchup_race.py``) drives a fake reader, which is the
right shape for asserting the DECISION -- wait, or refuse, and which refusal.
It cannot assert the two things only a real connection can settle:

* that ``max(observed_at)`` over the real relation comes back as an aware
  ``datetime`` that is comparable to an event timestamp at all. A mock returns
  whatever the test hands it; Postgres returns what the column's type says, and
  a naive/aware mismatch raises ``TypeError`` at the comparison rather than
  failing a leg. That class of defect (str-vs-datetime on a projection column)
  is what OMN-15905 cost.
* that the two lookups the resolver now makes -- the row read and the watermark
  read -- both work against the shipped migration's actual column names.

THE RACE IS PRODUCED, NOT SIMULATED. The in-window tenant's ``TENANT_CREATED``
is replayed through the REAL projection writer partway through the wait, so the
row appears the way it appears on the lane: written by shipped code, with
``observed_at`` stamped by the database. Nothing here inserts a mirror row by
hand.

Measured context for the numbers below: on onex-dev, over the 28 tenants minted
since ``node_projection_tenant_registry`` went live, the mirror materialises a
tenant 1.06 s to 8.78 s after its registry row is created. The C19 tenant that
was quarantined was minted at 18:01:42.487 and mirrored at 18:01:51.267, and
its delegation terminal was refused at 18:01:51 -- inside that window.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
)
from omnimarket.projection import tenant_registry_resolution as resolution
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
    async_registry_mirror_watermark,
    async_resolve_write_tenant_uuid,
)

_MIRROR_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_registry"
    / "migrations"
    / "0000_create_tenant_registry_mirror.sql"
)

_TOPIC = "onex.tenant.events"

# Synthetic identities over RFC 2606 `.invalid` names, so neither is anyone's
# real identifier -- the mechanism under test needs no real one (the reasoning
# OMN-17288 applied when it removed live customer identifiers from this tree).
_SETTLED_SLUG = "t-settled-omn18198.example.invalid"
_SETTLED_UUID = UUID("3a5c9e21-4d18-5f7b-8c02-1e9a4b7d3c55")
_RACING_SLUG = "t-racing-omn18198.example.invalid"
_RACING_UUID = UUID("7b21d4f0-92ac-5e13-bf46-08c5d2a91e64")
_NEVER_MINTED_UUID = UUID("f04e8c37-5b61-5a29-9d17-6c3e0f8b2a41")


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-18198 real-Postgres "
            "mirror-catchup race"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-18198 mirror catchup: {exc}")


class _SchemaScopedDb:
    """Routes the writer's SQL through the disposable schema's connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: object) -> list[dict[str, object]]:
        rows = await self._conn.fetch(sql, *args)
        return [dict(row) for row in rows]


def _tenant_created(*, slug: str, tenant_uuid: UUID, created_at: str) -> dict:
    return {
        "operation": "TENANT_CREATED",
        "success": True,
        "correlation_id": f"omn18198-{slug}",
        "payload": {
            "tenant": {
                "tenant_id": str(tenant_uuid),
                "tenant_slug": slug,
                "name": slug,
                "status": "active",
                "created_at": created_at,
                "plan_code": "beta",
            }
        },
    }


async def _mirror(conn: asyncpg.Connection, *, slug: str, tenant_uuid: UUID) -> None:
    """Materialise one tenant through the REAL projection writer."""
    runner = HandlerTenantRegistryProjectionRunner()
    runner._db = _SchemaScopedDb(conn)  # type: ignore[assignment]
    assert await runner.project_event(
        _TOPIC,
        _tenant_created(
            slug=slug,
            tenant_uuid=tenant_uuid,
            created_at=datetime.now(UTC).isoformat(),
        ),
        MessageMeta(partition=0, offset=0, fallback_id="omn18198", topic=_TOPIC),
    )


@asynccontextmanager
async def _mirror_schema() -> AsyncIterator[asyncpg.Connection]:
    """A disposable schema carrying the shipped mirror migration and one row."""
    conn = await _connect_or_skip()
    schema = f"omn18198_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_MIRROR_MIGRATION.read_text(encoding="utf-8"))
        await _mirror(conn, slug=_SETTLED_SLUG, tenant_uuid=_SETTLED_UUID)
        yield conn
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_watermark_comes_back_aware_and_comparable() -> None:
    """The premise every other assertion rests on, measured not assumed.

    ``observed_at`` is TIMESTAMPTZ in the shipped migration, so asyncpg must
    decode it as an AWARE datetime. If it came back naive, every comparison the
    resolver makes would raise ``TypeError`` at runtime and the fix would turn
    an attribution defect into a crash -- exactly the str-vs-datetime class
    OMN-15905 paid for on a different column.
    """
    async with _mirror_schema() as conn:
        watermark = await async_registry_mirror_watermark(conn)
        assert isinstance(watermark, datetime)
        assert watermark.tzinfo is not None, "TIMESTAMPTZ must decode as aware"
        # The comparison the resolver actually performs must not raise.
        assert watermark < datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_settled_tenant_resolves_against_the_real_relation() -> None:
    """Positive control. The ordinary path is untouched by the new wait."""
    async with _mirror_schema() as conn:
        resolved = await async_resolve_write_tenant_uuid(
            conn, str(_SETTLED_UUID), event_timestamp=datetime.now(UTC)
        )
        assert resolved == str(_SETTLED_UUID)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_tenant_mirrored_mid_wait_is_attributed_not_quarantined() -> None:
    """THE RACE, produced against a real relation rather than simulated.

    The event is stamped AFTER the mirror's current watermark, so the resolver
    can see the mirror is behind it. The tenant is then materialised through
    the real projection writer while the resolver is waiting. Before this fix
    the first miss was terminal and the terminal was quarantined.
    """
    async with _mirror_schema() as conn:
        watermark = await async_registry_mirror_watermark(conn)
        assert watermark is not None
        event_at = watermark + timedelta(seconds=1)

        async def _mint_after_a_moment() -> None:
            await asyncio.sleep(0.4)
            await _mirror(conn, slug=_RACING_SLUG, tenant_uuid=_RACING_UUID)

        # The writer shares this connection, so the mint is sequenced against
        # the resolver's polls rather than run concurrently on it -- asyncpg
        # forbids two operations in flight on one connection, and a second
        # connection would not be the same schema's search_path.
        mint = asyncio.create_task(_mint_after_a_moment())
        try:
            resolved = await async_resolve_write_tenant_uuid(
                conn, str(_RACING_UUID), event_timestamp=event_at
            )
        finally:
            await mint

        assert resolved == str(_RACING_UUID), (
            "a tenant that appears inside the window must be attributed"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_caught_up_mirror_refuses_a_never_minted_tenant_at_once() -> None:
    """The other half, and the one that protects throughput.

    The event predates the watermark, so the mirror has demonstrably processed
    everything up to it. A tenant it does not hold was never minted: refuse
    immediately, with the unknown-tenant message, and do not spend the window.
    """
    async with _mirror_schema() as conn:
        watermark = await async_registry_mirror_watermark(conn)
        assert watermark is not None
        stale_event = watermark - timedelta(minutes=5)

        started = asyncio.get_running_loop().time()
        with pytest.raises(TenantRegistryResolutionError) as caught:
            await async_resolve_write_tenant_uuid(
                conn, str(_NEVER_MINTED_UUID), event_timestamp=stale_event
            )
        elapsed = asyncio.get_running_loop().time() - started

        assert "OMN-16831" in str(caught.value), (
            "a caught-up mirror yields the unknown-tenant refusal"
        )
        assert elapsed < resolution.MIRROR_CATCHUP_DEADLINE_SECONDS / 2, (
            "it must refuse at once rather than spending the window; a stall "
            "on every unknown tenant is how this guard would wedge a partition"
        )
