# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17446 AC1: a tenant whose lifecycle event aged off the topic has no
mirror row, and the OMN-16804 write-path resolver therefore refuses.

WHAT THIS PINS. ``tenant_registry_mirror`` is materialized from exactly one
source -- ``onex.tenant.events`` -- and that topic ran at
``log.retention.hours=168`` on the dev MSK cluster. So the relation could only
ever hold tenants whose ``TENANT_CREATED`` was still inside a seven-day
window. Four of the five tenants that had written a delegation event on
staging were already outside it when the projection was first deployed. Their
events were not delayed or unconsumed; they were **gone**, and no offset
reset, consumer restart or redeploy could bring them back.

The consequence, and the thing this test drives, is one step further on: the
write path that resolves tenant identity against that relation refuses every
event from such a tenant, forever, because refusing is the correct behaviour.
The resolver is not the defect. Its refusal is the correct terminal state for
an event nobody can attribute, and OMN-16804 exists precisely to make it
refuse rather than invent an identity. What was broken was the population of
the relation it asks, which OMN-17446's reconcile emitter fixed.

WHY THE REAL RESOLVER AND A REAL RELATION. The two existing tests that come
closest each miss one half. ``test_omn16831_delegation_writer_zero_rows``
drives the right code path with the right UUID-shaped identity, but its mirror
is an ``AsyncMock`` -- it proves the resolver's logic, not that the logic is
reading the relation the migration actually creates.
``test_omn16804_..._real_postgres`` has a real relation, but resolves a
*slug*, which on this surface takes a different branch: for the three slugs in
the closed legacy map an empty mirror does NOT refuse, it falls back. Neither
one models the retention gap. This test uses the real
``0000_create_tenant_registry_mirror.sql`` relation, the real writer to
populate the in-window half of it, the real ``async_resolve_write_tenant_uuid``
composition every async projection writer calls, and a real ``asyncpg``
connection as the ``db`` -- so the SQL, the column types and the branch are
all the shipped ones.

WHY IT IS NOT RED AT ITS PARENT, STATED PLAINLY. The ticket wrote AC1 as
"fails today by construction". That was true of the SCENARIO when the ticket
was filed -- the mirror could not be populated -- but it was never true of
this ASSERTION: the resolver already refused an unresolvable identity, because
that is what OMN-16804 built. Making this test red at its parent would mean
first breaking the resolver, which is not something to ship in order to
satisfy the wording. Instead the test carries its own mutation control
(``test_the_refusal_assertion_is_probative``): it proves the assertion can
fail, by driving the same real path with the mirror populated, so a green
result here is a measurement rather than a tautology.

SKIPS (never ERRORs) without a reachable database, matching the harness in
``test_omn16930_real_postgres_tenant_registry_write_path.py``.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_PROJECTION_NODE,
    TenantRegistryResolutionError,
    async_registry_tenant_uuid,
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

# Two synthetic tenants standing in for the two sides of the retention
# boundary. Both UUIDs are uuid5 over RFC 2606 `.invalid` names, so neither is
# anyone's real identifier -- what is under test is the resolution mechanism,
# which needs no real identity (the same reasoning OMN-17288 applied when it
# replaced the live customer identifiers previously committed here).
#
# INSIDE the window: its TENANT_CREATED is still on the topic, so the
# projection consumed it and the mirror holds a row.
_IN_WINDOW_SLUG = "t-in-window-omn17446.example.invalid"
_IN_WINDOW_UUID = UUID("2f1e5a1c-6b21-5c5a-9a4e-2f6e0b6a5f11")

# OUTSIDE the window: created before the retention horizon, so its
# TENANT_CREATED expired off onex.tenant.events unconsumed and no row for it
# can ever appear. This is the OMN-17446 tenant.
_AGED_OUT_SLUG = "t-aged-out-omn17446.example.invalid"
_AGED_OUT_UUID = UUID("6c0d2b77-1f43-5de6-8b02-72ad0c9f4e30")


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
            "POSTGRES_PASSWORD not set -- skipping OMN-17446 real-Postgres "
            "retention-gap resolver refusal"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-17446 retention gap: {exc}")


class _SchemaScopedDb:
    """Routes the writer's SQL through the disposable schema's connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, *args: object) -> list[dict[str, object]]:
        rows = await self._conn.fetch(sql, *args)
        return [dict(row) for row in rows]


def _tenant_created(
    *, slug: str, tenant_uuid: UUID, created_at: str
) -> dict[str, object]:
    return {
        "operation": "TENANT_CREATED",
        "success": True,
        "correlation_id": f"omn17446-{slug}",
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


@asynccontextmanager
async def _mirror_after_retention_window() -> AsyncIterator[asyncpg.Connection]:
    """The mirror as the retention window actually leaves it.

    The in-window tenant's ``TENANT_CREATED`` is replayed through the REAL
    projection writer, so its row is written by shipped code rather than by a
    fixture INSERT. The aged-out tenant's event is simply never delivered --
    that is what expiry IS, and it is why the gap cannot be closed by
    replaying the topic. Nothing here deletes a row; the absence is the
    starting condition, not something the test arranges after the fact.
    """
    conn = await _connect_or_skip()
    schema = f"omn17446_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_MIRROR_MIGRATION.read_text(encoding="utf-8"))

        runner = HandlerTenantRegistryProjectionRunner()
        runner._db = _SchemaScopedDb(conn)  # type: ignore[assignment]
        assert await runner.project_event(
            _TOPIC,
            _tenant_created(
                slug=_IN_WINDOW_SLUG,
                tenant_uuid=_IN_WINDOW_UUID,
                created_at="2026-08-29T09:00:00+00:00",
            ),
            MessageMeta(partition=0, offset=0, fallback_id="omn17446", topic=_TOPIC),
        )
        yield conn
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_aged_out_tenant_has_no_mirror_row_and_the_resolver_refuses() -> None:
    """AC1, both halves, against the real relation and the real resolver."""
    async with _mirror_after_retention_window() as conn:
        # Half one: the mirror holds no row for the aged-out tenant, in either
        # column. Asserted directly against the relation, so the premise is
        # measured rather than assumed by the test that depends on it.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM tenant_registry_mirror "
                "WHERE tenant_uuid = $1 OR tenant_slug = $2",
                _AGED_OUT_UUID,
                _AGED_OUT_SLUG,
            )
            == 0
        )
        # ...and the real reader agrees, through the shipped SQL rather than
        # the test's own query.
        assert (await async_registry_tenant_uuid(conn, str(_AGED_OUT_UUID))) is None

        # Half two: the write path therefore refuses. This is the composition
        # every async projection writer calls (OMN-15583), not a re-derivation
        # of it.
        with pytest.raises(TenantRegistryResolutionError) as excinfo:
            await async_resolve_write_tenant_uuid(conn, str(_AGED_OUT_UUID))

        message = str(excinfo.value)
        assert str(_AGED_OUT_UUID) in message
        # The refusal must say the tenant is UNMATERIALIZED, not that it does
        # not exist -- an operator who reads it as "no such tenant" deletes
        # real data. It must also name the projection to look at.
        assert TENANT_REGISTRY_PROJECTION_NODE in message
        assert "No identity will be invented" in message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_refusal_assertion_is_probative() -> None:
    """The mutation control for the test above.

    A refusal test passes trivially if the path under it refuses everything.
    Same relation, same resolver, same call -- but the tenant whose event was
    still inside the window resolves, and resolves to the UUID the authority
    recorded. Without this, a green result above would not distinguish "the
    resolver correctly refuses an aged-out tenant" from "the resolver refuses".
    """
    async with _mirror_after_retention_window() as conn:
        resolved = await async_resolve_write_tenant_uuid(conn, str(_IN_WINDOW_UUID))
        assert resolved == str(_IN_WINDOW_UUID)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_reconcile_path_is_what_closes_the_gap_not_a_replay() -> None:
    """AC2's mechanism, demonstrated at the seam it has to work at.

    Re-publishing the tenant's own ``TENANT_CREATED`` -- which is exactly what
    the ``tenant_event_reemit`` primitive does, from the authoritative
    ``tenants`` row, UUID-preserving -- moves the aged-out tenant from refusing
    to resolving, with no invented identity and no fixture INSERT into the
    mirror. That is the property the reconcile path has to hold and topic
    retention cannot: the recovery does not depend on the original event still
    existing.
    """
    async with _mirror_after_retention_window() as conn:
        with pytest.raises(TenantRegistryResolutionError):
            await async_resolve_write_tenant_uuid(conn, str(_AGED_OUT_UUID))

        runner = HandlerTenantRegistryProjectionRunner()
        runner._db = _SchemaScopedDb(conn)  # type: ignore[assignment]
        assert await runner.project_event(
            _TOPIC,
            _tenant_created(
                slug=_AGED_OUT_SLUG,
                tenant_uuid=_AGED_OUT_UUID,
                created_at="2026-08-12T09:00:00+00:00",
            ),
            MessageMeta(
                partition=0, offset=1, fallback_id="omn17446-reemit", topic=_TOPIC
            ),
        )

        # The identity is the one the authority recorded, not a fresh one --
        # the load-bearing property, because the resolver makes a mirror /
        # legacy-map disagreement a hard raise, so a re-mint would convert a
        # resolving tenant into a permanent failure.
        assert await async_resolve_write_tenant_uuid(conn, str(_AGED_OUT_UUID)) == str(
            _AGED_OUT_UUID
        )
