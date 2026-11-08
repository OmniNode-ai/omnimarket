# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer advances a test order on the Postgres gate store, and survives a restart.

Needs the integration database (a runner without one skips under the repository's silent-skip
guard); the relations are the gate store node's migrations, applied by the shared fixture.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_order_advance import ModelOrderAdvanceRequest
from omnimarket.nodes.node_gate_store_effect.handlers.store_postgres import (
    PostgresGateStore,
)
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
    InMemoryOrderReadiness,
)
from tests.nodes.node_gate_store_effect.contract_suite import POLICY
from tests.nodes.node_gate_store_effect.test_postgres_store import (  # noqa: F401
    _connect,
    _database,
    _policy,
    postgres_factory,
)
from tests.nodes.node_order_advance_effect.test_order_advance import ORDER, WRITER

pytestmark = pytest.mark.integration


def writer() -> HandlerOrderAdvance:
    store = postgres_factory()(POLICY, None)
    return HandlerOrderAdvance(
        store, InMemoryOrderReadiness(WRITER.ready_pieces), WRITER, ORDER
    )


def test_writer_advances_the_test_order_on_postgres_and_restarts() -> None:
    first = writer()
    seen = [
        first.handle(ModelOrderAdvanceRequest(correlation_id=f"p{i}")) for i in (1, 2)
    ]
    assert [(r.outcome.value, r.order_position) for r in seen] == [
        ("advanced", 1),
        ("advanced", 2),
    ]
    # restart: a new writer object over a new connection factory reads the committed position
    restarted = HandlerOrderAdvance(
        PostgresGateStore(_policy(POLICY), _connect, None),
        InMemoryOrderReadiness(WRITER.ready_pieces),
        WRITER,
        ORDER,
    )
    third = restarted.handle(ModelOrderAdvanceRequest(correlation_id="p3"))
    done = restarted.handle(ModelOrderAdvanceRequest(correlation_id="p4"))
    assert (third.outcome.value, third.order_position) == ("advanced", 3)
    assert (done.outcome.value, done.order_position) == ("complete", 3)
