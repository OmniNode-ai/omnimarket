# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation projection is written inside the one runtime, not by its own process.

A runner-shaped handler (its own consume loop, its own pool) that does not
declare ``onex_runtime_inprocess_dispatch`` is treated by a folded runtime as a
standalone process: the runtime subscribes the topics and dispatches nothing, so
``delegation_events`` stops filling. This module proves the delegation node's
routed handler is the in-process writer, that its contract routes every
subscribed topic to it, and that one delivery through the runtime entry point
writes a ``delegation_events`` row in a real, migrated PostgreSQL schema.

The database is the ``INTEGRATION_POSTGRES_*`` one when its password is set (CI),
and otherwise a disposable native cluster the fixture starts. A host with
neither FAILS rather than skips: a skipped real-database proof exits 0 although
it proved nothing.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg
import pytest
import yaml

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_runtime_writer import (
    DelegationProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as _native_cluster,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _base_dsn,
    _provisioned_runner,
    _real_delegation_completed_payload,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import _ROLES_SQL

_CONTRACT = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)

# pytest finds a fixture by module attribute; the dsn fixture below requests it
# by this name only when no INTEGRATION_POSTGRES_* database is configured.
local_postgres = _native_cluster


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class TestTheContractRoutesTheInProcessWriter:
    def test_the_writer_declares_in_process_dispatch(self) -> None:
        assert DelegationProjectionWriter.onex_runtime_inprocess_dispatch is True
        assert DelegationProjectionWriter.runner_type is DelegationProjectionRunner

    def test_the_runner_alone_is_not_dispatched_in_process(self) -> None:
        # The runner owns a consume loop: it stays the standalone entry, and the
        # writer composes it per delivery.
        assert not getattr(
            DelegationProjectionRunner, "onex_runtime_inprocess_dispatch", False
        )

    def test_every_subscribed_topic_is_routed_to_the_writer_alone(self) -> None:
        contract = _contract()
        subscribed = set(contract["event_bus"]["subscribe_topics"])
        routes = contract["handler_routing"]["handlers"]
        assert {route["handler"]["name"] for route in routes} == {
            "DelegationProjectionWriter"
        }
        assert {route["topic"] for route in routes} == subscribed
        # The top-level handler stays the pure fold the writer's helpers come
        # from; it is not routed, so it is never dispatched beside the writer.
        assert contract["handler"]["class"] == "HandlerProjectionDelegation"
        assert subscribed == set(DelegationProjectionRunner().subscribe_topics)


@pytest.fixture
async def dsn(request: pytest.FixtureRequest) -> str | None:
    """None selects the INTEGRATION_POSTGRES_* database; else a local cluster."""
    if os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        return None
    try:
        pg = request.getfixturevalue("local_postgres")[0]
    except pytest.skip.Exception as exc:
        pytest.fail(f"a skip is not a pass: no real Postgres to prove on ({exc})")
    url = f"postgresql://{pg.user}@/{pg.database}?host={pg.host}&port={pg.port}"
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
    finally:
        await admin.close()
    return url


@pytest.mark.integration
class TestOneRuntimeDeliveryWritesTheRow:
    async def test_a_delivery_through_the_writer_stores_a_delegation_row(
        self, dsn: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        try:
            provisioned = _provisioned_runner(dsn)
            runner, admin_conn, schema = await provisioned.__aenter__()
        except pytest.skip.Exception as exc:
            pytest.fail(f"a skip is not a pass: no real Postgres to prove on ({exc})")
        adapters: list[AsyncpgAdapter] = []
        base = dsn or _base_dsn()
        joiner = "&" if "?" in base else "?"
        scoped_dsn = f"{base}{joiner}search_path={quote(schema)},public"

        async def connect_fixture_database(self: BaseProjectionRunner) -> None:
            # The hermetic suite proves per-binding resolution and wrong-principal
            # refusal. This fixture supplies the isolated, migrated real schema.
            self._db = AsyncpgAdapter(dsn=scoped_dsn)
            adapters.append(self._db)
            await self._db.connect()

        monkeypatch.setattr(
            BaseProjectionRunner,
            "_connect_standalone_databases",
            connect_fixture_database,
        )
        try:
            correlation_id = str(uuid4())
            request: dict[str, Any] = {
                "_topic": runner._topic_delegation_completed,
                "_partition": 3,
                "_offset": 7,
                **_real_delegation_completed_payload(
                    correlation_id=correlation_id, tenant_id="beta-business-proof"
                ),
            }

            # The runtime calls handle() synchronously; it owns its event loop.
            result = await asyncio.to_thread(
                DelegationProjectionWriter().handle, request
            )

            assert result == {"projected": True}
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None, "the in-process delivery must land a row"
            assert row["tenant_id"] == UUID("91c74442-1233-4c97-b191-911a10346fdf")
            assert row["task_type"] == "code-review"
            assert row["quality_gate_passed"] is True
            assert adapters, "the delivery opened its own pool"
            assert all(adapter._pool is None for adapter in adapters), (
                "no pool outlives the delivery that opened it"
            )

            # A redelivery of the same event keeps one row (UPSERT on correlation_id).
            again = await asyncio.to_thread(
                DelegationProjectionWriter().handle, request
            )
            assert again == {"projected": True}
            count = await admin_conn.fetchval(
                "SELECT count(*) FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert count == 1
        finally:
            await provisioned.__aexit__(None, None, None)
