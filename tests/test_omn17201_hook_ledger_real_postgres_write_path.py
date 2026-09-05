# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17201: real-Postgres proof of the cloud hook-ledger write path.

WHY A REAL DATABASE AND NOT A DOUBLE
    OMN-15909 records the exact defect this closes. ``DelegationProjectionRunner``
    bound a wall-clock ``.isoformat()`` STRING to a ``TIMESTAMPTZ`` param at four
    call sites; the seam test guarding it drove an ``AsyncMock``, which accepts a
    ``str`` as happily as a ``datetime``. Only live asyncpg enforces the column
    type. The mock-DB blind spot let the defect merge, deploy and CrashLoopBackOff.

    This module drives the SAME ``project_event`` the deployed writer runs,
    against a real ``public.hook_events`` created by the owning node's own
    migration, so every bound parameter meets its real column type and the
    table's real constraints:

      * ``occurred_at``   TIMESTAMPTZ  -- a str here raises DataError
      * ``payload``       JSONB        -- with a CHECK that it is an object
      * ``event_sha``     CHAR(64)     -- with a CHECK that it is sha256 hex
      * ``batch_sha``     CHAR(64)     -- same CHECK
      * UNIQUE (tenant_id, event_sha) -- the idempotency the ON CONFLICT rides

    Every one of those is a real refusal this node's derivation must satisfy and
    that no in-memory double can enforce.

    It SKIPS (never ERRORs) without a reachable database, matching this repo's
    established ``_connect_or_skip`` idiom.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_physical_topic,
)

from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
    HandlerHookLedgerProjection,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

_RLS_MIGRATION_NAME = "0002_hook_events_tenant_rls.sql"

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_event_capture"
    / "migrations"
    / "0001_create_hook_events.sql"
)
#: FORCE ROW LEVEL SECURITY on public.hook_events. Applied here even though it
#: is OPERATOR-FENCED on the live lanes today (see its own header), because the
#: point of this module is to prove the writer against the posture it will meet
#: when the fence lifts -- not only against today's laxer one.
_RLS_MIGRATION = _MIGRATION.parent / _RLS_MIGRATION_NAME

_CANONICAL = "onex.evt.omniclaude.tool-executed.v1"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping hook-ledger real-DB write proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for hook-ledger write proof: {exc}")


class _RealConnAdapter:
    """Binds the runner's execute() onto a live connection, GUC and all.

    This mirrors AsyncpgAdapter's real contract rather than a convenient
    subset: it opens ONE transaction and sets ``app.tenant_id`` with the
    parameterized ``set_config`` form on the SAME connection before the
    statement runs. Both halves matter -- ``SET LOCAL`` semantics only hold
    inside a transaction, and OMN-15306 is the recorded case of autocommit
    dropping the GUC before the statement, which reads as a silent no-op.

    Without this the test would pass for the wrong reason and prove nothing
    about the RLS path the deployed writer actually takes.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(
        self, sql: str, *args: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._conn.transaction():
            if tenant is not None:
                await self._conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", tenant
                )
            rows = await self._conn.fetch(sql, *args)
        return [dict(r) for r in rows]


_ROLE_SECRET = "omn17201-" + "rls-probe"  # nosec B105 - throwaway test role


def _role_secret_literal() -> str:
    return "'" + _ROLE_SECRET + "'"


def _role_dsn(role: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return (
        f"postgresql://{quote_plus(role)}:{quote_plus(_ROLE_SECRET)}@{host}:{port}/{db}"
    )


def _cloud_record(*, tenant_slug: str, correlation_id: str) -> dict[str, Any]:
    body = {
        "session_id": correlation_id,
        "working_directory": "omni_home",
        "tool_name": "Bash",
        "duration_ms": 184,
        "interrupted": False,
        "hook_source": "post_tool_use",
        "correlation_id": correlation_id,
        "causation_id": None,
        "emitted_at": "2026-08-30T01:59:07.891697+00:00",
        "schema_version": "1.0.0",
    }
    envelope = {
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": _CANONICAL,
        "payload": body,
        "metadata": {"tags": {"gateway_tenant_slug": tenant_slug}},
    }
    data = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert data is not None
    return data


def _runner(conn: asyncpg.Connection) -> HandlerHookLedgerProjection:
    runner = HandlerHookLedgerProjection.__new__(HandlerHookLedgerProjection)
    runner._load_contract()
    runner._db = _RealConnAdapter(conn)  # type: ignore[assignment]

    async def _publish(topic: str, value: bytes) -> None:
        return None

    runner._publish_fn = _publish  # type: ignore[assignment]
    return runner


@pytest.mark.integration
async def test_real_postgres_accepts_every_bound_parameter_and_lands_one_row() -> None:
    """The whole point: real column types, real CHECKs, real UNIQUE key."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(_MIGRATION.read_text())
        await conn.execute(_RLS_MIGRATION.read_text())

        tenant = "beta-gateway-canary"
        correlation_id = str(uuid4())
        wire_topic = resolve_physical_topic(_CANONICAL, tenant_slug=tenant)
        runner = _runner(conn)
        meta = MessageMeta(partition=0, offset=1, fallback_id="", topic=wire_topic)

        projected = await runner.project_event(
            wire_topic,
            _cloud_record(tenant_slug=tenant, correlation_id=correlation_id),
            meta,
        )
        assert projected is True

        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
        rows = await conn.fetch(
            "SELECT tenant_id, event_type, correlation_id, run_id, source, "
            "occurred_at, payload, event_sha, batch_sha "
            "FROM public.hook_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert len(rows) == 1, "exactly one row per hook event"
        row = rows[0]
        assert row["tenant_id"] == tenant
        assert row["event_type"] == _CANONICAL
        assert row["source"] == "gateway-relay"
        assert row["run_id"] == correlation_id
        assert row["occurred_at"].isoformat() == "2026-08-30T01:59:07.891697+00:00"
        assert json.loads(row["payload"])["tool_name"] == "Bash"
    finally:
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_redelivery_is_suppressed_by_the_unique_key() -> None:
    """Idempotent replay proven against the actual constraint, not an assertion."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(_MIGRATION.read_text())
        await conn.execute(_RLS_MIGRATION.read_text())

        tenant = "beta-gateway-canary"
        correlation_id = str(uuid4())
        wire_topic = resolve_physical_topic(_CANONICAL, tenant_slug=tenant)
        runner = _runner(conn)
        record = _cloud_record(tenant_slug=tenant, correlation_id=correlation_id)

        # Same record, DIFFERENT delivery coordinates -- exactly what a
        # consumer-group rebalance produces. If the content address took the
        # coordinates as input, this would double the row.
        for partition, offset in ((0, 1), (4, 9999)):
            meta = MessageMeta(
                partition=partition, offset=offset, fallback_id="", topic=wire_topic
            )
            assert await runner.project_event(wire_topic, dict(record), meta) is True

        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
        count = await conn.fetchval(
            "SELECT count(*) FROM public.hook_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert count == 1, "a redelivery must not create a second row"
    finally:
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_keeps_two_tenants_hook_events_on_separate_rows() -> None:
    """OMN-17066's defect class, proven absent against real FORCE RLS.

    That ticket records the failure: a writer keying tenant isolation on a
    single house-tenant stamp collapsed cross-tenant events onto one
    idempotency key.

    THIS TEST CONNECTS AS AN UNPRIVILEGED ROLE, DELIBERATELY. The two tests
    above run as the ``postgres`` SUPERUSER, which holds BYPASSRLS -- FORCE ROW
    LEVEL SECURITY binds the table OWNER but not a superuser, so a superuser
    connection proves column types, CHECKs and the UNIQUE key and proves
    NOTHING about row isolation. An earlier revision of this test asserted RLS
    isolation on the superuser connection and saw both tenants' rows; that is
    recorded here rather than quietly fixed, because "the isolation test
    passed" on a BYPASSRLS connection is exactly the kind of evidence that
    reads as proof and is not.
    """
    conn = await _connect_or_skip()
    role = f"omn17201_writer_{uuid4().hex[:8]}"
    role_conn: asyncpg.Connection | None = None
    try:
        await conn.execute(_MIGRATION.read_text())
        await conn.execute(_RLS_MIGRATION.read_text())

        # A stand-in for the constrained login the deployed writer uses. It is
        # NOT a superuser and NOT the table owner, so FORCE RLS binds it.
        await conn.execute(
            f'CREATE ROLE "{role}" LOGIN PASSWORD {_role_secret_literal()}'
        )
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await conn.execute(
            f'GRANT SELECT, INSERT, UPDATE ON public.hook_events TO "{role}"'
        )

        role_conn = await asyncpg.connect(_role_dsn(role))
        correlation_id = str(uuid4())
        runner = _runner(role_conn)

        for tenant in ("beta-gateway-canary", "beta-second-tenant"):
            wire_topic = resolve_physical_topic(_CANONICAL, tenant_slug=tenant)
            runner._wire_topics = (*runner._wire_topics, wire_topic)
            meta = MessageMeta(partition=0, offset=1, fallback_id="", topic=wire_topic)
            projected = await runner.project_event(
                wire_topic,
                _cloud_record(tenant_slug=tenant, correlation_id=correlation_id),
                meta,
            )
            assert projected is True, (
                f"the write for {tenant} was refused -- if this fails on the "
                "WITH CHECK predicate, the writer is not setting app.tenant_id"
            )

        # Read each tenant's row under ITS OWN GUC, on the SAME unprivileged
        # connection. Under FORCE RLS there is no single read that sees both,
        # and that is the isolation being proven.
        for tenant in ("beta-gateway-canary", "beta-second-tenant"):
            await role_conn.execute(
                "SELECT set_config('app.tenant_id', $1, false)", tenant
            )
            rows = await role_conn.fetch(
                "SELECT tenant_id FROM public.hook_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert [r["tenant_id"] for r in rows] == [tenant], (
                f"tenant {tenant} must see exactly its own row and no other"
            )

        # And with NO tenant context the policy predicate is NULL, so the
        # fail-closed posture returns nothing rather than everything.
        await role_conn.execute("SELECT set_config('app.tenant_id', '', false)")
        unscoped = await role_conn.fetch(
            "SELECT tenant_id FROM public.hook_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert unscoped == [], "an unscoped read must fail closed, not open"
    finally:
        if role_conn is not None:
            await role_conn.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await conn.execute(f'REVOKE ALL ON public.hook_events FROM "{role}"')
            await conn.execute(f'REVOKE USAGE ON SCHEMA public FROM "{role}"')
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        await conn.close()
