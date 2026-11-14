# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24286: typed experiment arm result rows against real Postgres, never SQLite.

Apply the node migrations to a unique scratch schema and execute the writer's
actual upsert. Without INTEGRATION_POSTGRES credentials this proof skips.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    ModelExperimentArmResult,
)
from omnimarket.nodes.node_projection_experiment_result.handlers.handler_experiment_result_writer import (
    _UPSERT,
)
from omnimarket.nodes.node_projection_experiment_result.handlers.handler_projection_experiment_result import (
    HandlerProjectionExperimentResult,
)
from omnimarket.nodes.node_projection_experiment_result.models import (
    ModelExperimentResultProjectionRequest,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_experiment_result/migrations"
)
_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
_MEASURED_AT = datetime(2026, 11, 13, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if not dsn:
        secret = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        if not secret:
            pytest.skip(
                "INTEGRATION_POSTGRES_DSN/INTEGRATION_POSTGRES_PASSWORD unset -- "
                "skipping the experiment-result write-path proof"
            )
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn, timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the write-path proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    """Retarget objects and grant recipients without requiring cluster roles."""
    return (
        statement.replace("public.", f"{schema}.")
        .replace("ON SCHEMA public", f"ON SCHEMA {schema}")
        .replace("table_schema = 'public'", f"table_schema = '{schema}'")
        .replace("grantee = 'tenant_projection_writer'", "grantee = CURRENT_USER")
        .replace("TO tenant_projection_writer", "TO CURRENT_USER")
        .replace("rolname = 'tenant_projection_writer'", "rolname = CURRENT_USER")
        .replace("'tenant_projection_writer',", "CURRENT_USER,")
        .replace("TO app_dashboard", "TO CURRENT_USER")
    )


def _wilson_bounds(
    correct: int, n: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    """A small independent Wilson 95% interval helper for the test."""
    p = correct / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _upsert_params(row, measured_at: datetime) -> tuple[object, ...]:
    """The 20 typed parameters in the order the writer passes them."""
    return (
        row.tenant_id,
        row.experiment_id,
        row.arm,
        row.model_id,
        row.engine,
        row.n,
        row.correct,
        row.accuracy,
        row.wilson_low,
        row.wilson_high,
        row.baseline_accuracy,
        row.advantage_over_baseline,
        row.clears_baseline,
        row.latency_p50_ms,
        row.latency_p90_ms,
        row.gpu_seconds,
        row.corpus_sha256,
        row.truth_sha256,
        row.report_ref,
        measured_at,
    )


def _build_row(measured_at: datetime) -> object:
    """Build the row through the real fold so the test never hand-types it."""
    wilson_low, wilson_high = _wilson_bounds(48, 60)
    result = ModelExperimentArmResult(
        tenant_id=_TENANT,
        experiment_id="exp-1",
        arm="arm-a",
        model_id="model-x",
        engine="local",
        n=60,
        correct=48,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
        baseline_accuracy=0.7,
        latency_p50_ms=12.5,
        latency_p90_ms=34.0,
        gpu_seconds=90.0,
        corpus_sha256="a" * 64,
        truth_sha256="b" * 64,
        measured_at=measured_at,
    )
    request = ModelExperimentResultProjectionRequest(
        result_id=uuid4(),
        result=result,
        accuracy=0.8,
    )
    return HandlerProjectionExperimentResult().handle(request).row


async def _open_scratch(conn: asyncpg.Connection, schema: str) -> None:
    await conn.execute(f"CREATE SCHEMA {schema}")
    for migration in sorted(_MIGRATIONS.glob("*.sql")):
        await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))


@pytest.mark.integration
async def test_a_row_reads_back_with_typed_columns() -> None:
    conn = await _connect_or_skip()
    schema = f"omn24286_experiment_result_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await _open_scratch(conn, schema)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        row = _build_row(_MEASURED_AT)
        returned = await conn.fetch(
            _scoped(_UPSERT, schema), *_upsert_params(row, _MEASURED_AT)
        )
        assert len(returned) == 1
        assert returned[0]["projection_cursor"] is not None

        read = await conn.fetchrow(
            f"SELECT *, pg_typeof(n)::text AS n_type, "
            f"pg_typeof(correct)::text AS correct_type, "
            f"pg_typeof(clears_baseline)::text AS clears_type, "
            f"pg_typeof(measured_at)::text AS measured_type, "
            f"pg_typeof(accuracy)::text AS accuracy_type "
            f"FROM {schema}.experiment_arm_results"
        )
        assert read is not None
        assert isinstance(read["n"], int)
        assert read["n"] == 60
        assert isinstance(read["correct"], int)
        assert read["correct"] == 48
        assert isinstance(read["clears_baseline"], bool)
        assert read["clears_baseline"] is True
        assert isinstance(read["measured_at"], datetime)
        assert read["measured_at"] == _MEASURED_AT
        assert read["measured_at"].utcoffset() == timedelta(0)
        assert isinstance(read["accuracy"], float)
        assert read["accuracy"] == 0.8
        assert read["n_type"] == "integer"
        assert read["correct_type"] == "integer"
        assert read["clears_type"] == "boolean"
        assert read["measured_type"] == "timestamp with time zone"
        assert read["accuracy_type"] == "double precision"
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_a_redelivery_inserts_nothing() -> None:
    conn = await _connect_or_skip()
    schema = f"omn24286_experiment_result_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await _open_scratch(conn, schema)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        row = _build_row(_MEASURED_AT)
        first = await conn.fetch(
            _scoped(_UPSERT, schema), *_upsert_params(row, _MEASURED_AT)
        )
        assert len(first) == 1
        second = await conn.fetch(
            _scoped(_UPSERT, schema), *_upsert_params(row, _MEASURED_AT)
        )
        assert second == []
        count = await conn.fetchval(
            f"SELECT count(*) FROM {schema}.experiment_arm_results"
        )
        assert count == 1
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_a_later_measured_at_is_a_second_row() -> None:
    conn = await _connect_or_skip()
    schema = f"omn24286_experiment_result_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await _open_scratch(conn, schema)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        row = _build_row(_MEASURED_AT)
        later = _MEASURED_AT + timedelta(days=1)
        assert (
            len(
                await conn.fetch(
                    _scoped(_UPSERT, schema), *_upsert_params(row, _MEASURED_AT)
                )
            )
            == 1
        )
        assert (
            len(await conn.fetch(_scoped(_UPSERT, schema), *_upsert_params(row, later)))
            == 1
        )
        count = await conn.fetchval(
            f"SELECT count(*) FROM {schema}.experiment_arm_results"
        )
        assert count == 2
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_row_level_security_hides_another_tenants_rows() -> None:
    conn = await _connect_or_skip()
    schema = f"omn24286_experiment_result_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await _open_scratch(conn, schema)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        row = _build_row(_MEASURED_AT)
        assert (
            len(
                await conn.fetch(
                    _scoped(_UPSERT, schema), *_upsert_params(row, _MEASURED_AT)
                )
            )
            == 1
        )

        # The exemplar binds the tenant in the same transaction as both the write and the
        # read; FORCE RLS covers a non-superuser table owner. We mirror that: the owner
        # here is the connecting role, so we assert the tenant-filtered count rather than
        # inventing a stronger claim about a bypassing owner.
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, true)", str(_TENANT_B)
        )
        hidden = await conn.fetchval(
            f"SELECT count(*) FROM {schema}.experiment_arm_results"
        )
        assert hidden == 0

        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))
        visible = await conn.fetchval(
            f"SELECT count(*) FROM {schema}.experiment_arm_results"
        )
        assert visible == 1
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_a_check_constraint_refuses_correct_above_n() -> None:
    conn = await _connect_or_skip()
    schema = f"omn24286_experiment_result_{uuid4().hex}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        await _open_scratch(conn, schema)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(_TENANT))

        row = _build_row(_MEASURED_AT)
        params = list(_upsert_params(row, _MEASURED_AT))
        params[5] = 10  # n
        params[6] = 11  # correct, above n
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.fetch(_scoped(_UPSERT, schema), *params)
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()
