# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review regressions at real storage boundaries, written before the fixes.

Failures: a verdict leaves an old/unpriced savings stamp, a contract without
row exposures cannot finish its derived write, publication precedes commit,
or a deferred commit failure leaks a snapshot of a rolled-back input.
Only the snapshot transport and external pricing input are intercepted.
integration_postgres_dsn reads INTEGRATION_POSTGRES_* from tests/conftest.py.
"""

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.projection.test_omn21015_run_savings_served import (
    TENANT,
    store,
)
from tests.projection.test_omn21015_run_savings_served import (
    priced as priced,
)
from tests.projection.test_omn21015_transactional_run_write import (
    _handler,
    _paired,
    _rows,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn17228_real_postgres_drifted_default_write_path import (
    _quality_gate_delivery,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("priced")]


@pytest.mark.integration
async def test_an_async_verdict_recomputes_the_stored_inputs_and_publishes_them(
    integration_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, _):
        _, terminal = _rows()
        await conn.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE, status text)"
        )
        await conn.execute(
            "INSERT INTO tenant_registry_mirror VALUES ('savings-tenant', $1, 'active')",
            UUID(TENANT),
        )
        await conn.execute(
            "INSERT INTO delegation_events (correlation_id, tenant_id, timestamp,"
            " created_at, task_type, delegated_to, tokens_input, tokens_output, cost_usd)"
            " VALUES ($1,$2,$3,$3,'document','fixture-serving-model',1000,200,0.004)",
            terminal["correlation_id"],
            UUID(TENANT),
            terminal["timestamp"],
        )
        published = []

        async def publish(rows, _meta):
            published.extend(rows)

        monkeypatch.setattr(runner, "_publish_row_snapshot", publish)
        data = _quality_gate_delivery(correlation_id=str(terminal["correlation_id"]))
        data["_envelope"]["tenant_id"] = TENANT
        assert await runner._project_quality_gate_result(
            data, MessageMeta(partition=0, offset=0, fallback_id="verdict")
        )
        stored = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id=$1",
            terminal["correlation_id"],
        )
        _paired(dict(stored))
        assert len(published) == 1
        _paired(published[0])


@pytest.mark.parametrize("path", ["async", "sync", "sqlite"])
@pytest.mark.integration
async def test_a_contract_without_row_exposures_still_commits_its_derivation(
    path: str,
    integration_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, terminal = _rows()
    if path == "sqlite":
        db = store(tmp_path)
        handler = _handler(monkeypatch, [])
        handler._row_exposures = ()
        handler._write_delegation_row(db, terminal)
        _paired(db.query("delegation_events")[0])
        return
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, schema):
        if path == "async":
            runner._row_exposures = ()
            await runner._write_delegation_row(
                terminal, MessageMeta(partition=0, offset=0, fallback_id="no-exposure")
            )
        else:
            db = PostgresSyncProjectionAdapter(integration_postgres_dsn, schema=schema)
            handler = _handler(monkeypatch, [])
            handler._row_exposures = ()
            await asyncio.to_thread(handler._write_delegation_row, db, terminal)
        stored = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id=$1",
            terminal["correlation_id"],
        )
        _paired(dict(stored))


@pytest.mark.parametrize("path", ["async", "sync"])
@pytest.mark.integration
async def test_real_adapter_publishes_only_committed_rows_and_not_a_failed_commit(
    path: str, integration_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, schema):
        _, terminal = _rows()
        sync_db = PostgresSyncProjectionAdapter(integration_postgres_dsn, schema=schema)
        observer = PostgresSyncProjectionAdapter(
            integration_postgres_dsn, schema=schema
        )
        published = []
        handler = _handler(monkeypatch, published)

        def check_committed(rows):
            assert len(rows) == 1
            actual = observer.query(
                "delegation_events",
                {
                    "correlation_id": terminal["correlation_id"],
                    "tenant_id": TENANT,
                },
            )
            assert len(actual) == 1, "snapshot preceded its commit"
            _paired(actual[0])
            _paired(rows[0])
            assert Decimal(str(rows[0]["cost_usd"])) == actual[0]["cost_usd"]
            published.extend(rows)

        async def publish(rows, _meta):
            await asyncio.to_thread(check_committed, rows)

        monkeypatch.setattr(runner, "_publish_row_snapshot", publish)
        monkeypatch.setattr(handler, "_publish_row_snapshot", check_committed)

        async def write(row):
            if path == "async":
                await runner._write_delegation_row(
                    row, MessageMeta(partition=0, offset=0, fallback_id="commit")
                )
            else:
                await asyncio.to_thread(handler._write_delegation_row, sync_db, row)

        await write(terminal)
        assert len(published) == 1
        # The server, not a mocked connection/context manager, rejects COMMIT.
        await conn.execute(
            "CREATE FUNCTION refuse_commit() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE EXCEPTION 'owned commit refusal'; END $$;"
            " CREATE CONSTRAINT TRIGGER refuse_commit AFTER INSERT OR UPDATE"
            " ON delegation_events DEFERRABLE INITIALLY DEFERRED"
            " FOR EACH ROW EXECUTE FUNCTION refuse_commit()"
        )
        with pytest.raises(Exception, match="owned commit refusal"):
            await write({**terminal, "cost_usd": Decimal("0.009")})
        assert len(published) == 1, "a failed commit leaked a snapshot"
        stored = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id=$1",
            terminal["correlation_id"],
        )
        _paired(dict(stored))
