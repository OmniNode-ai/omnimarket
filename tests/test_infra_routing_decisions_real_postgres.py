# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One-runtime plan T14: the new writer against real Postgres, beside the old one.

The module being replaced, ``WriterInfraRoutingDecisionsPostgres`` in
omnibase_infra, and this node's writer are each pointed at their own throwaway
schema holding the table as migration 080 declares it. Both are fed the same
replayed deliveries, twice. The rows they leave must be equal, column for column
(the defaults for the columns a decision does not carry included), and a second
replay must leave one row per correlation id.

Real Postgres, never SQLite: the table's uniqueness is a PARTIAL unique index,
and only Postgres decides whether an ``ON CONFLICT`` target matches it. The
harness SKIPS without a reachable database; CI provisions one for
integration-marked tests and fails a missing-service skip (OMN-14172).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest
from omnibase_infra.services.observability.infra_routing_decisions.model_routing_decision_ingest import (
    ModelInfraRoutingDecisionIngest,
)
from omnibase_infra.services.observability.infra_routing_decisions.writer_postgres import (
    WriterInfraRoutingDecisionsPostgres,
)

from omnimarket.nodes.node_projection_infra_routing_decisions.handlers import (
    InfraRoutingDecisionsProjectionWriter,
)
from omnimarket.nodes.node_projection_infra_routing_decisions.handlers import (
    handler_infra_routing_decisions_writer as writer_module,
)
from tests.test_golden_chain_projection_infra_routing_decisions import (
    _CORR_A,
    _CORR_B,
    _CORR_ENVELOPE_ONLY,
    _REPLAY,
)

_SCHEMA_PREFIX = "omn21935_t14"

#: The table as omnibase_infra migration 080 declares it. Restated here because
#: that migration is not packaged with omnibase_infra; the node ships no DDL.
_TABLE_DDL = """
CREATE TABLE {schema}.infra_routing_decisions (
    id                      UUID        NOT NULL DEFAULT gen_random_uuid(),
    correlation_id          UUID,
    selected_provider       TEXT        NOT NULL,
    selected_tier           TEXT        NOT NULL,
    selected_model          TEXT        NOT NULL DEFAULT '',
    selection_mode          TEXT        NOT NULL DEFAULT 'round_robin',
    fallback_indicator      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_fallback             BOOLEAN     NOT NULL DEFAULT FALSE,
    reason                  TEXT        NOT NULL DEFAULT '',
    candidates_evaluated    INTEGER     NOT NULL DEFAULT 0,
    candidate_providers     JSONB,
    task_type               TEXT,
    session_id              TEXT,
    latency_ms              NUMERIC(12, 3),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_infra_routing_decisions PRIMARY KEY (id)
);
CREATE UNIQUE INDEX uq_infra_routing_decisions_correlation
    ON {schema}.infra_routing_decisions (correlation_id)
    WHERE correlation_id IS NOT NULL;
"""

_WRITE_TIME_COLUMNS = frozenset({"id", "created_at", "projected_at"})


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the routing "
            "decisions write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Schema:
    """One disposable schema carrying the table."""

    def __init__(self, dsn: str, name: str) -> None:
        self.dsn = dsn
        self.name = name

    def scoped(self, statement: str) -> str:
        return statement.replace("public.", f"{self.name}.")

    async def _reset(self) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
            await conn.execute(f"CREATE SCHEMA {self.name}")
            await conn.execute(_TABLE_DDL.format(schema=self.name))
        finally:
            await conn.close()

    async def _drop(self) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
        finally:
            await conn.close()

    async def _fetch(self) -> list[dict[str, Any]]:
        conn = await asyncpg.connect(self.dsn)
        try:
            rows = await conn.fetch(
                f"SELECT * FROM {self.name}.infra_routing_decisions "
                "ORDER BY correlation_id NULLS LAST, selected_model, reason"
            )
        finally:
            await conn.close()
        return [
            {k: v for k, v in dict(row).items() if k not in _WRITE_TIME_COLUMNS}
            for row in rows
        ]

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = _run(self._fetch())
        return rows

    def new_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> InfraRoutingDecisionsProjectionWriter:
        """The real writer, its statement retargeted at this schema."""
        monkeypatch.setattr(
            writer_module, "_UPSERT", self.scoped(writer_module._UPSERT)
        )
        instance = InfraRoutingDecisionsProjectionWriter()
        instance.bind_projection_database_url(self.dsn)
        return instance

    def replay_through_old_writer(self, deliveries: list[dict[str, object]]) -> None:
        events = [
            event
            for raw in deliveries
            if (event := ModelInfraRoutingDecisionIngest.from_envelope(raw))
        ]

        async def go() -> None:
            pool = await asyncpg.create_pool(
                dsn=self.dsn, server_settings={"search_path": self.name}
            )
            try:
                writer = WriterInfraRoutingDecisionsPostgres(pool=pool)
                await writer.write_routing_decisions(events)
            finally:
                await pool.close()

        _run(go())


@pytest.fixture
def schema_factory() -> Iterator[Any]:
    dsn = _dsn_or_skip()
    made: list[_Schema] = []

    def _make(suffix: str) -> _Schema:
        schema = _Schema(dsn, f"{_SCHEMA_PREFIX}_{os.getpid()}_{suffix}")
        _run(schema._reset())
        made.append(schema)
        return schema

    yield _make
    for schema in made:
        _run(schema._drop())


def _keyed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["correlation_id"] is not None]


@pytest.mark.integration
def test_the_new_writer_leaves_the_rows_the_old_writer_leaves(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = schema_factory("old")
    new = schema_factory("new")
    writer = new.new_writer(monkeypatch)

    for _ in range(2):
        old.replay_through_old_writer(_REPLAY)
        for raw in _REPLAY:
            assert writer.handle(dict(raw))["rows_upserted"] == 1

    assert new.rows() == old.rows()
    keyed = _keyed(new.rows())
    assert {str(row["correlation_id"]) for row in keyed} == {
        _CORR_A,
        _CORR_B,
        _CORR_ENVELOPE_ONLY,
    }
    assert len(keyed) == 3, "one row per key after the second replay"
    # Positive control: the unkeyed deliveries are really in the comparison, and
    # they accumulate on every replay in the old module too (no key to UPSERT on).
    assert len(new.rows()) > len(keyed)


@pytest.mark.integration
def test_a_second_replay_changes_nothing_the_first_left(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory("replay")
    writer = schema.new_writer(monkeypatch)
    for raw in _REPLAY:
        writer.handle(dict(raw))
    first = _keyed(schema.rows())
    for raw in _REPLAY:
        writer.handle(dict(raw))
    assert _keyed(schema.rows()) == first


@pytest.mark.integration
def test_the_conflict_target_must_repeat_the_partial_index_predicate(
    schema_factory: Any,
) -> None:
    """Positive control for the statement text: a bare target is refused."""
    schema = schema_factory("bare")
    bare = schema.scoped(
        writer_module._UPSERT.replace(
            "ON CONFLICT (correlation_id) WHERE correlation_id IS NOT NULL",
            "ON CONFLICT (correlation_id)",
        )
    )

    async def go() -> None:
        conn = await asyncpg.connect(schema.dsn)
        try:
            with pytest.raises(asyncpg.InvalidColumnReferenceError):
                await conn.fetch(bare, None, "p", "t", "m", "r", None)
        finally:
            await conn.close()

    _run(go())
