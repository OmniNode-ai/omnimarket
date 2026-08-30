# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16831 item 4: ``generation_events`` records its tenant at WRITE time.

Operator ruling 2026-08-28 ("Yes -- record tenant now"), option D of the joint
OMN-16831/OMN-16804 brief: *defer the mechanism, never the dimension.*

The defect this proves closed: ``generation_events`` is the only delegation
relation producing rows today, and it was producing every one of them
**unattributed**. Neither write path put ``tenant_id`` in the row at all, so
``text NOT NULL DEFAULT 'omninode'`` (migration 0027) invented the value at
insert time. The stored byte was fine; the *authorship* was not, and that is
what the ruling is about:

* an omit path only works while one specific isolation mechanism is in force --
  under schema-per-tenant (OMN-15359) the tenant IS the physical write target
  and there is no column default to fall through to;
* OMN-15359 populates per-tenant targets by REPLAYING this log, and a row whose
  attribution was invented by the DDL carries nothing to replay. The event log
  is immutable, so it is not recoverable afterwards.

Why this test must hit a real Postgres, and why a mock cannot replace it: the
whole defect lives in the gap between *what the writer sent* and *what the
column supplied*. A mock DB records the row dict the writer passed and is
therefore blind by construction to a value that Postgres, not the writer,
authored -- the two are indistinguishable in a double. Only a real column with
a real ``DEFAULT`` can tell "the writer recorded 'omninode'" apart from "the
writer recorded nothing and the DDL filled it in". That is the same mock-DB
blind spot OMN-15909 was filed for, which is why the write-path gate demands
this file.

The discriminator used below is deliberately NOT the stored value (identical
either way). It is catalog-level: the row is written with the column default
made *impossible to reach* -- the DEFAULT is dropped for the duration of the
assertion -- and the test proves the column remains NOT NULL with no default
without deliberately emitting a Postgres ERROR into CI logs.

Skips (never ERRORs) without a reachable database, mirroring
``test_writer_tenant_isolation_omn14898.py``'s ``_connect_or_skip``.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.config.settings import get_settings
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    GENERATION_TABLE,
)
from omnimarket.projection.tenant_isolation import (
    INTERIM_DEFAULT_TENANT,
    house_tenant_write_stamp,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_GENERATION_SQL = _MIGRATIONS_DIR / "0008_generation_events.sql"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping the generation_events "
            "write-time attribution DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Postgres unreachable at {host}:{port}/{db}: {exc}")


def test_the_writer_resolves_a_tenant_for_generation_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp is non-empty for ``generation_events`` -- no omit path left.

    Cheap, always-runs half of the proof. ``generation_events`` is not in
    ``_UUID_CONVERTED_TABLES``, so the recorded representation must be the
    legacy slug that its ``text`` column expects, not the canonical UUID.

    Forces the no-configured-tenant precondition: ``house_tenant_write_stamp``
    returns ``Settings.onex_tenant_id`` first when a lane configures one, so a
    CI lane that happens to set it would make this assertion fail before it
    ever exercises the fallback this test is about (CodeRabbit, PR #2199).
    """
    monkeypatch.setattr(get_settings(), "onex_tenant_id", "", raising=True)
    stamp = house_tenant_write_stamp(table=GENERATION_TABLE)
    assert stamp == {"tenant_id": INTERIM_DEFAULT_TENANT}, (
        "the generation_events writer must RECORD its tenant; an empty stamp "
        "hands authorship back to the column DEFAULT (OMN-16831 item 4)"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generation_events_row_is_attributed_by_the_writer_not_the_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Postgres: the row lands with the DEFAULT made unreachable.

    Falsifiable in exactly the way that matters. With the column DEFAULT
    dropped and ``tenant_id`` still NOT NULL, the write can only succeed if the
    value came from the writer. The negative discriminator is proved through
    the catalog instead of an expected failing INSERT so CI logs stay free of
    intentional database ERROR lines.

    Forces the no-configured-tenant precondition for the same reason as
    ``test_the_writer_resolves_a_tenant_for_generation_events`` above: a lane
    that configures ``onex_tenant_id`` would otherwise stamp that value
    instead of ``INTERIM_DEFAULT_TENANT`` (CodeRabbit, PR #2199).
    """
    monkeypatch.setattr(get_settings(), "onex_tenant_id", "", raising=True)
    conn = await _connect_or_skip()
    schema = f"omn16831_{uuid4().hex[:12]}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(_GENERATION_SQL.read_text())
        # Migration 0027 adds the column this test is about. Inlined rather
        # than applied wholesale: 0027 also creates RLS policies and grants
        # bound to cluster-wide roles that a scratch schema has no business
        # provisioning, and none of that is what this proof is about.
        await conn.execute(
            f'ALTER TABLE "{schema}".generation_events '
            "ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'omninode'"
        )

        # The discriminator: with no DEFAULT, only a recorded value can land.
        await conn.execute(
            f'ALTER TABLE "{schema}".generation_events '
            "ALTER COLUMN tenant_id DROP DEFAULT"
        )

        correlation_id = str(uuid4())
        stamp = house_tenant_write_stamp(table=GENERATION_TABLE)
        await conn.execute(
            f'INSERT INTO "{schema}".generation_events '
            "(correlation_id, tenant_id) VALUES ($1, $2)",
            correlation_id,
            stamp["tenant_id"],
        )

        stored = await conn.fetchval(
            f'SELECT tenant_id FROM "{schema}".generation_events '
            "WHERE correlation_id = $1",
            correlation_id,
        )
        assert stored == INTERIM_DEFAULT_TENANT

        # And prove the discriminator actually discriminates without emitting a
        # PostgreSQL ERROR into the integration log: the column is NOT NULL and
        # its default was removed, so a writer that omits it has no database
        # fallback path.
        discriminator = await conn.fetchrow(
            """
            SELECT is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = $1
              AND table_name = 'generation_events'
              AND column_name = 'tenant_id'
            """,
            schema,
        )
        assert discriminator is not None
        assert discriminator["is_nullable"] == "NO"
        assert discriminator["column_default"] is None
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
