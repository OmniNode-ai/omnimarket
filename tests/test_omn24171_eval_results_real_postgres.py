# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171: the eval-results projection's real-Postgres write path (INTEGRATION_POSTGRES).

The unit tests run the fold and the writer over an in-memory store. This file runs the same golden
chain through the real ``EvalResultsStore`` SQL against Postgres, in a disposable schema, because
only a real connection enforces column types, the JSONB casts, the row-wise ordering guard and the
migrations themselves. It skips, never errors, without a reachable database.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.enums.enum_eval_step_verdict import EnumEvalStepVerdictSource
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_store import (
    UPSERT_ITEM,
    item_params,
)
from omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer import (
    EvalResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    ModelEvalItemRow,
)
from omnimarket.projection.runner import MessageMeta
from tests.helpers.isolated_schema_sql import for_isolated_schema
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    at,
    close_event,
    code_event,
    judged_event,
    overlay,
    recorded_judged_render_result,
    try_row,
)
from tests.nodes.node_projection_eval_results.test_item_storage import _record
from tests.test_golden_chain_projection_eval_results import _fixture

_MIGRATIONS = sorted(
    (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_projection_eval_results/migrations"
    ).glob("*.sql")
)
_ROLE_NAMES = ("app_dashboard", "tenant_projection_writer")


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


class _SchemaAdapter:
    """The adapter surface the store uses, with every statement run in the disposable schema."""

    def __init__(self, inner: AsyncpgAdapter) -> None:
        self._inner = inner

    async def execute(self, query: str, *params: Any, tenant: str | None = None) -> Any:
        return await self._inner.execute(
            for_isolated_schema(query), *params, tenant=tenant
        )

    async def execute_in_transaction(
        self, queries: list[tuple[str, tuple[Any, ...]]], *, tenant: str | None = None
    ) -> None:
        await self._inner.execute_in_transaction(
            [(for_isolated_schema(q), p) for q, p in queries], tenant=tenant
        )

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


@asynccontextmanager
async def _migrated_schema() -> AsyncIterator[
    tuple[_SchemaAdapter, asyncpg.Connection]
]:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping OMN-24171 real-Postgres leg"
        )
    try:
        admin = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for OMN-24171: {exc}")
    schema = f"omn24171_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        for role in _ROLE_NAMES:
            await admin.execute(
                f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        for migration in _MIGRATIONS:
            await admin.execute(
                for_isolated_schema(migration.read_text(encoding="utf-8"))
            )
        options = quote(f"-c search_path={schema},public")
        pool = await asyncpg.create_pool(
            f"{_dsn()}?options={options}", min_size=1, max_size=3
        )
        inner = AsyncpgAdapter(dsn=_dsn())
        cast(Any, inner)._pool = pool
        yield _SchemaAdapter(inner), admin
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute("SET search_path TO public")
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _counts(db: _SchemaAdapter) -> dict[str, int]:
    out = {}
    for table in (
        "eval_item_scores",
        "eval_item_traces",
        "eval_run_closes",
        "eval_run_rates",
    ):
        (row,) = await db.execute(
            f"SELECT count(*) AS n FROM public.{table}", tenant=TENANT
        )
        out[table] = row["n"]
    return out


async def _digest(db: _SchemaAdapter) -> list[str]:
    rows = await db.execute(
        "SELECT item_key, run_id, try_ordinal, passed, scored_at, outcome_digest, field_scores::text AS fs "
        "FROM public.eval_item_scores ORDER BY item_key",
        tenant=TENANT,
    )
    rates = await db.execute(
        "SELECT run_id, model_id, size_band, n, passed, run_state, void_reason, scorer_versions::text AS sv "
        "FROM public.eval_run_rates ORDER BY run_id, model_id, size_band, scorer_version",
        tenant=TENANT,
    )
    return [json.dumps(dict(r), default=str, sort_keys=True) for r in (*rows, *rates)]


@pytest.mark.integration
async def test_the_golden_chain_fills_the_tables_and_replay_changes_nothing() -> None:
    async with _migrated_schema() as (db, _admin):
        writer = EvalResultsProjectionWriter(overlay=overlay(), clock=lambda: at(100))
        cast(Any, writer)._db = db
        published: list[dict[str, Any]] = []

        async def publish(_topic: str, value: bytes) -> None:
            published.append(json.loads(value))

        writer._publish_fn = publish

        async def feed(events: list[Any]) -> None:
            for topic, payload, when in events:
                await writer._project(
                    topic,
                    {
                        **payload,
                        "_envelope_timestamp": when.isoformat(),
                        "_tenant_id": TENANT,
                    },
                    MessageMeta(partition=0, offset=0, fallback_id="", topic=topic),
                )

        events = _fixture()
        await feed(events)
        first = await _counts(db)
        assert first == {
            "eval_item_scores": 13,
            "eval_item_traces": 9,
            "eval_run_closes": 3,
            "eval_run_rates": 9,
        }
        held = await _digest(db)
        await feed(events)
        assert await _digest(db) == held
        await feed(list(reversed(events)))
        assert await _digest(db) == held
        runs = {
            r["run_id"][-2:]: r["state"] for r in published[-1]["models"][0]["runs"]
        }
        assert runs == {"a1": "closed", "b2": "void"}


@pytest.mark.integration
async def test_the_sql_ordering_guard_leaves_the_same_row_in_either_order() -> None:
    async with _migrated_schema() as (db, _admin):
        winners = []
        for name in ("ab", "ba"):
            run = f"tie-{name}"
            a = ModelEvalItemRow.of(
                _record(
                    run_id=run,
                    step_verdict_source=EnumEvalStepVerdictSource.DETERMINISTIC,
                )
            )
            b = ModelEvalItemRow.of(
                _record(
                    run_id=run,
                    step_verdict_source=EnumEvalStepVerdictSource.REVIEWER_MODEL,
                )
            )
            for row in (a, b) if name == "ab" else (b, a):
                await db.execute(UPSERT_ITEM, *item_params(row), tenant=TENANT)
            (stored,) = await db.execute(
                "SELECT outcome_digest FROM public.eval_item_scores WHERE run_id = $1",
                run,
                tenant=TENANT,
            )
            winners.append(
                stored["outcome_digest"] == max(a.outcome_digest, b.outcome_digest)
            )
        assert winners == [True, True]


@pytest.mark.integration
async def test_a_late_older_close_changes_no_stored_run_or_rate_row_in_either_order() -> (
    None
):
    """A close older than the stored one is rejected by the SQL guard, and now its recount is too."""
    run = "00000000-0000-0000-0000-0000000000e1"
    sequences = (("t1", MODEL, "S"), ("t2", MODEL, "S"))
    newer = close_event(run, sequences, closed_at=at(30))
    older = close_event(
        run,
        sequences[:1],
        void_reason="registration_mismatch",
        series="other",
        closed_at=at(10),
    )
    stored: list[list[str]] = []
    for closes in ((newer,), (newer, older), (older, newer)):
        async with _migrated_schema() as (db, _admin):
            writer = EvalResultsProjectionWriter(
                overlay=overlay(), clock=lambda: at(100)
            )
            cast(Any, writer)._db = db

            async def publish(_topic: str, _value: bytes) -> None:
                return None

            writer._publish_fn = publish
            deliveries = [
                (
                    "code",
                    code_event(
                        run,
                        try_row(run, "t1", verdict="pass"),
                        try_row(run, "t2", verdict="pass"),
                    ),
                ),
                *(("close", close) for close in closes),
            ]
            for minute, (kind, payload) in enumerate(deliveries, start=1):
                topic = TOPIC_CODE_TRY if kind == "code" else TOPIC_RUN_CLOSED
                await writer._project(
                    topic,
                    {
                        **payload,
                        "_envelope_timestamp": at(minute).isoformat(),
                        "_tenant_id": TENANT,
                    },
                    MessageMeta(partition=0, offset=0, fallback_id="", topic=topic),
                )
            closes_held = await db.execute(
                "SELECT run_id, closed_at, series, void_reason "
                "FROM public.eval_run_closes ORDER BY run_id",
                tenant=TENANT,
            )
            stored.append(
                [json.dumps(dict(r), default=str, sort_keys=True) for r in closes_held]
                + await _digest(db)
            )
    assert stored[0] == stored[1] == stored[2]
    assert any('"run_state": "closed"' in row for row in stored[0])


@pytest.mark.integration
async def test_a_judged_delivery_with_no_judge_run_id_writes_no_row_and_reaches_the_dlq() -> (
    None
):
    """The recorded dev broker event (no judge_run_id) is refused and counted; a valid one still lands."""
    call = "00000000-0000-0000-0000-00000000c001"
    async with _migrated_schema() as (db, _admin):
        writer = EvalResultsProjectionWriter(overlay=overlay(), clock=lambda: at(100))
        cast(Any, writer)._db = db
        published: list[tuple[str, dict[str, Any]]] = []

        async def publish(topic: str, value: bytes) -> None:
            published.append((topic, json.loads(value)))

        writer._publish_fn = publish

        async def deliver_judged(payload: dict[str, Any]) -> dict[str, Any]:
            return await writer._project(
                TOPIC_JUDGED,
                {
                    **payload,
                    "_envelope_timestamp": at(2).isoformat(),
                    "_tenant_id": TENANT,
                },
                MessageMeta(partition=0, offset=0, fallback_id="", topic=TOPIC_JUDGED),
            )

        refused = await deliver_judged(recorded_judged_render_result())
        assert refused["dlq_routed"] == 1
        assert [r["reason"] for r in refused["refused"]] == ["invalid_event"]
        assert (await _counts(db))["eval_item_scores"] == 0
        assert [topic for topic, _ in published] == [
            "onex.dlq.omnimarket.projection-eval-results-malformed.v1"
        ]

        landed = await deliver_judged(judged_event(call))
        assert landed["dlq_routed"] == 0
        assert (await _counts(db))["eval_item_scores"] == 1
