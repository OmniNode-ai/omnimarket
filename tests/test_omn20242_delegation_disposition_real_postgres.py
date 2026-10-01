# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20242: actual upsert ordering and tenant-scoped usage query on PostgreSQL.

All DDL and data are rolled back in a scratch schema. Credentials are the
same integration environment seam as the delegation-eval template.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.events.delegation_disposition import ModelDelegationDispositionRecorded
from omnimarket.nodes.node_projection_delegation_disposition.handlers.handler_delegation_disposition_writer import (
    _UPSERT_DISPOSITION,
)
from omnimarket.nodes.node_projection_delegation_disposition.queries import (
    DISPOSITION_USAGE_QUERY,
)

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_OTHER = UUID("33333333-3333-3333-3333-333333333333")
_T0 = datetime(2026, 9, 30, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if not dsn:
        secret = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        if not secret:
            pytest.skip(
                "INTEGRATION_POSTGRES_DSN/INTEGRATION_POSTGRES_PASSWORD unset -- "
                "skipping the delegation-disposition PostgreSQL proof"
            )
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn, timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for the disposition proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    return (
        statement.replace("public.", f"{schema}.")
        .replace("ON SCHEMA public", f"ON SCHEMA {schema}")
        .replace("TO tenant_projection_writer", "TO CURRENT_USER")
        .replace("rolname = 'tenant_projection_writer'", "rolname = CURRENT_USER")
        .replace("TO app_dashboard", "TO CURRENT_USER")
    )


@pytest.mark.integration
async def test_disposition_ordering_and_usage_query_on_real_postgres() -> None:
    conn = await _connect_or_skip()
    schema = f"omn20242_dispositions_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET LOCAL search_path TO {schema}, public")
        # Only the owning node's migrations needed by the query. 0048 carries
        # BEGIN/COMMIT, removed here to preserve the scratch transaction.
        for name in (
            "0007_delegation_events.sql",
            "0022_delegation_events_tenant_id.sql",
            "0048_delegation_events_caller_lane.sql",
        ):
            statement = (
                _NODES / "node_projection_delegation/migrations" / name
            ).read_text()
            await conn.execute(
                _scoped(statement.replace("BEGIN;", "").replace("COMMIT;", ""), schema)
            )
        for migration in sorted(
            (_NODES / "node_projection_delegation_disposition/migrations").glob("*.sql")
        ):
            await conn.execute(_scoped(migration.read_text(), schema))
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        correlation_ids = [uuid4() for _ in range(5)]
        for correlation in correlation_ids:
            await conn.execute(
                f"INSERT INTO {schema}.delegation_events "
                "(tenant_id, correlation_id, model_name, task_type, caller_lane) "
                "VALUES ($1, $2, 'glm', 'code_review', 'lane-1')",
                str(_TENANT),
                str(correlation),
            )
        # Slug keys must not be cast to UUID by the join.
        await conn.execute(
            f"INSERT INTO {schema}.delegation_events "
            "(tenant_id, correlation_id, model_name, task_type, caller_lane) "
            "VALUES ('omninode', 'legacy-call', 'legacy-model', 'planning', NULL)"
        )

        async def write(event: ModelDelegationDispositionRecorded) -> int:
            row = event.model_dump()
            returned = await conn.fetch(
                _scoped(_UPSERT_DISPOSITION, schema), *row.values()
            )
            return len(returned)

        for index, (disposition, reason) in enumerate(
            (
                ("accepted_as_is", "correct_as_is"),
                ("edited", "minor_fix"),
                ("rejected", "wrong_answer"),
                ("ignored", "not_needed"),
            )
        ):
            newer = ModelDelegationDispositionRecorded.build(
                tenant_id=_TENANT,
                delegation_correlation_id=correlation_ids[index],
                disposition=disposition,
                reason_code=reason,
                caller_lane="lane-1",
                engine="glm",
                artifact_kind="commit",
                artifact_ref="abcdef0",
                edit_ratio=0.2 if disposition == "edited" else None,
                ticket_id="OMN-20242",
                answer_sha256="a" * 64,
                recorded_at=_T0 + timedelta(seconds=1),
            )
            first_write = await write(newer)
            repeat_write = await write(newer)
            assert first_write == 1
            assert repeat_write == 0
            older_data = newer.model_dump(exclude={"disposition_id"})
            older_data["recorded_at"] = _T0
            older_write = await write(
                ModelDelegationDispositionRecorded.build(**older_data)
            )
            assert older_write == 0
            row = await conn.fetchrow(
                f"SELECT *, pg_typeof(tenant_id)::text AS tenant_type "
                f"FROM {schema}.delegation_dispositions "
                "WHERE tenant_id = $1 AND delegation_correlation_id = $2",
                _TENANT,
                correlation_ids[index],
            )
            assert row is not None
            assert row["tenant_type"] == "uuid"
            assert row["recorded_at"] == newer.recorded_at
            assert row["disposition_id"] == newer.disposition_id

        # Same timestamp, UUID ordering: exercise both SQL conflict arms.
        base = newer.model_dump(exclude={"disposition_id"})
        tied = [
            ModelDelegationDispositionRecorded.build(**(base | {"caller_lane": lane}))
            for lane in ("lane-2", "lane-3")
        ]
        low, high = sorted(tied, key=lambda event: event.disposition_id)
        await write(low)
        await write(high)
        low_rewrite = await write(low)
        assert low_rewrite == 0
        stored_id = await conn.fetchval(
            f"SELECT disposition_id FROM {schema}.delegation_dispositions "
            "WHERE tenant_id = $1 AND delegation_correlation_id = $2",
            _TENANT,
            high.delegation_correlation_id,
        )
        assert stored_id == max(newer.disposition_id, high.disposition_id)

        # A disposition without a delegation_events row is still stored, but
        # cannot inflate the usage query's delegation denominator.
        orphan_write = await write(
            ModelDelegationDispositionRecorded.build(
                **(base | {"delegation_correlation_id": uuid4()})
            )
        )
        assert orphan_write == 1
        summaries = await conn.fetch(DISPOSITION_USAGE_QUERY, str(_TENANT))
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["delegations_total"] == 5
        for column in (
            "accepted_as_is_n",
            "edited_n",
            "rejected_n",
            "ignored_n",
            "undisposed_n",
        ):
            assert summary[column] == 1
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_usage_query_joins_a_uuid_tenant_column() -> None:
    """After node_projection_delegation 0031 the events tenant_id is UUID (the
    dev lane's live shape); the query's join must be type-correct there too."""
    conn = await _connect_or_skip()
    schema = f"omn20242_uuid_tenant_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET LOCAL search_path TO {schema}, public")
        await conn.execute(
            f"CREATE TABLE {schema}.delegation_events (tenant_id UUID, "
            "correlation_id TEXT, model_name TEXT, task_type TEXT, caller_lane TEXT)"
        )
        for migration in sorted(
            (_NODES / "node_projection_delegation_disposition/migrations").glob("*.sql")
        ):
            await conn.execute(_scoped(migration.read_text(), schema))
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))
        correlation = uuid4()
        # The same grouping keys in another tenant must not enter the total.
        await conn.execute(
            f"INSERT INTO {schema}.delegation_events VALUES ($1, $2, 'glm', 'review', 'lane-1')",
            _OTHER,
            str(correlation),
        )
        await conn.execute(
            f"INSERT INTO {schema}.delegation_events VALUES ($1, $2, 'glm', 'review', 'lane-1')",
            _TENANT,
            str(correlation),
        )
        event = ModelDelegationDispositionRecorded.build(
            tenant_id=_TENANT,
            delegation_correlation_id=correlation,
            disposition="rejected",
            reason_code="wrong_answer",
            caller_lane="lane-1",
            artifact_kind="none",
            recorded_at=_T0,
        )
        await conn.fetch(
            _scoped(_UPSERT_DISPOSITION, schema), *event.model_dump().values()
        )
        summaries = await conn.fetch(DISPOSITION_USAGE_QUERY, str(_TENANT))
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["delegations_total"] == 1
        assert summary["rejected_n"] == 1
        assert summary["undisposed_n"] == 0
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()
