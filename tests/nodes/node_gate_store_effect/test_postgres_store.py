# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres backend passes the frozen contract suite; named breaks fail it for real.

The suite runs against a real Postgres: the integration database of ``conftest`` (a runner
without one skips the integration tests under the repository's silent-skip guard). The
relations come only from the node's two migration files (see ``support_postgres``); no table is
created by hand. The refusal tests need no database.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
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
from tests.nodes.node_gate_store_effect import contract_suite as suite
from tests.nodes.node_gate_store_effect.support_postgres import (
    DSN_ENV,
    connect,
    postgres_factory,
    with_postgres,
)


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
    second = PostgresGateStore(with_postgres(suite.POLICY), connect, None)
    assert suite.read(second, "alpha").counted == 1


def test_postgres_policy_without_its_block_is_refused() -> None:
    policy = suite.POLICY.model_copy(update={"backend": EnumGateStoreBackend.POSTGRES})
    with pytest.raises(GateStoreBackendNotBuiltError):
        build_store(policy)


def test_unset_dsn_variable_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(GateStoreBackendNotBuiltError, match=DSN_ENV):
        connect_from_overlay(with_postgres(suite.POLICY))


def test_set_dsn_variable_builds_aconnect_function() -> None:
    connect = connect_from_overlay(
        with_postgres(suite.POLICY), {DSN_ENV: "postgresql://documentation.test/none"}
    )
    assert callable(connect)


def test_result_type_is_the_stores_answer() -> None:
    assert ModelGateStoreResult.model_fields["outcome"] is not None
