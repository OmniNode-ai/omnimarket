# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate store contract suite, run on both stores the plan names.

The suite is ``tests/nodes/node_gate_store_effect/contract_suite.py`` (nine racing reservations
in one transaction, a handover, a kill at each step, a duplicate delivery, keys repeated by a
replay, and the safety write that commits before it counts), frozen with a hash that
``test_gate_store_suite_hash.py`` checks. It runs here on the in-memory twin and on the
serializable (Postgres) store, unchanged, through one factory each. The Postgres run needs the
integration database; a store that counts before the safety write commits fails the suite.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.models.model_gate_store import ModelGateStorePolicy
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.nodes.node_gate_store_effect import contract_suite as suite
from tests.nodes.node_gate_store_effect.support_postgres import postgres_factory
from tests.nodes.node_gate_store_effect.test_in_memory_store import UnstoppedStore

CheckFn = Callable[[suite.Factory], None]


def twin_factory(
    store_type: type[InMemoryGateStore] = InMemoryGateStore,
) -> suite.Factory:
    def factory(
        policy: ModelGateStorePolicy, hook: Callable[[str], None] | None
    ) -> ProtocolGateStore:
        return store_type(policy, hook)

    return factory


STORES: list[object] = [
    pytest.param(twin_factory, id="in_memory_twin"),
    pytest.param(postgres_factory, id="timer_store", marks=pytest.mark.integration),
]


@pytest.mark.parametrize("make_factory", STORES)
@pytest.mark.parametrize("check", suite.CHECKS, ids=lambda c: c.__name__)
def test_store_passes_contract_suite(
    make_factory: Callable[[], suite.Factory], check: CheckFn
) -> None:
    check(make_factory())


@pytest.mark.parametrize("make_factory", STORES)
def test_safety_write_first_passes(make_factory: Callable[[], suite.Factory]) -> None:
    suite.check_safety_write_commits_before_it_counts(make_factory())


def test_safety_write_first_store_that_counts_first_fails_the_suite() -> None:
    """Positive control: a store whose reservation ignores the recorded stop is refused."""
    with pytest.raises(AssertionError, match="a start passed a stop"):
        suite.check_safety_write_commits_before_it_counts(twin_factory(UnstoppedStore))
