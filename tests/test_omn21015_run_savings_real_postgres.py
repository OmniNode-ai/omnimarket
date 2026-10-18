# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real PostgreSQL twin of the per-run savings write/serve checks.

Failure modes: the migration is absent or defaults old rows to zero; the
deployed async writer omits/recalculates the figure or loses its nullable
basis; numeric text cannot bind to the actual column type; a negative or a
measured zero changes meaning; snapshot encoding differs from the stored row.
Uses an owned container and the existing full-migration writer harness.
No shared database, service or broker is used. Run on the lab with TMPDIR and
pytest --basetemp inside the lab account's private working directory, and
require zero skips for a proof.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID

import asyncpg
import pytest

from omnimarket.projection.runner import MessageMeta
from tests.projection.test_omn21015_run_savings_served import TENANT, TOPIC, event
from tests.projection.test_omn21015_run_savings_served import priced as priced
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _provisioned_runner,
)
from tests.test_omn18147_delegation_completed_using_rls import _LocalPostgres
from tests.test_omn18147_delegation_completed_using_rls import (
    local_postgres as local_postgres,
)

pytestmark = pytest.mark.usefixtures("priced")
COLUMNS = (
    "modelled_savings_usd",
    "savings_baseline_model",
    "savings_pricing_manifest_version",
    "savings_measurement",
)


def dsn(postgres: _LocalPostgres) -> str:
    return f"postgresql://postgres@/{postgres.database}?host={quote(postgres.host)}&port={postgres.port}"


@pytest.fixture
async def postgres(local_postgres: _LocalPostgres) -> _LocalPostgres:
    conn = await asyncpg.connect(dsn(local_postgres))
    try:
        # Required by the live migration set. This role exists only inside the
        # owned test cluster and cannot grant access to a shared lane.
        await conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
            "WHERE rolname = 'tenant_projection_writer') THEN "
            "CREATE ROLE tenant_projection_writer NOLOGIN NOSUPERUSER NOBYPASSRLS; "
            "END IF; END $$"
        )
        # Forward migration 098 provisions this schema before node migration
        # 0051's audit table. Reproduce that prerequisite in the owned cluster.
        await conn.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
    finally:
        await conn.close()
    return local_postgres


@pytest.fixture
def integration_postgres_dsn(
    postgres: _LocalPostgres, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The real DSN of this test's owned, provisioned PostgreSQL instance."""
    monkeypatch.setenv("INTEGRATION_POSTGRES_DSN", dsn(postgres))
    return os.environ["INTEGRATION_POSTGRES_DSN"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("changes", "saving", "measurement"),
    [
        ({}, Decimal("0.010"), "measured"),
        (
            {"tokens_input": 100, "tokens_output": 100, "cost_usd": 0.005},
            Decimal("-0.002"),
            "measured",
        ),
        ({"cost_usd": 0.014}, Decimal("0.000"), "measured"),
        ({"tokens_input": 0, "tokens_output": 0}, None, "unknown_tokens"),
    ],
)
async def test_deployed_writer_persists_and_republishes_canonical_run_savings(
    integration_postgres_dsn: str,
    changes: dict[str, object],
    saving: Decimal | None,
    measurement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, schema):
        columns = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'delegation_events'",
            schema,
        )
        nullable = {r["column_name"]: r["is_nullable"] for r in columns}
        assert all(nullable.get(c) == "YES" for c in COLUMNS), nullable
        await conn.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO tenant_registry_mirror VALUES ('savings-tenant', $1::uuid)",
            UUID(TENANT),
        )
        published: list[dict[str, object]] = []

        class RecordingProducer:
            async def send_and_wait(
                self,
                topic: str,
                *,
                value: bytes | None,
                key: bytes,
                headers: list[tuple[str, bytes]],
            ) -> None:
                if topic.endswith(TOPIC):
                    assert value is not None
                    published.append(json.loads(value)["row"])

        async def producer() -> RecordingProducer:
            return RecordingProducer()

        # Replace only the network transport. Snapshot encoding, size checking,
        # returned-row selection and namespace application all remain real.
        monkeypatch.setattr(runner, "_ensure_producer", producer)
        payload = event(**changes).model_dump(mode="json")
        assert await runner.project_event(
            runner._topic_delegated,
            payload,
            MessageMeta(
                partition=0, offset=0, fallback_id=str(payload["correlation_id"])
            ),
        )
        row = await conn.fetchrow(
            f"SELECT {', '.join(COLUMNS)} FROM delegation_events WHERE correlation_id = $1",
            str(payload["correlation_id"]),
        )
        assert row is not None
        stored = row["modelled_savings_usd"]
        assert (None if stored is None else Decimal(str(stored))) == saving
        assert row["savings_measurement"] == measurement
        assert row["savings_baseline_model"] == "fixture-baseline"
        assert row["savings_pricing_manifest_version"] == "fixture-v1"
        assert len(published) == 1
        actual = published[0]["modelled_savings_usd"]
        assert (None if actual is None else Decimal(str(actual))) == saving
        for name in COLUMNS[1:]:
            assert published[0][name] == row[name]


@pytest.mark.integration
async def test_migration_is_repeatable_and_never_prices_an_old_row(
    integration_postgres_dsn: str,
) -> None:
    migrations = list(_MIGRATIONS_DIR.glob("*modelled_run_savings.sql"))
    assert len(migrations) == 1
    async with _provisioned_runner(integration_postgres_dsn) as (
        _runner,
        conn,
        _schema,
    ):
        await conn.execute(
            "ALTER TABLE delegation_events "
            + ", ".join(f"DROP COLUMN {c}" for c in COLUMNS)
        )
        await conn.execute(
            "INSERT INTO delegation_events (correlation_id, task_type, delegated_to, tenant_id) "
            "VALUES ('old-unmeasured', 'document', 'old-model', $1)",
            TENANT,
        )
        for _ in range(2):
            await conn.execute(migrations[0].read_text())
        row = await conn.fetchrow(
            f"SELECT {', '.join(COLUMNS)} FROM delegation_events "
            "WHERE correlation_id = 'old-unmeasured'"
        )
        assert row is not None
        assert tuple(row.values()) == (None, None, None, None)


@pytest.mark.integration
@pytest.mark.parametrize("held_writer", ["sparse", "terminal"])
async def test_two_writers_keep_the_saving_paired_with_concurrent_inputs(
    integration_postgres_dsn: str,
    held_writer: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold one real writer at its storage boundary, race a second, in both orders.

    Three fresh correlation ids per order. Only the scheduling boundary is
    intercepted; reads, computation and both upserts use the owned Postgres.
    A serialized second writer may wait until the held first is released.
    """
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, _):
        peer = copy.copy(runner)

        async def no_snapshot(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(runner, "_publish_row_snapshot", no_snapshot)
        monkeypatch.setattr(peer, "_publish_row_snapshot", no_snapshot)
        original = runner._dynamic_upsert
        for repeat in range(3):
            cid = str(event(100 + repeat).correlation_id)
            common: dict[str, object] = {
                "correlation_id": cid,
                "tenant_id": TENANT,
                "created_at": datetime(2026, 10, 13, 12, tzinfo=UTC),
                "timestamp": datetime(2026, 10, 13, 12, tzinfo=UTC),
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
            }
            terminal = {
                **common,
                "tokens_input": 1000,
                "tokens_output": 200,
                "cost_usd": Decimal("0.004"),
            }
            sparse = dict(common)
            ready, release = asyncio.Event(), asyncio.Event()

            async def held_upsert(
                *,
                _ready: asyncio.Event = ready,
                _release: asyncio.Event = release,
                **kwargs: object,
            ) -> list[dict[str, object]]:
                _ready.set()
                await _release.wait()
                return await original(**kwargs)

            monkeypatch.setattr(runner, "_dynamic_upsert", held_upsert)
            meta = MessageMeta(partition=0, offset=repeat, fallback_id=cid)
            first_row, second_row = (
                (sparse, terminal) if held_writer == "sparse" else (terminal, sparse)
            )
            first = asyncio.create_task(runner._write_delegation_row(first_row, meta))
            await asyncio.wait_for(ready.wait(), 10)
            second = asyncio.create_task(peer._write_delegation_row(second_row, meta))
            try:
                await asyncio.wait({second}, timeout=1)
            finally:
                release.set()
                await asyncio.gather(first, second)
            row = await conn.fetchrow(
                "SELECT tokens_input, tokens_output, cost_usd, "
                + ", ".join(COLUMNS)
                + " FROM delegation_events WHERE correlation_id=$1",
                cid,
            )
            assert row is not None
            assert (row["tokens_input"], row["tokens_output"]) == (1000, 200)
            assert Decimal(str(row["cost_usd"])) == Decimal("0.004")
            assert row["modelled_savings_usd"] is not None, dict(row)
            assert Decimal(str(row["modelled_savings_usd"])) == Decimal("0.010")
            assert row["savings_measurement"] == "measured"
            assert row["savings_baseline_model"] == "fixture-baseline"
            assert row["savings_pricing_manifest_version"] == "fixture-v1"
