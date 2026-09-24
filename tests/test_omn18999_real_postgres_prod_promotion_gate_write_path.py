# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres write-path gate for the promotion-gate projection (OMN-18999).

WHY THIS EXISTS BESIDE THE GOLDEN CHAIN
    A database double accepts an ISO string bound to a TIMESTAMPTZ, and a
    string bound to a UUID, exactly as readily as a real ``datetime`` or a
    real ``UUID``. That is the one question a double cannot answer, and it is
    the gap that took a crash-looping runtime to production with every mock
    green (OMN-15905). This projection binds a UUID primary key, two
    TIMESTAMPTZ columns and a BOOLEAN, so it is squarely in that class.

    The BOOLEAN matters for its own reason here: ``allowed`` is what keeps an
    allowed promotion distinguishable from a gate that never ran, and a double
    would have accepted the string ``"false"`` -- which Postgres also accepts,
    and which is truthy in Python.

    Real Postgres, never SQLite: the upsert's conflict target, the BIGSERIAL
    cursor's own sequence and the enum-as-TEXT columns all behave differently
    on anything else, so a substitute would prove something about the
    substitute.

The migration is applied into a throwaway ``uuid4``-named schema so concurrent
runs never collide, and the test SKIPS rather than errors when no server is
reachable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGateOutcome,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    evaluate_gate,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.handlers import (
    handler_prod_promotion_gate_writer as writer_module,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_prod_promotion_gate_writer import (
    ProdPromotionGateProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

# Both forms deliberately: the module mark is what pytest selects on, and the
# per-test decorator is what scripts/ci/check_projection_write_path_db_gate.py
# reads to confirm a write-path change brought a real-Postgres test with it.
pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_prod_promotion_gate/migrations"
    / "0000_create_prod_promotion_gate_decisions.sql"
)

CORRELATION = UUID("6a2f1d3e-9b47-4c58-8e21-0d5f7a3b9c14")
EVALUATED_AT = datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
BATCH = "promo-batch-2026-09-20"
GRANT_ID = "grant-omn-18999-01"
ROLLBACK = "sha256:" + "3" * 64
TABLE_NAME = "prod_promotion_gate_decisions"


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
            "POSTGRES_PASSWORD not set -- skipping the OMN-18999 real-Postgres "
            "promotion-gate write-path gate"
        )
        raise AssertionError("unreachable: pytest.skip always raises")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18999 write-path gate: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc


class _ConnectionDb:
    """The two methods the writer calls, bound to one disposable connection.

    A pooled adapter would not keep the throwaway schema isolated, and the
    writer must not assume it owns the adapter it was handed -- the
    connect/close bracket it opens per message is honoured here and does
    nothing, which is the point.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        rows = await self._connection.fetch(sql, *args)
        return [dict(row) for row in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._connection.fetchval(sql, *args)

    async def connect(self) -> None:
        """No-op: already bound to one live connection."""

    async def close(self) -> None:
        """No-op, for the same reason as :meth:`connect`."""


@asynccontextmanager
async def _migrated_writer() -> AsyncIterator[
    tuple[ProdPromotionGateProjectionWriter, asyncpg.Connection, str]
]:
    """A throwaway schema carrying the real migration, wired to the real writer."""
    connection = await _connect_or_skip()
    schema = f"omn18999_{uuid4().hex[:12]}"
    original_table = writer_module.TABLE
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        ddl = MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        await connection.execute(ddl)

        writer = ProdPromotionGateProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]

        # Point the writer's SQL at the disposable schema. The statement is
        # formatted from one TABLE constant, so rebinding both rewrites every
        # occurrence consistently rather than per call site.
        writer_module.TABLE = f"{schema}.{TABLE_NAME}"
        writer_module._UPSERT = original_upsert.replace(
            f"omninode_internal.{TABLE_NAME}", f"{schema}.{TABLE_NAME}"
        )
        yield writer, connection, schema
    finally:
        writer_module.TABLE = original_table
        writer_module._UPSERT = original_upsert
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _command(**overrides: Any) -> ModelProdPromotionGateCommand:
    """A prod gate command that would be allowed, with named fields replaced."""
    fields: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "runtime_lane": EnumRuntimeLane.PROD,
        "requested_image_digest": DIGEST,
        "promotion_batch_id": BATCH,
        "readiness_projection": ModelReadinessProjectionFact(
            readiness_state="READY", image_digest=DIGEST, promotion_batch_id=BATCH
        ),
        "occ_gate_state": EnumOccGateState.MERGED,
        "rollback_target": ROLLBACK,
        "requested_by": "jonah",
        "promotion_grant": ModelProdPromotionGrant(
            grant_id=GRANT_ID,
            approved_lane=EnumRuntimeLane.PROD,
            approved_image_digest=DIGEST,
            approved_promotion_batch_id=BATCH,
            approved_by="jonah",
            created_at=EVALUATED_AT - timedelta(hours=1),
            expires_at=EVALUATED_AT + timedelta(hours=1),
        ),
        "evaluated_at": EVALUATED_AT,
    }
    fields.update(overrides)
    return ModelProdPromotionGateCommand(**fields)


def _decision(**overrides: Any) -> dict[str, Any]:
    """Run the REAL gate and serialise the decision as the bus carries it."""
    payload: dict[str, Any] = evaluate_gate(_command(**overrides)).model_dump(
        mode="json"
    )
    return payload


def _meta() -> MessageMeta:
    return MessageMeta(
        partition=0,
        offset=7,
        fallback_id=str(uuid4()),
        topic="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: a MessageMeta coordinate, not an event_type assignment
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_refusal_lands_with_typed_column_values() -> None:
    """The write reaches real Postgres and reads back with real types.

    ``evaluated_at`` comes back a ``datetime``, ``correlation_id`` a ``UUID``
    and ``allowed`` a real ``bool``. A double would have accepted the ISO
    string and the string ``"false"`` the JSON payload carries, and this
    assertion is the only place that distinction is made.
    """
    async with _migrated_writer() as (writer, connection, schema):
        written = await writer._project_decision(
            _decision(requested_image_digest=OTHER_DIGEST), _meta()
        )
        assert written is not None

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            CORRELATION,
        )
        assert stored is not None
        assert isinstance(stored["correlation_id"], UUID)
        assert stored["correlation_id"] == CORRELATION
        assert isinstance(stored["evaluated_at"], datetime)
        assert stored["evaluated_at"] == EVALUATED_AT
        assert isinstance(stored["projected_at"], datetime)
        assert stored["allowed"] is False
        assert stored["outcome"] == EnumProdGateOutcome.DIGEST_MISMATCH.value
        assert stored["grant_id"] == GRANT_ID
        assert stored["requested_image_digest"] == OTHER_DIGEST
        assert stored["resolved_image_digest"] is None
        assert stored["projection_cursor"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_allowed_promotion_is_a_row_too_and_a_boolean_apart() -> None:
    """AC3 against the real column: allow and never-ran are distinguishable."""
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_decision(_decision(), _meta())

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            CORRELATION,
        )
        assert stored is not None
        assert stored["allowed"] is True
        assert stored["outcome"] == EnumProdGateOutcome.ALLOWED.value
        assert stored["resolved_image_digest"] == DIGEST

        # The negative half: a correlation nobody evaluated has no row, which
        # is what makes the boolean above meaningful rather than decorative.
        absent = await connection.fetchrow(
            f"SELECT * FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            uuid4(),
        )
        assert absent is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_redelivery_converges_on_one_row() -> None:
    """The declared dedupe key is enforced by the real conflict target.

    A writer whose ON CONFLICT named the wrong column would duplicate every
    redelivery, and no double would notice: a double has no unique index.
    """
    async with _migrated_writer() as (writer, connection, schema):
        payload = _decision(requested_image_digest=OTHER_DIGEST)
        await writer._project_decision(dict(payload), _meta())
        await writer._project_decision(dict(payload), _meta())

        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            CORRELATION,
        )
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_corrected_re_evaluation_replaces_rather_than_accumulates() -> None:
    """The same run re-evaluated stores the newer answer, not both."""
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_decision(
            _decision(requested_image_digest=OTHER_DIGEST), _meta()
        )
        await writer._project_decision(_decision(), _meta())

        rows = await connection.fetch(
            f"SELECT outcome, allowed FROM {schema}.{TABLE_NAME} "
            "WHERE correlation_id = $1",
            CORRELATION,
        )
        assert len(rows) == 1
        assert rows[0]["outcome"] == EnumProdGateOutcome.ALLOWED.value
        assert rows[0]["allowed"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_non_prod_decision_stores_a_null_evaluation_time() -> None:
    """NULL is a real value in this column, and the schema must allow it.

    A non-prod lane runs no grant resolver, so there is no deterministic
    evaluation clock to record. A NOT NULL here would have failed every dev
    deploy at write time -- on the lane that runs most often.
    """
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_decision(
            _decision(
                runtime_lane=EnumRuntimeLane.DEV,
                promotion_grant=None,
                evaluated_at=None,
            ),
            _meta(),
        )
        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            CORRELATION,
        )
        assert stored is not None
        assert stored["evaluated_at"] is None
        assert stored["grant_id"] is None
        assert stored["allowed"] is True
        assert stored["outcome"] == EnumProdGateOutcome.ALLOWED_LANE_NOT_GATED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_live_dead_lettered_decision_lands_and_the_fold_agrees() -> None:
    """OMN-19240: the captured live decision writes, and both halves agree.

    The payload is the real one the ``.201`` dev lane dead-lettered about
    22,900 times. The writer's row and the row the fold produces through the
    runtime adapter's own invoker must describe the same decision -- the two
    dispatchers run side by side on every message, and a fold that raised was
    what turned a successful write into a dead letter.
    """
    from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method

    from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_projection_prod_promotion_gate import (
        HandlerProjectionProdPromotionGate,
    )
    from tests.test_omn19240_adapter_path_prod_promotion_gate import _REAL_DECISION

    live_correlation = UUID(_REAL_DECISION["correlation_id"])
    async with _migrated_writer() as (writer, connection, schema):
        written = await writer._project_decision(dict(_REAL_DECISION), _meta())
        assert written is not None

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.{TABLE_NAME} WHERE correlation_id = $1",
            live_correlation,
        )
        assert stored is not None
        assert stored["allowed"] is True
        assert stored["outcome"] == EnumProdGateOutcome.ALLOWED_LANE_NOT_GATED.value
        assert stored["runtime_lane"] == "dev"
        assert stored["evaluated_at"] is None

    folded = _invoke_handle_method(
        HandlerProjectionProdPromotionGate().handle, dict(_REAL_DECISION)
    )
    row = folded.row  # type: ignore[attr-defined]
    assert row.correlation_id == stored["correlation_id"]
    assert row.outcome == stored["outcome"]
    assert row.allowed == stored["allowed"]
    assert row.rollback_target == stored["rollback_target"]
    assert row.runtime_lane == stored["runtime_lane"]
