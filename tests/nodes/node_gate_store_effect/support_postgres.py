# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Support for the tests that run on the integration Postgres: the fixture, the connection, the factory.

The suite runs against a real Postgres: the integration database of ``conftest`` (a runner
without one skips the integration tests under the repository's own silent-skip guard). The
relations come only from the node's two migration files, applied here as the migration runner
applies them (``scripts/run-projection-migrations.py``, shown on the lab in the pull request);
no table is created by hand. The refusal tests need no database.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
    ModelGateStorePostgres,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_postgres import (
    PostgresGateStore,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.nodes.node_gate_store_effect import contract_suite as suite

DSN_ENV = "GATE_STORE_TEST_DSN"
POSTGRES_BLOCK = ModelGateStorePostgres(
    dsn_env=DSN_ENV,
    schema_name="omninode_internal",
    connect_timeout_seconds=5,
    lock_timeout_ms=5000,
)


def with_postgres(base: ModelGateStorePolicy) -> ModelGateStorePolicy:
    return base.model_copy(
        update={"backend": EnumGateStoreBackend.POSTGRES, "postgres": POSTGRES_BLOCK}
    )


MIGRATIONS = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_gate_store_effect/migrations"
)


@pytest.fixture(autouse=True)
def database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """For an integration test: the integration DSN, and the node's migrations applied to it."""
    if request.node.get_closest_marker("integration") is None:
        return
    dsn = request.getfixturevalue("integration_postgres_dsn")
    monkeypatch.setenv(DSN_ENV, dsn)
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
            cur.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles "
                "WHERE rolname = 'omninode_runtime') "
                "THEN CREATE ROLE omninode_runtime NOLOGIN; END IF; END $$"
            )
            for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                cur.execute(sql_file.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def connect() -> Any:
    import psycopg2

    return psycopg2.connect(os.environ[DSN_ENV], connect_timeout=5)


def _reset() -> None:
    """A clean store for one check: the relations are the migration's, only the rows go."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            for table in (
                "gate_store_reservations",
                "gate_store_safety",
                "gate_store_steps",
            ):
                cur.execute(f"DELETE FROM omninode_internal.{table}")
            cur.execute(
                "UPDATE omninode_internal.gate_store_state "
                "SET epoch = 0, holder = NULL, order_position = 0"
            )
        conn.commit()
    finally:
        conn.close()


def postgres_factory(
    store_type: type[PostgresGateStore] = PostgresGateStore,
) -> suite.Factory:
    def factory(
        policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
    ) -> ProtocolGateStore:
        _reset()
        return store_type(with_postgres(policy), connect, hook)

    return factory
