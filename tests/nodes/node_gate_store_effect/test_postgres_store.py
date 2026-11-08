# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres backend passes the frozen contract suite; named breaks fail it for real.

The suite runs against a real Postgres: the integration database of ``conftest`` (a runner
without one skips the integration tests under the repository's own silent-skip guard). The
relations come only from the node's two migration files, applied here as the migration runner
applies them (``scripts/run-projection-migrations.py``, shown on the lab in the pull request);
no table is created by hand. The refusal tests need no database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
    ModelGateStorePostgres,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    GateStoreBackendNotBuiltError,
    build_store,
    connect_from_overlay,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import STAGES
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


def _policy(base: ModelGateStorePolicy) -> ModelGateStorePolicy:
    return base.model_copy(
        update={"backend": EnumGateStoreBackend.POSTGRES, "postgres": POSTGRES_BLOCK}
    )


MIGRATIONS = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_gate_store_effect/migrations"
)


@pytest.fixture(autouse=True)
def _database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _connect() -> Any:
    import psycopg2

    return psycopg2.connect(os.environ[DSN_ENV], connect_timeout=5)


def _reset() -> None:
    """A clean store for one check: the relations are the migration's, only the rows go."""
    conn = _connect()
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
        return store_type(_policy(policy), _connect, hook)

    return factory


class UnlockedStore(PostgresGateStore):
    """Break ``no_row_lock``: the state row is read without the lock, so racers share a snapshot."""

    _LOCK_CLAUSE = ""

    def _apply(
        self, state: Any, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        time.sleep(0.05)  # widen the window between the read and the write
        return super()._apply(state, request)


class EarlyCommitStore(PostgresGateStore):
    """Break ``commit_before_kill_point``: the writes commit before the kill points are passed."""

    def _store(self, cur: Any, *args: Any) -> None:
        super()._store(cur, *args)
        cur.connection.commit()


class NoStepKeyStore(PostgresGateStore):
    """Break ``forget_step_keys``: a recorded answer is never found again."""

    def _load(self, cur: Any, request: ModelGateStoreRequest) -> Any:
        state = super()._load(cur, request)
        state.steps.clear()
        return state


@pytest.mark.integration
@pytest.mark.parametrize("check", suite.CHECKS, ids=lambda c: c.__name__)
def test_postgres_passes_contract_suite(
    check: Callable[[suite.Factory], None],
) -> None:
    check(postgres_factory())


@pytest.mark.integration
def test_stages_match_the_suite() -> None:
    assert STAGES == suite.KILL_STAGES


BREAKS: list[tuple[str, type[PostgresGateStore], Callable[[suite.Factory], None]]] = [
    ("no_row_lock", UnlockedStore, suite.check_nine_racing_reservations),
    ("commit_before_kill_point", EarlyCommitStore, suite.check_kill_at_each_stage),
    (
        "forget_step_keys",
        NoStepKeyStore,
        suite.check_duplicate_delivery_and_replayed_keys,
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "store_type", "check"), BREAKS, ids=[b[0] for b in BREAKS]
)
def test_named_break_fails_the_suite_on_postgres(
    name: str,
    store_type: type[PostgresGateStore],
    check: Callable[[suite.Factory], None],
) -> None:
    with pytest.raises(AssertionError):
        check(postgres_factory(store_type))


@pytest.mark.integration
def test_a_second_store_object_sees_the_first_ones_commit() -> None:
    """The state is the database's: a new store object reads what another committed."""
    factory = postgres_factory()
    first = factory(suite.POLICY, None)
    assert suite.reserve(first, "alpha", "r1").counted == 1
    second = PostgresGateStore(_policy(suite.POLICY), _connect, None)
    assert suite.read(second, "alpha").counted == 1


def test_postgres_policy_without_its_block_is_refused() -> None:
    policy = suite.POLICY.model_copy(update={"backend": EnumGateStoreBackend.POSTGRES})
    with pytest.raises(GateStoreBackendNotBuiltError):
        build_store(policy)


def test_unset_dsn_variable_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(GateStoreBackendNotBuiltError, match=DSN_ENV):
        connect_from_overlay(_policy(suite.POLICY))


def test_set_dsn_variable_builds_a_connect_function() -> None:
    connect = connect_from_overlay(
        _policy(suite.POLICY), {DSN_ENV: "postgresql://documentation.test/none"}
    )
    assert callable(connect)


def test_result_type_is_the_stores_answer() -> None:
    assert ModelGateStoreResult.model_fields["outcome"] is not None
