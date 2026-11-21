# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres tests for the floor integration rows write path.

Catches: parameter binding (projected_at must bind as a datetime against
TIMESTAMPTZ, the OMN-15909 class); a newer pass not deleting the keys it no
longer names; an older pass overwriting or re-inserting rows; and the
migration's CHECK constraints not holding.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_projection_floor_integration.handlers.handler_floor_integration_writer import (
    FloorIntegrationProjectionWriter,
    upsert_statement,
)
from omnimarket.nodes.node_projection_floor_integration.ports.floor_integration_store import (
    PassOutcome,
    PostgresFloorIntegrationStore,
)
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    _batch,
)

_SCHEMA = "omn25103_floor_integration_write_path_test"
EVENT = "onex.evt.omnimarket.floor-integration-pass.v1"


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping floor integration write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = _dsn_or_skip()
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        raise pytest.skip.Exception(
            f"no reachable Postgres for floor integration write-path proof: {exc}"
        ) from exc


async def _prepare() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        migration = (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_projection_floor_integration/migrations"
            / "0000_create_floor_integration_rows.sql"
        )
        await conn.execute(
            migration.read_text().replace("omninode_internal.", f"{_SCHEMA}.")
        )
    finally:
        await conn.close()


async def _drop() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    finally:
        await conn.close()


async def _served() -> list[asyncpg.Record]:
    conn = await _connect_or_skip()
    try:
        return list(
            await conn.fetch(
                f"SELECT key, state, seen_running, as_of, projected_at FROM {_SCHEMA}.floor_integration_rows"
            )
        )
    finally:
        await conn.close()


class _AsyncpgFloorIntegrationStore(PostgresFloorIntegrationStore):
    """The runtime's own Postgres store over a real asyncpg pool; only the
    schema name is moved to the test schema, so apply_pass's transaction is the
    code under test."""

    def __init__(self, dsn: str) -> None:
        super().__init__(AsyncpgAdapter(dsn, min_size=1, max_size=2))

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        return await super().execute(_in_test_schema(query), *params)

    async def apply_pass(
        self,
        *,
        lock_key: str,
        held_statement: str,
        upsert_statement: str,
        prune_statement: str,
        incoming_as_of: str,
        rows: list[tuple[object, ...]],
        is_current: Callable[[object], bool],
    ) -> PassOutcome:
        return await super().apply_pass(
            lock_key=lock_key,
            held_statement=_in_test_schema(held_statement),
            upsert_statement=_in_test_schema(upsert_statement),
            prune_statement=_in_test_schema(prune_statement),
            incoming_as_of=incoming_as_of,
            rows=rows,
            is_current=is_current,
        )


def _in_test_schema(query: str) -> str:
    return query.replace("omninode_internal.", f"{_SCHEMA}.")


def _envelope(
    batch: ModelIntegrationReadingBatch,
) -> tuple[ModelIntegrationProjection, dict[str, object]]:
    projection = HandlerIntegrationProjection().handle(batch)
    payload = ModelFloorProjectionEnvelope(
        projection_id="integration",
        as_of=projection.as_of,
        producer="real-postgres-test",
        payload=projection.model_dump(mode="json"),
    ).model_dump(mode="json")
    return projection, payload


@pytest.mark.integration
def test_real_postgres_writer_holds_the_newest_pass() -> None:
    """One pass in, a newer pass that drops a key, then the older pass again."""
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())
    try:
        writer = FloorIntegrationProjectionWriter(
            store=_AsyncpgFloorIntegrationStore(dsn)
        )
        first_batch = _batch()
        newer_batch = first_batch.model_copy(
            update={
                "as_of": first_batch.as_of + timedelta(hours=1),
                "readings": first_batch.readings[1:],
            }
        )
        first, first_payload = _envelope(first_batch)
        newer, newer_payload = _envelope(newer_batch)

        result = writer.handle(
            {**first_payload, "_topic": EVENT, "_partition": 0, "_offset": 0}
        )
        assert result["rows_upserted"] == len(first.rows)
        rows = asyncio.run(_served())
        assert {r["key"] for r in rows} == set(first.rows)
        assert all(isinstance(r["projected_at"], datetime) for r in rows)
        assert all(r["projected_at"].tzinfo is not None for r in rows)

        writer.handle({**newer_payload, "_topic": EVENT, "_partition": 0, "_offset": 1})
        rows = asyncio.run(_served())
        assert {r["key"] for r in rows} == set(newer.rows)
        assert len({r["as_of"] for r in rows}) == 1

        stale = writer.handle(
            {**first_payload, "_topic": EVENT, "_partition": 0, "_offset": 2}
        )
        assert stale.get("skipped_older_pass") is True
        assert {r["key"] for r in asyncio.run(_served())} == set(newer.rows)
    finally:
        asyncio.run(_drop())


async def _execute_in_test_db(statement: str) -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.integration
def test_real_postgres_failed_write_keeps_the_held_pass() -> None:
    """A newer pass whose last upsert fails leaves the held pass row for row."""
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())
    try:
        writer = FloorIntegrationProjectionWriter(
            store=_AsyncpgFloorIntegrationStore(dsn)
        )
        first_batch = _batch()
        newer_batch = first_batch.model_copy(
            update={"as_of": first_batch.as_of + timedelta(hours=1)}
        )
        _, first_payload = _envelope(first_batch)
        newer, newer_payload = _envelope(newer_batch)
        assert len(newer.rows) > 1, "the pass must write rows before the failing one"
        last_key = list(newer.rows)[-1]

        writer.handle({**first_payload, "_topic": EVENT, "_partition": 0, "_offset": 0})
        held = sorted(tuple(r) for r in asyncio.run(_served()))

        asyncio.run(
            _execute_in_test_db(
                f"CREATE FUNCTION {_SCHEMA}.fail_one() RETURNS trigger LANGUAGE plpgsql AS "
                f"$$ BEGIN IF NEW.key = '{last_key}' THEN "
                "RAISE EXCEPTION 'injected write failure'; END IF; RETURN NEW; END $$"
            )
        )
        asyncio.run(
            _execute_in_test_db(
                f"CREATE TRIGGER fail_one BEFORE UPDATE ON {_SCHEMA}.floor_integration_rows "
                f"FOR EACH ROW EXECUTE FUNCTION {_SCHEMA}.fail_one()"
            )
        )
        with pytest.raises(asyncpg.PostgresError, match="injected write failure"):
            writer.handle(
                {**newer_payload, "_topic": EVENT, "_partition": 0, "_offset": 1}
            )

        assert sorted(tuple(r) for r in asyncio.run(_served())) == held
    finally:
        asyncio.run(_drop())


@pytest.mark.integration
def test_real_postgres_refuses_a_non_positive_change_number() -> None:
    dsn = _dsn_or_skip()
    asyncio.run(_prepare())

    async def insert_bad() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                upsert_statement(f"{_SCHEMA}.floor_integration_rows"),
                "omnimarket#0",
                "omnimarket",
                0,
                "t",
                "2026-11-18T00:00:00+00:00",
                "dev",
                "running",
                "none",
                "",
                "",
                "",
                1,
                1,
                0,
                0,
                0,
                "2026-11-18T00:00:00+00:00",
                datetime.now().astimezone(),
            )
        finally:
            await conn.close()

    try:
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(insert_bad())
    finally:
        asyncio.run(_drop())
