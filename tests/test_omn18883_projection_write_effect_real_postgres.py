# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18883: the generic projection-write effect node against real Postgres.

Drives ``HandlerProjectionWrite`` with a fold result through a real
``AsyncpgAdapter`` and asserts the rows exist, with TIMESTAMPTZ, UUID and JSONB
columns bound as their own types. A second write over the same key proves the
targeted-column UPSERT updates in place. Uses this repo's real-Postgres idiom
(``INTEGRATION_POSTGRES_*`` / ``POSTGRES_PASSWORD``, as in
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``) and a
disposable table that is dropped afterwards.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.model_projection_write import (
    ModelProjectionEnvelopeMetadata,
    ModelProjectionFoldResult,
    ModelProjectionWriteRequest,
)
from omnimarket.nodes.node_projection_write_effect.handlers.handler_projection_write import (
    HandlerProjectionWrite,
)


def _password() -> str:
    return os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )


def _dsn() -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(_password())}@{host}:{port}/{db}"
    )


async def _connect_or_skip() -> asyncpg.Connection:
    if not _password():
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-18883 real-Postgres write"
        )
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for OMN-18883 write: {exc}")


def _envelope() -> ModelProjectionEnvelopeMetadata:
    return ModelProjectionEnvelopeMetadata(
        envelope_id=str(uuid4()),
        event_type="omn18883.example.observed",
        topic="onex.evt.omnimarket.example.v1",
        envelope_timestamp=datetime.now(UTC),
        tenant_id=None,
    )


async def _drive(table: str) -> tuple[int, int, list[asyncpg.Record]]:
    admin = await _connect_or_skip()
    await admin.execute(
        f"CREATE TABLE {table} ("
        " row_id TEXT PRIMARY KEY,"
        " run_id UUID NOT NULL,"
        " observed_at TIMESTAMPTZ NOT NULL,"
        " count INTEGER NOT NULL,"
        " detail JSONB NOT NULL)"
    )
    adapter = AsyncpgAdapter(dsn=_dsn(), min_size=1, max_size=2)
    try:
        await adapter.connect()
        handler = HandlerProjectionWrite(adapter)
        run_id = uuid4()
        observed = datetime(2026, 11, 3, 19, 0, tzinfo=UTC)
        first = await handler.handle(
            ModelProjectionWriteRequest(
                fold_result=ModelProjectionFoldResult(
                    table=table,
                    conflict_key="row_id",
                    rows=(
                        {
                            "row_id": "a",
                            "run_id": run_id,
                            "observed_at": observed,
                            "count": 1,
                            "detail": {"lane": "dev"},
                        },
                        {
                            "row_id": "b",
                            "run_id": run_id,
                            "observed_at": observed,
                            "count": 2,
                            "detail": {"lane": "dev"},
                        },
                    ),
                    jsonb_columns=("detail",),
                ),
                envelope=_envelope(),
            )
        )
        second = await handler.handle(
            ModelProjectionWriteRequest(
                fold_result=ModelProjectionFoldResult(
                    table=table,
                    conflict_key="row_id",
                    rows=(
                        {
                            "row_id": "a",
                            "run_id": run_id,
                            "observed_at": observed,
                            "count": 7,
                            "detail": {"lane": "dev", "again": True},
                        },
                    ),
                    jsonb_columns=("detail",),
                ),
                envelope=_envelope(),
            )
        )
        rows = await admin.fetch(
            f"SELECT row_id, run_id, observed_at, count, detail::text AS detail"
            f" FROM {table} ORDER BY row_id"
        )
        return first.rows_upserted, second.rows_upserted, rows
    finally:
        await adapter.close()
        await admin.execute(f"DROP TABLE IF EXISTS {table}")
        await admin.close()


@pytest.mark.integration
def test_fold_result_rows_exist_after_the_effect_write() -> None:
    table = f"omn18883_projection_write_{uuid4().hex[:12]}"
    first, second, rows = asyncio.run(_drive(table))
    assert first == 2
    assert second == 1
    assert [r["row_id"] for r in rows] == ["a", "b"]
    by_id = {r["row_id"]: r for r in rows}
    assert by_id["a"]["count"] == 7
    assert by_id["b"]["count"] == 2
    assert isinstance(by_id["a"]["run_id"], UUID)
    assert by_id["a"]["observed_at"] == datetime(2026, 11, 3, 19, 0, tzinfo=UTC)
    assert '"again": true' in by_id["a"]["detail"]
