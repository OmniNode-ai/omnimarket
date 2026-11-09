# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One frozen contract suite, two backends: the in-memory twin and the Postgres store.

Every check of ``contract_suite`` runs unchanged on both. A Postgres store gets a fresh schema
per factory call on a disposable native server (the fixture of the replay tests; a host with no
``initdb`` on its path skips the Postgres legs). The safety-write checks also run against a
store that counts first, which must fail them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.models.model_gate_store import ModelGateStorePolicy
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_postgres import (
    PostgresGateStore,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.nodes.node_gate_store_effect import contract_suite as suite
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as local_postgres_fixture,
)

local_postgres = local_postgres_fixture


class CountsFirstPostgresStore(PostgresGateStore):
    """Break ``safety_after_count``: a recorded stop is dropped before a reservation reads it."""

    def _reserve(self, cur: Any, meta: Any, request: Any) -> Any:
        cur.execute("DELETE FROM gate_store_safety")
        return super()._reserve(cur, meta, request)


@pytest.fixture
def postgres_factory(
    local_postgres: tuple[Any, str],
) -> Callable[[type[PostgresGateStore]], suite.Factory]:
    pg, _ = local_postgres
    dsn = f"host={pg.host} port={pg.port} dbname=postgres user=postgres"

    def make(store_type: type[PostgresGateStore]) -> suite.Factory:
        def factory(
            policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
        ) -> ProtocolGateStore:
            store = store_type(
                policy, dsn, schema=f"gs_{uuid.uuid4().hex[:16]}", fault_hook=hook
            )
            store.install_schema()
            return store

        return factory

    return make


def twin(
    policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
) -> ProtocolGateStore:
    return InMemoryGateStore(policy, hook)


CHECKS = [
    pytest.param(
        check,
        id=check.__name__.removeprefix("check_").replace(
            "commits_before_it_counts", "first"
        ),
    )
    for check in suite.CHECKS
]


@pytest.mark.unit
@pytest.mark.parametrize("check", CHECKS)
def test_twin_passes_the_contract_suite(check: Callable[[suite.Factory], None]) -> None:
    check(twin)


@pytest.mark.integration
@pytest.mark.parametrize("check", CHECKS)
def test_postgres_passes_the_contract_suite(
    check: Callable[[suite.Factory], None],
    postgres_factory: Callable[[type[PostgresGateStore]], suite.Factory],
) -> None:
    check(postgres_factory(PostgresGateStore))


@pytest.mark.integration
def test_safety_write_first_on_both_backends(
    postgres_factory: Callable[[type[PostgresGateStore]], suite.Factory],
) -> None:
    suite.check_safety_write_commits_before_it_counts(twin)
    suite.check_safety_write_commits_before_it_counts(
        postgres_factory(PostgresGateStore)
    )


@pytest.mark.integration
def test_safety_write_first_fails_a_store_that_counts_first(
    postgres_factory: Callable[[type[PostgresGateStore]], suite.Factory],
) -> None:
    with pytest.raises(AssertionError):
        suite.check_safety_write_commits_before_it_counts(
            postgres_factory(CountsFirstPostgresStore)
        )
