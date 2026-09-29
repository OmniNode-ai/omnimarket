# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19790: typed label replacement against real Postgres, never SQLite.

Apply the node migrations to a unique scratch schema and execute the writer's
actual upsert. Without INTEGRATION_POSTGRES credentials this proof skips.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_delegation_eval_writer import (
    _UPSERT_ITEM,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_eval/migrations"
)
_T0 = datetime(2026, 9, 28, tzinfo=UTC)
_TENANT = UUID("11111111-1111-1111-1111-111111111111")


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if not dsn:
        secret = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        if not secret:
            pytest.skip(
                "INTEGRATION_POSTGRES_DSN/INTEGRATION_POSTGRES_PASSWORD unset -- "
                "skipping the delegation-eval write-path proof"
            )
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn, timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for the write-path proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    """Retarget objects and grant recipients without requiring cluster roles."""
    return (
        statement.replace("public.", f"{schema}.")
        .replace("ON SCHEMA public", f"ON SCHEMA {schema}")
        .replace("TO omninode_runtime", "TO CURRENT_USER")
        .replace("TO app_dashboard", "TO CURRENT_USER")
    )


@pytest.mark.integration
async def test_delegation_eval_replacement_reads_back_typed_columns() -> None:
    conn = await _connect_or_skip()
    schema = f"omn19790_delegation_eval_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await conn.execute(f"CREATE SCHEMA {schema}")
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))
        # FORCE RLS also covers a non-superuser table owner. Bind its tenant
        # in the same transaction as both writes and the read.
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        for index, label in enumerate(("correct", "incorrect")):
            returned = await conn.fetch(
                _scoped(_UPSERT_ITEM, schema),
                _TENANT,
                "call-1:0",
                "call-1",
                0,
                "code",
                "hard",
                "explain this",
                "an explanation",
                "pass",
                "tests",
                label,
                "human",
                "v1",
                json.dumps({"passed": index == 0, "attempts": index + 1}),
                _T0 + timedelta(seconds=index),
            )
            assert len(returned) == 1

        rows = await conn.fetch(
            f"SELECT *, pg_typeof(tenant_id)::text AS tenant_type, "
            "pg_typeof(observed_at)::text AS observed_type, "
            "pg_typeof(computed_facts)::text AS facts_type "
            f"FROM {schema}.delegation_eval_items"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["item_key"] == "call-1:0"
        assert row["rater_role"] == "human"
        assert row["rubric_version"] == "v1"
        assert row["label"] == "incorrect"
        assert row["tenant_type"] == "uuid"
        assert isinstance(row["tenant_id"], UUID)
        assert row["tenant_id"] == _TENANT
        assert row["observed_type"] == "timestamp with time zone"
        assert isinstance(row["observed_at"], datetime)
        assert row["observed_at"].utcoffset() == timedelta(0)
        assert row["observed_at"] == _T0 + timedelta(seconds=1)
        assert row["facts_type"] == "jsonb"
        # asyncpg's default JSONB codec returns JSON text.
        assert json.loads(row["computed_facts"]) == {"passed": False, "attempts": 2}
    finally:
        try:
            # DDL and grants are transactional; rollback removes the scratch
            # schema even when an assertion or statement fails.
            await transaction.rollback()
        finally:
            await conn.close()
