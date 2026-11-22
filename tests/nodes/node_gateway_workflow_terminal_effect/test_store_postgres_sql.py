# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SQL contract of the terminal store and the submit path's status guard (OMN-26464)."""

from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers import (
    store_postgres,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalStoreOutcome as Store,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.protocols.protocol_gateway_terminal_store import (
    GatewayTerminalStoreError,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers import (
    store_postgres as gateway_store,
)


class _Conn:
    def __init__(self, lookup: list[dict[str, Any]], applied: int = 1) -> None:
        self.lookup, self.applied = lookup, applied
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.statements.append((sql, args))
        if sql == store_postgres.SQL_LOOKUP:
            return self.lookup
        return [{"workflow_id": args[0]}] * self.applied


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def transaction(self) -> Any:
        conn = self.conn

        class _Tx:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Tx()


def _update() -> ModelTerminalUpdate:
    return ModelTerminalUpdate(
        tenant_id=uuid4(),
        correlation_id=uuid4(),
        status="completed",
        model_used="m",
        total_tokens=1,
        latency_ms=2,
        rule_evaluations=[],
    )


def test_apply_statement_is_status_guarded_and_keyed_on_tenant() -> None:
    assert "status NOT IN ('completed', 'failed')" in store_postgres.SQL_APPLY
    assert "workflow_id = $1 AND tenant_id = $2" in store_postgres.SQL_APPLY
    assert "FOR UPDATE" in store_postgres.SQL_LOOKUP
    assert "tenant_id = $1 AND correlation_id = $2" in store_postgres.SQL_LOOKUP
    assert (
        "status" not in store_postgres.SQL_LOOKUP.split("WHERE")[1]
    )  # rows of every status


def test_submit_published_does_not_overwrite_terminal() -> None:
    source = inspect.getsource(gateway_store)
    assert (
        "\"WHERE workflow_id = $2 AND tenant_id = $3 AND status NOT IN ('completed', 'failed')\""
        in source
    )


async def test_store_applies_single_writable_row() -> None:
    wid = uuid4()
    conn = _Conn([{"workflow_id": wid, "status": "published"}])
    store = store_postgres.PostgresGatewayTerminalStore(_Pool(conn))  # type: ignore[arg-type]
    assert await store.apply_terminal(_update()) == (Store.APPLIED, wid)
    assert len(conn.statements) == 2


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [
        ([], Store.NO_ROW),
        ([{"workflow_id": uuid4(), "status": "published"}] * 2, Store.AMBIGUOUS),
        ([{"workflow_id": uuid4(), "status": "failed"}], Store.ALREADY_TERMINAL),
    ],
)
async def test_store_decisions_write_nothing(
    lookup: list[dict[str, Any]], expected: Store
) -> None:
    conn = _Conn(lookup)
    store = store_postgres.PostgresGatewayTerminalStore(_Pool(conn))  # type: ignore[arg-type]
    assert (await store.apply_terminal(_update()))[0] is expected
    assert len(conn.statements) == 1  # the lookup only


async def test_store_raises_when_update_does_not_change_exactly_one_row() -> None:
    conn = _Conn([{"workflow_id": uuid4(), "status": "published"}], applied=0)
    store = store_postgres.PostgresGatewayTerminalStore(_Pool(conn))  # type: ignore[arg-type]
    with pytest.raises(GatewayTerminalStoreError):
        await store.apply_terminal(_update())
