# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23002: the fill's conditional state rows against the real ``floor_fill_state`` table shape.

The table is omnibase_infra's migration 113 (key ``scope``, sequence column ``version``). The race and row tests
run where CI or the lab supplies ``INTEGRATION_POSTGRES_*``; each makes its own schema, so the database's own
tables are never touched, and skips otherwise (the precedent of the other real-Postgres tests of this repository).
The unreachable-store tests need no database: they point the store at a port nothing listens on.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import psycopg2  # type: ignore[import-untyped]
import pytest
from psycopg2 import sql
from pydantic import SecretStr

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillAnswer,
    ModelFloorFillStoreSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelStateRead,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedStarter,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store_postgres import (
    PostgresFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    CountingStore,
    SynchronizedSnapshot,
    harness,
)

pytestmark = pytest.mark.integration

TICK = EnumFloorFillCommand.TICK
ONE_TASK = {"alpha": ["a1"]}
CONNECT_TIMEOUT_SECONDS = 3
NOTHING_LISTENS_DSN = "postgresql://nobody@127.0.0.1:1/none"

# The table of migration 113 (omnibase_infra), column for column, in a schema of the test's own.
_TABLE_DDL = """
CREATE TABLE {relation} (
    scope             TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    state             TEXT NOT NULL,
    in_flight         BOOLEAN NOT NULL DEFAULT FALSE,
    payload           JSONB NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0,
    pending_emissions JSONB,
    publish_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


class ConditionlessStore(PostgresFloorStateStore):
    """The injected break: the update no longer asks for the version the writer read."""

    def _update_statement(self) -> Any:
        return sql.SQL(
            "UPDATE {relation} SET payload = %s::jsonb, version = %s WHERE scope = %s AND %s::int IS NOT NULL"
        ).format(
            relation=sql.Identifier(self._settings.schema_name, "floor_fill_state")
        )


class SwallowingStore(PostgresFloorStateStore):
    """The injected break: a store that cannot be read answers "no row" instead of refusing."""

    def load(self, key: str) -> ModelStateRead:
        try:
            return super().load(key)
        except FloorFillUnreadableError:
            return ModelStateRead(payload=None, version=0)


def _settings(dsn: str, schema_name: str = "public") -> ModelFloorFillStoreSettings:
    return ModelFloorFillStoreSettings(
        dsn=SecretStr(dsn),
        schema_name=schema_name,
        table_name="floor_fill_state",
        connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
    )


def _database_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping the real floor_fill_state")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return (
        f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{quote(database)}"
    )


@pytest.fixture
def schema_dsn() -> Iterator[tuple[str, str]]:
    """A schema holding a fresh ``floor_fill_state`` table; yields ``(dsn, schema name)``."""
    dsn = _database_dsn()
    schema_name = f"ff_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg2.OperationalError as error:
        pytest.skip(f"no reachable Postgres for the real floor_fill_state: {error}")
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE SCHEMA {schema}").format(schema=sql.Identifier(schema_name))
        )
        cursor.execute(
            sql.SQL(_TABLE_DDL).format(
                relation=sql.Identifier(schema_name, "floor_fill_state")
            )
        )
    try:
        yield dsn, schema_name
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA {schema} CASCADE").format(
                    schema=sql.Identifier(schema_name)
                )
            )
        admin.close()


def _race(
    stores: tuple[PostgresFloorStateStore, PostgresFloorStateStore],
    key: str,
    expected: int,
) -> list[bool]:
    """Both instances read the row, then both write the next version: the outcome of each write."""
    barrier = threading.Barrier(2, timeout=10)
    outcomes: list[bool] = [False, False]

    def go(index: int) -> None:
        read = stores[index].load(key)
        assert read.version == expected
        barrier.wait()
        outcomes[index] = stores[index].compare_and_set(
            key, read.version, read.version + 1, f'{{"writer": {index}}}'
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return outcomes


def test_one_writer_two_instances_store_race(schema_dsn: tuple[str, str]) -> None:
    dsn, schema_name = schema_dsn
    stores = (
        PostgresFloorStateStore(_settings(dsn, schema_name)),
        PostgresFloorStateStore(_settings(dsn, schema_name)),
    )
    first = _race(stores, "scope-a", 0)
    assert sorted(first) == [False, True]
    second = _race(stores, "scope-a", 1)
    assert sorted(second) == [False, True]
    assert stores[0].load("scope-a").version == 2


def test_one_writer_two_instances_control_without_sequence_condition(
    schema_dsn: tuple[str, str],
) -> None:
    """With the sequence condition removed both instances win the update: the property above is the condition's."""
    dsn, schema_name = schema_dsn
    stores = (
        ConditionlessStore(_settings(dsn, schema_name)),
        ConditionlessStore(_settings(dsn, schema_name)),
    )
    assert sorted(_race(stores, "scope-b", 0)) == [False, True]
    seeded = stores[0].load("scope-b")
    assert seeded.version == 1
    outcomes = _race(stores, "scope-b", 1)
    assert outcomes == [True, True], "the control must show two winners"


def test_one_writer_two_instances_orchestrator(schema_dsn: tuple[str, str]) -> None:
    """Two orchestrators tick on one table: one lane starts and the loser answers STALE-STATE."""
    dsn, schema_name = schema_dsn
    real = CountingStore(PostgresFloorStateStore(_settings(dsn, schema_name)))
    shared = SynchronizedSnapshot(real, OVERLAY["scope"], 2)
    starters = [ScriptedStarter(), ScriptedStarter()]
    orchestrators = [
        harness(lines=ONE_TASK, store=shared, starter=starter).orchestrator()
        for starter in starters
    ]
    answers: list[ModelFloorFillAnswer | None] = [None, None]

    def go(index: int) -> None:
        answers[index] = orchestrators[index].run(TICK, None)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    done = [a for a in answers if a is not None]
    assert len(done) == 2
    assert sum(len(s.starts) for s in starters) == 1
    stale = [a for a in done if a.lines == ("STALE-STATE",)]
    assert len(stale) == 1
    assert stale[0].status is EnumFloorFillStatus.FAILED


def test_absent_row_vs_unreadable_store_table(schema_dsn: tuple[str, str]) -> None:
    dsn, schema_name = schema_dsn
    store = PostgresFloorStateStore(_settings(dsn, schema_name))
    assert store.load("never-written") == ModelStateRead(payload=None, version=0)
    assert store.compare_and_set("row", 0, 1, '{"seq": 1}') is True
    assert store.load("row").version == 1
    missing = PostgresFloorStateStore(_settings(dsn, "no_such_schema_for_the_fill"))
    with pytest.raises(FloorFillUnreadableError) as refused:
        missing.load("row")
    assert refused.value.cause.startswith("store-unreachable:")


def test_absent_row_vs_unreadable_store_unreachable() -> None:
    store = PostgresFloorStateStore(_settings(NOTHING_LISTENS_DSN))
    with pytest.raises(FloorFillUnreadableError) as read_refused:
        store.load("row")
    assert read_refused.value.cause.startswith("store-unreachable:OperationalError")
    with pytest.raises(FloorFillUnreadableError):
        store.compare_and_set("row", 0, 1, "{}")
    answer = harness(store=store, lines=ONE_TASK).orchestrator().run(TICK, None)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.exit_code != 0
    assert answer.lines[0].startswith("UNKNOWN fill-state:store-unreachable:")
    assert "nobody" not in answer.lines[0]


def test_absent_row_vs_unreadable_store_control_swallowing() -> None:
    """With the refusal swallowed an unreachable store reads as an absent row: the answer the real store never gives."""
    store = SwallowingStore(_settings(NOTHING_LISTENS_DSN))
    assert store.load("row") == ModelStateRead(payload=None, version=0)
